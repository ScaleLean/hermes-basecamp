import json
import unittest
from unittest.mock import AsyncMock, patch

from basecamp_cli import BasecampCLI, ExpectedIdentity, IdentityMismatchError, _parser


class _Process:
    def __init__(self, payload, returncode=0):
        self.returncode = returncode
        self._payload = payload

    async def communicate(self):
        return json.dumps(self._payload).encode(), b""


class BasecampCLITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = BasecampCLI(
            profile="agent",
            expected=ExpectedIdentity("1234567", "123", "agent@example.com"),
        )

    def test_command_is_explicit_and_shell_free(self):
        self.assertEqual(
            self.client.command(["me"]),
            ["basecamp", "--profile", "agent", "--account", "1234567", "--json", "me"],
        )

    def test_operator_command_grammar(self):
        self.assertTrue(_parser().parse_args(["doctor", "--probe"]).probe)
        self.assertEqual(
            _parser().parse_args(["webhooks", "sync", "--public-url", "https://example.test/hook"]).webhook_command,
            "sync",
        )
        self.assertEqual(_parser().parse_args(["journal", "list"]).journal_command, "list")
        self.assertEqual(
            _parser().parse_args(["test", "live", "--campfire-id", "1", "--todolist-id", "2"]).test_command,
            "live",
        )

    async def test_matching_identity_passes(self):
        process = _Process({"ok": True, "data": {"id": 123, "email_address": "agent@example.com"}})
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            data = await self.client.verify_identity()
        self.assertEqual(data["id"], 123)

    async def test_wrong_person_fails_closed(self):
        process = _Process({"ok": True, "data": {"id": 999, "email_address": "agent@example.com"}})
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            self.assertRaises(IdentityMismatchError),
        ):
            await self.client.verify_identity()

    async def test_wrong_email_fails_closed(self):
        process = _Process({"ok": True, "data": {"id": 123, "email_address": "human@example.com"}})
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            self.assertRaises(IdentityMismatchError),
        ):
            await self.client.verify_identity()

    async def test_write_readback_requires_expected_creator(self):
        process = _Process({"ok": True, "data": {"id": 77, "creator": {"id": 123}}})
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            data = await self.client.verify_authorship("77")
        self.assertEqual(data["creator"]["id"], 123)

    async def test_write_readback_rejects_human_creator(self):
        process = _Process({"ok": True, "data": {"id": 77, "creator": {"id": 999}}})
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            self.assertRaises(IdentityMismatchError),
        ):
            await self.client.verify_authorship("77")
