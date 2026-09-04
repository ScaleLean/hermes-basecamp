import tempfile
import unittest
from pathlib import Path

from delivery_journal import DeliveryJournal


class DeliveryJournalTests(unittest.TestCase):
    def test_reply_intent_survives_restart_and_rejects_changed_content(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "delivery.sqlite3"
            journal = DeliveryJournal(path)
            journal.reserve(
                event_id="e1", sequence=0, chat_id="recording:3", target_type="recording",
                project_id="2", target_id="3", content="result",
            )
            journal.transition("e1", 0, "dispatched")
            recovered = DeliveryJournal(path).pending("e1")
            self.assertEqual((recovered[0].state, recovered[0].content), ("dispatched", "result"))
            with self.assertRaisesRegex(ValueError, "different reply"):
                journal.reserve(
                    event_id="e1", sequence=0, chat_id="recording:3", target_type="recording",
                    project_id="2", target_id="3", content="different",
                )

    def test_verified_delivery_is_not_pending_but_retains_deduplication_record(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = DeliveryJournal(Path(temp) / "delivery.sqlite3")
            journal.reserve(
                event_id="e1", sequence=0, chat_id="ping:3", target_type="chat",
                project_id="3", target_id="4", content="hello",
            )
            journal.transition("e1", 0, "verified", item_id="5")
            self.assertEqual(journal.pending("e1"), ())
            self.assertTrue(journal.has_any("e1"))
