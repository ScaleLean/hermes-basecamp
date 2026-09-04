import json
import tempfile
import unittest
from pathlib import Path

from replay_store import ReplayStore


class ReplayStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_claim_commit_survives_restart(self):
        first = ReplayStore(self.path)
        self.assertTrue(first.claim("event-1"))
        first.commit("event-1")
        second = ReplayStore(self.path)
        second.load()
        self.assertFalse(second.claim("event-1"))

    def test_release_allows_retry(self):
        store = ReplayStore(self.path)
        self.assertTrue(store.claim("event-1"))
        store.release("event-1")
        self.assertTrue(store.claim("event-1"))

    def test_bootstrap_persists_without_content(self):
        store = ReplayStore(self.path)
        store.bootstrap(["event-1", "event-2"])
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["committed"], ["event-1", "event-2"])
        self.assertEqual(payload["pending"], {})

    def test_two_instances_cannot_claim_the_same_event(self):
        first = ReplayStore(self.path)
        second = ReplayStore(self.path)
        self.assertTrue(first.claim("event-race"))
        self.assertFalse(second.claim("event-race"))
