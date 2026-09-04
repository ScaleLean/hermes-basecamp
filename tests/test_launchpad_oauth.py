import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from basecamp.oauth import OAuthToken

from launchpad_oauth import AppCredentials, AuthorizationRequest, finish_authorization


class LaunchpadOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "agent.json"
        self.credentials = AppCredentials("client", "secret", "http://127.0.0.1:8976/callback")
        self.request = AuthorizationRequest(
            "https://launchpad.37signals.com/authorization/new",
            "expected-state",
            "https://launchpad.37signals.com/authorization/token",
            self.credentials,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_state_mismatch_fails_before_exchange(self):
        with (
            patch("launchpad_oauth.exchange_code") as exchange,
            self.assertRaisesRegex(RuntimeError, "state mismatch"),
        ):
            finish_authorization(
                self.request,
                "http://127.0.0.1:8976/callback?code=abc&state=wrong",
                account_id="1234567",
                person_id="7654321",
                email="agent@example.com",
                output_path=self.output,
            )
        exchange.assert_not_called()
        self.assertFalse(self.output.exists())

    @patch("launchpad_oauth._verify_basecamp_identity")
    @patch("launchpad_oauth._verify_launchpad_identity")
    @patch("launchpad_oauth.exchange_code")
    def test_verified_legacy_token_is_saved_owner_only(self, exchange, verify_launchpad, verify_basecamp):
        exchange.return_value = OAuthToken(
            access_token="access",
            refresh_token="refresh",
            expires_in=3600,
        )
        stored = finish_authorization(
            self.request,
            "http://127.0.0.1:8976/callback?code=abc&state=expected-state",
            account_id="1234567",
            person_id="7654321",
            email="agent@example.com",
            output_path=self.output,
        )
        self.assertTrue(stored.legacy_format)
        self.assertEqual(stored.client_secret, "secret")
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        verify_launchpad.assert_called_once()
        verify_basecamp.assert_called_once()
