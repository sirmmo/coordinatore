"""SQLite-backed session storage.

Schema is intentionally minimal: one row per session, with the mutable
state serialised as JSON. A session lives in exactly one Telegram chat,
which is what we key off most of the time.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    game_id       TEXT NOT NULL,
    variant_id    TEXT NOT NULL,
    status        TEXT NOT NULL,         -- 'opening' | 'running' | 'ended'
    created_at    REAL NOT NULL,
    started_at    REAL,
    ended_at      REAL,
    state_json    TEXT NOT NULL          -- scores, masters, log
);

CREATE UNIQUE INDEX IF NOT EXISTS sessions_active_per_chat
    ON sessions(chat_id) WHERE status != 'ended';
"""


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---- writes ----

    def create_session(
        self,
        chat_id: int,
        game_id: str,
        variant_id: str,
        initial_state: dict,
    ) -> str:
        session_id = uuid.uuid4().hex
        with self._conn() as c:
            c.execute(
                """INSERT INTO sessions
                   (id, chat_id, game_id, variant_id, status, created_at, state_json)
                   VALUES (?, ?, ?, ?, 'opening', ?, ?)""",
                (
                    session_id,
                    chat_id,
                    game_id,
                    variant_id,
                    time.time(),
                    json.dumps(initial_state),
                ),
            )
        return session_id

    def update_state(self, session_id: str, state: dict) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(state), session_id),
            )

    def mark_started(self, session_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE sessions SET status = 'running', started_at = ? WHERE id = ?",
                (time.time(), session_id),
            )

    def mark_ended(self, session_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE sessions SET status = 'ended', ended_at = ? WHERE id = ?",
                (time.time(), session_id),
            )

    # ---- reads ----

    def active_for_chat(self, chat_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE chat_id = ? AND status != 'ended'",
                (chat_id,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def get(self, session_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _row_to_dict(row) if row else None


def _row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["state"] = json.loads(out.pop("state_json"))
    return out
