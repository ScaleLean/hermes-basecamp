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
    from .event_model import is_addressed_to, normalize_event, parse_target
    from .formatter import format_chunks
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
    from .webhook_ingress import DurableWebhookStore, WebhookHTTPReceiver, WebhookIngress
except ImportError:  # pragma: no cover - installed flat-module wheel
    from event_model import is_addressed_to, normalize_event, parse_target
    from formatter import format_chunks
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
    from webhook_ingress import DurableWebhookStore, WebhookHTTPReceiver, WebhookIngress

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
        "access_token": str(configured("access_token", "BASECAMP_ACCESS_TOKEN")),
        "token_file": str(configured("token_file", "BASECAMP_OAUTH_TOKEN_FILE")),
        "refresh_token": str(configured("refresh_token", "BASECAMP_REFRESH_TOKEN")),
        "client_id": str(configured("client_id", "BASECAMP_CLIENT_ID")),
        "client_secret": str(configured("client_secret", "BASECAMP_CLIENT_SECRET")),
        "expires_at": expires,
        "chat_poll_seconds": int(configured("chat_poll_seconds", "BASECAMP_POLL_CHAT_SECONDS", "15")),
        "notification_poll_seconds": int(configured("notification_poll_seconds", "BASECAMP_POLL_NOTIFY_SECONDS", "45")),
        "reconciliation_seconds": int(configured("reconciliation_seconds", "BASECAMP_RECONCILE_SECONDS", "300")),
        "webhook_token": str(configured("webhook_token", "BASECAMP_WEBHOOK_TOKEN")),
        "webhook_host": str(configured("webhook_host", "BASECAMP_WEBHOOK_HOST", "127.0.0.1")),
        "webhook_port": int(configured("webhook_port", "BASECAMP_WEBHOOK_PORT", "0")),
        "webhook_tls_proxy": str(configured("webhook_tls_proxy", "BASECAMP_WEBHOOK_TLS_PROXY", "false")).lower()
        in {"1", "true", "yes", "on"},
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
        self._media_roots = configured_media_roots()
        self._inbound_media_root = configured_inbound_media_root(values["account_id"], values["person_id"])
        self._processing_waiters: dict[str, asyncio.Future[ProcessingOutcome]] = {}
        self._poll_seconds = 10
        self._poll_task: asyncio.Task | None = None
        self._replay = ReplayStore(default_replay_path(values["account_id"], values["person_id"]))
        self._poller = CompositePoller(
            self._client,
            chat_seconds=values["chat_poll_seconds"],
            notification_seconds=values["notification_poll_seconds"],
            reconciliation_seconds=values["reconciliation_seconds"],
            cursors=CursorStore(
                default_replay_path(values["account_id"], values["person_id"]).with_name(
                    f"{values['account_id']}-{values['person_id']}-cursors.json"
                )
            ),
        )
        self._health = self._poller.health
        self._runtime_key = (values["account_id"], values["person_id"])
        _RUNTIME_HEALTH[self._runtime_key] = self._health
        self._webhooks: WebhookIngress | None = None
        self._webhook_server: WebhookHTTPReceiver | None = None
        if values["webhook_token"]:
            webhook_path = default_replay_path(values["account_id"], values["person_id"]).with_name(
                f"{values['account_id']}-{values['person_id']}-webhooks.sqlite3"
            )
            self._webhooks = WebhookIngress(
                self._client,
                token=values["webhook_token"],
                store=DurableWebhookStore(webhook_path),
            )
            if values["webhook_port"] > 0:
                self._webhook_server = WebhookHTTPReceiver(
                    self._webhooks,
                    token=values["webhook_token"],
                    host=values["webhook_host"],
                    port=values["webhook_port"],
                    tls_proxy=values["webhook_tls_proxy"],
                )
        self._bootstrapped = False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        try:
            await self._client.attest_full_member()
            await asyncio.to_thread(self._replay.load)
            if self._webhooks:
                await asyncio.to_thread(self._webhooks.recover)
        except IdentityMismatchError as exc:
            self._health.connected = False
            self._health.identity_ok = False
            self._health.role_ok = False
            logger.error("[%s] Identity check failed: %s", self.name, exc)
            self._set_fatal_error("basecamp_identity_mismatch", str(exc), retryable=False)
            await self._client.close()
            return False
        except Exception as exc:
            self._health.connected = False
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
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._health.identity_ok = True
        self._health.role_ok = True
        self._health.revoked = False
        self._health.connected = True
        self._mark_connected()
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._health.connected = False
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
                self._health.connected = True
                delay = float(self._poll_seconds)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[%s] Timeline poll failed: %s", self.name, exc)
                self._health.mark("poll_loop_failures")
                self._health.connected = False
                if self._poller.health.revoked:
                    self._health.connected = False
                    self._set_fatal_error("basecamp_oauth_revoked", str(exc), retryable=False)
                    return
                delay = _backoff_after_failure(delay, float(self._poll_seconds))
            await asyncio.sleep(delay)

    def health_snapshot(self) -> dict[str, Any]:
        """Return safe operator health without credentials or message content."""
        return self._health.snapshot()

    async def _poll_once(self) -> None:
        events = await self._poller.collect()
        if self._poller.health.revoked:
            raise BasecampRuntimeError("Basecamp OAuth grant was revoked")
        if self._webhooks:
            events.extend(await self._webhooks.drain_nowait())
        pointers = []
        for raw in events:
            pointer = normalize_event(raw)
            if pointer is None:
                if self._webhooks and raw.get("_durable_event_id"):
                    self._webhooks.ack(raw)
                continue
            pointers.append(pointer)
        if not self._bootstrapped:
            polling_ids = [item.event_id for item in pointers if not item.raw.get("_durable_event_id")]
            await asyncio.to_thread(self._replay.bootstrap, polling_ids)
            self._bootstrapped = True
            pointers = [item for item in pointers if item.raw.get("_durable_event_id")]
        for pointer in reversed(pointers):
            if pointer.project_id not in self._client.project_ids:
                logger.warning("[%s] Rejected event from denied project %s", self.name, pointer.project_id)
                if self._webhooks:
                    self._webhooks.ack(pointer.raw)
                continue
            if not await asyncio.to_thread(self._replay.claim, pointer.event_id):
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
                verified = normalize_event(verified_raw)
                if (
                    not verified
                    or verified.project_id not in self._client.project_ids
                    or not is_addressed_to(
                        verified_raw, person_id=self._client.expected.person_id, mention=self._mention
                    )
                ):
                    await self._commit_pointer(pointer)
                    continue
                source = self.build_source(
                    chat_id=verified.chat_id,
                    chat_name=f"Basecamp {verified.project_id}",
                    chat_type="group",
                    user_id=verified.person_id,
                    user_name=verified.person_name,
                    scope_id=self._client.expected.account_id,
                    message_id=verified.event_id,
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
                    metadata={"basecamp_kind": verified.kind, "project_id": verified.project_id},
                )
                completion = asyncio.get_running_loop().create_future()
                self._processing_waiters[verified.event_id] = completion
                try:
                    await self.handle_message(message_event)
                    outcome = await asyncio.shield(completion)
                finally:
                    self._processing_waiters.pop(verified.event_id, None)
                if outcome is not ProcessingOutcome.SUCCESS:
                    raise RuntimeError(f"Hermes processing did not complete successfully: {outcome.value}")
                await self._commit_pointer(pointer)
            except asyncio.CancelledError:
                await asyncio.to_thread(self._replay.release, pointer.event_id)
                if self._webhooks:
                    self._webhooks.retry(pointer.raw)
                raise
            except Exception:
                await asyncio.to_thread(self._replay.release, pointer.event_id)
                if self._webhooks:
                    self._webhooks.retry(pointer.raw)
                logger.exception("[%s] Isolated failed Basecamp event %s", self.name, pointer.event_id)
                continue

    async def _commit_pointer(self, pointer: Any) -> None:
        await asyncio.to_thread(self._replay.commit, pointer.event_id)
        if self._webhooks:
            self._webhooks.ack(pointer.raw)

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
            manager = MediaManager(self._client, allowed_roots=self._media_roots)
            markup = await manager.upload_markup([file_path], force_document=force_document)
            chunks = format_chunks(caption or "", max_length=MAX_MESSAGE_LENGTH - len(markup)) if caption else [""]
            chunks[-1] = chunks[-1] + markup
            message_id = ""
            for chunk in chunks:
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
            if project_id not in self._client.project_ids:
                raise BasecampRuntimeError("Basecamp target project is not allowlisted")
            verified_items: list[dict[str, Any]] = []
            message_id = ""
            for chunk in format_chunks(content, max_length=MAX_MESSAGE_LENGTH):
                result = (
                    await self._client.post_chat(project_id, target_id, chunk)
                    if target_type == "chat"
                    else await self._client.post_comment(project_id, target_id, chunk)
                )
                message_id = str(result.get("id") or "")
                if not message_id:
                    raise BasecampRuntimeError("Basecamp write lacked an item ID")
                verified = (
                    await self._client.verify_chat_authorship(target_id, message_id)
                    if target_type == "chat"
                    else await self._client.verify_comment_authorship(message_id)
                )
                verified_items.append(dict(verified))
            return SendResult(success=True, message_id=message_id, raw_response={"items": verified_items})
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        target_type, project_id, target_id = parse_target(chat_id)
        return {"name": f"Basecamp {target_type} {target_id}", "type": "group", "project_id": project_id}


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
    if target_type not in {"chat", "recording"} or project_id not in _settings()["project_ids"]:
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
        "token_path": "token_file",
        "token_file": "token_file",
        "poll_chat_seconds": "chat_poll_seconds",
        "poll_notify_seconds": "notification_poll_seconds",
        "reconcile_seconds": "reconciliation_seconds",
        "webhook_host": "webhook_host",
        "webhook_port": "webhook_port",
        "webhook_tls_proxy": "webhook_tls_proxy",
    }
    extras: dict[str, Any] = {}
    for source, destination in aliases.items():
        if source not in basecamp_cfg or destination in extras:
            continue
        value = basecamp_cfg[source]
        if destination == "project_ids" and isinstance(value, list):
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
