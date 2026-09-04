import asyncio
import unittest
from contextlib import contextmanager

from gateway import session_context

from approval import pre_tool_call
from policy import TriggerClass
from trusted_context import derive_execution_context


@contextmanager
def _trusted_values(**values):
    tokens = []
    try:
        for name, value in values.items():
            variable = session_context._VAR_MAP[name]
            tokens.append((variable, variable.set(value)))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class TrustedContextTests(unittest.IsolatedAsyncioTestCase):
    def test_addressed_basecamp_session_derives_direct_mention(self):
        with _trusted_values(
            HERMES_CRON_SESSION="",
            HERMES_SESSION_PLATFORM="basecamp",
            HERMES_SESSION_CHAT_ID="bucket:10/recording:30",
        ):
            result = derive_execution_context("10", "1:2")
        self.assertIs(result.trigger, TriggerClass.DIRECT_MENTION)

    def test_pre_tool_hook_allows_ordinary_write_only_in_trusted_context(self):
        arguments = {
            "operation": "todos.complete",
            "project_id": "10",
            "arguments": {"todo_id": 40},
        }
        with _trusted_values(
            HERMES_CRON_SESSION="",
            HERMES_SESSION_PLATFORM="basecamp",
            HERMES_SESSION_CHAT_ID="bucket:10/recording:30",
        ):
            self.assertIsNone(pre_tool_call("basecamp_todos", arguments))
        with _trusted_values(
            HERMES_CRON_SESSION="",
            HERMES_SESSION_PLATFORM="basecamp",
            HERMES_SESSION_CHAT_ID="bucket:99/recording:30",
        ):
            self.assertEqual(pre_tool_call("basecamp_todos", arguments)["action"], "block")

    def test_wrong_platform_fails_closed(self):
        with (
            _trusted_values(
                HERMES_CRON_SESSION="",
                HERMES_SESSION_PLATFORM="slack",
                HERMES_SESSION_CHAT_ID="bucket:10/recording:30",
            ),
            self.assertRaisesRegex(PermissionError, "addressed Basecamp"),
        ):
            derive_execution_context("10", "1:2")

    def test_cross_project_session_target_fails_closed(self):
        with (
            _trusted_values(
                HERMES_CRON_SESSION="",
                HERMES_SESSION_PLATFORM="basecamp",
                HERMES_SESSION_CHAT_ID="bucket:99/recording:40",
            ),
            self.assertRaisesRegex(PermissionError, "another project"),
        ):
            derive_execution_context("10", "1:2")

    def test_cron_requires_basecamp_platform_and_exact_target(self):
        with _trusted_values(
            HERMES_CRON_SESSION="true",
            HERMES_CRON_AUTO_DELIVER_PLATFORM="basecamp",
            HERMES_CRON_AUTO_DELIVER_CHAT_ID="bucket:10/recording:40",
        ):
            result = derive_execution_context("10", "1:2")
        self.assertIs(result.trigger, TriggerClass.APPROVED_SCHEDULE)

        with (
            _trusted_values(
                HERMES_CRON_SESSION="1",
                HERMES_CRON_AUTO_DELIVER_PLATFORM="slack",
                HERMES_CRON_AUTO_DELIVER_CHAT_ID="bucket:10/recording:30",
            ),
            self.assertRaisesRegex(PermissionError, "auto-delivery"),
        ):
            derive_execution_context("10", "1:2")

        with (
            _trusted_values(
                HERMES_CRON_SESSION="1",
                HERMES_CRON_AUTO_DELIVER_PLATFORM="basecamp",
                HERMES_CRON_AUTO_DELIVER_CHAT_ID="bucket:99/recording:30",
            ),
            self.assertRaisesRegex(PermissionError, "another project"),
        ):
            derive_execution_context("10", "1:2")

    async def test_concurrent_tasks_keep_session_projects_isolated(self):
        ready = asyncio.Event()
        arrivals = 0
        lock = asyncio.Lock()

        async def worker(project_id: str):
            nonlocal arrivals
            with _trusted_values(
                HERMES_CRON_SESSION="",
                HERMES_SESSION_PLATFORM="basecamp",
                HERMES_SESSION_CHAT_ID=f"bucket:{project_id}/recording:30",
            ):
                async with lock:
                    arrivals += 1
                    if arrivals == 2:
                        ready.set()
                await ready.wait()
                await asyncio.sleep(0)
                return derive_execution_context(project_id, "1:2")

        first, second = await asyncio.gather(worker("10"), worker("11"))
        self.assertEqual((first.project_id, second.project_id), ("10", "11"))
