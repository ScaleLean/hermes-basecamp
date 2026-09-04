import tempfile
import unittest
from pathlib import Path

from poller import CompositePoller, CursorStore


class _Client:
    def __init__(self):
        self.calls = []

    async def campfires(self, *, max_items=200):
        self.calls.append("campfires")
        return [{"id": 30, "bucket": {"id": 20}}]

    async def campfire_lines(self, *, project_id, campfire_id, max_items=100):
        self.calls.append(f"chat:{project_id}:{campfire_id}")
        return [{"id": 1, "created_at": "2026-09-04T01:00:00Z"}]

    async def notifications(self):
        self.calls.append("notifications")
        return [{"id": 2, "created_at": "2026-09-04T02:00:00Z"}]

    async def assignments(self):
        self.calls.append("assignments")
        return [{"id": 4, "created_at": "2026-09-04T04:00:00Z"}]

    async def timeline(self, *, limit_per_project=100):
        self.calls.append("timeline")
        return [
            {"id": 2, "created_at": "2026-09-04T02:00:00Z"},
            {"id": 3, "created_at": "2026-09-04T03:00:00Z"},
        ]


class _FailingNotificationClient(_Client):
    async def notifications(self):
        self.calls.append("notifications")
        raise RuntimeError("poison lane")


class PollerTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_poll_collects_all_lanes_and_deduplicates(self):
        client = _Client()
        poller = CompositePoller(client, clock=lambda: 1000)
        events = await poller.collect()
        self.assertEqual([event["id"] for event in events], [4, 3, 2, 1])
        self.assertEqual(client.calls, ["campfires", "notifications", "assignments", "timeline", "chat:20:30"])

    async def test_respects_independent_lane_cadence(self):
        now = [1000.0]
        client = _Client()
        poller = CompositePoller(client, chat_seconds=15, notification_seconds=45, clock=lambda: now[0])
        await poller.collect()
        client.calls.clear()

        now[0] += 16
        await poller.collect()
        self.assertEqual(client.calls, ["chat:20:30"])

        client.calls.clear()
        now[0] += 30
        await poller.collect()
        self.assertEqual(client.calls, ["notifications", "assignments", "chat:20:30"])

    async def test_one_failed_lane_does_not_drop_other_lanes(self):
        client = _FailingNotificationClient()
        poller = CompositePoller(client, clock=lambda: 1000)
        events = await poller.collect()
        self.assertEqual([event["id"] for event in events], [4, 3, 2, 1])
        self.assertEqual(poller.health.counters["notifications_failures"], 1)

    async def test_cursor_filters_replayed_burst_larger_than_one_page(self):
        class BurstClient(_Client):
            async def timeline(self, *, limit_per_project=None):
                self.calls.append("timeline")
                self.asserted_limit = limit_per_project
                return [
                    {"id": 1000 + index, "created_at": f"2026-09-04T03:{index // 60:02d}:{index % 60:02d}Z"}
                    for index in range(125)
                ]

        client = BurstClient()
        cursors = CursorStore()
        poller = CompositePoller(client, clock=lambda: 1000, cursors=cursors)
        events = await poller.collect()
        self.assertEqual(len(events), 128)
        self.assertEqual(client.asserted_limit, 500)

        # A new poller simulates restart and receives the same complete pages.
        second = CompositePoller(client, clock=lambda: 2000, cursors=cursors)
        repeated = await second.collect()
        self.assertEqual(repeated, [])

        # A late event at the same timestamp is not lost behind the watermark.
        late = {"id": 9999, "created_at": "2026-09-04T03:02:04Z"}
        self.assertEqual(cursors.after("reconciliation", [late]), [late])

    async def test_history_fetches_use_bounded_per_cycle_limits(self):
        class LimitsClient(_Client):
            async def campfires(self, *, max_items=None):
                self.campfire_limit = max_items
                return [{"id": 30, "bucket": {"id": 20}}]

            async def campfire_lines(self, *, project_id, campfire_id, max_items=None):
                self.line_limit = max_items
                return []

            async def timeline(self, *, limit_per_project=None):
                self.timeline_limit = limit_per_project
                return []

        client = LimitsClient()
        await CompositePoller(client, clock=lambda: 1000).collect()
        self.assertEqual(
            (client.campfire_limit, client.line_limit, client.timeline_limit),
            (500, 500, 500),
        )

    async def test_all_due_lane_failures_surface_as_outage(self):
        class Failed(_Client):
            async def campfires(self, *, max_items=None):
                raise RuntimeError("offline")

            async def notifications(self):
                raise RuntimeError("offline")

            async def assignments(self):
                raise RuntimeError("offline")

            async def timeline(self, *, limit_per_project=None):
                raise RuntimeError("offline")

        with self.assertRaisesRegex(RuntimeError, "All due"):
            await CompositePoller(Failed(), clock=lambda: 1000).collect()

    async def test_each_campfire_uses_an_independent_durable_cursor(self):
        from inbox import DurableInbox

        class TwoCampfires(_Client):
            async def campfires(self, *, max_items=None):
                return [{"id": 30, "bucket": {"id": 20}}, {"id": 31, "bucket": {"id": 20}}]

            async def campfire_lines(self, *, project_id, campfire_id, max_items=None):
                return [{"id": 1, "created_at": "2026-09-04T01:00:00Z", "_stream_id": f"campfire:{project_id}:{campfire_id}"}]

        with tempfile.TemporaryDirectory() as temp:
            inbox = DurableInbox(Path(temp) / "inbox.sqlite3")
            await CompositePoller(TwoCampfires(), clock=lambda: 1000, inbox=inbox).collect()
            self.assertEqual(inbox.after_cursor("campfire:20:30", [{"id": 1, "created_at": "2026-09-04T01:00:00Z"}]), [])
            self.assertEqual(inbox.after_cursor("campfire:20:31", [{"id": 1, "created_at": "2026-09-04T01:00:00Z"}]), [])

    def test_cursor_never_regresses_and_reloads_other_process_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cursor.json"
            first = CursorStore(path)
            second = CursorStore(path)
            newest = {"id": 2, "created_at": "2026-09-04T02:00:00Z"}
            older = {"id": 1, "created_at": "2026-09-04T01:00:00Z"}
            first.update("timeline", [newest])
            second.update("timeline", [older])
            self.assertEqual(CursorStore(path).after("timeline", [newest, older]), [])

    def test_malformed_cursor_is_preserved_with_recovery_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cursor.json"
            path.write_text("not json")
            with self.assertRaisesRegex(RuntimeError, "preserving it for recovery"):
                CursorStore(path)
            self.assertEqual(path.read_text(), "not json")
