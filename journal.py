"""Multiprocess-safe operation journal with idempotency reservations."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class JournalEntry:
    profile_id: str
    idempotency_key: str
    capability: str
    state: str
    arguments_digest: str
    result: Mapping[str, Any] | None
    error: str | None


class OperationJournal:
    """SQLite journal. Transactions serialize reservations across processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self._initialize()
        os.chmod(path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    profile_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    state TEXT NOT NULL,
                    arguments_digest TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (profile_id, idempotency_key)
                )
                """
            )

    def reserve(
        self,
        *,
        profile_id: str,
        idempotency_key: str,
        capability: str,
        arguments_digest: str,
    ) -> JournalEntry | None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE profile_id = ? AND idempotency_key = ?",
                (profile_id, idempotency_key),
            ).fetchone()
            if row is not None:
                if row["capability"] != capability or row["arguments_digest"] != arguments_digest:
                    raise ValueError("Basecamp idempotency key was reused with different arguments")
                return self._entry(row)
            connection.execute(
                "INSERT INTO operations VALUES (?, ?, ?, 'reserved', ?, NULL, NULL, ?)",
                (profile_id, idempotency_key, capability, arguments_digest, time.time()),
            )
        return None

    def mark_dispatched(self, *, profile_id: str, idempotency_key: str) -> None:
        with closing(self._connect()) as connection, connection:
            changed = connection.execute(
                """
                UPDATE operations SET state = 'dispatched', updated_at = ?
                WHERE profile_id = ? AND idempotency_key = ? AND state = 'reserved'
                """,
                (time.time(), profile_id, idempotency_key),
            ).rowcount
            if changed != 1:
                raise LookupError("Basecamp operation is not in pre-dispatch reserved state")

    def unresolved(self, profile_id: str) -> tuple[JournalEntry, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM operations
                WHERE profile_id = ? AND state IN ('reserved', 'pending', 'dispatched', 'uncertain')
                ORDER BY updated_at
                """,
                (profile_id,),
            ).fetchall()
        return tuple(self._entry(row) for row in rows)

    def resolve_for_retry(
        self,
        *,
        profile_id: str,
        idempotency_key: str,
        capability: str,
        arguments_digest: str,
        confirmed: bool,
        confirmed_not_applied: bool = False,
    ) -> str:
        """Release an exact reservation only after an explicit operator decision."""
        if not confirmed:
            raise PermissionError("Basecamp journal recovery requires explicit confirmation")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE profile_id = ? AND idempotency_key = ?",
                (profile_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise LookupError("Basecamp operation journal entry is missing")
            if row["capability"] != capability or row["arguments_digest"] != arguments_digest:
                raise ValueError("Basecamp journal recovery identity does not match the entry")
            state = str(row["state"])
            if state == "reserved":
                disposition = "released_pre_dispatch"
            elif state in {"pending", "dispatched", "uncertain"}:
                if not confirmed_not_applied:
                    raise PermissionError(
                        "Post-dispatch uncertainty requires confirmation that reconciliation proved no mutation"
                    )
                disposition = "released_post_dispatch_not_applied"
            else:
                raise ValueError(f"Basecamp journal entry is not unresolved: {state}")
            connection.execute(
                "DELETE FROM operations WHERE profile_id = ? AND idempotency_key = ?",
                (profile_id, idempotency_key),
            )
        return disposition

    def finish(
        self,
        *,
        profile_id: str,
        idempotency_key: str,
        state: str,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if state not in {"succeeded", "failed", "uncertain"}:
            raise ValueError(f"Invalid operation journal state: {state}")
        result_json = json.dumps(result, separators=(",", ":"), default=str) if result is not None else None
        with closing(self._connect()) as connection, connection:
            changed = connection.execute(
                """
                    UPDATE operations
                    SET state = ?, result_json = ?, error = ?, updated_at = ?
                    WHERE profile_id = ? AND idempotency_key = ?
                    """,
                (state, result_json, error, time.time(), profile_id, idempotency_key),
            ).rowcount
            if changed != 1:
                raise LookupError("Basecamp operation journal reservation is missing")

    def get(self, profile_id: str, idempotency_key: str) -> JournalEntry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE profile_id = ? AND idempotency_key = ?",
                (profile_id, idempotency_key),
            ).fetchone()
        return self._entry(row) if row is not None else None

    @staticmethod
    def _entry(row: sqlite3.Row) -> JournalEntry:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return JournalEntry(
            profile_id=row["profile_id"],
            idempotency_key=row["idempotency_key"],
            capability=row["capability"],
            state=row["state"],
            arguments_digest=row["arguments_digest"],
            result=result,
            error=row["error"],
        )
