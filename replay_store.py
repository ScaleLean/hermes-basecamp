"""Durable, content-free replay protection for Basecamp event IDs."""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ReplayStore:
    """Persist pending and committed event IDs with atomic file replacement."""

    def __init__(self, path: Path, *, max_committed: int = 5_000, claim_ttl: int = 300) -> None:
        self.path = path
        self.max_committed = max_committed
        self.claim_ttl = claim_ttl
        self.pending: dict[str, float] = {}
        self.committed: list[str] = []
        self._committed_set: set[str] = set()

    def load(self) -> None:
        with self._locked():
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        if not self.path.exists():
            self.pending = {}
            self.committed = []
            self._committed_set = set()
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("Basecamp replay state must be a JSON object")
        now = time.time()
        pending = payload.get("pending") or {}
        committed = payload.get("committed") or []
        if isinstance(pending, dict):
            self.pending = {
                str(key): float(value) for key, value in pending.items() if now - float(value) < self.claim_ttl
            }
        if isinstance(committed, list):
            self.committed = [str(value) for value in committed][-self.max_committed :]
            self._committed_set = set(self.committed)

    def bootstrap(self, event_ids: list[str]) -> None:
        with self._locked():
            self._load_unlocked()
            for event_id in event_ids:
                self._commit_in_memory(event_id)
            self._save_unlocked()

    def claim(self, event_id: str) -> bool:
        with self._locked():
            self._load_unlocked()
            now = time.time()
            if event_id in self._committed_set:
                return False
            claimed_at = self.pending.get(event_id)
            if claimed_at is not None and now - claimed_at < self.claim_ttl:
                return False
            self.pending[event_id] = now
            self._save_unlocked()
            return True

    def commit(self, event_id: str) -> None:
        with self._locked():
            self._load_unlocked()
            self.pending.pop(event_id, None)
            self._commit_in_memory(event_id)
            self._save_unlocked()

    def release(self, event_id: str) -> None:
        with self._locked():
            self._load_unlocked()
            if self.pending.pop(event_id, None) is not None:
                self._save_unlocked()

    def _commit_in_memory(self, event_id: str) -> None:
        if event_id in self._committed_set:
            return
        self.committed.append(event_id)
        self._committed_set.add(event_id)
        if len(self.committed) > self.max_committed:
            removed = self.committed[: -self.max_committed]
            self.committed = self.committed[-self.max_committed :]
            self._committed_set.difference_update(removed)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _save_unlocked(self) -> None:
        payload = {"version": 1, "pending": self.pending, "committed": self.committed}
        temp = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(self.path)
        finally:
            if temp.exists():
                temp.unlink()


def default_replay_path(account_id: str, person_id: str) -> Path:
    from agent.secret_scope import get_secret
    from hermes_constants import get_hermes_home

    configured = str(get_secret("BASECAMP_STATE_DIR", "") or "").strip()
    root = Path(configured).expanduser() if configured else get_hermes_home() / "state" / "basecamp"
    return root / f"{account_id}-{person_id}-replay.json"
