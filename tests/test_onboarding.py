import tempfile
import unittest
from pathlib import Path

from oauth_store import OAuthTokenStore, StoredOAuthToken
from onboarding import revoke_local_token


class OnboardingTests(unittest.TestCase):
    def test_revoke_rejects_non_token_file_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "unrelated.json"
            path.write_text("{}")
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "not a valid Basecamp OAuth token"):
                revoke_local_token(path, approved=True)
            self.assertTrue(path.exists())

    def test_revoke_removes_schema_valid_owner_only_token(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "oauth.json"
            OAuthTokenStore(path).save(
                StoredOAuthToken(
                    access_token="access",
                    refresh_token="refresh",
                    expires_at=None,
                    resource="urn:bc:account:1",
                    token_endpoint="https://example.test/token",
                )
            )
            self.assertTrue(revoke_local_token(path, approved=True))
            self.assertFalse(path.exists())
