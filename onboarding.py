"""Setup, doctor, and local revoke workflows for one Hermes Basecamp profile."""

from __future__ import annotations

import os
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
