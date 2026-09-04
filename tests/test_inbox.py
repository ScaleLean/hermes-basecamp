import sqlite3
import tempfile
import unittest
from pathlib import Path

from inbox import DurableInbox


def event(event_id=1, created_at="2026-09-04T01:00:00Z"):
    return {
        "id": event_id,
        "created_at": created_at,
        "creator": {"id": 8},
        "bucket": {"id": 10},
        "recording": {"id": 40, "type": "Todo"},
    }


class DurableInboxTests(unittest.TestCase):
    def test_accept_and_cursor_advance_are_one_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            inbox = DurableInbox(Path(temp) / "inbox.sqlite3", max_pending=0)
            with self.assertRaisesRegex(RuntimeError, "cursor was not advanced"):
                inbox.accept_batch("poll", "campfire:10:30", [event()])
            self.assertEqual(inbox.after_cursor("campfire:10:30", [event()]), [event()])

    def test_webhook_and_polling_copies_deduplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            inbox = DurableInbox(Path(temp) / "inbox.sqlite3")
            self.assertEqual(inbox.accept_batch("poll", "activity:10", [event()]), 1)
            self.assertEqual(inbox.accept_batch("webhook", "webhook:10", [event()]), 0)
            self.assertIsNotNone(inbox.claim())
            self.assertIsNone(inbox.claim())

    def test_expired_lease_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "inbox.sqlite3"
            inbox = DurableInbox(path, lease_seconds=1)
            inbox.accept_batch("poll", "assignments", [event()])
            claimed = inbox.claim()
            self.assertIsNotNone(claimed)
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE inbox_events SET lease_expires_at=0")
            recovered = DurableInbox(path, lease_seconds=1).claim()
            self.assertEqual(recovered.key, claimed.key)

    def test_stream_cursors_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            inbox = DurableInbox(Path(temp) / "inbox.sqlite3")
            inbox.accept_batch("poll", "campfire:10:30", [event(1)])
            self.assertEqual(inbox.after_cursor("campfire:10:30", [event(1)]), [])
            self.assertEqual(inbox.after_cursor("campfire:10:31", [event(1)]), [event(1)])

    def test_snapshot_stream_without_timestamps_uses_id_set_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            inbox = DurableInbox(Path(temp) / "inbox.sqlite3")
            assigned = {"id": "assignment:40", "recording": {"id": 40}}
            inbox.accept_batch("poll", "assignments", [assigned])
            self.assertEqual(inbox.after_cursor("assignments", [assigned]), [])
            new = {"id": "assignment:41", "recording": {"id": 41}}
            self.assertEqual(inbox.after_cursor("assignments", [assigned, new]), [new])

    def test_payload_is_removed_from_terminal_record(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "inbox.sqlite3"
            inbox = DurableInbox(path)
            inbox.accept_batch("poll", "activity:10", [event()])
            claimed = inbox.claim()
            inbox.complete(claimed)
            with sqlite3.connect(path) as connection:
                payload = connection.execute("SELECT payload_json FROM inbox_events").fetchone()[0]
            self.assertEqual(payload, "{}")

    def test_bootstrap_marker_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "inbox.sqlite3"
            inbox = DurableInbox(path)
            self.assertFalse(inbox.is_bootstrapped())
            inbox.mark_bootstrapped()
            self.assertTrue(DurableInbox(path).is_bootstrapped())
