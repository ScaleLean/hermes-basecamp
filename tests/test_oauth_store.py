import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from basecamp.oauth import OAuthToken

from oauth_store import OAuthTokenStore, ResourceOAuthTokenProvider, StoredOAuthToken


class OAuthTokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "oauth.json"
        self.store = OAuthTokenStore(self.path)
        self.token = StoredOAuthToken(
            access_token="access",
            refresh_token="refresh",
            expires_at=123.0,
            resource="urn:bc:account:1234567",
            token_endpoint="https://app.basecamp.com/oauth/token",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_is_owner_only(self):
        self.store.save(self.token)
        self.assertEqual(self.store.load(), self.token)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_rejects_group_readable_token_file(self):
        self.store.save(self.token)
        os.chmod(self.path, 0o640)
        with self.assertRaises(PermissionError):
            self.store.load()

    def test_corrupt_token_is_preserved_with_recovery_diagnostic(self):
        self.path.write_text("not json")
        os.chmod(self.path, 0o600)
        with self.assertRaisesRegex(RuntimeError, "preserving it for recovery"):
            self.store.load()
        self.assertEqual(self.path.read_text(), "not json")

    def test_refresh_preserves_legacy_client_and_rotates_token(self):
        legacy = StoredOAuthToken(
            access_token="old-access",
            refresh_token="old-refresh",
            expires_at=0,
            resource=None,
            token_endpoint="https://launchpad.37signals.com/authorization/token",
            client_id="client",
            client_secret="secret",
            legacy_format=True,
        )
        self.store.save(legacy)
        provider = ResourceOAuthTokenProvider(self.store)
        refreshed = OAuthToken(
            access_token="new-access",
            refresh_token="new-refresh",
            token_type="Bearer",
            expires_in=3600,
            scope="full",
        )

        with patch("oauth_store.refresh_token", return_value=refreshed) as refresh:
            self.assertEqual(asyncio.run(provider.access_token()), "new-access")

        refresh.assert_called_once_with(
            legacy.token_endpoint,
            "old-refresh",
            client_id="client",
            client_secret="secret",
            use_legacy_format=True,
            resource=None,
        )
        self.assertEqual(self.store.load().access_token, "new-access")

    def test_second_provider_reuses_refresh_saved_by_first_provider(self):
        expired = StoredOAuthToken(
            access_token="old", refresh_token="refresh", expires_at=0,
            resource="urn:bc:account:1", token_endpoint="https://example.test/token",
        )
        self.store.save(expired)
        first = ResourceOAuthTokenProvider(self.store)
        second = ResourceOAuthTokenProvider(self.store)
        refreshed = OAuthToken(
            access_token="new", refresh_token="rotated", token_type="Bearer", expires_in=3600,
        )
        with patch("oauth_store.refresh_token", return_value=refreshed) as refresh:
            self.assertEqual(asyncio.run(first.access_token()), "new")
            self.assertEqual(asyncio.run(second.access_token()), "new")
        refresh.assert_called_once()
