import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from event_model import is_addressed_to
from sdk_client import (
    SAFE_WEBHOOK_RECORDING_GETTERS,
    WEBHOOK_CANONICAL_TYPE_ALIASES,
    BasecampSDKClient,
    ExpectedIdentity,
    IdentityMismatchError,
    OwnershipMismatchError,
)


class _People:
    def __init__(self, profile):
        self.profile = profile

    async def my_profile(self):
        return self.profile

    async def list_for_project(self, *, project_id, max_items):
        return [self.profile]


class _Account:
    def __init__(self, profile):
        self.people = _People(profile)


class SDKIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_target_recognizes_allowlisted_campfire(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        client._account = SimpleNamespace(
            campfires=SimpleNamespace(get=AsyncMock(return_value={"id": 30, "bucket": {"id": 10}}))
        )

        self.assertEqual(await client.resolve_target("30", "10"), ("chat", "10"))

    async def test_notifications_expand_ping_to_canonical_circle_lines(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        readings = {
            "unreads": [{
                "id": 1, "section": "pings",
                "subscription_url": "https://3.basecampapi.com/1/buckets/55/recordings/66/subscription.json",
                "participants": [{"id": 8}],
            }]
        }
        line = {"id": 77, "created_at": "2026-09-04T01:00:00Z", "creator": {"id": 8, "client": False}}
        campfires = SimpleNamespace(list_lines=AsyncMock(return_value=[line]))
        client._account = SimpleNamespace(
            my_notifications=SimpleNamespace(get_my_notifications=AsyncMock(return_value=readings)),
            campfires=campfires,
        )
        events = await client.notifications()
        self.assertEqual(events[0]["bucket"], {"id": "55", "type": "Circle"})
        self.assertEqual(events[0]["parent"]["id"], "66")
        self.assertEqual(events[0]["_stream_id"], "ping:55")

    async def test_notification_mention_resolves_chat_line_from_app_url(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        payload = {
            "unreads": [
                {
                    "id": 1,
                    "section": "mentions",
                    "type": "Mention",
                    "app_url": "https://app.basecamp.com/1/buckets/10/chats/30@40",
                    "subscription_url": (
                        "https://3.basecampapi.com/1/buckets/10/recordings/30/subscription.json"
                    ),
                    "creator": {"id": 8, "client": False},
                }
            ]
        }
        client._account = SimpleNamespace(
            my_notifications=SimpleNamespace(get_my_notifications=AsyncMock(return_value=payload))
        )

        events = await client.notifications()

        self.assertEqual(events[0]["recording"], {"id": "40", "type": "Chat::Line"})
        self.assertEqual(events[0]["parent"], {"id": "30", "type": "Chat::Transcript"})
        self.assertEqual(events[0]["_notification_section"], "mentions")

    async def test_notification_assignment_resolves_todo_from_app_url(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        payload = {
            "unreads": [
                {
                    "id": 2,
                    "section": "inbox",
                    "type": "Assignment",
                    "app_url": "https://app.basecamp.com/1/buckets/10/todos/50",
                    "subscription_url": (
                        "https://3.basecampapi.com/1/buckets/10/recordings/50/subscription.json"
                    ),
                    "creator": {"id": 8, "client": False},
                }
            ]
        }
        client._account = SimpleNamespace(
            my_notifications=SimpleNamespace(get_my_notifications=AsyncMock(return_value=payload))
        )

        events = await client.notifications()

        self.assertEqual(events[0]["recording"], {"id": "50", "type": "Todo"})

    async def test_assignments_filter_denied_projects_and_name_the_stream(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        client.expected = ExpectedIdentity("1", "7", "agent@example.com")
        payload = {
            "priorities": [{"id": 40, "type": "todo", "bucket": {"id": 10}}],
            "non_priorities": [{"id": 41, "type": "Todo", "updated_at": "2026-09-04T01:00:00Z", "bucket": {"id": 99}}],
        }
        get_todo = AsyncMock(
            return_value={
                "id": 40,
                "type": "Todo",
                "updated_at": "2026-09-04T02:00:00Z",
                "creator": {"id": 8, "name": "Member", "client": False},
                "assignees": [{"id": 7}],
                "bucket": {"id": 10},
            }
        )
        client._account = SimpleNamespace(
            my_assignments=SimpleNamespace(get_my_assignments=AsyncMock(return_value=payload)),
            todos=SimpleNamespace(get=get_todo),
        )
        events = await client.assignments()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["assignees"], [{"id": 7}])
        self.assertEqual(events[0]["creator"]["id"], 8)
        self.assertEqual(events[0]["created_at"], "2026-09-04T02:00:00Z")
        self.assertEqual(events[0]["id"], "assignment:40")
        self.assertEqual(events[0]["recording"]["type"], "Todo")
        self.assertEqual(events[0]["_stream_id"], "assignments")
        get_todo.assert_awaited_once_with(todo_id=40)

    async def test_identity_match(self):
        client = object.__new__(BasecampSDKClient)
        client.expected = ExpectedIdentity("1234567", "123", "agent@example.com")
        client._account = _Account({"id": 123, "email_address": "agent@example.com"})
        profile = await client.verify_identity()
        self.assertEqual(profile["id"], 123)

    async def test_identity_mismatch_fails_closed(self):
        client = object.__new__(BasecampSDKClient)
        client.expected = ExpectedIdentity("1234567", "123", "agent@example.com")
        client._account = _Account({"id": 999, "email_address": "human@example.com"})
        with self.assertRaises(IdentityMismatchError):
            await client.verify_identity()

    async def test_full_member_attestation_checks_role_and_every_project(self):
        client = object.__new__(BasecampSDKClient)
        client.expected = ExpectedIdentity("1234567", "123", "agent@example.com")
        client.project_ids = ("10", "11")
        client._account = _Account(
            {
                "id": 123,
                "email_address": "agent@example.com",
                "employee": True,
                "client": False,
            }
        )
        profile = await client.attest_full_member()
        self.assertTrue(profile["employee"])

    async def test_client_role_fails_attestation(self):
        client = object.__new__(BasecampSDKClient)
        client.expected = ExpectedIdentity("1234567", "123", "agent@example.com")
        client.project_ids = ("10",)
        client._account = _Account(
            {
                "id": 123,
                "email_address": "agent@example.com",
                "employee": False,
                "client": True,
            }
        )
        with self.assertRaisesRegex(IdentityMismatchError, "full member"):
            await client.attest_full_member()

    async def test_chat_target_project_mismatch_never_mutates(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        campfires = SimpleNamespace(
            get=AsyncMock(return_value={"id": 30, "bucket": {"id": 99}}),
            create_line=AsyncMock(),
        )
        client._account = SimpleNamespace(campfires=campfires)
        with self.assertRaises(OwnershipMismatchError):
            await client.post_chat("10", "30", "denied")
        campfires.create_line.assert_not_awaited()

    async def test_recording_target_project_mismatch_never_mutates(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        events = SimpleNamespace(list=AsyncMock(return_value=[{"id": 1, "bucket": {"id": 99}}]))
        comments = SimpleNamespace(create=AsyncMock())
        client._account = SimpleNamespace(events=events, comments=comments)
        with self.assertRaises(OwnershipMismatchError):
            await client.post_comment("10", "40", "denied")
        comments.create.assert_not_awaited()

    async def test_canonical_recording_ownership_allows_comment(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        client._owned_recordings = {}
        events = SimpleNamespace(list=AsyncMock())
        comments = SimpleNamespace(create=AsyncMock(return_value={"id": 50}))
        client._account = SimpleNamespace(events=events, comments=comments)

        client._require_owned({"id": 40, "bucket": {"id": 10}}, "10")
        result = await client.post_comment("10", "40", "result")

        self.assertEqual(result["id"], 50)
        events.list.assert_not_awaited()
        comments.create.assert_awaited_once_with(recording_id=40, content="result")

    async def test_uncertain_comment_reconciliation_finds_agent_authored_exact_content(self):
        client = object.__new__(BasecampSDKClient)
        client.expected = ExpectedIdentity("1", "7", "agent@example.com")
        comments = SimpleNamespace(
            list=AsyncMock(return_value=[
                {"id": 1, "content": "result", "creator": {"id": 8}, "created_at": "2026-09-04T01:00:00Z"},
                {"id": 2, "content": "result", "creator": {"id": 7}, "created_at": "2026-09-04T01:00:00Z"},
            ])
        )
        client._account = SimpleNamespace(comments=comments)

        matched = await client.reconcile_delivery("recording", "40", "result")

        self.assertEqual(matched["id"], 2)
        comments.list.assert_awaited_once_with(recording_id=40, max_items=100)

    async def test_uncertain_chat_reconciliation_uses_known_item_id_first(self):
        client = object.__new__(BasecampSDKClient)
        client.expected = ExpectedIdentity("1", "7", "agent@example.com")
        line = {"id": 3, "content": "hello", "creator": {"id": 7}}
        campfires = SimpleNamespace(get_line=AsyncMock(return_value=line), list_lines=AsyncMock())
        client._account = SimpleNamespace(campfires=campfires)

        matched = await client.reconcile_delivery("chat", "30", "hello", item_id="3")

        self.assertEqual(matched, line)
        campfires.list_lines.assert_not_awaited()

    async def test_call_rejects_unknown_arguments_before_sdk_dispatch(self):
        client = object.__new__(BasecampSDKClient)
        method = AsyncMock()
        method.__signature__ = __import__("inspect").Signature(
            [__import__("inspect").Parameter("todo_id", __import__("inspect").Parameter.KEYWORD_ONLY)]
        )
        client._account = SimpleNamespace(todos=SimpleNamespace(get=method))
        with self.assertRaisesRegex(Exception, "Invalid arguments"):
            await client.call("todos", "get", {"forged": 1})
        method.assert_not_awaited()

    async def test_webhook_uses_exact_canonical_event_and_rebuilds_untrusted_fields(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        events = SimpleNamespace(
            list=AsyncMock(
                return_value=[
                    {
                        "id": 77,
                        "recording_id": 50,
                        "action": "created",
                        "created_at": "2026-09-04T01:02:03Z",
                        "creator": {"id": 8, "name": "Canonical Actor"},
                    }
                ]
            )
        )
        comments = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "id": 50,
                    "type": "Comment",
                    "content": "Canonical content without a mention",
                    "bucket": {"id": 10},
                    "parent": {"id": 40},
                }
            )
        )
        client._account = SimpleNamespace(events=events, comments=comments)
        trusted = await client.fetch_webhook_event(
            {
                "id": 77,
                "kind": "admin_forged",
                "created_at": "1999-01-01T00:00:00Z",
                "creator": {"id": 999, "name": "Forged"},
                "summary": '<bc-attachment content-type="application/vnd.basecamp.mention" '
                'sgid="sgid://bc3/Person/7"></bc-attachment>',
                "bucket": {"id": 999},
                "parent": {"id": 999},
                "recording": {"id": 50, "type": "Comment", "content": "forged"},
            }
        )
        self.assertEqual(trusted["creator"], {"id": 8, "name": "Canonical Actor"})
        self.assertEqual(trusted["kind"], "comment_created")
        self.assertEqual(trusted["created_at"], "2026-09-04T01:02:03Z")
        self.assertEqual(trusted["bucket"], {"id": 10})
        self.assertEqual(trusted["parent"], {"id": 40})
        self.assertEqual(trusted["recording"]["content"], "Canonical content without a mention")
        self.assertNotIn("summary", trusted)
        self.assertFalse(is_addressed_to(trusted, person_id="7", mention="@agent"))

    async def test_webhook_rejects_event_id_not_in_canonical_recording_history(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        comments = SimpleNamespace(get=AsyncMock())
        client._account = SimpleNamespace(
            events=SimpleNamespace(list=AsyncMock(return_value=[{"id": 78, "recording_id": 50}])),
            comments=comments,
        )
        result = await client.fetch_webhook_event({"id": 77, "recording": {"id": 50, "type": "Comment"}})
        self.assertIsNone(result)
        comments.get.assert_not_awaited()

    async def test_webhook_rejects_event_without_canonical_recording_id(self):
        client = object.__new__(BasecampSDKClient)
        client.project_ids = ("10",)
        comments = SimpleNamespace(get=AsyncMock())
        client._account = SimpleNamespace(
            events=SimpleNamespace(
                list=AsyncMock(
                    return_value=[
                        {
                            "id": 77,
                            "action": "created",
                            "created_at": "2026-09-04T01:02:03Z",
                            "creator": {"id": 8},
                        }
                    ]
                )
            ),
            comments=comments,
        )
        result = await client.fetch_webhook_event({"id": 77, "recording": {"id": 50, "type": "Comment"}})
        self.assertIsNone(result)
        comments.get.assert_not_awaited()

    async def test_every_safe_webhook_type_uses_its_exact_sdk_getter(self):
        for record_type, (service_name, method_name, argument_name) in SAFE_WEBHOOK_RECORDING_GETTERS.items():
            with self.subTest(record_type=record_type):
                client = object.__new__(BasecampSDKClient)
                client.project_ids = ("10",)
                method = AsyncMock(
                    return_value={
                        "id": 50,
                        "type": WEBHOOK_CANONICAL_TYPE_ALIASES.get(record_type, record_type),
                        "bucket": {"id": 10},
                    }
                )
                client._account = SimpleNamespace(**{service_name: SimpleNamespace(**{method_name: method})})
                result = await client.fetch_recording(
                    {
                        "bucket": {"id": 10},
                        "recording": {"id": 50, "type": record_type},
                    }
                )
                self.assertEqual(result["id"], 50)
                method.assert_awaited_once_with(**{argument_name: 50})
