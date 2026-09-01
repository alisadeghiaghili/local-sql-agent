# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""SQLite persistence for the web app: users + query logs.

The database file lives next to this module (``webapp/app.db``) and is
created automatically on first use.  Passwords are stored as salted
hashes (werkzeug), never in plain text.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = Path(__file__).resolve().parent / "app.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL,
    question        TEXT NOT NULL,
    generated_sql   TEXT,
    interpretation  TEXT,
    output_file     TEXT,
    row_count       INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    error_message   TEXT,
    elapsed_seconds REAL,
    created_at      TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they do not exist yet.  Safe to call repeatedly."""
    with closing(_connect()) as conn, conn:
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username: str, password: str) -> None:
    """Insert a new user.  Raises sqlite3.IntegrityError on duplicate name."""
    init_db()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (
                username,
                generate_password_hash(password),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def verify_user(username: str, password: str) -> dict[str, Any] | None:
    """Return the user row as a dict if credentials match, else None."""
    init_db()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Query logs
# ---------------------------------------------------------------------------

def log_query(
    username: str,
    question: str,
    status: str,
    generated_sql: str | None = None,
    interpretation: str | None = None,
    output_file: str | None = None,
    row_count: int = 0,
    error_message: str | None = None,
    elapsed_seconds: float | None = None,
) -> None:
    """Record one submitted question and its outcome."""
    init_db()
    with closing(_connect()) as conn, conn:
        conn.execute(
            """INSERT INTO logs (
                   username, question, generated_sql, interpretation,
                   output_file, row_count, status, error_message,
                   elapsed_seconds, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                username,
                question,
                generated_sql,
                interpretation,
                output_file,
                row_count,
                status,
                error_message,
                elapsed_seconds,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
