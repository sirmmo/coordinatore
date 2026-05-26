"""Session storage with pluggable backends.

Two backends are shipped:

* ``SqliteStorage`` — uses the stdlib ``sqlite3`` module against a local file.
  Used by the Docker / VPS deployment and by the test suite.
* ``LibSqlStorage`` — uses ``libsql-client`` to talk to a remote Turso
  database (or any libsql server). Used by the Vercel serverless deploy.

Both classes expose the same synchronous method surface; handlers don't
know which one they're talking to. ``make_storage(url)`` picks the right
backend from a URL scheme.

Schema is intentionally minimal: one row per session, mutable state as JSON.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS sessions (
        id            TEXT PRIMARY KEY,
        chat_id       INTEGER NOT NULL,
        game_id       TEXT NOT NULL,
        variant_id    TEXT NOT NULL,
        status        TEXT NOT NULL,
        created_at    REAL NOT NULL,
        started_at    REAL,
        ended_at      REAL,
        state_json    TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS sessions_active_per_chat
        ON sessions(chat_id) WHERE status != 'ended'""",
]


# ---------------------- SQLite (local file) ----------------------


class SqliteStorage:
    """Local SQLite backend; used for Docker / VPS deployments and tests."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            for stmt in SCHEMA_STATEMENTS:
                c.execute(stmt)

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

    def active_for_chat(self, chat_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE chat_id = ? AND status != 'ended'",
                (chat_id,),
            ).fetchone()
        return _sqlite_row_to_dict(row) if row else None

    def get(self, session_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _sqlite_row_to_dict(row) if row else None


def _sqlite_row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["state"] = json.loads(out.pop("state_json"))
    return out


# ---------------------- libsql / Turso ----------------------


class LibSqlStorage:
    """Remote libsql (Turso) backend; used by the serverless deploy.

    The official ``libsql`` Python package exposes a sqlite3-compatible
    Connection, so the implementation mirrors SqliteStorage almost exactly
    — only the connection factory differs.
    """

    def __init__(self, url: str, auth_token: str | None = None) -> None:
        import libsql  # imported lazily — optional dep

        self._url = url
        self._auth_token = auth_token
        self._libsql = libsql
        with self._conn() as c:
            for stmt in SCHEMA_STATEMENTS:
                c.execute(stmt)

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        c = self._libsql.connect(self._url, auth_token=self._auth_token)
        try:
            yield c
            c.commit()
        finally:
            c.close()

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

    def active_for_chat(self, chat_id: int) -> dict | None:
        with self._conn() as c:
            cur = c.execute(
                "SELECT id, chat_id, game_id, variant_id, status, created_at, "
                "started_at, ended_at, state_json "
                "FROM sessions WHERE chat_id = ? AND status != 'ended'",
                (chat_id,),
            )
            row = cur.fetchone()
        return _libsql_row_to_dict(row, cur.description) if row else None

    def get(self, session_id: str) -> dict | None:
        with self._conn() as c:
            cur = c.execute(
                "SELECT id, chat_id, game_id, variant_id, status, created_at, "
                "started_at, ended_at, state_json "
                "FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
        return _libsql_row_to_dict(row, cur.description) if row else None


def _libsql_row_to_dict(row: Any, description: Any) -> dict:
    columns = [col[0] for col in description]
    out = dict(zip(columns, row, strict=False))
    out["state"] = json.loads(out.pop("state_json"))
    return out


# ---------------------- factory ----------------------


def make_storage(url: str, auth_token: str | None = None):
    """Construct the right backend from a connection URL.

    Accepts:
        file:./data/coordinatore.sqlite        (local file)
        file:/absolute/path.sqlite             (local file)
        sqlite:///path.sqlite                  (alias)
        ./relative/path.sqlite                 (bare path; treated as file:)
        libsql://my-db.turso.io                (remote)
        https://my-db.turso.io                 (remote; libsql over https)
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in ("libsql", "https", "http", "wss", "ws"):
        return LibSqlStorage(url=url, auth_token=auth_token)

    if scheme in ("file", "sqlite", ""):
        if scheme == "":
            path = Path(url)
        elif scheme == "file":
            # file:./foo.sqlite  -> parsed.path is "./foo.sqlite"; urlparse
            # treats the part after "file:" as path.
            raw = url[5:]
            path = Path(raw)
        else:  # sqlite
            # sqlite:///abs/path.sqlite  or  sqlite:/relative
            path = Path(parsed.path)
        return SqliteStorage(db_path=path)

    raise ValueError(f"unknown storage URL scheme: {scheme!r}")
