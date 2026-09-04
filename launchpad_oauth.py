"""Legacy Launchpad OAuth for Basecamp accounts without BC5 device flow."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from basecamp import Client
from basecamp.oauth import build_authorization_url, discover_launchpad, exchange_code, generate_state

from oauth_store import OAuthTokenStore, StoredOAuthToken

AUTHORIZATION_URL = "https://launchpad.37signals.com/authorization.json"
USER_AGENT = "Hermes-Basecamp (+https://github.com/ScaleLean/hermes-basecamp)"


@dataclass(frozen=True)
class AppCredentials:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    state: str
    token_endpoint: str
    credentials: AppCredentials


def load_app_credentials(path: Path) -> AppCredentials:
    resolved = path.expanduser().resolve()
    if stat.S_IMODE(resolved.stat().st_mode) & 0o077:
        raise PermissionError(f"OAuth app credential file must be mode 0600: {resolved}")
    return AppCredentials(**json.loads(resolved.read_text(encoding="utf-8")))


def begin_authorization(credentials: AppCredentials) -> AuthorizationRequest:
    config = discover_launchpad()
    if not config.authorization_endpoint:
        raise RuntimeError("Launchpad did not advertise an authorization endpoint")
    state = generate_state()
    url = build_authorization_url(
        config.authorization_endpoint,
        credentials.client_id,
        credentials.redirect_uri,
        state,
    )
    return AuthorizationRequest(url, state, config.token_endpoint, credentials)


def finish_authorization(
    request: AuthorizationRequest,
    callback_url: str,
    *,
    account_id: str,
    person_id: str,
    email: str,
    output_path: Path,
) -> StoredOAuthToken:
    query = parse_qs(urlparse(callback_url).query)
    returned_state = (query.get("state") or [""])[0]
    if returned_state != request.state:
        raise RuntimeError("Basecamp OAuth state mismatch")
    if query.get("error"):
        raise RuntimeError(f"Basecamp OAuth was denied: {query['error'][0]}")
    code = (query.get("code") or [""])[0]
    if not code:
        raise RuntimeError("Basecamp OAuth callback did not contain a code")

    token = exchange_code(
        request.token_endpoint,
        code,
        request.credentials.redirect_uri,
        request.credentials.client_id,
        client_secret=request.credentials.client_secret,
        use_legacy_format=True,
    )
    _verify_launchpad_identity(token.access_token, account_id=account_id, email=email)
    _verify_basecamp_identity(
        token.access_token,
        account_id=account_id,
        person_id=person_id,
        email=email,
    )
    if not token.refresh_token:
        raise RuntimeError("Basecamp OAuth grant did not return a refresh token")

    stored = StoredOAuthToken(
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        expires_at=token.expires_at,
        resource=None,
        token_endpoint=request.token_endpoint,
        client_id=request.credentials.client_id,
        client_secret=request.credentials.client_secret,
        legacy_format=True,
        scope=token.scope,
    )
    OAuthTokenStore(output_path).save(stored)
    return stored


def _verify_launchpad_identity(access_token: str, *, account_id: str, email: str) -> None:
    response = httpx.get(
        AUTHORIZATION_URL,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    actual_email = str((payload.get("identity") or {}).get("email_address") or "").lower()
    account_ids = {str(item.get("id")) for item in payload.get("accounts", []) if item.get("product") == "bc3"}
    if actual_email != email.lower() or account_id not in account_ids:
        raise RuntimeError("Launchpad identity or Basecamp account mismatch")


def _verify_basecamp_identity(
    access_token: str,
    *,
    account_id: str,
    person_id: str,
    email: str,
) -> None:
    with Client(access_token=access_token, user_agent=USER_AGENT) as client:
        profile = client.for_account(account_id).people.my_profile()
    actual_id = str(profile.get("id") or "")
    actual_email = str(profile.get("email_address") or "").lower()
    if actual_id != person_id or actual_email != email.lower():
        raise RuntimeError("Basecamp member identity mismatch")
