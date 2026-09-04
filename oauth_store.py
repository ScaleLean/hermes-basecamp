"""Profile-isolated OAuth storage and BC5 resource-aware token refresh."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from basecamp.oauth import OAuthToken, refresh_token


@dataclass(frozen=True)
class StoredOAuthToken:
    access_token: str
    refresh_token: str
    expires_at: float | None
    resource: str | None
    token_endpoint: str
    client_id: str = "basecamp-cli"
    client_secret: str = ""
    legacy_format: bool = False
    scope: str | None = None


class OAuthTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def load(self) -> StoredOAuthToken:
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(f"OAuth token file must be mode 0600: {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("token payload must be an object")
            return StoredOAuthToken(**payload)
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Basecamp OAuth token state is unreadable; preserving it for recovery: {self.path}"
            ) from exc

    def save(self, token: StoredOAuthToken) -> None:
        with self.locked():
            self.save_locked(token)

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a+") as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def save_locked(self, token: StoredOAuthToken) -> None:
        """Save while the caller holds ``locked()``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(token), handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(self.path)
            self.path.chmod(0o600)
        finally:
            if temp.exists():
                temp.unlink()


class ResourceOAuthTokenProvider:
    """SDK token-provider protocol with durable, resource-bound refresh."""

    def __init__(self, store: OAuthTokenStore) -> None:
        self.store = store
        self.token = store.load()
        self._lock = asyncio.Lock()

    @property
    def refreshable(self) -> bool:
        return bool(self.token.refresh_token)

    async def access_token(self) -> str:
        async with self._lock:
            if self._expired():
                await self._refresh_locked()
            return self.token.access_token

    async def refresh(self) -> bool:
        async with self._lock:
            if not self.refreshable:
                return False
            await self._refresh_locked()
            return True

    def _expired(self) -> bool:
        return self.token.expires_at is not None and time.time() + 60 >= self.token.expires_at

    async def _refresh_locked(self) -> None:
        previous = self.token
        self.token = await asyncio.to_thread(self._refresh_across_processes, previous)

    def _refresh_across_processes(self, previous: StoredOAuthToken) -> StoredOAuthToken:
        with self.store.locked():
            current = self.store.load()
            if current != previous and not self._token_expired(current):
                return current
            refreshed: OAuthToken = refresh_token(
                current.token_endpoint,
                current.refresh_token,
                client_id=current.client_id,
                client_secret=current.client_secret or None,
                use_legacy_format=current.legacy_format,
                resource=current.resource,
            )
            stored = StoredOAuthToken(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token or current.refresh_token,
                expires_at=refreshed.expires_at,
                resource=refreshed.resource or current.resource,
                token_endpoint=current.token_endpoint,
                client_id=current.client_id,
                client_secret=current.client_secret,
                legacy_format=current.legacy_format,
                scope=refreshed.scope or current.scope,
            )
            self.store.save_locked(stored)
            return stored

    @staticmethod
    def _token_expired(token: StoredOAuthToken) -> bool:
        return token.expires_at is not None and time.time() + 60 >= token.expires_at
