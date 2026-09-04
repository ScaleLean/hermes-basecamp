import unittest

from sdk_client import ExpectedIdentity
from webhook_reconciliation import WebhookReconciler


class _Client:
    expected = ExpectedIdentity("1", "2", "agent@example.com")
    project_ids = ("10", "11")

    def __init__(self, existing=None):
        self.existing = existing or {}
        self.calls = []
        self.next_id = 100

    async def attest_full_member(self):
        return {"id": 2, "employee": True, "client": False}

    async def call(self, service, method, arguments):
        self.calls.append((service, method, dict(arguments)))
        if method == "list":
            return list(self.existing.get(str(arguments["bucket_id"]), []))
        if method == "create":
            project_id = str(arguments["bucket_id"])
            self.next_id += 1
            item = {
                "id": self.next_id,
                "bucket": {"id": int(project_id)},
                "payload_url": arguments["payload_url"],
                "types": arguments["types"],
                "active": True,
            }
            self.existing[project_id] = [item]
            return item
        if method == "update":
            for values in self.existing.values():
                for item in values:
                    if item["id"] == arguments["webhook_id"]:
                        item.update(arguments)
                        return item
        if method == "get":
            for values in self.existing.values():
                for item in values:
                    if item["id"] == arguments["webhook_id"]:
                        return {key: value for key, value in item.items() if key != "bucket"}
        raise AssertionError((service, method, arguments))


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_registration_requires_approval(self):
        reconciler = WebhookReconciler(
            _Client(), payload_url="https://example.com/basecamp/webhooks/token", event_types=("Todo",)
        )
        with self.assertRaises(PermissionError):
            await reconciler.reconcile(approved=False)

    async def test_creates_each_project_and_verifies_readback(self):
        client = _Client()
        reconciler = WebhookReconciler(
            client,
            payload_url="https://example.com/basecamp/webhooks/token",
            event_types=("Todo", "Message"),
        )
        results = await reconciler.reconcile(approved=True)
        self.assertEqual([result.action for result in results], ["created", "created"])
        self.assertTrue(all(result.verified for result in results))

    async def test_matching_registration_is_read_only(self):
        item = {
            "id": 5,
            "bucket": {"id": 10},
            "payload_url": "https://example.com/basecamp/webhooks/token",
            "types": ["Todo"],
            "active": True,
        }
        client = _Client({"10": [item]})
        client.project_ids = ("10",)
        result = await WebhookReconciler(client, payload_url=item["payload_url"], event_types=("Todo",)).reconcile(
            approved=False
        )
        self.assertEqual(result[0].action, "unchanged")
        self.assertFalse(any(call[1] in {"create", "update"} for call in client.calls))

    def test_rejects_types_without_project_bound_canonical_getter(self):
        for recording_type in ("all", "Client::Approval::Response", "Client::Reply", "Unknown"):
            with self.subTest(recording_type=recording_type), self.assertRaisesRegex(ValueError, "Unsafe"):
                WebhookReconciler(
                    _Client(),
                    payload_url="https://example.com/basecamp/webhooks/token",
                    event_types=(recording_type,),
                )
