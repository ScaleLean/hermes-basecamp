import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from basecamp.oauth import OAuthToken
from basecamp.oauth.config import DiscoveryResult, OAuthConfig

from oauth_onboarding import authorize


class OAuthOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "oauth.json"
        self.config = OAuthConfig(
            issuer="https://app.basecamp.com",
            authorization_endpoint=None,
            token_endpoint="https://app.basecamp.com/oauth/token",
            device_authorization_endpoint="https://app.basecamp.com/oauth/device",
            grant_types_supported=["urn:ietf:params:oauth:grant-type:device_code"],
        )

    def tearDown(self):
        self.temp.cleanup()

    @patch("oauth_onboarding.perform_device_login")
    @patch("oauth_onboarding.discover_from_resource")
    @patch("oauth_onboarding.Client")
    def test_saves_identity_bound_full_scope_token(self, client_cls, discover, login):
        discover.return_value = DiscoveryResult(kind="selected", config=self.config, issuer=self.config.issuer)
        login.return_value = OAuthToken(
            access_token="access",
            refresh_token="refresh",
            expires_in=3600,
            scope="full",
            resource="urn:bc:account:1234567",
        )
        client_cls.return_value.__enter__.return_value.for_account.return_value.people.my_profile.return_value = {
            "id": 7654321,
            "email_address": "agent@example.com",
        }
        stored = authorize(
            account_id="1234567",
            person_id="7654321",
            email="agent@example.com",
            output_path=self.path,
            display=lambda auth: None,
        )
        self.assertEqual(stored.resource, "urn:bc:account:1234567")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        login.assert_called_once_with(self.config, "basecamp-cli", scope="full", display=unittest.mock.ANY)

    @patch("oauth_onboarding.perform_device_login")
    @patch("oauth_onboarding.discover_from_resource")
    def test_rejects_wrong_account_without_saving(self, discover, login):
        discover.return_value = DiscoveryResult(kind="selected", config=self.config, issuer=self.config.issuer)
        login.return_value = OAuthToken(
            access_token="access",
            refresh_token="refresh",
            scope="read write",
            resource="urn:bc:account:999",
        )
        with self.assertRaisesRegex(RuntimeError, "account mismatch"):
            authorize(
                account_id="1234567",
                person_id="7654321",
                email="agent@example.com",
                output_path=self.path,
                display=lambda auth: None,
            )
        self.assertFalse(self.path.exists())

    @patch("oauth_onboarding.perform_device_login")
    @patch("oauth_onboarding.discover_from_resource")
    def test_rejects_read_only_scope_without_saving(self, discover, login):
        discover.return_value = DiscoveryResult(kind="selected", config=self.config, issuer=self.config.issuer)
        login.return_value = OAuthToken(
            access_token="access",
            refresh_token="refresh",
            scope="read",
            resource="urn:bc:account:1234567",
        )
        with self.assertRaisesRegex(RuntimeError, "lacks required"):
            authorize(
                account_id="1234567",
                person_id="7654321",
                email="agent@example.com",
                output_path=self.path,
                display=lambda auth: None,
            )
        self.assertFalse(self.path.exists())

    @patch("oauth_onboarding.perform_device_login")
    @patch("oauth_onboarding.discover_from_resource")
    @patch("oauth_onboarding.Client")
    def test_rejects_wrong_member_without_saving(self, client_cls, discover, login):
        discover.return_value = DiscoveryResult(kind="selected", config=self.config, issuer=self.config.issuer)
        login.return_value = OAuthToken(
            access_token="access",
            refresh_token="refresh",
            scope="full",
            resource="urn:bc:account:1234567",
        )
        client_cls.return_value.__enter__.return_value.for_account.return_value.people.my_profile.return_value = {
            "id": 999,
            "email_address": "someone@example.com",
        }

        with self.assertRaisesRegex(RuntimeError, "member identity mismatch"):
            authorize(
                account_id="1234567",
                person_id="7654321",
                email="agent@example.com",
                output_path=self.path,
                display=lambda auth: None,
            )
        self.assertFalse(self.path.exists())
