"""Validated Basecamp attachment upload and download handling."""

from __future__ import annotations

import hmac
import mimetypes
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")
_ATTACHMENT_SGID = re.compile(r"^sgid://bc3/(?:Attachment|Blob)/[A-Za-z0-9_-]+(?:\?.*)?$")


class MediaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedMedia:
    path: Path
    name: str
    mime_type: str
    size: int


class MediaManager:
    def __init__(
        self,
        client: Any,
        *,
        allowed_roots: Iterable[Path],
        max_bytes: int = 50 * 1024 * 1024,
        allowed_mime_prefixes: tuple[str, ...] = ("image/", "audio/", "video/", "text/", "application/pdf"),
    ) -> None:
        self.client = client
        self.allowed_roots = tuple(root.expanduser().resolve() for root in allowed_roots)
        self.max_bytes = max_bytes
        self.allowed_mime_prefixes = allowed_mime_prefixes
        if not self.allowed_roots:
            raise MediaValidationError("At least one Basecamp media root is required")

    def prepare(self, raw_path: str, *, force_document: bool = False) -> PreparedMedia:
        path = Path(raw_path).expanduser().resolve(strict=True)
        if not path.is_file() or not any(path.is_relative_to(root) for root in self.allowed_roots):
            raise MediaValidationError("Basecamp media path is outside the configured roots")
        size = path.stat().st_size
        if size <= 0 or size > self.max_bytes:
            raise MediaValidationError(f"Basecamp media size must be between 1 and {self.max_bytes} bytes")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        del force_document  # Basecamp attachments retain their canonical MIME type.
        if not any(mime_type == prefix or mime_type.startswith(prefix) for prefix in self.allowed_mime_prefixes):
            raise MediaValidationError(f"Basecamp media MIME type is not allowed: {mime_type}")
        safe_name = _SAFE_NAME.sub("_", path.name).strip(". ")
        if not safe_name:
            raise MediaValidationError("Basecamp media filename is invalid")
        return PreparedMedia(path, safe_name, mime_type, size)

    async def upload(self, media: PreparedMedia) -> Mapping[str, Any]:
        result = await self.client.call(
            "attachments",
            "create",
            {"content": media.path.read_bytes(), "content_type": media.mime_type, "name": media.name},
        )
        if not isinstance(result, Mapping) or not result.get("attachable_sgid"):
            raise RuntimeError("Basecamp attachment upload lacked an attachable SGID")
        return result

    async def upload_markup(self, paths: Iterable[str], *, force_document: bool = False) -> str:
        markup: list[str] = []
        for raw_path in paths:
            media = self.prepare(raw_path, force_document=force_document)
            uploaded = await self.upload(media)
            sgid = str(uploaded["attachable_sgid"])
            if not _ATTACHMENT_SGID.fullmatch(sgid):
                raise RuntimeError("Basecamp attachment upload returned an invalid SGID")
            markup.append(f'<bc-attachment sgid="{sgid}"></bc-attachment>')
        return "".join(markup)

    async def receive(self, attachment: Mapping[str, Any], destination_root: Path) -> PreparedMedia:
        """Download an authenticated Basecamp attachment into an approved root."""
        raw_url = str(attachment.get("download_url") or attachment.get("url") or "")
        if not raw_url.startswith("https://"):
            raise MediaValidationError("Basecamp attachment URL must use HTTPS")
        destination = destination_root.expanduser().resolve()
        if destination not in self.allowed_roots:
            raise MediaValidationError("Basecamp download destination is outside the configured roots")
        result = await self.client.download_url(raw_url)
        body = bytes(result.body)
        mime_type = str(result.content_type or "application/octet-stream").split(";", 1)[0]
        if len(body) <= 0 or len(body) > self.max_bytes or result.content_length != len(body):
            raise MediaValidationError("Basecamp attachment length is missing, mismatched, or too large")
        if not any(mime_type == prefix or mime_type.startswith(prefix) for prefix in self.allowed_mime_prefixes):
            raise MediaValidationError(f"Basecamp media MIME type is not allowed: {mime_type}")
        prefix = str(attachment.get("id") or "")
        filename = f"{prefix}-{result.filename}" if prefix.isdigit() else str(result.filename or "attachment")
        safe_name = _SAFE_NAME.sub("_", filename).strip(". ")
        if not safe_name:
            raise MediaValidationError("Basecamp attachment filename is invalid")
        target = (destination / safe_name).resolve()
        if not target.is_relative_to(destination):
            raise MediaValidationError("Basecamp attachment filename escapes the destination")
        if target.exists():
            existing = target.read_bytes()
            if hmac.compare_digest(existing, body):
                return PreparedMedia(target, safe_name, mime_type, len(body))
            raise MediaValidationError("Basecamp attachment retry collided with different existing content")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        return PreparedMedia(target, safe_name, mime_type, len(body))


def find_attachments(value: Any, *, max_items: int = 20) -> tuple[Mapping[str, Any], ...]:
    """Find bounded attachment descriptors without treating arbitrary links as files."""
    found: list[Mapping[str, Any]] = []

    def visit(item: Any) -> None:
        if len(found) >= max_items:
            return
        if isinstance(item, Mapping):
            if item.get("download_url") and (item.get("filename") or item.get("name") or item.get("id")):
                found.append(item)
                return
            for key in ("attachments", "files", "embeds"):
                if key in item:
                    visit(item[key])
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found)


def configured_media_roots() -> tuple[Path, ...]:
    """Return operator-approved roots for outbound uploads only."""
    from agent.secret_scope import get_secret

    raw = str(get_secret("BASECAMP_MEDIA_ROOTS", "") or "").strip()
    if raw:
        return tuple(Path(item) for item in raw.split(os.pathsep) if item)
    return ()


def configured_inbound_media_root(account_id: str = "", person_id: str = "") -> Path:
    """Return a private spool that is never an implicit outbound allow-root."""
    from agent.secret_scope import get_secret
    from hermes_constants import get_hermes_home

    state = str(get_secret("BASECAMP_STATE_DIR", "") or "").strip()
    base = Path(state).expanduser() if state else get_hermes_home() / "state" / "basecamp"
    identity = f"{account_id}-{person_id}" if account_id and person_id else "default"
    spool = base / "inbound-media" / identity
    spool.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(spool, 0o700)
    return spool.resolve()
