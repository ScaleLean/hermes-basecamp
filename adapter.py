"""First-class Basecamp platform adapter for Hermes Agent."""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent.secret_scope import get_secret as _scoped_get_secret
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)

try:
    from .delivery_journal import Delivery, DeliveryJournal
    from .event_model import is_eligible_for_agent, normalize_event, parse_target
    from .formatter import format_chunks
    from .inbox import DurableInbox
    from .media import MediaManager, configured_inbound_media_root, configured_media_roots, find_attachments
    from .oauth_store import OAuthTokenStore, ResourceOAuthTokenProvider
    from .poller import CompositePoller, CursorStore
    from .replay_store import ReplayStore, default_replay_path
    from .sdk_client import (
        SDK_AVAILABLE,
        BasecampRuntimeError,
        BasecampSDKClient,
        ExpectedIdentity,
        IdentityMismatchError,
        OAuthCredentials,
    )
    from .webhook_ingress import WebhookHTTPReceiver, WebhookIngress
except ImportError:  # pragma: no cover - installed flat-module wheel
    from delivery_journal import Delivery, DeliveryJournal
    from event_model import is_eligible_for_agent, normalize_event, parse_target
    from formatter import format_chunks
    from inbox import DurableInbox
    from media import MediaManager, configured_inbound_media_root, configured_media_roots, find_attachments
    from oauth_store import OAuthTokenStore, ResourceOAuthTokenProvider
    from poller import CompositePoller, CursorStore
    from replay_store import ReplayStore, default_replay_path
    from sdk_client import (
        SDK_AVAILABLE,
        BasecampRuntimeError,
        BasecampSDKClient,
        ExpectedIdentity,
        IdentityMismatchError,
        OAuthCredentials,
    )
    from webhook_ingress import WebhookHTTPReceiver, WebhookIngress

logger = logging.getLogger(__name__)
MAX_MESSAGE_LENGTH = 50_000
_RUNTIME_HEALTH: dict[tuple[str, str], Any] = {}


def _secret(name: str, default: str = "") -> str:
    value = _scoped_get_secret(name, default)
    return str(value if value is not None else default).strip()


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _project_ids(value: Any) -> tuple[str, ...]:
    """Normalize YAML lists and environment CSV into one allowlist shape."""
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return _csv(str(value or ""))


def _settings(config: PlatformConfig | None = None) -> dict[str, Any]:
    extra = getattr(config, "extra", None) or {}

    def configured(key: str, environment: str, default: str = "") -> Any:
        return extra[key] if key in extra else _secret(environment, default)

    expires_value = configured("expires_at", "BASECAMP_TOKEN_EXPIRES_AT")
    expires = None if expires_value is None or str(expires_value).strip() == "" else float(expires_value)
    return {
        "account_id": str(configured("account_id", "BASECAMP_ACCOUNT_ID")),
        "person_id": str(configured("person_id", "BASECAMP_PERSON_ID")),
        "person_email": str(configured("person_email", "BASECAMP_PERSON_EMAIL")),
        "mention": str(configured("mention", "BASECAMP_AGENT_MENTION")),
        "project_ids": _project_ids(configured("project_ids", "BASECAMP_PROJECT_IDS")),
        "peer_agent_ids": _project_ids(configured("peer_agent_ids", "BASECAMP_PEER_AGENT_IDS")),
        "access_token": str(configured("access_token", "BASECAMP_ACCESS_TOKEN")),
        "token_file": str(configured("token_file", "BASECAMP_OAUTH_TOKEN_FILE")),
        "refresh_token": str(configured("refresh_token", "BASECAMP_REFRESH_TOKEN")),
        "client_id": str(configured("client_id", "BASECAMP_CLIENT_ID")),
        "client_secret": str(configured("client_secret", "BASECAMP_CLIENT_SECRET")),
        "expires_at": expires,
        "chat_poll_seconds": int(configured("chat_poll_seconds", "BASECAMP_POLL_CHAT_SECONDS", "15")),
        "notification_poll_seconds": int(configured("notification_poll_seconds", "BASECAMP_POLL_NOTIFY_SECONDS", "45")),
        "assignment_poll_seconds": int(configured("assignment_poll_seconds", "BASECAMP_POLL_ASSIGNMENT_SECONDS", "30")),
        "reconciliation_seconds": int(configured("reconciliation_seconds", "BASECAMP_RECONCILE_SECONDS", "300")),
        "webhook_token": str(configured("webhook_token", "BASECAMP_WEBHOOK_TOKEN")),
        "webhook_host": str(configured("webhook_host", "BASECAMP_WEBHOOK_HOST", "127.0.0.1")),
        "webhook_port": int(configured("webhook_port", "BASECAMP_WEBHOOK_PORT", "0")),
        "webhook_tls_proxy": str(configured("webhook_tls_proxy", "BASECAMP_WEBHOOK_TLS_PROXY", "false")).lower()
        in {"1", "true", "yes", "on"},
        "webhook_public_url": str(configured("webhook_public_url", "BASECAMP_WEBHOOK_PUBLIC_URL")),
    }


def _configured(values: dict[str, Any]) -> bool:
    return SDK_AVAILABLE and bool(
        (values["token_file"] or values["access_token"])
        and values["account_id"]
        and values["person_id"]
        and values["person_email"]
        and values["mention"]
        and values["project_ids"]
    )


def _safe_webhook_bind(values: Mapping[str, Any]) -> bool:
    if int(values.get("webhook_port") or 0) <= 0 or values.get("webhook_tls_proxy"):
        return True
    host = str(values.get("webhook_host") or "")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def check_requirements() -> bool:
    values = _settings()
    return _configured(values) and _safe_webhook_bind(values)


def validate_config(config: PlatformConfig) -> bool:
    values = _settings(config)
    return _configured(values) and _safe_webhook_bind(values)


def is_connected(config: PlatformConfig) -> bool:
    values = _settings(config)
    health = _RUNTIME_HEALTH.get((values["account_id"], values["person_id"]))
    return bool(health and health.connected and health.identity_ok and health.role_ok and not health.revoked)


def _backoff_after_failure(current: float, base: float) -> float:
    return min(300.0, max(base, current * 2))


def _make_client(values: dict[str, Any]) -> BasecampSDKClient:
    token_provider = (
        ResourceOAuthTokenProvider(OAuthTokenStore(Path(values["token_file"]))) if values["token_file"] else None
    )
    return BasecampSDKClient(
        expected=ExpectedIdentity(values["account_id"], values["person_id"], values["person_email"]),
        credentials=None
        if token_provider
        else OAuthCredentials(
            values["access_token"],
            values["refresh_token"],
            values["client_id"],
            values["client_secret"],
            values["expires_at"],
        ),
        project_ids=values["project_ids"],
        token_provider=token_provider,
    )


class BasecampAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("basecamp"))
        values = _settings(config)
        if not _safe_webhook_bind(values):
            raise ValueError(
                "Basecamp webhook receiver must bind to loopback unless an explicit TLS proxy is configured"
            )
        self._client = _make_client(values)
        self._mention = values["mention"]
        self._peer_agent_ids = values["peer_agent_ids"]
        self._media_roots = configured_media_roots()
        self._inbound_media_root = configured_inbound_media_root(values["account_id"], values["person_id"])
        self._processing_waiters: dict[str, asyncio.Future[ProcessingOutcome]] = {}
        self._active_dispatches: dict[str, str] = {}
        self._verified_deliveries: set[str] = set()
        self._delivery_sequences: dict[str, int] = {}
        self._chat_transcripts: dict[str, str] = {}
        self._target_types: dict[str, str] = {}
        self._poll_seconds = 10
        self._poll_task: asyncio.Task | None = None
        self._replay = ReplayStore(default_replay_path(values["account_id"], values["person_id"]))
        self._inbox = DurableInbox(
            default_replay_path(values["account_id"], values["person_id"]).with_name(
                f"{values['account_id']}-{values['person_id']}-inbox.sqlite3"
            )
        )
        self._deliveries = DeliveryJournal(
            default_replay_path(values["account_id"], values["person_id"]).with_name(
                f"{values['account_id']}-{values['person_id']}-deliveries.sqlite3"
            )
        )
        self._poller = CompositePoller(
            self._client,
            chat_seconds=values["chat_poll_seconds"],
            notification_seconds=values["notification_poll_seconds"],
            assignment_seconds=values["assignment_poll_seconds"],
            reconciliation_seconds=values["reconciliation_seconds"],
            cursors=CursorStore(
                default_replay_path(values["account_id"], values["person_id"]).with_name(
                    f"{values['account_id']}-{values['person_id']}-cursors.json"
                )
            ),
            inbox=self._inbox,
        )
        self._health = self._poller.health
        self._runtime_key = (values["account_id"], values["person_id"])
        _RUNTIME_HEALTH[self._runtime_key] = self._health
        self._webhooks: WebhookIngress | None = None
        self._webhook_server: WebhookHTTPReceiver | None = None
        self._webhook_public_url = values["webhook_public_url"]
        if values["webhook_token"]:
            self._webhooks = WebhookIngress(
                self._client,
                token=values["webhook_token"],
                inbox=self._inbox,
            )
            if values["webhook_port"] > 0:
                self._webhook_server = WebhookHTTPReceiver(
                    self._webhooks,
                    token=values["webhook_token"],
                    host=values["webhook_host"],
                    port=values["webhook_port"],
                    tls_proxy=values["webhook_tls_proxy"],
                )
        self._bootstrapped = self._inbox.is_bootstrapped()

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._health.transition("starting")
        try:
            await self._client.attest_full_member()
            await asyncio.to_thread(self._replay.load)
            await asyncio.to_thread(self._inbox.recover)
            if self._webhooks:
                await asyncio.to_thread(self._webhooks.recover)
        except IdentityMismatchError as exc:
            self._health.transition("blocked")
            self._health.identity_ok = False
            self._health.role_ok = False
            logger.error("[%s] Identity check failed: %s", self.name, exc)
            self._set_fatal_error("basecamp_identity_mismatch", str(exc), retryable=False)
            await self._client.close()
            return False
        except Exception as exc:
            self._health.transition("blocked")
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            code = "basecamp_auth_unavailable" if status in {401, 403} else "basecamp_service_unavailable"
            logger.warning("[%s] Basecamp connection check failed: %s", self.name, exc)
            self._set_fatal_error(code, str(exc), retryable=True)
            return False
        if self._webhook_server:
            try:
                await self._webhook_server.start()
            except Exception as exc:
                await self._client.close()
                self._set_fatal_error("basecamp_webhook_bind_failed", str(exc), retryable=False)
                return False
        self._health.identity_ok = True
        self._health.role_ok = True
        self._health.revoked = False
        webhook_healthy = await self._verify_webhook_registration()
        try:
            await self._poll_once()
        except Exception as exc:
            logger.warning("[%s] Initial receive probe failed: %s", self.name, exc)
            self._health.transition("recovering")
        else:
            self._health.transition("ready" if webhook_healthy and self._receive_lanes_ready() else "recovering")
        self._mark_connected()
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._health.transition("stopped")
        self._mark_disconnected()
        try:
            if self._poll_task:
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass
                self._poll_task = None
        finally:
            try:
                if self._webhooks:
                    await asyncio.to_thread(self._webhooks.recover)
                if self._webhook_server:
                    await self._webhook_server.stop()
            finally:
                await self._client.close()

    async def _poll_loop(self) -> None:
        delay = float(self._poll_seconds)
        while self._running:
            try:
                await self._poll_once()
                self._transition(
                    "ready"
                    if getattr(self._health, "webhook_registration", "") == "healthy"
                    and self._receive_lanes_ready()
                    else "recovering"
                )
                delay = float(self._poll_seconds)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[%s] Timeline poll failed: %s", self.name, exc)
                self._health.mark("poll_loop_failures")
                self._transition("recovering")
                if self._poller.health.revoked:
                    self._health.connected = False
                    self._set_fatal_error("basecamp_oauth_revoked", str(exc), retryable=False)
                    return
                delay = _backoff_after_failure(delay, float(self._poll_seconds))
            await asyncio.sleep(delay)

    def health_snapshot(self) -> dict[str, Any]:
        """Return safe operator health without credentials or message content."""
        self._health.inbox = self._inbox.stats()
        return self._health.snapshot()

    def _transition(self, state: str) -> None:
        if hasattr(self._health, "transition"):
            self._health.transition(state)
        else:  # Compatibility for narrow host/test health doubles.
            self._health.connected = state in {"ready", "recovering"}

    def _receive_lanes_ready(self) -> bool:
        required = {"campfire_discovery", "chat", "notifications", "assignments", "reconciliation"}
        return (
            required.issubset(getattr(self._health, "lane_last_success", {}))
            and self._inbox.stats().get("poison", 0) == 0
        )

    async def _verify_webhook_registration(self) -> bool:
        if not self._webhook_server or not self._webhook_public_url:
            self._health.webhook_registration = "unconfigured"
            return False
        try:
            try:
                from .sdk_client import SAFE_WEBHOOK_RECORDING_GETTERS
                from .webhook_reconciliation import WebhookReconciler
            except ImportError:  # pragma: no cover
                from sdk_client import SAFE_WEBHOOK_RECORDING_GETTERS
                from webhook_reconciliation import WebhookReconciler
            results = await WebhookReconciler(
                self._client,
                payload_url=self._webhook_public_url,
                event_types=tuple(SAFE_WEBHOOK_RECORDING_GETTERS),
            ).reconcile(approved=False)
        except Exception:
            self._health.webhook_registration = "drifted"
            return False
        healthy = bool(results) and all(item.verified for item in results)
        self._health.webhook_registration = "healthy" if healthy else "drifted"
        return healthy

    async def _poll_once(self) -> None:
        polled = await self._poller.collect()
        if polled:
            await asyncio.to_thread(self._inbox.accept_batch, "poll", "compatibility", polled)
        if self._poller.health.revoked:
            raise BasecampRuntimeError("Basecamp OAuth grant was revoked")
        events: list[Mapping[str, Any]] = []
        if self._webhooks and self._webhooks.inbox is not self._inbox:
            for webhook_event in await self._webhooks.drain_nowait():
                await asyncio.to_thread(
                    self._inbox.accept_batch, "webhook", "webhook:compatibility", [webhook_event]
                )
        runner = getattr(self, "gateway_runner", None)
        if runner is not None and getattr(runner, "_startup_restore_in_progress", False):
            return
        while len(events) < 100:
            claimed = await asyncio.to_thread(self._inbox.claim)
            if claimed is None:
                break
            events.append(claimed.payload)
        pointers = []
        for raw in events:
            pointer = normalize_event(raw)
            if pointer is None:
                await asyncio.to_thread(self._inbox.complete, raw)
                if self._webhooks:
                    self._webhooks.ack(raw)
                continue
            pointers.append(pointer)
        if not self._bootstrapped:
            polling_ids = [item.event_id for item in pointers]
            await asyncio.to_thread(self._replay.bootstrap, polling_ids)
            self._bootstrapped = True
            for item in pointers:
                await asyncio.to_thread(self._inbox.complete, item.raw)
            await asyncio.to_thread(self._inbox.mark_bootstrapped)
            pointers = []
        for pointer in reversed(pointers):
            bucket = pointer.raw.get("bucket") or {}
            is_ping = isinstance(bucket, Mapping) and str(bucket.get("type") or "") == "Circle"
            if not is_ping and pointer.project_id not in self._client.project_ids:
                logger.warning("[%s] Rejected event from denied project %s", self.name, pointer.project_id)
                await asyncio.to_thread(self._inbox.complete, pointer.raw)
                if self._webhooks:
                    self._webhooks.ack(pointer.raw)
                continue
            if not await asyncio.to_thread(self._replay.claim, pointer.event_id):
                await asyncio.to_thread(self._inbox.complete, pointer.raw)
                if self._webhooks:
                    self._webhooks.ack(pointer.raw)
                continue
            try:
                if pointer.person_id == self._client.expected.person_id:
                    await self._commit_pointer(pointer)
                    continue
                canonical = await self._client.fetch_recording(pointer.raw)
                if canonical is None:
                    await self._commit_pointer(pointer)
                    continue
                verified_raw = dict(pointer.raw)
                verified_raw["recording"] = canonical
                canonical_creator = canonical.get("creator") or {}
                if isinstance(canonical_creator, Mapping) and canonical_creator.get("id"):
                    verified_raw["creator"] = canonical_creator
                verified = normalize_event(verified_raw)
                active_recording = bool(
                    verified
                    and await asyncio.to_thread(
                        self._inbox.is_active, verified.project_id, verified.recording_id
                    )
                )
                if (
                    not verified
                    or verified.person_id == self._client.expected.person_id
                    or (not is_ping and verified.project_id not in self._client.project_ids)
                    or not is_eligible_for_agent(
                        verified_raw,
                        person_id=self._client.expected.person_id,
                        mention=self._mention,
                        peer_agent_ids=self._peer_agent_ids,
                        active=active_recording,
                    )
                ):
                    await self._commit_pointer(pointer)
                    continue
                verified_recording = verified.raw.get("recording") or {}
                if isinstance(verified_recording, Mapping):
                    recording_type = str(verified_recording.get("type") or "")
                    self._target_types[verified.chat_id] = recording_type
                    parent = verified.raw.get("parent") or {}
                    if (
                        recording_type in {"Chat::Line", "Chat::Transcript"}
                        and isinstance(parent, Mapping)
                        and str(parent.get("id") or "")
                    ):
                        self._chat_transcripts[verified.chat_id] = str(parent["id"])
                is_assignment = verified.kind == "assignment_created"
                if await asyncio.to_thread(self._deliveries.has_any, verified.event_id):
                    final_delivery = await self._resume_deliveries(verified)
                    if final_delivery:
                        if is_assignment:
                            await self._complete_assignment(verified)
                        self._health.last_completed_run_at = __import__("time").time()
                        await self._commit_pointer(pointer)
                        continue
                participants = verified.raw.get("participants") or []
                source = self.build_source(
                    chat_id=verified.chat_id,
                    chat_name=f"Basecamp {verified.project_id}",
                    chat_type="direct" if is_ping and len(participants) <= 1 else "group",
                    user_id=verified.person_id,
                    user_name=verified.person_name,
                    scope_id=self._client.expected.account_id,
                    message_id=verified.event_id,
                    role_authorized=(
                        isinstance(canonical_creator, Mapping)
                        and canonical_creator.get("client") is False
                    ),
                )
                received_media = await self._receive_media(canonical)
                message_event = MessageEvent(
                    text=verified.text,
                    message_type=MessageType.TEXT,
                    source=source,
                    user_id=verified.person_id,
                    user_name=verified.person_name,
                    raw_message=verified.raw,
                    message_id=verified.event_id,
                    timestamp=verified.timestamp,
                    allow_gateway_control=False,
                    media_urls=[str(item.path) for item in received_media],
                    media_types=[item.mime_type for item in received_media],
                    media_text_inlined=[False for _ in received_media],
                    metadata={
                        "basecamp_kind": verified.kind,
                        "bucket_id": verified.project_id,
                        "scope_type": "circle" if is_ping else "project",
                    },
                )
                completion = asyncio.get_running_loop().create_future()
                self._processing_waiters[verified.event_id] = completion
                self._active_dispatches[verified.chat_id] = verified.event_id
                self._delivery_sequences[verified.event_id] = await asyncio.to_thread(
                    self._deliveries.next_sequence, verified.event_id
                )
                acknowledgement = asyncio.create_task(
                    self._acknowledge_later(verified, 60.0 if is_assignment else 25.0)
                ) if is_assignment or is_ping or str((verified.raw.get("recording") or {}).get("type") or "") == "Chat::Line" else None
                try:
                    await self.handle_message(message_event)
                    outcome = await asyncio.shield(completion)
                finally:
                    self._processing_waiters.pop(verified.event_id, None)
                    if self._active_dispatches.get(verified.chat_id) == verified.event_id:
                        self._active_dispatches.pop(verified.chat_id, None)
                    self._delivery_sequences.pop(verified.event_id, None)
                    if acknowledgement:
                        acknowledgement.cancel()
                        try:
                            await acknowledgement
                        except asyncio.CancelledError:
                            pass
                delivery_verified = verified.event_id in self._verified_deliveries
                self._verified_deliveries.discard(verified.event_id)
                if outcome is not ProcessingOutcome.SUCCESS or not delivery_verified:
                    if await asyncio.to_thread(self._deliveries.has_final, verified.event_id):
                        raise RuntimeError("Basecamp reply is uncertain and requires delivery reconciliation")
                    if is_assignment:
                        await self._post_assignment_status(
                            verified, "I could not complete this work. The assigned item remains open."
                        )
                        await self._commit_pointer(pointer)
                        continue
                    raise RuntimeError(f"Hermes processing did not complete successfully: {outcome.value}")
                if is_assignment:
                    await self._complete_assignment(verified)
                self._health.last_completed_run_at = __import__("time").time()
                await self._commit_pointer(pointer)
            except asyncio.CancelledError:
                await asyncio.to_thread(self._replay.release, pointer.event_id)
                await asyncio.to_thread(self._inbox.retry, pointer.raw, "cancelled")
                if self._webhooks:
                    self._webhooks.retry(pointer.raw)
                raise
            except Exception as exc:
                await asyncio.to_thread(self._replay.release, pointer.event_id)
                await asyncio.to_thread(self._inbox.retry, pointer.raw, str(exc))
                if self._webhooks:
                    self._webhooks.retry(pointer.raw)
                logger.exception("[%s] Isolated failed Basecamp event %s", self.name, pointer.event_id)
                continue

    async def _commit_pointer(self, pointer: Any) -> None:
        await asyncio.to_thread(self._replay.commit, pointer.event_id)
        await asyncio.to_thread(self._inbox.complete, pointer.raw)
        if self._webhooks:
            self._webhooks.ack(pointer.raw)

    async def _acknowledge_later(self, pointer: Any, delay: float) -> None:
        await asyncio.sleep(delay)
        content = "I received this and am working on it."
        if pointer.kind == "assignment_created":
            await self._post_assignment_status(pointer, content, purpose="acknowledgement")
        else:
            result = await self.send(pointer.chat_id, content)
            if not result.success:
                raise BasecampRuntimeError(result.error or "Basecamp acknowledgement failed")

    async def _post_assignment_status(self, pointer: Any, content: str, *, purpose: str = "status") -> None:
        chunk = format_chunks(content, max_length=MAX_MESSAGE_LENGTH)[0]
        event_id = self._active_dispatches.get(pointer.chat_id)
        if event_id:
            sequence = self._delivery_sequences.get(event_id, 0)
            self._delivery_sequences[event_id] = sequence + 1
            delivery = await asyncio.to_thread(
                self._deliveries.reserve,
                event_id=event_id,
                sequence=sequence,
                chat_id=pointer.chat_id,
                target_type="recording",
                project_id=pointer.project_id,
                target_id=pointer.recording_id,
                content=chunk,
                purpose=purpose,
            )
            comment_id = await self._deliver_reserved(delivery, reconcile_first=delivery.state != "reserved")
        else:
            result = await self._client.post_comment(pointer.project_id, pointer.recording_id, chunk)
            comment_id = str(result.get("id") or "")
        if not comment_id:
            raise BasecampRuntimeError("Basecamp assignment status comment lacked an ID")
        if not event_id:
            await self._client.verify_comment_authorship(comment_id)
        await asyncio.to_thread(self._inbox.activate, pointer.project_id, pointer.recording_id)

    async def _complete_assignment(self, pointer: Any) -> None:
        recording = pointer.raw.get("recording") or {}
        if not isinstance(recording, Mapping) or str(recording.get("type") or "").lower() != "todo":
            return
        await self._client.call("todos", "complete", {"todo_id": int(pointer.recording_id)})
        readback = await self._client.call("todos", "get", {"todo_id": int(pointer.recording_id)})
        bucket = readback.get("bucket") or {} if isinstance(readback, Mapping) else {}
        if (
            not isinstance(readback, Mapping)
            or readback.get("completed") is not True
            or not isinstance(bucket, Mapping)
            or str(bucket.get("id") or "") != pointer.project_id
        ):
            raise BasecampRuntimeError("Basecamp to-do completion read-back failed")

    async def _resume_deliveries(self, pointer: Any) -> bool:
        pending = await asyncio.to_thread(self._deliveries.pending, pointer.event_id)
        for delivery in pending:
            await self._deliver_reserved(delivery, reconcile_first=delivery.state != "reserved")
        has_final = await asyncio.to_thread(self._deliveries.has_final, pointer.event_id)
        if has_final:
            self._verified_deliveries.add(pointer.event_id)
        return has_final

    async def _deliver_reserved(self, delivery: Delivery, *, reconcile_first: bool) -> str:
        if reconcile_first:
            verified = await self._client.reconcile_delivery(
                delivery.target_type,
                delivery.target_id,
                delivery.content,
                item_id=delivery.item_id,
                not_before=delivery.created_at,
            )
            if verified is not None:
                item_id = str(verified.get("id") or delivery.item_id)
                await asyncio.to_thread(
                    self._deliveries.transition, delivery.event_id, delivery.sequence, "verified", item_id=item_id
                )
                return item_id
        await asyncio.to_thread(
            self._deliveries.transition, delivery.event_id, delivery.sequence, "dispatched"
        )
        try:
            if delivery.target_type == "chat":
                result = await self._client.post_chat(delivery.project_id, delivery.target_id, delivery.content)
            elif delivery.target_type == "question":
                result = await self._client.call(
                    "checkins", "create_answer", {"question_id": int(delivery.target_id), "content": delivery.content}
                )
            else:
                result = await self._client.post_comment(delivery.project_id, delivery.target_id, delivery.content)
            item_id = str(result.get("id") or "")
            if not item_id:
                raise BasecampRuntimeError("Basecamp write lacked an item ID")
            await asyncio.to_thread(
                self._deliveries.transition, delivery.event_id, delivery.sequence, "uncertain", item_id=item_id
            )
            if delivery.target_type == "chat":
                await self._client.verify_chat_authorship(delivery.target_id, item_id)
            elif delivery.target_type == "question":
                self._client.verify_creator(
                    await self._client.call("checkins", "get_answer", {"answer_id": int(item_id)})
                )
            else:
                await self._client.verify_comment_authorship(item_id)
            await asyncio.to_thread(
                self._deliveries.transition, delivery.event_id, delivery.sequence, "verified", item_id=item_id
            )
            return item_id
        except Exception:
            await asyncio.to_thread(
                self._deliveries.transition, delivery.event_id, delivery.sequence, "uncertain"
            )
            raise

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Expose Hermes's reply-completion boundary to durable event ingress."""
        await super().on_processing_complete(event, outcome)
        waiter = self._processing_waiters.get(str(event.message_id or ""))
        if waiter is not None and not waiter.done():
            waiter.set_result(outcome)

    async def ingest_webhook(self, token: str, payload: Mapping[str, Any]) -> bool:
        if self._webhooks is None:
            raise BasecampRuntimeError("Basecamp webhook ingress is not configured")
        return await self._webhooks.ingest(token, payload)

    async def _receive_media(self, canonical: Mapping[str, Any]) -> list[Any]:
        attachments = find_attachments(canonical)
        if not attachments:
            return []
        manager = MediaManager(self._client, allowed_roots=(self._inbound_media_root,))
        destination = self._inbound_media_root
        received = []
        for attachment in attachments:
            received.append(await manager.receive(attachment, destination))
        return received

    async def _send_media_file(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None,
        reply_to: str | None,
        metadata: dict[str, Any] | None,
        *,
        force_document: bool = False,
    ) -> SendResult:
        try:
            await self._client.attest_full_member()
            target_type, project_id, target_id = parse_target(chat_id)
            if target_type == "ping":
                project_id, target_id, target_type = target_id, self._chat_transcripts.get(chat_id, ""), "chat"
                if not target_id:
                    raise BasecampRuntimeError("Basecamp Ping transcript is not known from inbound context")
            elif chat_id in self._chat_transcripts:
                target_type = "chat"
            elif target_type == "recording":
                target_type, project_id = await self._client.resolve_target(target_id, project_id)
            manager = MediaManager(self._client, allowed_roots=self._media_roots)
            markup = await manager.upload_markup([file_path], force_document=force_document)
            chunks = format_chunks(caption or "", max_length=MAX_MESSAGE_LENGTH - len(markup)) if caption else [""]
            chunks[-1] = chunks[-1] + markup
            message_id = ""
            for chunk in chunks:
                event_id = self._active_dispatches.get(chat_id)
                if event_id:
                    sequence = self._delivery_sequences.get(event_id, 0)
                    self._delivery_sequences[event_id] = sequence + 1
                    delivery = await asyncio.to_thread(
                        self._deliveries.reserve,
                        event_id=event_id,
                        sequence=sequence,
                        chat_id=chat_id,
                        target_type=target_type,
                        project_id=project_id,
                        target_id=target_id,
                        content=chunk,
                    )
                    message_id = await self._deliver_reserved(
                        delivery, reconcile_first=delivery.state != "reserved"
                    )
                else:
                    result = (
                        await self._client.post_chat(project_id, target_id, chunk)
                        if target_type == "chat"
                        else await self._client.post_comment(project_id, target_id, chunk)
                    )
                    message_id = str(result.get("id") or "")
                    if not message_id:
                        raise BasecampRuntimeError("Basecamp media write lacked an item ID")
                    if target_type == "chat":
                        await self._client.verify_chat_authorship(target_id, message_id)
                    else:
                        await self._client.verify_comment_authorship(message_id)
            event_id = self._active_dispatches.get(chat_id)
            if event_id:
                self._verified_deliveries.add(event_id)
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_media_file(chat_id, image_path, caption, reply_to, metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_media_file(chat_id, file_path, caption, reply_to, metadata, force_document=True)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_media_file(chat_id, video_path, caption, reply_to, metadata)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_media_file(chat_id, audio_path, caption, reply_to, metadata)

    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict[str, Any] | None = None
    ) -> SendResult:
        try:
            await self._client.attest_full_member()
            target_type, project_id, target_id = parse_target(chat_id)
            if target_type == "ping":
                project_id, target_id, target_type = target_id, self._chat_transcripts.get(chat_id, ""), "chat"
                if not target_id:
                    raise BasecampRuntimeError("Basecamp Ping transcript is not known from inbound context")
            elif chat_id in self._chat_transcripts:
                target_type = "chat"
            elif target_type == "recording":
                target_type, project_id = await self._client.resolve_target(target_id, project_id)
            if target_type != "chat" and project_id not in self._client.project_ids:
                raise BasecampRuntimeError("Basecamp target project is not allowlisted")
            verified_items: list[dict[str, Any]] = []
            message_id = ""
            for chunk in format_chunks(content, max_length=MAX_MESSAGE_LENGTH):
                event_id = self._active_dispatches.get(chat_id)
                if event_id:
                    sequence = self._delivery_sequences.get(event_id, 0)
                    self._delivery_sequences[event_id] = sequence + 1
                    delivery_target_type = "question" if self._target_types.get(chat_id) == "Question" else target_type
                    delivery = await asyncio.to_thread(
                        self._deliveries.reserve,
                        event_id=event_id,
                        sequence=sequence,
                        chat_id=chat_id,
                        target_type=delivery_target_type,
                        project_id=project_id,
                        target_id=target_id,
                        content=chunk,
                    )
                    message_id = await self._deliver_reserved(
                        delivery, reconcile_first=delivery.state != "reserved"
                    )
                    verified_items.append({"id": message_id})
                    continue
                if self._target_types.get(chat_id) == "Question":
                    result = await self._client.call(
                        "checkins", "create_answer", {"question_id": int(target_id), "content": chunk}
                    )
                else:
                    result = (
                        await self._client.post_chat(project_id, target_id, chunk)
                        if target_type == "chat"
                        else await self._client.post_comment(project_id, target_id, chunk)
                    )
                message_id = str(result.get("id") or "")
                if not message_id:
                    raise BasecampRuntimeError("Basecamp write lacked an item ID")
                if self._target_types.get(chat_id) == "Question":
                    verified = await self._client.call("checkins", "get_answer", {"answer_id": int(message_id)})
                    self._client.verify_creator(verified)
                else:
                    verified = (
                        await self._client.verify_chat_authorship(target_id, message_id)
                        if target_type == "chat"
                        else await self._client.verify_comment_authorship(message_id)
                    )
                verified_items.append(dict(verified))
            await asyncio.to_thread(self._inbox.activate, project_id, target_id)
            event_id = self._active_dispatches.get(chat_id)
            if event_id:
                self._verified_deliveries.add(event_id)
            return SendResult(success=True, message_id=message_id, raw_response={"items": verified_items})
        except Exception as exc:
            event_id = self._active_dispatches.get(chat_id)
            if event_id and await asyncio.to_thread(self._deliveries.has_final, event_id):
                self._verified_deliveries.discard(event_id)
                logger.warning(
                    "[%s] Final reply accepted by the durable Basecamp journal for reconciliation",
                    self.name,
                )
                return SendResult(success=True, raw_response={"delivery_pending": True})
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        target_type, project_id, target_id = parse_target(chat_id)
        return {
            "name": f"Basecamp {target_type} {target_id}",
            "type": "direct" if target_type == "ping" else "group",
            "project_id": project_id,
        }


def _env_enablement() -> dict[str, Any] | None:
    values = _settings()
    return values if _configured(values) and _safe_webhook_bind(values) else None


def _parse_target_ref(target_ref: str) -> tuple[str, None] | None:
    value = target_ref.strip()
    if value.startswith("basecamp:"):
        value = value.removeprefix("basecamp:")
    try:
        parse_target(value)
    except ValueError:
        return None
    return value, None


def _validate_target_ref(target_ref: str) -> bool | str:
    try:
        target_type, project_id, _ = parse_target(target_ref)
    except ValueError as exc:
        return str(exc)
    if target_type == "ping":
        return True
    if target_type == "bucket":
        return "A Basecamp bucket is a scope, not a conversation target"
    if target_type not in {"chat", "recording"} or (project_id and project_id not in _settings()["project_ids"]):
        return "Basecamp target is outside the configured project allowlist"
    return True


def _apply_yaml_config(_yaml_cfg: dict, basecamp_cfg: dict) -> dict[str, Any] | None:
    """Map platforms.basecamp YAML keys into profile-local adapter extras."""
    aliases = {
        "account_id": "account_id",
        "person_id": "person_id",
        "person_email": "person_email",
        "mention": "mention",
        "projects": "project_ids",
        "project_ids": "project_ids",
        "peer_agent_ids": "peer_agent_ids",
        "token_path": "token_file",
        "token_file": "token_file",
        "poll_chat_seconds": "chat_poll_seconds",
        "poll_notify_seconds": "notification_poll_seconds",
        "poll_assignment_seconds": "assignment_poll_seconds",
        "reconcile_seconds": "reconciliation_seconds",
        "webhook_host": "webhook_host",
        "webhook_port": "webhook_port",
        "webhook_tls_proxy": "webhook_tls_proxy",
        "webhook_public_url": "webhook_public_url",
    }
    extras: dict[str, Any] = {}
    for source, destination in aliases.items():
        if source not in basecamp_cfg or destination in extras:
            continue
        value = basecamp_cfg[source]
        if destination in {"project_ids", "peer_agent_ids"} and isinstance(value, list):
            value = ",".join(str(item) for item in value)
        extras[destination] = value
    return extras or None


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    client = _make_client(_settings(pconfig))
    try:
        await client.attest_full_member()
        target_type, project_id, target_id = parse_target(chat_id)
        if target_type == "ping":
            raise BasecampRuntimeError("Standalone Ping delivery requires an active inbound transcript")
        if target_type == "recording":
            target_type, project_id = await client.resolve_target(target_id, project_id)
        if project_id not in client.project_ids:
            raise BasecampRuntimeError("Basecamp target project is not allowlisted")
        attachment_markup = ""
        if media_files:
            attachment_markup = await MediaManager(client, allowed_roots=configured_media_roots()).upload_markup(
                media_files, force_document=force_document
            )
        message_ids: list[str] = []
        chunks = format_chunks(message, max_length=MAX_MESSAGE_LENGTH - len(attachment_markup)) if message else [""]
        if attachment_markup:
            chunks[-1] += attachment_markup
        for chunk in chunks:
            result = (
                await client.post_chat(project_id, target_id, chunk)
                if target_type == "chat"
                else await client.post_comment(project_id, target_id, chunk)
            )
            message_id = str(result.get("id") or "")
            if not message_id:
                raise BasecampRuntimeError("Basecamp write lacked an item ID")
            if target_type == "chat":
                await client.verify_chat_authorship(target_id, message_id)
            else:
                await client.verify_comment_authorship(message_id)
            message_ids.append(message_id)
        return {
            "success": True,
            "platform": "basecamp",
            "chat_id": chat_id,
            "message_id": message_ids[-1],
            "message_ids": message_ids,
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        await client.close()


def register(ctx) -> None:
    ctx.register_platform(
        name="basecamp",
        label="Basecamp",
        adapter_factory=lambda cfg: BasecampAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[
            "BASECAMP_ACCOUNT_ID",
            "BASECAMP_PERSON_ID",
            "BASECAMP_PERSON_EMAIL",
            "BASECAMP_AGENT_MENTION",
            "BASECAMP_PROJECT_IDS",
        ],
        install_hint="pip install 'basecamp-sdk>=0.16.0,<0.17'",
        env_enablement_fn=_env_enablement,
        standalone_sender_fn=_standalone_send,
        allowed_users_env="BASECAMP_ALLOWED_USERS",
        allow_all_env="BASECAMP_ALLOW_ALL_USERS",
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="BASECAMP_HOME_CHANNEL",
        parse_target_ref_fn=_parse_target_ref,
        validate_target_ref_fn=_validate_target_ref,
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="⛺",
        pii_safe=False,
        allow_update_command=False,
        platform_hint=(
            "You are participating in Basecamp as a distinct member. "
            "Do not imply that you are a human colleague. Keep replies concise and contextual."
        ),
    )
    if hasattr(ctx, "register_cli_command"):
        try:
            from .basecamp_cli import dispatch, register_cli
        except ImportError:  # pragma: no cover - installed flat-module wheel
            from basecamp_cli import dispatch, register_cli

        ctx.register_cli_command(
            name="basecamp",
            help="Set up, diagnose, and test the Basecamp integration",
            setup_fn=register_cli,
            handler_fn=dispatch,
        )
    if hasattr(ctx, "register_tool"):
        try:
            from .tools import register_tools
        except ImportError:  # pragma: no cover - installed flat-module wheel
            module_name = "_hermes_basecamp_capability_tools"
            spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name("tools.py"))
            if spec is None or spec.loader is None:
                raise RuntimeError("Cannot load installed Basecamp capability tools")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            register_tools = module.register_tools

        # Generic tools are process-global in a multiplex gateway. Resolve the
        # client inside the task-local Hermes secret scope on every call so a
        # tool can never capture or borrow another profile's Basecamp identity.
        register_tools(ctx, lambda: _make_client(_settings()))
    if hasattr(ctx, "register_hook"):
        try:
            from .approval import post_approval_response, pre_tool_call
        except ImportError:  # pragma: no cover - installed flat-module wheel
            from approval import post_approval_response, pre_tool_call

        ctx.register_hook("pre_tool_call", pre_tool_call)
        ctx.register_hook("post_approval_response", post_approval_response)
