"""Durable outbound reply intents for crash-safe event delivery."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Delivery:
    event_id: str
    sequence: int
    chat_id: str
    target_type: str
    project_id: str
    target_id: str
    content: str
    state: str
    item_id: str
    purpose: str
    created_at: float


class DeliveryJournal:
    """Private SQLite journal. Reply content is operational state, never a log field."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS outbound_deliveries (
                       event_id TEXT NOT NULL,
                       sequence INTEGER NOT NULL,
                       chat_id TEXT NOT NULL,
                       target_type TEXT NOT NULL,
                       project_id TEXT NOT NULL,
                       target_id TEXT NOT NULL,
                       content TEXT NOT NULL,
                       content_digest TEXT NOT NULL,
                       state TEXT NOT NULL,
                       item_id TEXT NOT NULL DEFAULT '',
                       purpose TEXT NOT NULL DEFAULT 'final',
                       created_at REAL NOT NULL DEFAULT 0,
                       updated_at REAL NOT NULL,
                       PRIMARY KEY(event_id, sequence)
                   )"""
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(outbound_deliveries)")}
            if "purpose" not in columns:
                connection.execute(
                    "ALTER TABLE outbound_deliveries ADD COLUMN purpose TEXT NOT NULL DEFAULT 'final'"
                )
            if "created_at" not in columns:
                connection.execute(
                    "ALTER TABLE outbound_deliveries ADD COLUMN created_at REAL NOT NULL DEFAULT 0"
                )
        os.chmod(path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def reserve(
        self,
        *,
        event_id: str,
        sequence: int,
        chat_id: str,
        target_type: str,
        project_id: str,
        target_id: str,
        content: str,
        purpose: str = "final",
    ) -> Delivery:
        digest = hashlib.sha256(content.encode()).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM outbound_deliveries WHERE event_id=? AND sequence=?",
                (event_id, sequence),
            ).fetchone()
            if row is not None:
                if row["content_digest"] != digest or row["chat_id"] != chat_id or row["purpose"] != purpose:
                    raise ValueError("Hermes produced a different reply for an existing delivery intent")
                connection.commit()
                return self._delivery(row)
            connection.execute(
                """INSERT INTO outbound_deliveries
                   (event_id, sequence, chat_id, target_type, project_id, target_id, content,
                    content_digest, state, item_id, purpose, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', '', ?, ?, ?)""",
                (
                    event_id, sequence, chat_id, target_type, project_id, target_id, content,
                    digest, purpose, time.time(), time.time(),
                ),
            )
            connection.commit()
        return self.pending(event_id)[-1]

    def transition(self, event_id: str, sequence: int, state: str, *, item_id: str = "") -> None:
        if state not in {"dispatched", "uncertain", "verified"}:
            raise ValueError(f"Invalid delivery state: {state}")
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """UPDATE outbound_deliveries SET state=?, item_id=CASE WHEN ?='' THEN item_id ELSE ? END,
                   updated_at=? WHERE event_id=? AND sequence=?""",
                (state, item_id, item_id, time.time(), event_id, sequence),
            ).rowcount
        if changed != 1:
            raise LookupError("Outbound delivery intent is missing")

    def pending(self, event_id: str) -> tuple[Delivery, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM outbound_deliveries WHERE event_id=? AND state!='verified'
                   ORDER BY sequence""",
                (event_id,),
            ).fetchall()
        return tuple(self._delivery(row) for row in rows)

    def has_any(self, event_id: str) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM outbound_deliveries WHERE event_id=? LIMIT 1", (event_id,)
            ).fetchone() is not None

    def has_final(self, event_id: str) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM outbound_deliveries WHERE event_id=? AND purpose='final' LIMIT 1",
                (event_id,),
            ).fetchone() is not None

    def next_sequence(self, event_id: str) -> int:
        with closing(self._connect()) as connection:
            value = connection.execute(
                "SELECT MAX(sequence) FROM outbound_deliveries WHERE event_id=?", (event_id,)
            ).fetchone()[0]
        return 0 if value is None else int(value) + 1

    @staticmethod
    def _delivery(row: Mapping[str, Any]) -> Delivery:
        return Delivery(
            str(row["event_id"]), int(row["sequence"]), str(row["chat_id"]),
            str(row["target_type"]), str(row["project_id"]), str(row["target_id"]),
            str(row["content"]), str(row["state"]), str(row["item_id"]), str(row["purpose"]),
            float(row["created_at"]),
        )
