"""Setup, doctor, and local revoke workflows for one Hermes Basecamp profile."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .oauth_store import OAuthTokenStore
except ImportError:  # pragma: no cover - installed flat-module wheel
    from oauth_store import OAuthTokenStore


@dataclass(frozen=True)
class DoctorReport:
    healthy: bool
    account_id: str
    person_id: str
    email: str
    project_ids: tuple[str, ...]
    problems: tuple[str, ...] = ()
    health: dict[str, Any] | None = None


ADMINLAND_PREREQUISITES = (
    "A Basecamp administrator creates or selects a dedicated agent email address.",
    "A Basecamp administrator invites that email as someone who works at the company.",
    "The invitation recipient accepts the invitation and creates the Basecamp login.",
    "A Basecamp administrator confirms employee=true, client=false and adds the member to every configured project.",
    "The agent member authorizes its own OAuth grant. Do not authorize a human member account.",
)


async def doctor(client: Any) -> DoctorReport:
    try:
        profile = await client.attest_full_member()
    except Exception as exc:  # noqa: BLE001 - doctor must report every runtime failure
        return DoctorReport(
            False,
            client.expected.account_id,
            client.expected.person_id,
            client.expected.email,
            tuple(client.project_ids),
            (str(exc),),
            {"attestation_ok": False, "identity_ok": False, "role_ok": False, "revoked": False},
        )
    return DoctorReport(
        True,
        client.expected.account_id,
        str(profile.get("id") or ""),
        str(profile.get("email_address") or ""),
        tuple(client.project_ids),
        health={"attestation_ok": True, "identity_ok": True, "role_ok": True, "revoked": False},
    )


async def probe_runtime(client: Any, inbox: Any) -> dict[str, Any]:
    """Probe each receive lane independently and return content-free health."""
    lanes: dict[str, dict[str, Any]] = {}

    async def run(name: str, operation: Any) -> Any:
        try:
            value = await operation
        except Exception as exc:  # noqa: BLE001 - each lane must report independently
            lanes[name] = {"ok": False, "error": type(exc).__name__}
            return None
        count = len(value) if isinstance(value, list) else 1
        lanes[name] = {"ok": True, "items": count}
        return value

    campfires = await run("campfire_discovery", client.campfires(max_items=500))
    chat_operations = []
    if isinstance(campfires, list):
        for room in campfires:
            bucket = room.get("bucket") or {} if isinstance(room, Mapping) else {}
            project_id = str(bucket.get("id") or "") if isinstance(bucket, Mapping) else ""
            room_id = str(room.get("id") or "") if isinstance(room, Mapping) else ""
            if project_id and room_id:
                chat_operations.append(
                    run(
                        f"campfire:{room_id}",
                        client.campfire_lines(project_id=project_id, campfire_id=room_id, max_items=1),
                    )
                )
    await asyncio.gather(
        run("pings", client.pings()),
        run("notifications", client.notifications()),
        run("assignments", client.assignments()),
        run("activity", client.timeline(limit_per_project=1)),
        *chat_operations,
    )
    required = {"campfire_discovery", "pings", "notifications", "assignments", "activity"}
    lane_ok = all(lanes.get(name, {}).get("ok") is True for name in required)
    webhook_state = "unconfigured"
    public_url = os.getenv("BASECAMP_WEBHOOK_PUBLIC_URL", "").strip()
    if public_url:
        webhook_state = "healthy"
        for project_id in client.project_ids:
            try:
                webhooks = await client.call(
                    "webhooks", "list", {"bucket_id": int(project_id), "max_items": 100}
                )
            except Exception:  # noqa: BLE001 - health output records state, not message content
                webhook_state = "unavailable"
                break
            matches = [
                item
                for item in webhooks
                if isinstance(item, Mapping)
                and str(item.get("payload_url") or "") == public_url
                and item.get("active") is True
            ]
            if len(matches) != 1:
                webhook_state = "drifted"
                break
    inbox_health = inbox.stats()
    state = (
        "ready"
        if lane_ok and webhook_state == "healthy" and inbox_health.get("poison", 0) == 0
        else "recovering"
    )
    return {
        "state": state,
        "lanes": lanes,
        "inbox": inbox_health,
        "webhook_registration": webhook_state,
    }


def revoke_local_token(path: Path, *, approved: bool) -> bool:
    """Remove only the named local credential after exact operator approval."""
    target = path.expanduser().resolve()
    if not approved:
        raise PermissionError("Basecamp local token revocation requires --yes")
    if not target.is_file():
        return False
    mode = target.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError("Refusing to remove an insecure Basecamp token file; correct permissions first")
    try:
        OAuthTokenStore(target).load()
    except Exception as exc:
        raise ValueError("Refusing to remove a file that is not a valid Basecamp OAuth token") from exc
    target.unlink()
    return not target.exists()


def secure_state_directory(path: Path) -> None:
    path.expanduser().mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.expanduser(), 0o700)
