import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from journal import OperationJournal


class _ConnectionProxy:
    def __init__(self, connection, closed):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_closed", closed)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def close(self):
        self._closed.append(True)
        self._connection.close()


class JournalTests(unittest.TestCase):
    def test_every_opened_sqlite_connection_is_closed(self):
        real_connect = sqlite3.connect
        closed = []

        def connect(*args, **kwargs):
            return _ConnectionProxy(real_connect(*args, **kwargs), closed)

        with (
            tempfile.TemporaryDirectory() as temp,
            patch("journal.sqlite3.connect", side_effect=connect) as mocked_connect,
        ):
            journal = OperationJournal(Path(temp) / "operations.sqlite3")
            journal.reserve(
                profile_id="profile",
                idempotency_key="one",
                capability="todos.update",
                arguments_digest="digest",
            )
            journal.finish(
                profile_id="profile",
                idempotency_key="one",
                state="succeeded",
                result={"verified": True},
            )
            self.assertIsNotNone(journal.get("profile", "one"))

        self.assertEqual(len(closed), mocked_connect.call_count)

    def test_restart_recovery_distinguishes_reserved_from_dispatched(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "operations.sqlite3"
            first = OperationJournal(path)
            first.reserve(
                profile_id="profile",
                idempotency_key="reserved",
                capability="todos.update",
                arguments_digest="digest-a",
            )
            first.reserve(
                profile_id="profile",
                idempotency_key="dispatched",
                capability="todos.update",
                arguments_digest="digest-b",
            )
            first.mark_dispatched(profile_id="profile", idempotency_key="dispatched")

            restarted = OperationJournal(path)
            self.assertEqual(
                {entry.idempotency_key: entry.state for entry in restarted.unresolved("profile")},
                {"reserved": "reserved", "dispatched": "dispatched"},
            )
            self.assertEqual(
                restarted.resolve_for_retry(
                    profile_id="profile",
                    idempotency_key="reserved",
                    capability="todos.update",
                    arguments_digest="digest-a",
                    confirmed=True,
                ),
                "released_pre_dispatch",
            )
            with self.assertRaises(PermissionError):
                restarted.resolve_for_retry(
                    profile_id="profile",
                    idempotency_key="dispatched",
                    capability="todos.update",
                    arguments_digest="digest-b",
                    confirmed=True,
                )
            self.assertEqual(
                restarted.resolve_for_retry(
                    profile_id="profile",
                    idempotency_key="dispatched",
                    capability="todos.update",
                    arguments_digest="digest-b",
                    confirmed=True,
                    confirmed_not_applied=True,
                ),
                "released_post_dispatch_not_applied",
            )

    def test_legacy_pending_requires_post_dispatch_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "operations.sqlite3"
            journal = OperationJournal(path)
            journal.reserve(
                profile_id="profile",
                idempotency_key="legacy",
                capability="todos.update",
                arguments_digest="digest",
            )
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE operations SET state = 'pending' WHERE idempotency_key = 'legacy'")
            with self.assertRaises(PermissionError):
                journal.resolve_for_retry(
                    profile_id="profile",
                    idempotency_key="legacy",
                    capability="todos.update",
                    arguments_digest="digest",
                    confirmed=True,
                )
