"""Interactive device authorization for one identity-locked Basecamp member."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from basecamp import Client
from basecamp.oauth import discover_from_resource, perform_device_login
from basecamp.oauth.device_authorization import DeviceAuthorization

from oauth_store import OAuthTokenStore, StoredOAuthToken

API_ORIGIN = "https://3.basecampapi.com"
EXPECTED_ISSUER = "https://app.basecamp.com"
DEVICE_CLIENT_ID = "basecamp-cli"
FULL_SCOPE = "full"


def authorize(
    *,
    account_id: str,
    person_id: str,
    email: str,
    output_path: Path,
    display: Callable[[DeviceAuthorization], object],
) -> StoredOAuthToken:
    """Authorize and persist a token only when its account resource matches."""
    if not account_id.isdigit() or not person_id.isdigit():
        raise ValueError("Basecamp account and person IDs must be numeric")
    if "@" not in email:
        raise ValueError("Basecamp member email is required")

    discovery = discover_from_resource(API_ORIGIN, expected_issuer=EXPECTED_ISSUER)
    if discovery.kind != "selected":
        raise RuntimeError(f"Basecamp 5 device authorization is unavailable: {discovery.reason}")
    config = discovery.selected_config()
    token = perform_device_login(config, DEVICE_CLIENT_ID, scope=FULL_SCOPE, display=display)

    expected_resource = f"urn:bc:account:{account_id}"
    if token.resource != expected_resource:
        raise RuntimeError(
            f"Basecamp OAuth account mismatch: expected {expected_resource}, got {token.resource or 'missing'}"
        )
    granted_scopes = set((token.scope or "").split())
    if "full" not in granted_scopes:
        raise RuntimeError(f"Basecamp OAuth grant lacks required full scope: {token.scope or 'missing'}")

    with Client(access_token=token.access_token, user_agent="Hermes-Basecamp/0.1") as client:
        profile = client.for_account(account_id).people.my_profile()
    actual_id = str(profile.get("id") or "")
    actual_email = str(profile.get("email_address") or "").lower()
    if actual_id != person_id or actual_email != email.lower():
        raise RuntimeError("Basecamp member identity mismatch")

    stored = StoredOAuthToken(
        access_token=token.access_token,
        refresh_token=token.refresh_token or "",
        expires_at=token.expires_at,
        resource=token.resource,
        token_endpoint=config.token_endpoint,
        client_id=DEVICE_CLIENT_ID,
        scope=token.scope,
    )
    if not stored.refresh_token:
        raise RuntimeError("Basecamp OAuth grant did not return a refresh token")
    OAuthTokenStore(output_path).save(stored)
    return stored
