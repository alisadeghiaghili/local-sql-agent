# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""SQLAlchemy-backed persistence for sessions, turns, and cross-session memory.

Backs :class:`session.store.SessionStore` as an *optional* constructor
argument (§9, §10 — "the default SessionStore must not change at all").
Ported from a raw ``sqlite3`` connection to SQLAlchemy Core (admin panel
phase 2 — the same layer :mod:`appdb` uses for the key store) so this
module's own construction, table creation, and querying use the same
library the rest of the application database does. A single shared
connection (``poolclass=StaticPool``) guarded by one ``threading.Lock``,
mirroring ``SessionStore``'s own single-lock discipline and this module's
pre-port behaviour exactly — not a connection pool, and not an ORM; every
statement here is a SQLAlchemy Core construct (``Table``/``select``/
``insert``), the same style ``database/connection.py`` and
``database/executor.py`` already use for the warehouse connection.

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

Deliberately its own engine, not the shared application-database engine
(:mod:`appdb.engine`)
------------------------------------------------------------------------
This module keeps constructing its own SQLAlchemy engine from the
``db_path`` its caller supplies (``config.Settings.session_store_path``),
independent of ``config.Settings.app_db_url`` — "port onto the same
SQLAlchemy layer" (the phase 2 spec's own words) is read here as "the same
*technology*", not "the same physical database". Three call sites depend
on behaviour a shared engine would break: :func:`api.v2_routes.get_session_store`
passes the literal sentinel ``":memory:"`` for the memory-only fallback
when persistence is disabled but cross-session memory still needs
somewhere to live; ``session_store_path=""`` must keep disabling
persistence entirely (:class:`session.store.SessionStore` never
constructs this class at all in that case); and every existing test in
``tests/test_session_persistence.py`` constructs this class directly
against a bare filesystem path. Folding session storage into the key
store's own database would also mean a single ``APP_DB_URL`` outage takes
down conversation history *and* authentication together, where today they
fail independently.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import StaticPool

from session.models import Turn
from session.store import TurnMemory

# ---------------------------------------------------------------------------
# Schema -- this module's own MetaData, separate from appdb.models (see
# module docstring for why the two are deliberately different databases).
# ---------------------------------------------------------------------------

_metadata = MetaData()

_sessions = Table(
    "sessions",
    _metadata,
    Column("session_id", String, primary_key=True),
    Column("owner_id", String, nullable=True),
    Column("title", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("last_active_at", String, nullable=False),
)

_turns = Table(
    "turns",
    _metadata,
    Column("session_id", String, primary_key=True),
    Column("turn_id", String, primary_key=True),
    Column("turn_index", Integer, nullable=False),
    Column("turn_json", String, nullable=False),
    Column("memory_json", String, nullable=False),
)

_memory_entries = Table(
    "memory_entries",
    _metadata,
    Column("owner_id", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("field", String, nullable=False),
    Column("value", String, nullable=False),
    Column("updated_at", String, nullable=False),
)


def _build_engine(db_path: str):
    """A SQLite engine for *db_path* -- a bare filesystem path, the
    ``":memory:"`` sentinel, or (for a future caller) an already-complete
    SQLAlchemy URL.

    Always ``poolclass=StaticPool`` plus ``check_same_thread=False``: a
    single, shared connection reused across threads, exactly matching
    this module's pre-port single-``sqlite3.Connection`` behaviour (see
    module docstring) and required outright for the ``":memory:"`` case
    -- SQLAlchemy's default pool would otherwise hand each checkout an
    independent, empty in-memory database.
    """
    if "://" in db_path:
        url = db_path
    elif db_path == ":memory:":
        url = "sqlite://"
    else:
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{db_path}"

    engine = create_engine(
        url, poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    _metadata.create_all(engine)
    return engine


class SessionPersistence:
    """SQLite backing store for :class:`session.store.SessionStore`.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file (see
        ``config.Settings.session_store_path``), or the ``":memory:"``
        sentinel. Parent directories are created if missing.
    """

    def __init__(self, db_path: str) -> None:
        import threading

        self._db_path = db_path
        self._lock = threading.Lock()
        self._engine = _build_engine(db_path)

    def close(self) -> None:
        with self._lock:
            self._engine.dispose()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def upsert_session(
        self, session_id: str, owner_id: str | None, title: str | None,
        created_at: str, last_active_at: str,
    ) -> None:
        """Insert *session_id*, or update its owner/title/``last_active_at``
        if it already exists (``created_at`` is never overwritten)."""
        stmt = sqlite_insert(_sessions).values(
            session_id=session_id, owner_id=owner_id, title=title,
            created_at=created_at, last_active_at=last_active_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["session_id"],
            set_={
                "owner_id": stmt.excluded.owner_id,
                "title": stmt.excluded.title,
                "last_active_at": stmt.excluded.last_active_at,
            },
        )
        with self._lock, self._engine.begin() as conn:
            conn.execute(stmt)

    def touch_session(self, session_id: str, last_active_at: str) -> None:
        """Update only ``last_active_at`` — called on every turn synced."""
        with self._lock, self._engine.begin() as conn:
            conn.execute(
                _sessions.update()
                .where(_sessions.c.session_id == session_id)
                .values(last_active_at=last_active_at)
            )

    def set_title(self, session_id: str, title: str) -> None:
        with self._lock, self._engine.begin() as conn:
            conn.execute(
                _sessions.update().where(_sessions.c.session_id == session_id).values(title=title)
            )

    def load_session_row(self, session_id: str) -> dict | None:
        """Return ``{"owner_id", "title", "created_at", "last_active_at"}``,
        or ``None`` if *session_id* has never been persisted (or was
        permanently deleted)."""
        with self._lock, self._engine.connect() as conn:
            row = conn.execute(
                select(
                    _sessions.c.owner_id, _sessions.c.title,
                    _sessions.c.created_at, _sessions.c.last_active_at,
                ).where(_sessions.c.session_id == session_id)
            ).first()
        if row is None:
            return None
        return {"owner_id": row[0], "title": row[1], "created_at": row[2], "last_active_at": row[3]}

    def session_last_active_at(self, session_id: str) -> str | None:
        """``None`` for a session never persisted, or since permanently deleted."""
        with self._lock, self._engine.connect() as conn:
            row = conn.execute(
                select(_sessions.c.last_active_at).where(_sessions.c.session_id == session_id)
            ).first()
        return row[0] if row is not None else None

    def list_sessions(self, owner_id: str, cutoff_iso: str) -> list[dict]:
        """Rows owned by *owner_id* (or ownerless, see ``SessionRecord.owner_id``'s
        docstring) whose ``last_active_at`` is at or after *cutoff_iso*, most
        recently active first. Each row carries a live ``turn_count``."""
        turn_count = (
            select(func.count())
            .select_from(_turns)
            .where(_turns.c.session_id == _sessions.c.session_id)
            .scalar_subquery()
        )
        stmt = (
            select(
                _sessions.c.session_id, _sessions.c.title,
                _sessions.c.created_at, _sessions.c.last_active_at,
                turn_count.label("turn_count"),
            )
            .where(
                or_(_sessions.c.owner_id == owner_id, _sessions.c.owner_id.is_(None)),
                _sessions.c.last_active_at >= cutoff_iso,
            )
            .order_by(_sessions.c.last_active_at.desc())
        )
        with self._lock, self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [
            {
                "session_id": r[0], "title": r[1], "created_at": r[2],
                "last_active_at": r[3], "turn_count": r[4],
            }
            for r in rows
        ]

    def delete_session(self, session_id: str) -> None:
        """Permanently remove *session_id* and its turns. Idempotent."""
        with self._lock, self._engine.begin() as conn:
            conn.execute(_turns.delete().where(_turns.c.session_id == session_id))
            conn.execute(_sessions.delete().where(_sessions.c.session_id == session_id))

    def purge_sessions_older_than(self, cutoff_iso: str) -> int:
        """Permanently delete every session whose ``last_active_at`` is
        strictly before *cutoff_iso* (§9's retention purge). Returns the
        number of sessions removed."""
        with self._lock, self._engine.begin() as conn:
            ids = conn.execute(
                select(_sessions.c.session_id).where(_sessions.c.last_active_at < cutoff_iso)
            ).scalars().all()
            if ids:
                conn.execute(_turns.delete().where(_turns.c.session_id.in_(ids)))
                conn.execute(_sessions.delete().where(_sessions.c.session_id.in_(ids)))
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

        stmt = sqlite_insert(_turns).values(
            session_id=session_id, turn_id=turn.turn_id, turn_index=turn.index,
            turn_json=turn_json, memory_json=memory_json,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["session_id", "turn_id"],
            set_={
                "turn_index": stmt.excluded.turn_index,
                "turn_json": stmt.excluded.turn_json,
                "memory_json": stmt.excluded.memory_json,
            },
        )
        with self._lock, self._engine.begin() as conn:
            conn.execute(stmt)

    def load_turns(self, session_id: str) -> tuple[list[Turn], dict[str, TurnMemory]]:
        """Rehydrate every persisted turn for *session_id*, oldest first.

        Every returned :class:`~session.models.Turn` with a non-``None``
        ``result`` carries ``result.rows_omitted=True`` and
        ``result.rows=[]`` -- see module docstring.
        """
        with self._lock, self._engine.connect() as conn:
            rows = conn.execute(
                select(_turns.c.turn_json, _turns.c.memory_json)
                .where(_turns.c.session_id == session_id)
                .order_by(_turns.c.turn_index.asc())
            ).all()

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
        stmt = sqlite_insert(_memory_entries).values(
            owner_id=owner_id, key=key, field=field, value=value, updated_at=updated_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["owner_id", "key"],
            set_={
                "field": stmt.excluded.field,
                "value": stmt.excluded.value,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        with self._lock, self._engine.begin() as conn:
            conn.execute(stmt)

    def get_memory_entries(self, owner_id: str) -> dict[str, dict]:
        """``{key: {"field", "value", "updated_at"}}`` for *owner_id* alone
        -- never another principal's entries (§5 cross-principal isolation)."""
        with self._lock, self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    _memory_entries.c.key, _memory_entries.c.field,
                    _memory_entries.c.value, _memory_entries.c.updated_at,
                ).where(_memory_entries.c.owner_id == owner_id)
            ).all()
        return {r[0]: {"field": r[1], "value": r[2], "updated_at": r[3]} for r in rows}

    def count_memory_entries(self, owner_id: str) -> int:
        with self._lock, self._engine.connect() as conn:
            row = conn.execute(
                select(func.count())
                .select_from(_memory_entries)
                .where(_memory_entries.c.owner_id == owner_id)
            ).first()
        return int(row[0]) if row is not None else 0

    def delete_memory_entry(self, owner_id: str, key: str) -> None:
        with self._lock, self._engine.begin() as conn:
            conn.execute(
                _memory_entries.delete().where(
                    (_memory_entries.c.owner_id == owner_id) & (_memory_entries.c.key == key)
                )
            )

    def delete_all_memory_entries(self, owner_id: str) -> None:
        with self._lock, self._engine.begin() as conn:
            conn.execute(_memory_entries.delete().where(_memory_entries.c.owner_id == owner_id))
