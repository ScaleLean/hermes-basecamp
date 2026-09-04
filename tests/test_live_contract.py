import os
import unittest

import pytest


@pytest.mark.live
@unittest.skipUnless(os.getenv("BASECAMP_LIVE_TEST") == "1", "set BASECAMP_LIVE_TEST=1")
class LiveContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_role_memberships_and_project_read(self):
        """Read-only contract proof. Mutation canaries require a separate exact approval."""
        from adapter import _make_client, _settings

        values = _settings()
        client = _make_client(values)
        try:
            profile = await client.attest_full_member()
            self.assertEqual(str(profile["id"]), client.expected.person_id)
            for project_id in client.project_ids:
                project = await client.call("projects", "get", {"project_id": int(project_id)})
                self.assertEqual(str(project["id"]), project_id)
        finally:
            await client.close()
