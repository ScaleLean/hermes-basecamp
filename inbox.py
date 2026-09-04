"""Transactional, source-neutral ingress queue for Basecamp events."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InboxEvent:
    key: str
    payload: Mapping[str, Any]
    source_type: str
    stream_id: str
    attempts: int


def event_identity(payload: Mapping[str, Any]) -> str:
    """Return the same identity for webhook and polling copies of one event."""
    event_id = str(payload.get("id") or payload.get("event_id") or "")
    recording = payload.get("recording") or payload.get("recordable") or payload
    recording_id = str(recording.get("id") or "") if isinstance(recording, Mapping) else ""
    recording_type = str(recording.get("type") or "") if isinstance(recording, Mapping) else ""
    if recording_type == "Chat::Line" and recording_id:
        return f"recording:{recording_type}:{recording_id}"
    version = str(
        payload.get("updated_at")
        or payload.get("created_at")
        or (recording.get("updated_at") if isinstance(recording, Mapping) else "")
        or ""
    )
    if event_id:
        return f"event:{event_id}"
    if recording_id and version:
        return f"recording:{recording_id}:{version}"
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


class DurableInbox:
    """SQLite inbox whose cursor and accepted batch commit atomically."""

    def __init__(
        self,
        path: Path,
        *,
        max_pending: int = 10_000,
        max_attempts: int = 5,
        lease_seconds: int = 300,
        terminal_retention_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        self.path = path
        self.max_pending = max_pending
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.terminal_retention_seconds = terminal_retention_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox_events (
                    event_key TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    event_version TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    recording_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    received_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    failure TEXT
                );
                CREATE INDEX IF NOT EXISTS inbox_state_idx
                    ON inbox_events(state, available_at, received_at);
                CREATE TABLE IF NOT EXISTS inbox_cursors (
                    stream_id TEXT PRIMARY KEY,
                    watermark TEXT NOT NULL,
                    ids_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbox_snapshot_state (
                    stream_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(stream_id, resource_id)
                );
                CREATE TABLE IF NOT EXISTS active_recordings (
                    scope_id TEXT NOT NULL,
                    recording_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(scope_id, recording_id)
                );
                CREATE TABLE IF NOT EXISTS inbox_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
        os.chmod(path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def accept_batch(
        self,
        source_type: str,
        stream_id: str,
        events: Sequence[Mapping[str, Any]],
    ) -> int:
        """Accept unseen events and advance the stream cursor in one transaction."""
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired(connection, now)
            active = connection.execute(
                "SELECT COUNT(*) FROM inbox_events WHERE state IN ('pending', 'processing')"
            ).fetchone()[0]
            if active + len(events) > self.max_pending:
                connection.rollback()
                raise RuntimeError("Basecamp inbox is full; cursor was not advanced")
            accepted = 0
            for payload in events:
                key = event_identity(payload)
                recording = payload.get("recording") or payload.get("recordable") or payload
                bucket = payload.get("bucket") or payload.get("project") or {}
                creator = payload.get("creator") or payload.get("person") or {}
                version = str(payload.get("updated_at") or payload.get("created_at") or "")
                scope_id = str(bucket.get("id") or payload.get("bucket_id") or "") if isinstance(bucket, Mapping) else ""
                recording_id = str(recording.get("id") or "") if isinstance(recording, Mapping) else ""
                creator_id = str(creator.get("id") or "") if isinstance(creator, Mapping) else ""
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO inbox_events
                    (event_key, source_type, stream_id, event_version, scope_id, recording_id,
                     creator_id, payload_json, state, attempts, available_at, received_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (
                        key,
                        source_type,
                        stream_id,
                        version,
                        scope_id,
                        recording_id,
                        creator_id,
                        json.dumps(payload, separators=(",", ":"), default=str),
                        now,
                        now,
                        now,
                    ),
                )
                accepted += cursor.rowcount
            self._advance_cursor(connection, stream_id, events, now)
            self._prune(connection, now)
            connection.commit()
            return accepted

    def accept_snapshot_batch(
        self,
        source_type: str,
        stream_id: str,
        events: Sequence[Mapping[str, Any]],
    ) -> int:
        """Accept each resource once per continuous appearance in a snapshot stream."""
        now = time.time()
        resources: dict[str, Mapping[str, Any]] = {}
        for payload in events:
            recording = payload.get("recording") or payload.get("recordable") or payload
            resource_id = str(recording.get("id") or "") if isinstance(recording, Mapping) else ""
            if not resource_id:
                raise ValueError("Snapshot event is missing a recording ID")
            resources[resource_id] = payload

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired(connection, now)
            prior_rows = connection.execute(
                "SELECT resource_id, generation, active FROM inbox_snapshot_state WHERE stream_id=?",
                (stream_id,),
            ).fetchall()
            prior = {str(row["resource_id"]): row for row in prior_rows}
            new_ids = [resource_id for resource_id in resources if not prior.get(resource_id) or not prior[resource_id]["active"]]
            active = connection.execute(
                "SELECT COUNT(*) FROM inbox_events WHERE state IN ('pending', 'processing')"
            ).fetchone()[0]
            if active + len(new_ids) > self.max_pending:
                connection.rollback()
                raise RuntimeError("Basecamp inbox is full; snapshot state was not advanced")

            connection.execute(
                "UPDATE inbox_snapshot_state SET active=0, updated_at=? WHERE stream_id=? AND active=1",
                (now, stream_id),
            )
            accepted = 0
            for resource_id, payload in resources.items():
                row = prior.get(resource_id)
                generation = int(row["generation"]) if row else 0
                is_new_epoch = row is None or not bool(row["active"])
                if is_new_epoch:
                    generation += 1
                connection.execute(
                    """INSERT INTO inbox_snapshot_state(stream_id, resource_id, generation, active, updated_at)
                       VALUES (?, ?, ?, 1, ?)
                       ON CONFLICT(stream_id, resource_id) DO UPDATE SET
                           generation=excluded.generation, active=1, updated_at=excluded.updated_at""",
                    (stream_id, resource_id, generation, now),
                )
                if not is_new_epoch:
                    continue
                stored = dict(payload)
                stored["_snapshot_generation"] = generation
                stored["id"] = f"{stream_id}:{resource_id}:generation:{generation}"
                recording = stored.get("recording") or stored.get("recordable") or stored
                bucket = stored.get("bucket") or stored.get("project") or {}
                creator = stored.get("creator") or stored.get("person") or {}
                scope_id = str(bucket.get("id") or stored.get("bucket_id") or "") if isinstance(bucket, Mapping) else ""
                creator_id = str(creator.get("id") or "") if isinstance(creator, Mapping) else ""
                key = f"snapshot:{stream_id}:{resource_id}:{generation}"
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO inbox_events
                       (event_key, source_type, stream_id, event_version, scope_id, recording_id,
                        creator_id, payload_json, state, attempts, available_at, received_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
                    (
                        key,
                        source_type,
                        stream_id,
                        str(generation),
                        scope_id,
                        resource_id,
                        creator_id,
                        json.dumps(stored, separators=(",", ":"), default=str),
                        now,
                        now,
                        now,
                    ),
                )
                accepted += cursor.rowcount
            self._advance_cursor(connection, stream_id, events, now)
            self._prune(connection, now)
            connection.commit()
            return accepted

    def after_cursor(self, stream_id: str, events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT watermark, ids_json FROM inbox_cursors WHERE stream_id = ?", (stream_id,)
            ).fetchone()
        if row is None:
            return list(events)
        seen = set(json.loads(row["ids_json"]))
        watermark = str(row["watermark"])
        if not watermark:
            return [item for item in events if str(item.get("id") or "") not in seen]
        return [
            item
            for item in events
            if not item.get("created_at")
            or str(item.get("created_at")) > watermark
            or (str(item.get("created_at")) == watermark and str(item.get("id") or "") not in seen)
        ]

    def _advance_cursor(
        self,
        connection: sqlite3.Connection,
        stream_id: str,
        events: Sequence[Mapping[str, Any]],
        now: float,
    ) -> None:
        timestamps = [str(item.get("created_at") or "") for item in events if item.get("created_at")]
        if not timestamps:
            snapshot_ids = sorted(str(item.get("id") or "") for item in events if item.get("id"))
            connection.execute(
                """INSERT INTO inbox_cursors(stream_id, watermark, ids_json, updated_at)
                   VALUES (?, '', ?, ?)
                   ON CONFLICT(stream_id) DO UPDATE SET ids_json=excluded.ids_json, updated_at=excluded.updated_at""",
                (stream_id, json.dumps(snapshot_ids), now),
            )
            return
        incoming = max(timestamps)
        row = connection.execute(
            "SELECT watermark, ids_json FROM inbox_cursors WHERE stream_id = ?", (stream_id,)
        ).fetchone()
        prior = str(row["watermark"]) if row else ""
        watermark = max(prior, incoming)
        ids = set(json.loads(row["ids_json"])) if row and prior == watermark else set()
        ids.update(
            str(item.get("id") or "")
            for item in events
            if str(item.get("created_at") or "") == watermark and item.get("id")
        )
        connection.execute(
            """
            INSERT INTO inbox_cursors(stream_id, watermark, ids_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(stream_id) DO UPDATE SET
                watermark=excluded.watermark, ids_json=excluded.ids_json, updated_at=excluded.updated_at
            """,
            (stream_id, watermark, json.dumps(sorted(ids)), now),
        )

    def claim(self) -> InboxEvent | None:
        now = time.time()
        token = uuid.uuid4().hex
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired(connection, now)
            row = connection.execute(
                """SELECT * FROM inbox_events
                   WHERE state = 'pending' AND available_at <= ?
                   ORDER BY received_at, event_key LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """UPDATE inbox_events SET state='processing', attempts=attempts+1,
                   lease_token=?, lease_expires_at=?, updated_at=? WHERE event_key=?""",
                (token, now + self.lease_seconds, now, row["event_key"]),
            )
            connection.commit()
        payload = json.loads(row["payload_json"])
        payload["_inbox_event_key"] = row["event_key"]
        payload["_inbox_lease_token"] = token
        return InboxEvent(row["event_key"], payload, row["source_type"], row["stream_id"], row["attempts"] + 1)

    def complete(self, event: InboxEvent | Mapping[str, Any]) -> None:
        key, token = self._claim_identity(event)
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE inbox_events SET state='done', payload_json='{}', lease_token=NULL,
                   lease_expires_at=NULL, updated_at=? WHERE event_key=? AND lease_token=?""",
                (time.time(), key, token),
            )

    def retry(self, event: InboxEvent | Mapping[str, Any], failure: str = "") -> None:
        key, token = self._claim_identity(event)
        sanitized = " ".join(str(failure).split())[:500]
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE inbox_events SET
                   state=CASE WHEN attempts >= ? THEN 'poison' ELSE 'pending' END,
                   available_at=?, lease_token=NULL, lease_expires_at=NULL, updated_at=?, failure=?
                   WHERE event_key=? AND lease_token=?""",
                (self.max_attempts, time.time(), time.time(), sanitized, key, token),
            )

    def recover(self) -> None:
        with closing(self._connect()) as connection:
            self._recover_expired(connection, time.time(), all_processing=True)

    def stats(self) -> dict[str, Any]:
        now = time.time()
        with closing(self._connect()) as connection:
            counts = {row["state"]: row["count"] for row in connection.execute(
                "SELECT state, COUNT(*) AS count FROM inbox_events GROUP BY state"
            )}
            oldest = connection.execute(
                "SELECT MIN(received_at) FROM inbox_events WHERE state IN ('pending','processing')"
            ).fetchone()[0]
            cursors = {row["stream_id"]: max(0.0, now - row["updated_at"]) for row in connection.execute(
                "SELECT stream_id, updated_at FROM inbox_cursors"
            )}
        return {
            "depth": int(counts.get("pending", 0)) + int(counts.get("processing", 0)),
            "pending": int(counts.get("pending", 0)),
            "processing": int(counts.get("processing", 0)),
            "poison": int(counts.get("poison", 0)),
            "oldest_pending_age": None if oldest is None else max(0.0, now - float(oldest)),
            "cursor_age": cursors,
        }

    def activate(self, scope_id: str, recording_id: str) -> None:
        if not scope_id.isdigit() or not recording_id.isdigit():
            return
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO active_recordings(scope_id, recording_id, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(scope_id, recording_id) DO UPDATE SET updated_at=excluded.updated_at""",
                (scope_id, recording_id, time.time()),
            )

    def is_active(self, scope_id: str, recording_id: str, *, max_age_seconds: int = 30 * 24 * 60 * 60) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT updated_at FROM active_recordings WHERE scope_id=? AND recording_id=?",
                (scope_id, recording_id),
            ).fetchone()
        return bool(row and float(row["updated_at"]) >= time.time() - max_age_seconds)

    def is_bootstrapped(self) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT value FROM inbox_meta WHERE key='bootstrapped'").fetchone()
        return bool(row and row["value"] == "1")

    def mark_bootstrapped(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO inbox_meta(key, value) VALUES ('bootstrapped', '1')
                   ON CONFLICT(key) DO UPDATE SET value='1'"""
            )

    def _claim_identity(self, event: InboxEvent | Mapping[str, Any]) -> tuple[str, str]:
        if isinstance(event, InboxEvent):
            payload = event.payload
        else:
            payload = event
        return str(payload.get("_inbox_event_key") or ""), str(payload.get("_inbox_lease_token") or "")

    def _recover_expired(self, connection: sqlite3.Connection, now: float, *, all_processing: bool = False) -> None:
        if all_processing:
            connection.execute(
                """UPDATE inbox_events SET state='pending', lease_token=NULL,
                   lease_expires_at=NULL, updated_at=? WHERE state='processing'""",
                (now,),
            )
        else:
            connection.execute(
                """UPDATE inbox_events SET state='pending', lease_token=NULL,
                   lease_expires_at=NULL, updated_at=?
                   WHERE state='processing' AND lease_expires_at <= ?""",
                (now, now),
            )

    def _prune(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "DELETE FROM inbox_events WHERE state IN ('done','poison') AND updated_at < ?",
            (now - self.terminal_retention_seconds,),
        )
