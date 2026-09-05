# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``session.persistence`` — the SQLite backing store (§9, §10).

Every test here uses a REAL ``session.persistence.SessionPersistence``
against a REAL SQLite file on a REAL temp path (``tmp_path``), a REAL
``session.store.SessionStore``, and a REAL ``session.engine.TurnEngine`` --
no mock stands in for the boundary under test. The only injected
collaborator is ``TurnEngine``'s ``execute_fn`` (a plain function, the same
dependency-injection seam ``tests/test_session_engine.py`` already uses),
standing in for the warehouse database itself, which this phase's spec has
nothing to say about.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pandas as pd
import pytest

from config import override_settings
from llm.providers import MockBackend
from llm.router import LLMRouter
from session.engine import TurnEngine
from session.persistence import SessionPersistence
from session.store import SessionStore

SYSTEM_PROMPT = "You are a T-SQL expert."

# Customer(ID, Name, NationalID, IsActive) is the one table whose column
# set is shared, unchanged, between project_config/ and
# project_config.example/ (see both schema.yaml files' own comments) --
# using it here keeps this module runnable under CI's example config too,
# unlike tests/test_session_engine.py's own domain_data-marked scenario.
Q1_QUESTION = "معاملات مشتری‌های فعال را نشان بده"
Q1_SQL = "SELECT TOP 5 c.Name, c.IsActive FROM Customer c"
Q2_QUESTION = "از بین آن‌ها ۳ مورد را نشان بده"
Q2_OUTER_SQL = "SELECT TOP 3 c_Name FROM _prev ORDER BY c_Name"

_DISTINCTIVE_ROW_VALUE = "زید-محرمانه-غیرقابل-افشا-۹۹۱۱"


def _execute_fn(sql: str) -> pd.DataFrame:
    if "Cnt" in sql:  # session.composer.check_scan_truncated's probe query
        return pd.DataFrame({"Cnt": [1]})
    return pd.DataFrame({"Name": [_DISTINCTIVE_ROW_VALUE], "IsActive": [1]})


def _engine(response_sql: str) -> TurnEngine:
    router = LLMRouter(default_chain=[MockBackend(response=response_sql)])
    return TurnEngine(router=router, execute_fn=_execute_fn)


# ---------------------------------------------------------------------------
# Mandated test 1 (inline in spec's "Refinement must survive rehydration"):
# create a session, add a turn, force it out of the hot set, reopen it, ask
# a refining question, assert basis.composition == "cte".
# ---------------------------------------------------------------------------


class TestRefinementSurvivesRehydration:
    def test_cte_refinement_resolves_against_a_reopened_conversation(self, tmp_path):
        persistence = SessionPersistence(str(tmp_path / "sessions.db"))
        store = SessionStore(
            ttl_seconds=0.05, max_size=10, max_turns=10,
            persistence=persistence, retention_days=30,
        )
        record = store.create(owner_id="analyst-1")

        with override_settings(refinement_scan_cap=10_000, default_top_n=1000):
            turn1 = _engine(Q1_SQL).ask(record, Q1_QUESTION, SYSTEM_PROMPT)
        assert turn1.error is None
        store.sync_turn(record, turn1)

        time.sleep(0.15)  # exceed the 0.05s TTL -- the hot entry is now gone

        rehydrated = store.get(record.session_id)
        assert rehydrated is not None, "TTL expiry must demote, not delete, a persisted session"
        assert rehydrated is not record  # a genuinely different, rebuilt object
        assert rehydrated.turns[0].result.rows_omitted is True
        assert rehydrated.turns[0].sql == turn1.sql  # SQL text DOES survive

        with override_settings(refinement_scan_cap=10_000, default_top_n=1000):
            turn2 = _engine(Q2_OUTER_SQL).ask(rehydrated, Q2_QUESTION, SYSTEM_PROMPT)

        assert turn2.error is None, turn2.error
        assert turn2.basis.kind == "refines"
        assert turn2.basis.composition == "cte"
        assert turn2.basis.refines_turn_id == turn1.turn_id
        persistence.close()


# ---------------------------------------------------------------------------
# Mandated test: rows are not on disk -- check the raw bytes.
# ---------------------------------------------------------------------------


class TestRowsNeverWrittenToDisk:
    def test_distinctive_row_value_is_not_in_the_sqlite_file(self, tmp_path):
        db_path = tmp_path / "sessions.db"
        persistence = SessionPersistence(str(db_path))
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=10, persistence=persistence)
        record = store.create(owner_id="analyst-1")

        with override_settings(default_top_n=1000):
            turn = _engine(Q1_SQL).ask(record, Q1_QUESTION, SYSTEM_PROMPT)
        assert turn.error is None
        assert _DISTINCTIVE_ROW_VALUE in [row["Name"] for row in turn.result.rows]  # sanity
        store.sync_turn(record, turn)
        persistence.close()

        raw_bytes = db_path.read_bytes()
        assert _DISTINCTIVE_ROW_VALUE.encode("utf-8") not in raw_bytes, (
            "a row value reached the SQLite file -- session.persistence must strip "
            "TurnResult.rows before serialising a Turn to disk"
        )
        # The WAL file (if not yet checkpointed) is also part of "the database" --
        # check it too rather than trusting the main file alone.
        wal_path = db_path.with_name(db_path.name + "-wal")
        if wal_path.exists():
            assert _DISTINCTIVE_ROW_VALUE.encode("utf-8") not in wal_path.read_bytes()


# ---------------------------------------------------------------------------
# Mandated test: restart survival.
# ---------------------------------------------------------------------------


class TestRestartSurvival:
    def test_sessions_turns_memory_and_memory_entries_survive_a_restart(self, tmp_path):
        db_path = str(tmp_path / "sessions.db")

        persistence_a = SessionPersistence(db_path)
        store_a = SessionStore(ttl_seconds=1800, max_size=10, max_turns=10, persistence=persistence_a)
        record = store_a.create(owner_id="analyst-1")
        with override_settings(default_top_n=1000):
            turn = _engine(Q1_SQL).ask(record, Q1_QUESTION, SYSTEM_PROMPT)
        store_a.sync_turn(record, turn)
        persistence_a.set_memory_entry("analyst-1", "scope", "ring", "پیش‌فرض", "2026-01-01T00:00:00+00:00")
        persistence_a.close()  # simulate process shutdown

        # A brand-new process would construct a fresh SessionPersistence
        # against the SAME path -- nothing here is shared in memory.
        persistence_b = SessionPersistence(db_path)
        store_b = SessionStore(ttl_seconds=1800, max_size=10, max_turns=10, persistence=persistence_b)

        reopened = store_b.get(record.session_id)
        assert reopened is not None
        assert reopened.owner_id == "analyst-1"
        assert len(reopened.turns) == 1
        assert reopened.turns[0].turn_id == turn.turn_id
        assert reopened.turns[0].question == Q1_QUESTION
        restored_memory = reopened.memory_for(turn.turn_id)
        assert restored_memory is not None
        assert restored_memory.sql == turn.sql
        assert restored_memory.result_columns == [c.name for c in turn.result.columns]

        entries = persistence_b.get_memory_entries("analyst-1")
        assert entries["scope"]["value"] == "پیش‌فرض"
        persistence_b.close()


# ---------------------------------------------------------------------------
# Mandated test: concurrency.
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_many_threads_adding_turns_lose_none_and_corrupt_nothing(self, tmp_path):
        persistence = SessionPersistence(str(tmp_path / "sessions.db"))
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=1000, persistence=persistence)
        record = store.create(owner_id="analyst-1")

        n_threads = 16
        errors: list[Exception] = []

        def _worker(i: int) -> None:
            try:
                with override_settings(default_top_n=1000):
                    turn = _engine(Q1_SQL).ask(record, f"{Q1_QUESTION} {i}", SYSTEM_PROMPT)
                store.sync_turn(record, turn)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"worker thread(s) raised: {errors}"
        assert len(record.turns) == n_threads
        assert len({t.turn_id for t in record.turns}) == n_threads  # no lost/duplicate turns

        # A brand-new store built against the SAME persistence backend
        # must see exactly the same turn count -- proving the writes
        # actually landed on disk, not merely in this process's hot dict.
        fresh_store = SessionStore(
            ttl_seconds=1800, max_size=10, max_turns=1000, persistence=persistence,
        )
        reopened = fresh_store.get(record.session_id)
        assert reopened is not None
        assert len(reopened.turns) == n_threads
        persistence.close()


# ---------------------------------------------------------------------------
# Retention purge and permanent delete
# ---------------------------------------------------------------------------


class TestRetentionPurge:
    def test_purge_expired_removes_sessions_past_retention(self, tmp_path):
        persistence = SessionPersistence(str(tmp_path / "sessions.db"))
        store = SessionStore(
            ttl_seconds=1800, max_size=10, max_turns=10,
            persistence=persistence, retention_days=30,
        )
        record = store.create(owner_id="analyst-1")
        with override_settings(default_top_n=1000):
            turn = _engine(Q1_SQL).ask(record, Q1_QUESTION, SYSTEM_PROMPT)
        store.sync_turn(record, turn)

        # Force the persisted row to look 31 days stale.
        conn = sqlite3.connect(str(tmp_path / "sessions.db"))
        conn.execute(
            "UPDATE sessions SET last_active_at = '2000-01-01T00:00:00+00:00' WHERE session_id = ?",
            (record.session_id,),
        )
        conn.commit()
        conn.close()

        removed = store.purge_expired()
        assert removed == 1
        assert persistence.session_last_active_at(record.session_id) is None

    def test_delete_removes_persisted_session_permanently(self, tmp_path):
        persistence = SessionPersistence(str(tmp_path / "sessions.db"))
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=10, persistence=persistence)
        record = store.create(owner_id="analyst-1")
        with override_settings(default_top_n=1000):
            turn = _engine(Q1_SQL).ask(record, Q1_QUESTION, SYSTEM_PROMPT)
        store.sync_turn(record, turn)

        assert store.delete(record.session_id) is True
        assert store.get(record.session_id) is None  # not rehydrated back from disk
        assert persistence.session_last_active_at(record.session_id) is None


# ---------------------------------------------------------------------------
# The default SessionStore() (no persistence) is unaffected.
# ---------------------------------------------------------------------------


class TestDefaultStoreUnaffectedByPersistenceFeature:
    def test_no_backend_get_after_ttl_expiry_still_deletes(self):
        store = SessionStore(ttl_seconds=0.05, max_size=10, max_turns=10)
        record = store.create()
        time.sleep(0.1)
        assert store.get(record.session_id) is None
        assert store.stats()["size"] == 0

    def test_no_backend_list_sessions_reflects_only_hot_set(self):
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=10)
        record = store.create(owner_id="analyst-1")
        rows = store.list_sessions("analyst-1")
        assert [r["session_id"] for r in rows] == [record.session_id]
