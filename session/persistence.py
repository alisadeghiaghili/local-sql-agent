# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""SQLite-backed persistence for sessions, turns, and cross-session memory.

Backs :class:`session.store.SessionStore` as an *optional* constructor
argument (§9, §10 — "the default SessionStore must not change at all").
WAL mode, ``check_same_thread=False`` plus a single ``threading.Lock``
guarding every access, mirroring ``SessionStore``'s own single-lock
discipline — not an ORM; every statement here is parameterised SQL.

**Result rows are never written to disk.** :meth:`save_turn` strips
``TurnResult.rows`` before serialising a :class:`~session.models.Turn` to
JSON — only the question, the SQL, the result's column names/row_count/
truncated flag, and the :class:`~session.store.TurnMemory` sidecar
(filters, result column names, SQL, the injected cap — never row data)
ever reach this file. Two reasons, the second decisive:

* It would create an unencrypted copy of warehouse data at rest in a file
  outside the DBA's control, in a project whose entire posture is that
  data does not leave the machine it was queried from.
* A persisted row cannot be re-checked against a changed ACL. A
  principal's ``denied_columns`` can gain a column *after* the rows were
  written; handing those rows back later would serve a column the
  principal is no longer allowed to see, and no guard at query time can
  catch it, because no query runs on a rehydrated turn.

:meth:`load_turns` rehydrates a :class:`~session.models.Turn` with
``result.rows_omitted=True`` and ``result.rows=[]`` for exactly this
reason — the shape survives, the numbers do not.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from session.models import Turn
from session.store import TurnMemory


class SessionPersistence:
    """SQLite backing store for :class:`session.store.SessionStore`.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file (see
        ``config.Settings.session_store_path``). Parent directories are
        created if missing.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_id TEXT,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    turn_json TEXT NOT NULL,
                    memory_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, turn_id)
                );
                CREATE TABLE IF NOT EXISTS memory_entries (
                    owner_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    field TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, key)
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def upsert_session(
        self, session_id: str, owner_id: str | None, title: str | None,
        created_at: str, last_active_at: str,
    ) -> None:
        """Insert *session_id*, or update its owner/title/``last_active_at``
        if it already exists (``created_at`` is never overwritten)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_id, owner_id, title, created_at, last_active_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "owner_id=excluded.owner_id, title=excluded.title, "
                "last_active_at=excluded.last_active_at",
                (session_id, owner_id, title, created_at, last_active_at),
            )
            self._conn.commit()

    def touch_session(self, session_id: str, last_active_at: str) -> None:
        """Update only ``last_active_at`` — called on every turn synced."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
                (last_active_at, session_id),
            )
            self._conn.commit()

    def set_title(self, session_id: str, title: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET title = ? WHERE session_id = ?", (title, session_id),
            )
            self._conn.commit()

    def load_session_row(self, session_id: str) -> dict | None:
        """Return ``{"owner_id", "title", "created_at", "last_active_at"}``,
        or ``None`` if *session_id* has never been persisted (or was
        permanently deleted)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_id, title, created_at, last_active_at "
                "FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {"owner_id": row[0], "title": row[1], "created_at": row[2], "last_active_at": row[3]}

    def session_last_active_at(self, session_id: str) -> str | None:
        """``None`` for a session never persisted, or since permanently deleted."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_active_at FROM sessions WHERE session_id = ?", (session_id,),
            ).fetchone()
        return row[0] if row is not None else None

    def list_sessions(self, owner_id: str, cutoff_iso: str) -> list[dict]:
        """Rows owned by *owner_id* (or ownerless, see ``SessionRecord.owner_id``'s
        docstring) whose ``last_active_at`` is at or after *cutoff_iso*, most
        recently active first. Each row carries a live ``turn_count``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.session_id, s.title, s.created_at, s.last_active_at, "
                "  (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.session_id) AS turn_count "
                "FROM sessions s "
                "WHERE (s.owner_id = ? OR s.owner_id IS NULL) AND s.last_active_at >= ? "
                "ORDER BY s.last_active_at DESC",
                (owner_id, cutoff_iso),
            ).fetchall()
        return [
            {
                "session_id": r[0], "title": r[1], "created_at": r[2],
                "last_active_at": r[3], "turn_count": r[4],
            }
            for r in rows
        ]

    def delete_session(self, session_id: str) -> None:
        """Permanently remove *session_id* and its turns. Idempotent."""
        with self._lock:
            self._conn.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            self._conn.commit()

    def purge_sessions_older_than(self, cutoff_iso: str) -> int:
        """Permanently delete every session whose ``last_active_at`` is
        strictly before *cutoff_iso* (§9's retention purge). Returns the
        number of sessions removed."""
        with self._lock:
            ids = [
                r[0] for r in self._conn.execute(
                    "SELECT session_id FROM sessions WHERE last_active_at < ?", (cutoff_iso,),
                ).fetchall()
            ]
            if ids:
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(f"DELETE FROM turns WHERE session_id IN ({placeholders})", ids)
                self._conn.execute(f"DELETE FROM sessions WHERE session_id IN ({placeholders})", ids)
                self._conn.commit()
        return len(ids)

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def save_turn(self, session_id: str, turn: Turn, memory: TurnMemory | None) -> None:
        """Upsert *turn* -- with ``result.rows`` stripped -- and its
        :class:`~session.store.TurnMemory` sidecar. See module docstring
        for why rows never reach this call's serialised payload."""
        data = turn.model_dump(mode="json")
        if data.get("result") is not None:
            data["result"]["rows"] = []
        turn_json = json.dumps(data, ensure_ascii=False)

        memory_json = json.dumps(
            {
                "filters": memory.filters if memory is not None else {},
                "result_columns": memory.result_columns if memory is not None else [],
                "sql": memory.sql if memory is not None else None,
                "injected_top": memory.injected_top if memory is not None else None,
                "row_count": memory.row_count if memory is not None else 0,
            },
            ensure_ascii=False,
        )

        with self._lock:
            self._conn.execute(
                "INSERT INTO turns (session_id, turn_id, turn_index, turn_json, memory_json) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, turn_id) DO UPDATE SET "
                "turn_index=excluded.turn_index, turn_json=excluded.turn_json, "
                "memory_json=excluded.memory_json",
                (session_id, turn.turn_id, turn.index, turn_json, memory_json),
            )
            self._conn.commit()

    def load_turns(self, session_id: str) -> tuple[list[Turn], dict[str, TurnMemory]]:
        """Rehydrate every persisted turn for *session_id*, oldest first.

        Every returned :class:`~session.models.Turn` with a non-``None``
        ``result`` carries ``result.rows_omitted=True`` and
        ``result.rows=[]`` -- see module docstring.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT turn_json, memory_json FROM turns "
                "WHERE session_id = ? ORDER BY turn_index ASC",
                (session_id,),
            ).fetchall()

        turns: list[Turn] = []
        memories: dict[str, TurnMemory] = {}
        for turn_json, memory_json in rows:
            data = json.loads(turn_json)
            if data.get("result") is not None:
                data["result"]["rows"] = []
                data["result"]["rows_omitted"] = True
            turn = Turn.model_validate(data)
            turns.append(turn)

            mem = json.loads(memory_json)
            memories[turn.turn_id] = TurnMemory(
                turn_id=turn.turn_id,
                filters=mem.get("filters", {}),
                result_columns=mem.get("result_columns", []),
                sql=mem.get("sql"),
                injected_top=mem.get("injected_top"),
                row_count=mem.get("row_count", 0),
            )
        return turns, memories

    # ------------------------------------------------------------------
    # Cross-session memory (§5)
    # ------------------------------------------------------------------

    def set_memory_entry(
        self, owner_id: str, key: str, field: str, value: str, updated_at: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory_entries (owner_id, key, field, value, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_id, key) DO UPDATE SET "
                "field=excluded.field, value=excluded.value, updated_at=excluded.updated_at",
                (owner_id, key, field, value, updated_at),
            )
            self._conn.commit()

    def get_memory_entries(self, owner_id: str) -> dict[str, dict]:
        """``{key: {"field", "value", "updated_at"}}`` for *owner_id* alone
        -- never another principal's entries (§5 cross-principal isolation)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, field, value, updated_at FROM memory_entries WHERE owner_id = ?",
                (owner_id,),
            ).fetchall()
        return {r[0]: {"field": r[1], "value": r[2], "updated_at": r[3]} for r in rows}

    def count_memory_entries(self, owner_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM memory_entries WHERE owner_id = ?", (owner_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def delete_memory_entry(self, owner_id: str, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM memory_entries WHERE owner_id = ? AND key = ?", (owner_id, key),
            )
            self._conn.commit()

    def delete_all_memory_entries(self, owner_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM memory_entries WHERE owner_id = ?", (owner_id,))
            self._conn.commit()
