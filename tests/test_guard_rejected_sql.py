# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``session.models.GuardVerdict.rejected_sql`` -- the "See the
SQL" action ``docs/design/DESIGN-INVARIANTS.md`` §8's failure-anatomy table
names for a guard rejection ("Guard: forbidden statement" -> "Rephrase ·
See the SQL").

Before this field existed, ``session/engine.py`` built every guard-rejected
``_GenOutcome`` without the refused statement (see that module's
``_handle_cte_refinement``/``_generate_validate_execute``), so
``web/js/render/turn.js`` had nothing to show and printed a flat "not
retained" sentence regardless of how the guard actually failed.

This module proves the field is populated on the two rejection paths
``session/engine.py`` builds (a terminal ``PolicyRejection``, and a
``CorrectableRejection`` that survives the whole correction budget), stays
``None`` on an accepted turn, and round-trips through
``session.persistence`` both ways (present, and absent from an
older-shaped stored record). The contract-document assertion (`§4`'s
`guard` object must document the field) lives beside its sibling in
``tests/test_guard_error_contract.py::TestReasonEnumIsDocumented``.

Every SQL string below is either schema-agnostic (``SELECT @@version`` --
a forbidden state-reading node, ADR-001, that no schema makes legal) or an
unresolvable table name (``ThisTableDoesNotExist_zzz`` -- a
``CorrectableRejection`` regardless of which schema is loaded) -- never a
real deployment's table/column name, so this module runs the same under
``PROJECT_CONFIG_DIR=project_config.example`` (CI, a fresh clone) as
against a real one. The one test that needs an ACCEPTED turn derives its
table/column from ``schema_data.columns.TABLE_COLUMNS`` at runtime, same
as ``tests/test_sql_guard_schema.py``'s ``_ANY_TABLE``/``_ANY_COLUMN``.
"""

from __future__ import annotations

import json
import sqlite3

import pandas as pd
import pytest

from llm.base import LLMBackend
from llm.providers import MockBackend
from llm.router import LLMRouter
from session.engine import TurnEngine
from session.models import GuardVerdict, Turn, TurnResult
from session.persistence import SessionPersistence
from session.store import SessionStore

SYSTEM_PROMPT = "You are a T-SQL expert."


def _never_execute(sql: str) -> pd.DataFrame:  # noqa: ARG001 - the guard must reject before this runs
    raise AssertionError("execute_fn must not be called for a guard-rejected turn")


# ---------------------------------------------------------------------------
# 1. PolicyRejection (terminal -- no correction round can fix it)
# ---------------------------------------------------------------------------


class TestEngineAttachesRejectedSqlOnPolicyRejection:
    def test_state_reading_construct_carries_the_refused_statement(self):
        """``SELECT @@version`` is a forbidden state-reading node
        (ADR-001) -- a ``PolicyRejection`` under any schema, so this needs
        no table/column at all."""
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response="SELECT @@version")]),
            execute_fn=_never_execute,
        )

        turn = engine.ask(record, "نسخهٔ سرور دیتابیس چیست؟", SYSTEM_PROMPT)

        assert turn.guard is not None
        assert turn.guard.verdict == "rejected"
        assert turn.guard.reason == "forbidden_statement"
        assert turn.guard.rejected_sql == "SELECT @@version"

    def test_denied_column_also_carries_the_refused_statement(self):
        """A second PolicyRejection shape (denied_columns ACL, not a
        forbidden-statement node) -- ``Customer(ID, Name, NationalID,
        IsActive)`` is the one table shared, unchanged, between
        ``project_config/`` and ``project_config.example/`` (see
        ``tests/test_session_persistence.py``'s own comment to the same
        effect), so this is schema-portable without reaching into
        ``TABLE_COLUMNS``."""
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response="SELECT NationalID FROM Customer")]),
            execute_fn=_never_execute,
        )

        turn = engine.ask(
            record, "کد ملی مشتری‌ها را نشان بده", SYSTEM_PROMPT,
            denied_columns=("NationalID",),
        )

        assert turn.guard is not None
        assert turn.guard.verdict == "rejected"
        assert turn.guard.reason == "denied_column"
        assert turn.guard.rejected_sql == "SELECT NationalID FROM Customer"


# ---------------------------------------------------------------------------
# 2. CorrectableRejection, correction budget exhausted -- the LAST round's
#    refused statement, not merely some round's.
# ---------------------------------------------------------------------------


class _RoundAwareBackend(LLMBackend):
    """Returns a DIFFERENT SQL string each call, proving the outcome keeps
    the LAST round's statement rather than the first or any other --
    ``MockBackend`` returns one fixed string forever, which cannot
    distinguish "the last round's text" from "the only text there ever
    was". Deliberately a real ``LLMBackend`` subclass, mirroring
    ``tests/test_correction_loop_policy_rejection.py``'s ``_CountingBackend``,
    so ``call_count`` reflects true model invocations regardless of which
    ``LLMBackend`` wrapper method the router happens to call through.
    """

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def name(self) -> str:
        return "round-aware:test"

    def generate(self, prompt: str) -> str:  # noqa: ARG002
        self.call_count += 1
        return f"SELECT * FROM ThisTableDoesNotExist_round_{self.call_count}"


class TestEngineAttachesRejectedSqlOnExhaustedCorrection:
    def test_last_rounds_distinct_statement_is_the_one_kept(self):
        backend = _RoundAwareBackend()
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]),
            execute_fn=_never_execute,
            max_corrections=2,
        )

        turn = engine.ask(record, "پرسشی که هرگز جدول درستی نمی‌سازد", SYSTEM_PROMPT)

        assert backend.call_count == 3  # max_corrections (2) + 1
        assert turn.guard is not None
        assert turn.guard.verdict == "rejected"
        assert turn.guard.reason == "unknown_table"
        assert turn.guard.rejected_sql == "SELECT * FROM ThisTableDoesNotExist_round_3"

    def test_fixed_response_correctable_rejection_still_carries_it(self):
        """The simpler, single-string case (every round emits the exact
        same unresolvable-table query) -- exercises the same code path as
        ``tests/test_correction_loop_policy_rejection.py``'s
        ``CORRECTABLE_SQL`` scenario, with the new field asserted too."""
        response_sql = "SELECT * FROM ThisTableDoesNotExist_zzz"
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response=response_sql)]),
            execute_fn=_never_execute,
            max_corrections=1,
        )

        turn = engine.ask(record, "پرسشی نامعتبر", SYSTEM_PROMPT)

        assert turn.guard is not None
        assert turn.guard.verdict == "rejected"
        assert turn.guard.reason == "unknown_table"
        assert turn.guard.rejected_sql == response_sql


# ---------------------------------------------------------------------------
# 3. An accepted turn never carries a rejected_sql.
# ---------------------------------------------------------------------------


class TestEngineLeavesRejectedSqlNoneOnAcceptedTurn:
    def test_accepted_turn_has_no_rejected_sql(self):
        from schema_data.columns import TABLE_COLUMNS

        any_table = next(iter(TABLE_COLUMNS))
        any_column = next(iter(TABLE_COLUMNS[any_table]))
        sql = f"SELECT TOP 1 {any_column} FROM [{any_table}]"

        def execute(_sql: str) -> pd.DataFrame:
            return pd.DataFrame({any_column: [1]})

        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response=sql)]),
            execute_fn=execute,
        )

        turn = engine.ask(record, "یک پرسش معتبر", SYSTEM_PROMPT)

        assert turn.error is None
        assert turn.guard is not None
        assert turn.guard.verdict == "allowed"
        assert turn.guard.rejected_sql is None


# ---------------------------------------------------------------------------
# 4. session.models.GuardVerdict -- the field itself.
# ---------------------------------------------------------------------------


class TestGuardVerdictRejectedSqlField:
    def test_defaults_to_none(self):
        assert GuardVerdict(verdict="allowed").rejected_sql is None

    def test_round_trips_through_model_dump(self):
        v = GuardVerdict(verdict="rejected", rule="boom", rejected_sql="SELECT @@version")
        assert v.model_dump()["rejected_sql"] == "SELECT @@version"


# ---------------------------------------------------------------------------
# 5. session.persistence round-trip -- present, and absent from an
#    older-shaped stored record.
# ---------------------------------------------------------------------------


class TestPersistenceRoundTrip:
    def test_rejected_sql_round_trips(self, tmp_path):
        persistence = SessionPersistence(str(tmp_path / "sessions.db"))
        try:
            turn = Turn(
                turn_id="t1", session_id="s1", index=1,
                question="q",
                guard=GuardVerdict(
                    verdict="rejected", rule="boom", reason="forbidden_statement",
                    subject="@@VERSION", rejected_sql="SELECT @@version",
                ),
                result=TurnResult(),
            )
            persistence.save_turn("s1", turn, memory=None)

            loaded_turns, _ = persistence.load_turns("s1")
        finally:
            persistence.close()

        assert len(loaded_turns) == 1
        assert loaded_turns[0].guard.rejected_sql == "SELECT @@version"

    def test_turn_without_rejected_sql_still_round_trips_as_none(self, tmp_path):
        turn = Turn(
            turn_id="t2", session_id="s1", index=1,
            question="q",
            guard=GuardVerdict(verdict="allowed"),
        )
        persistence = SessionPersistence(str(tmp_path / "sessions.db"))
        try:
            persistence.save_turn("s1", turn, memory=None)
            loaded_turns, _ = persistence.load_turns("s1")
        finally:
            persistence.close()

        assert len(loaded_turns) == 1
        assert loaded_turns[0].guard.rejected_sql is None

    def test_pre_existing_record_without_the_key_at_all_still_loads(self, tmp_path):
        """Simulates a turn written to disk before this field existed:
        the stored JSON's `guard` object has no `rejected_sql` KEY at all
        (not merely a `null` value) -- ``Turn.model_validate`` must still
        succeed, defaulting the field to ``None`` rather than raising."""
        db_path = tmp_path / "sessions.db"
        persistence = SessionPersistence(str(db_path))
        try:
            old_turn = {
                "turn_id": "t_old", "session_id": "s1", "index": 1,
                "question": "q", "resolved_question": None,
                "basis": {"kind": "fresh", "refines_turn_id": None, "composition": "none", "inherited": []},
                "sql": None, "sql_display": None,
                "ambiguity": {"is_ambiguous": False, "assumptions": [], "clarifications": []},
                "guard": {
                    "verdict": "rejected", "rule": "boom", "reason": None,
                    "subject": None, "injected_top": None, "tables_touched": [],
                },
                "result": None,
                "interpretation": None, "tier": None, "warnings": [],
                "llm": None, "timings": {}, "error": None,
            }
            memory_blank = {
                "filters": {}, "result_columns": [], "sql": None,
                "injected_top": None, "row_count": 0,
            }

            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "INSERT INTO turns (session_id, turn_id, turn_index, turn_json, memory_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "s1", "t_old", 1,
                    json.dumps(old_turn, ensure_ascii=False),
                    json.dumps(memory_blank, ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()

            loaded_turns, _ = persistence.load_turns("s1")
        finally:
            persistence.close()

        assert len(loaded_turns) == 1
        assert loaded_turns[0].guard.rejected_sql is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
