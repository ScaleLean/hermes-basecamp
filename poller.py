"""Composite Basecamp ingress with independent chat and notification lanes."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

try:
    from .health import RuntimeHealth
    from .inbox import DurableInbox
except ImportError:  # pragma: no cover
    from health import RuntimeHealth
    from inbox import DurableInbox


class CursorStore:
    """Durable lane cursors. Payload content is never persisted."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.values: dict[str, dict[str, Any]] = {}
        if path and path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Basecamp cursor state is unreadable; preserving it for recovery: {path}") from exc
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, Mapping):
                        self.values[str(key)] = {
                            "created_at": str(item.get("created_at") or ""),
                            "ids": [str(value) for value in (item.get("ids") or [])],
                        }
                    else:  # Compatibility with the first timestamp-only cursor format.
                        self.values[str(key)] = {"created_at": str(item), "ids": []}

    def update(self, lane: str, events: list[Mapping[str, Any]]) -> None:
        timestamps = [str(item.get("created_at") or "") for item in events if item.get("created_at")]
        if timestamps:
            lock_handle = None
            try:
                if self.path is not None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    lock_handle = self.path.with_suffix(self.path.suffix + ".lock").open("a+")
                    fcntl.flock(lock_handle, fcntl.LOCK_EX)
                    if self.path.exists():
                        try:
                            disk = json.loads(self.path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError) as exc:
                            raise RuntimeError("Basecamp cursor file is malformed; preserving it") from exc
                        if isinstance(disk, dict):
                            for key, item in disk.items():
                                if isinstance(item, Mapping):
                                    self.values[str(key)] = dict(item)
                incoming_watermark = max(timestamps)
                prior = self.values.get(lane, {})
                watermark = max(str(prior.get("created_at") or ""), incoming_watermark)
                prior_ids = set(prior.get("ids") or []) if prior.get("created_at") == watermark else set()
                prior_ids.update(
                    str(item.get("id") or "")
                    for item in events
                    if str(item.get("created_at") or "") == watermark and item.get("id")
                )
                self.values[lane] = {"created_at": watermark, "ids": sorted(prior_ids)}
                self._save()
            finally:
                if lock_handle is not None:
                    fcntl.flock(lock_handle, fcntl.LOCK_UN)
                    lock_handle.close()

    def after(self, lane: str, events: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Return only events newer than the durable lane cursor."""
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            with lock_path.open("a+") as lock_handle:
                fcntl.flock(lock_handle, fcntl.LOCK_SH)
                try:
                    if self.path.exists():
                        try:
                            disk = json.loads(self.path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError) as exc:
                            raise RuntimeError("Basecamp cursor file is malformed; preserving it for recovery") from exc
                        if isinstance(disk, dict) and isinstance(disk.get(lane), Mapping):
                            self.values[lane] = dict(disk[lane])
                finally:
                    fcntl.flock(lock_handle, fcntl.LOCK_UN)
        state = self.values.get(lane)
        if not state or not state.get("created_at"):
            return events
        cursor = str(state["created_at"])
        seen_ids = set(state.get("ids") or [])
        return [
            item
            for item in events
            if not item.get("created_at")
            or str(item.get("created_at")) > cursor
            or (str(item.get("created_at")) == cursor and str(item.get("id") or "") not in seen_ids)
        ]

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        temp = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(self.values, separators=(",", ":")), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)


class PollClient(Protocol):
    async def campfires(self, *, max_items: int | None = None) -> list[Mapping[str, Any]]: ...

    async def campfire_lines(
        self, *, project_id: str, campfire_id: str, max_items: int | None = None
    ) -> list[Mapping[str, Any]]: ...

    async def notifications(self) -> list[Mapping[str, Any]]: ...

    async def assignments(self) -> list[Mapping[str, Any]]: ...

    async def timeline(self, *, limit_per_project: int | None = None) -> list[Mapping[str, Any]]: ...


class CompositePoller:
    """Poll each Basecamp surface at its own bounded cadence."""

    MAX_ITEMS_PER_LANE = 500

    def __init__(
        self,
        client: PollClient,
        *,
        chat_seconds: int = 15,
        notification_seconds: int = 45,
        assignment_seconds: int = 30,
        reconciliation_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
        health: RuntimeHealth | None = None,
        cursors: CursorStore | None = None,
        inbox: DurableInbox | None = None,
    ) -> None:
        self.client = client
        self.chat_seconds = max(10, chat_seconds)
        self.notification_seconds = max(15, notification_seconds)
        self.assignment_seconds = max(15, assignment_seconds)
        self.reconciliation_seconds = max(60, reconciliation_seconds)
        self.clock = clock
        self.health = health or RuntimeHealth()
        self.cursors = cursors or CursorStore()
        self.inbox = inbox
        self._campfires: dict[str, str] = {}
        self._next_chat = 0.0
        self._next_notification = 0.0
        self._next_assignment = 0.0
        self._next_reconciliation = 0.0
        self._lane_successes: set[str] = set()
        self._lane_failures: set[str] = set()

    async def collect(self) -> list[Mapping[str, Any]]:
        self._lane_successes.clear()
        self._lane_failures.clear()
        now = self.clock()
        do_reconciliation = now >= self._next_reconciliation
        do_chat = now >= self._next_chat
        do_notifications = now >= self._next_notification
        do_assignments = now >= self._next_assignment

        if do_reconciliation or (do_chat and not self._campfires):
            await self._isolated("campfire_discovery", self._refresh_campfires())

        tasks: list[tuple[str, Any]] = []
        if do_chat:
            tasks.append(("chat", self._collect_chat()))
            self._next_chat = now + self.chat_seconds
        if do_notifications and hasattr(self.client, "notifications"):
            tasks.append(("notifications", self.client.notifications()))
            self._next_notification = now + self.notification_seconds
        if do_assignments and hasattr(self.client, "assignments"):
            tasks.append(("assignments", self.client.assignments()))
            self._next_assignment = now + self.assignment_seconds
        if do_reconciliation and hasattr(self.client, "timeline"):
            tasks.append(("reconciliation", self.client.timeline(limit_per_project=self.MAX_ITEMS_PER_LANE)))
            self._next_reconciliation = now + self.reconciliation_seconds

        batches = await asyncio.gather(*(self._isolated(name, task) for name, task in tasks)) if tasks else []
        due_names = {name for name, _ in tasks}
        if due_names and not (due_names & self._lane_successes):
            raise RuntimeError("All due Basecamp polling lanes failed")
        unique: dict[str, Mapping[str, Any]] = {}
        for (lane, _), batch in zip(tasks, batches, strict=True):
            if self.inbox is not None and lane == "assignments":
                self.inbox.accept_snapshot_batch("poll", "assignments", batch)
                continue
            streams: dict[str, list[Mapping[str, Any]]] = {}
            for event in batch:
                stream_id = str(event.get("_stream_id") or lane)
                streams.setdefault(stream_id, []).append(event)
            for stream_id, stream_batch in streams.items():
                if self.inbox is not None:
                    fresh = self.inbox.after_cursor(stream_id, stream_batch)
                    self.inbox.accept_batch("poll", stream_id, fresh)
                else:
                    fresh = self.cursors.after(stream_id, stream_batch)
                    self.cursors.update(stream_id, fresh)
                    for event in fresh:
                        event_id = str(event.get("id") or "")
                        if event_id:
                            unique[event_id] = event
        return sorted(unique.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)

    async def _isolated(self, lane: str, awaitable: Any) -> Any:
        circuit = self.health.lane(lane)
        if circuit.open:
            if hasattr(awaitable, "close"):
                awaitable.close()
            return [] if lane != "campfire_discovery" else None
        try:
            result = await awaitable
        except Exception as exc:  # noqa: BLE001 - one failed lane must not suppress the others
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            if status in {401, 403}:
                self.health.revoked = status == 401
            circuit.failure()
            self._lane_failures.add(lane)
            self.health.mark(f"{lane}_failures")
            return [] if lane != "campfire_discovery" else None
        circuit.success()
        self._lane_successes.add(lane)
        self.health.lane_succeeded(lane)
        self.health.mark(f"{lane}_successes")
        return result

    async def _refresh_campfires(self) -> None:
        discovered: dict[str, str] = {}
        for campfire in await self.client.campfires(max_items=self.MAX_ITEMS_PER_LANE):
            bucket = campfire.get("bucket") or {}
            project_id = str(bucket.get("id") or campfire.get("bucket_id") or "") if isinstance(bucket, Mapping) else ""
            campfire_id = str(campfire.get("id") or "")
            if project_id.isdigit() and campfire_id.isdigit():
                discovered[campfire_id] = project_id
        self._campfires = discovered

    async def _collect_chat(self) -> list[Mapping[str, Any]]:
        if not self._campfires:
            if "campfire_discovery" in self._lane_failures:
                raise RuntimeError("Basecamp Campfire discovery failed")
            return []
        lane_names = {f"chat:{project_id}:{campfire_id}" for campfire_id, project_id in self._campfires.items()}
        tasks = [
            self._isolated(
                f"chat:{project_id}:{campfire_id}",
                self.client.campfire_lines(
                    project_id=project_id,
                    campfire_id=campfire_id,
                    max_items=self.MAX_ITEMS_PER_LANE,
                ),
            )
            for campfire_id, project_id in self._campfires.items()
        ]
        batches = await asyncio.gather(*tasks)
        if not (lane_names & self._lane_successes):
            raise RuntimeError("All Basecamp Campfire lanes failed")
        return [event for batch in batches for event in batch]
