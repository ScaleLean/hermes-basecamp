import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.secret_scope import reset_secret_scope, set_secret_scope

from adapter import register
from approval import ApprovalBroker, approval_rule_key, post_approval_response, pre_tool_call
from tools import register_tools


class ApprovalTests(unittest.TestCase):
    def test_plugin_registers_action_time_approval_hooks(self):
        class Context:
            def __init__(self):
                self.hooks = {}

            def register_platform(self, **kwargs):
                self.platform = kwargs

            def register_tool(self, **kwargs):
                pass

            def register_hook(self, name, callback):
                self.hooks[name] = callback

        context = Context()
        register(context)
        self.assertIs(context.hooks["pre_tool_call"], pre_tool_call)
        self.assertIn("post_approval_response", context.hooks)

    def test_model_cannot_supply_trigger_to_bypass_context(self):
        directive = pre_tool_call(
            "basecamp_todos",
            {
                "operation": "todos.complete",
                "project_id": "10",
                "arguments": {"todo_id": 1},
                "trigger": "direct_mention",
            },
        )
        self.assertEqual(directive["action"], "block")

    def test_sensitive_action_uses_exact_digest_and_one_time_grant(self):
        arguments = {"recording_id": 1}
        directive = pre_tool_call(
            "basecamp_extras",
            {"operation": "recordings.trash", "project_id": "10", "arguments": arguments},
        )
        self.assertEqual(directive["action"], "approve")
        broker = ApprovalBroker()
        broker.record([directive["rule_key"]], "once")
        self.assertIsNotNone(broker.consume("recordings.trash", "10", arguments))
        self.assertIsNone(broker.consume("recordings.trash", "10", arguments))

    def test_sensitive_arguments_on_ordinary_operation_use_approval_hook(self):
        arguments = {"todo_id": 1, "assignee_ids": [2]}
        directive = pre_tool_call(
            "basecamp_todos",
            {"operation": "todos.update", "project_id": "10", "arguments": arguments},
        )
        self.assertEqual(directive["action"], "approve")
        self.assertIn(approval_rule_key("todos.update", "10", arguments), directive["rule_key"])


class SensitiveToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_sensitive_tool_executes_only_after_exact_approval(self):
        class Context:
            def register_tool(self, **kwargs):
                if kwargs["name"] == "basecamp_projects_people":
                    self.handler = kwargs["handler"]

        class Client:
            expected = SimpleNamespace(account_id="1", person_id="2")
            project_ids = ("10",)

            async def attest_full_member(self):
                return {"id": 2, "employee": True, "client": False}

            async def call(self, service, method, arguments):
                if (service, method) == ("projects", "archive"):
                    self.mutated = True
                    return None
                if (service, method) == ("projects", "get"):
                    return {"id": 10, "status": "archived" if getattr(self, "mutated", False) else "active"}
                raise AssertionError((service, method, arguments))

            async def close(self):
                return None

        context = Context()
        client = Client()
        arguments = {"project_id": 10}
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                "os.environ",
                {
                    "BASECAMP_STATE_DIR": temp,
                    "BASECAMP_ACCOUNT_ID": "1",
                    "BASECAMP_PERSON_ID": "2",
                },
            ),
        ):
            register_tools(context, lambda: client, "1:2")
            directive = pre_tool_call(
                "basecamp_projects_people",
                {
                    "operation": "projects.archive",
                    "project_id": "10",
                    "arguments": arguments,
                    "idempotency_key": "archive-10",
                },
            )
            self.assertEqual(directive["action"], "approve")
            post_approval_response(pattern_keys=[directive["rule_key"]], choice="once")
            result = await context.handler("projects.archive", "10", arguments, idempotency_key="archive-10")

        self.assertTrue(client.mutated)
        self.assertTrue(result["verified"])

    async def test_registered_ordinary_tool_uses_trusted_session_context(self):
        from gateway import session_context

        class Context:
            def register_tool(self, **kwargs):
                if kwargs["name"] == "basecamp_todos":
                    self.handler = kwargs["handler"]

        class Client:
            expected = SimpleNamespace(account_id="1", person_id="2")
            project_ids = ("10",)

            async def attest_full_member(self):
                return {"id": 2, "employee": True, "client": False}

            async def call(self, service, method, arguments):
                if (service, method) == ("todos", "complete"):
                    self.mutated = True
                    return None
                if (service, method) == ("todos", "get"):
                    return {
                        "id": 40,
                        "bucket": {"id": 10},
                        "completed": getattr(self, "mutated", False),
                    }
                raise AssertionError((service, method, arguments))

            async def close(self):
                return None

        context = Context()
        client = Client()
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"BASECAMP_STATE_DIR": temp}):
            register_tools(context, lambda: client, "1:2")
            tokens = session_context.set_session_vars(platform="basecamp", chat_id="chat:10:30", cron_session="")
            try:
                result = await context.handler("todos.complete", "10", {"todo_id": 40}, idempotency_key="complete-40")
            finally:
                session_context.clear_session_vars(tokens)

        self.assertTrue(client.mutated)
        self.assertTrue(result["verified"])

    async def test_model_cannot_spoof_context_argument_on_registered_handler(self):
        class Context:
            def register_tool(self, **kwargs):
                if kwargs["name"] == "basecamp_todos":
                    self.handler = kwargs["handler"]

        context = Context()
        register_tools(context, lambda: None, "1:2")
        with self.assertRaises(TypeError):
            await context.handler(
                "todos.complete", "10", {"todo_id": 40}, idempotency_key="complete-40", trigger="direct_mention"
            )

    def test_digest_does_not_approve_changed_arguments(self):
        broker = ApprovalBroker()
        key = approval_rule_key("recordings.trash", "10", {"recording_id": 1})
        broker.record([key], "once")
        self.assertIsNone(broker.consume("recordings.trash", "10", {"recording_id": 2}))

    def test_approval_is_bound_to_task_local_basecamp_identity(self):
        arguments = {"recording_id": 1}
        first = set_secret_scope({"BASECAMP_ACCOUNT_ID": "1", "BASECAMP_PERSON_ID": "2"})
        try:
            first_key = pre_tool_call(
                "basecamp_extras",
                {"operation": "recordings.trash", "project_id": "10", "arguments": arguments},
            )["rule_key"]
        finally:
            reset_secret_scope(first)
        second = set_secret_scope({"BASECAMP_ACCOUNT_ID": "1", "BASECAMP_PERSON_ID": "3"})
        try:
            second_key = pre_tool_call(
                "basecamp_extras",
                {"operation": "recordings.trash", "project_id": "10", "arguments": arguments},
            )["rule_key"]
        finally:
            reset_secret_scope(second)

        self.assertNotEqual(first_key, second_key)

    def test_denied_approval_is_not_recorded(self):
        arguments = {"recording_id": 1}
        key = approval_rule_key("recordings.trash", "10", arguments)
        broker = ApprovalBroker()
        broker.record([key], "deny")
        self.assertIsNone(broker.consume("recordings.trash", "10", arguments))
