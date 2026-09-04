"""Bounded tokenized Basecamp webhook intake with canonical refetch."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import sqlite3
import time
from collections import deque
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .inbox import DurableInbox
except ImportError:  # pragma: no cover
    from inbox import DurableInbox


class WebhookRejectedError(PermissionError):
    pass


class DurableWebhookStore:
    """Durable bounded queue with duplicate and poison-event isolation."""

    def __init__(
        self,
        path: Path,
        *,
        max_events: int = 500,
        max_attempts: int = 5,
        max_terminal_events: int = 5_000,
        terminal_retention_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        self.path = path
        self.max_events = max_events
        self.max_attempts = max_attempts
        self.max_terminal_events = max_terminal_events
        self.terminal_retention_seconds = terminal_retention_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    event_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
        os.chmod(path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            return connection
        except Exception:
            connection.close()
            raise

    def put(self, event_id: str, payload: Mapping[str, Any]) -> bool:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune(connection)
            if connection.execute("SELECT 1 FROM webhook_events WHERE event_id = ?", (event_id,)).fetchone():
                connection.commit()
                return False
            count = connection.execute(
                "SELECT COUNT(*) FROM webhook_events WHERE state IN ('pending', 'processing')"
            ).fetchone()[0]
            if count >= self.max_events:
                connection.rollback()
                raise RuntimeError("Basecamp webhook queue is full; Basecamp should retry delivery")
            connection.execute(
                "INSERT INTO webhook_events VALUES (?, ?, 'pending', 0, ?)",
                (event_id, json.dumps(payload, separators=(",", ":"), default=str), time.time()),
            )
            connection.commit()
        return True

    def _prune(self, connection: sqlite3.Connection) -> None:
        cutoff = time.time() - self.terminal_retention_seconds
        connection.execute(
            "DELETE FROM webhook_events WHERE state IN ('done', 'poison') AND updated_at < ?",
            (cutoff,),
        )
        terminal_count = connection.execute(
            "SELECT COUNT(*) FROM webhook_events WHERE state IN ('done', 'poison')"
        ).fetchone()[0]
        excess = max(int(terminal_count) - self.max_terminal_events, 0)
        if excess:
            connection.execute(
                """
                DELETE FROM webhook_events WHERE event_id IN (
                    SELECT event_id FROM webhook_events
                    WHERE state IN ('done', 'poison') ORDER BY updated_at LIMIT ?
                )
                """,
                (excess,),
            )

    def next(self) -> tuple[str, Mapping[str, Any]] | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM webhook_events WHERE state = 'pending' ORDER BY updated_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                "UPDATE webhook_events SET state = 'processing', attempts = attempts + 1, updated_at = ? WHERE event_id = ?",
                (time.time(), row["event_id"]),
            )
            connection.commit()
        value = json.loads(row["payload_json"])
        return (row["event_id"], value)

    def ack(self, event_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE webhook_events SET state = 'done', payload_json = '{}', updated_at = ? WHERE event_id = ?",
                (time.time(), event_id),
            )
            self._prune(connection)

    def retry(self, event_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE webhook_events
                SET state = CASE WHEN attempts >= ? THEN 'poison' ELSE 'pending' END,
                    updated_at = ?
                WHERE event_id = ?
                """,
                (self.max_attempts, time.time(), event_id),
            )
            self._prune(connection)

    def recover(self) -> None:
        """Return interrupted work to the pending queue after restart."""
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE webhook_events SET state = 'pending', updated_at = ? WHERE state = 'processing'",
                (time.time(),),
            )


class WebhookIngress:
    """Accept untrusted payload pointers only after token and canonical ownership checks."""

    def __init__(
        self,
        client: Any,
        *,
        token: str,
        max_queue: int = 500,
        store: DurableWebhookStore | None = None,
        inbox: DurableInbox | None = None,
    ) -> None:
        if len(token) < 32:
            raise ValueError("Basecamp webhook token must contain at least 32 characters")
        self.client = client
        self._token = token
        self.queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._max_seen = max(500, max_queue * 10)
        self.store = store
        self.inbox = inbox
        if self.inbox:
            self.inbox.recover()
        elif self.store:
            self.store.recover()

    async def ingest(self, token: str, payload: Mapping[str, Any]) -> bool:
        if not hmac.compare_digest(token, self._token):
            raise WebhookRejectedError("Basecamp webhook token mismatch")
        event_id = str(payload.get("id") or "")
        if not event_id:
            raise WebhookRejectedError("Basecamp webhook event ID is missing")
        if event_id in self._seen:
            return False
        trusted = await self.client.fetch_webhook_event(payload)
        if trusted is None:
            raise WebhookRejectedError("Basecamp webhook event cannot be verified")
        bucket = trusted.get("bucket") or {}
        project_id = str(bucket.get("id") or "") if isinstance(bucket, Mapping) else ""
        if project_id not in self.client.project_ids:
            raise WebhookRejectedError("Basecamp webhook event is outside the allowlist")
        if self.inbox:
            if not self.inbox.accept_batch("webhook", f"webhook:{project_id}", [trusted]):
                return False
        elif self.store:
            if not self.store.put(event_id, trusted):
                return False
        else:
            try:
                self.queue.put_nowait(trusted)
            except asyncio.QueueFull as exc:
                raise RuntimeError("Basecamp webhook queue is full; Basecamp should retry delivery") from exc
        self._seen.add(event_id)
        self._seen_order.append(event_id)
        while len(self._seen_order) > self._max_seen:
            self._seen.discard(self._seen_order.popleft())
        return True

    async def get(self) -> Mapping[str, Any]:
        if self.inbox:
            while True:
                item = await asyncio.to_thread(self.inbox.claim)
                if item is not None:
                    return item.payload
                await asyncio.sleep(0.05)
        if self.store:
            while True:
                item = await self._claim_one()
                if item is not None:
                    event_id, payload = item
                    return {**payload, "_durable_event_id": event_id}
                await asyncio.sleep(0.05)
        return await self.queue.get()

    async def drain_nowait(self) -> list[Mapping[str, Any]]:
        """Lease at most one item so cancellation cannot strand a whole batch."""
        if self.inbox:
            item = await asyncio.to_thread(self.inbox.claim)
            return [] if item is None else [item.payload]
        if self.store:
            item = await self._claim_one()
            if item is None:
                return []
            event_id, payload = item
            return [{**payload, "_durable_event_id": event_id}]
        try:
            return [self.queue.get_nowait()]
        except asyncio.QueueEmpty:
            return []

    async def _claim_one(self) -> tuple[str, Mapping[str, Any]] | None:
        """Finish or undo the SQLite claim if cancellation races the worker thread."""
        if self.store is None:
            return None
        claim = asyncio.create_task(asyncio.to_thread(self.store.next))
        try:
            return await asyncio.shield(claim)
        except asyncio.CancelledError:
            item = await claim
            if item is not None:
                event_id, _ = item
                await asyncio.to_thread(self.store.retry, event_id)
            raise

    def recover(self) -> None:
        if self.inbox:
            self.inbox.recover()
        if self.store:
            self.store.recover()

    def ack(self, payload: Mapping[str, Any]) -> None:
        if self.inbox and payload.get("_inbox_event_key"):
            self.inbox.complete(payload)
        if self.store and payload.get("_durable_event_id"):
            self.store.ack(str(payload["_durable_event_id"]))

    def retry(self, payload: Mapping[str, Any]) -> None:
        if self.inbox and payload.get("_inbox_event_key"):
            self.inbox.retry(payload)
        if self.store and payload.get("_durable_event_id"):
            self.store.retry(str(payload["_durable_event_id"]))


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: bytes


class WebhookHTTPReceiver:
    """Small adapter-owned HTTP seam with a strict body bound."""

    def __init__(
        self,
        ingress: WebhookIngress,
        *,
        token: str,
        host: str = "127.0.0.1",
        port: int = 0,
        max_body_bytes: int = 256 * 1024,
        request_timeout_seconds: float = 10.0,
        tls_proxy: bool = False,
    ) -> None:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.lower() == "localhost"
        if not is_loopback and not tls_proxy:
            raise ValueError(
                "Basecamp webhook receiver must bind to loopback unless an explicit TLS proxy is configured"
            )
        self.ingress = ingress
        self.token = token
        self.host = host
        self.port = port
        self.max_body_bytes = max_body_bytes
        self.request_timeout_seconds = request_timeout_seconds
        self._server: asyncio.Server | None = None

    @property
    def path(self) -> str:
        return f"/basecamp/webhooks/{self.token}"

    @property
    def running(self) -> bool:
        return self._server is not None

    async def handle(self, method: str, path: str, body: bytes, content_type: str = "application/json") -> HTTPResult:
        if method != "POST":
            return HTTPResult(405, b"method not allowed")
        if len(body) > self.max_body_bytes:
            return HTTPResult(413, b"payload too large")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            return HTTPResult(415, b"unsupported media type")
        prefix = "/basecamp/webhooks/"
        if not path.startswith(prefix):
            return HTTPResult(404, b"not found")
        token = path.removeprefix(prefix)
        if not hmac.compare_digest(token, self.token):
            return HTTPResult(404, b"not found")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return HTTPResult(400, b"invalid json")
        if not isinstance(payload, Mapping):
            return HTTPResult(400, b"invalid payload")
        try:
            accepted = await self.ingress.ingest(token, payload)
        except WebhookRejectedError:
            return HTTPResult(404, b"not found")
        except Exception:  # noqa: BLE001 - upstream/auth/network failures must request retry
            return HTTPResult(503, b"retry later")
        return HTTPResult(202 if accepted else 200, b"accepted" if accepted else b"duplicate")

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._serve, self.host, self.port)
        socket = next(iter(self._server.sockets or ()), None)
        if socket is not None:
            self.port = int(socket.getsockname()[1])

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = 400
        body = b"bad request"
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self.request_timeout_seconds)
            if len(header) > 16 * 1024:
                raise ValueError("headers too large")
            lines = header.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower().strip()] = value.strip()
            length = int(headers.get("content-length", "0"))
            if length < 0 or length > self.max_body_bytes:
                result = HTTPResult(413, b"payload too large")
            else:
                payload = await asyncio.wait_for(reader.readexactly(length), timeout=self.request_timeout_seconds)
                result = await asyncio.wait_for(
                    self.handle(method, path, payload, headers.get("content-type", "")),
                    timeout=self.request_timeout_seconds,
                )
            status, body = result.status, result.body
        except Exception:  # noqa: BLE001 - malformed wire input must become a bounded HTTP error
            status, body = 400, b"bad request"
        reason = {
            200: "OK",
            202: "Accepted",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            413: "Payload Too Large",
            415: "Unsupported Media Type",
            503: "Service Unavailable",
        }.get(status, "Error")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()
