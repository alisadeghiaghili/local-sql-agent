# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The filter value check in a session turn (``session.engine.TurnEngine``), setting on.

``tests/test_filter_value_check_pins.py`` pins what the same turns do with the
setting off. Here: one correction message when a literal is not a value of its
column, the corrected statement runs only when it is clean, keeps the filters
the question named and works, and every other outcome runs the original
statement as the loop always did. The cases mirror
``tests/test_filter_value_check_query.py`` for the other generation path.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from config import override_settings
from core.models import RetrievalContext
from llm.router import LLMRouter, PromptSegments, build_prompt_segments
from observability.audit import AuditRecord
from retrieval import dimension_vocabulary as dv
from retrieval.filter_values import check_filter_values, correction_text
from session.engine import TurnEngine
from session.store import SessionStore

SYSTEM_PROMPT = "You are a T-SQL expert."
QUESTION = "total value in Gamma Hall"
CONTEXT = RetrievalContext(entities=["Ring"], filters={})
RESULT = pd.DataFrame({"Name": ["x"]})
META = {"raw": {}, "endpoint_status": 200, "attempts": 1}

UNKNOWN_SQL = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Gamma Hall'"
CORRECTED_SQL = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Alpha Hall'"
REMOVED_SQL = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r"
OTHER_UNKNOWN_SQL = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Delta Hall'"

#: Every key of an audit line without the check, then with it.
BASE_KEYS = [
    "timestamp", "request_id", "question", "generated_sql", "guard", "row_count", "tier",
    "error_code", "error_message", "timings", "llm", "columns", "principal_id",
    "resolved_columns", "session_id", "turn_id", "config_version_id", "assumptions",
    "datasource", "datasource_selection",
]


class _Recorder:
    name = "recorder"

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.segments: list[PromptSegments] = []

    @property
    def calls(self) -> int:
        return len(self.segments)

    def generate_with_meta_segments(self, segments: PromptSegments):
        self.segments.append(segments)
        response = self._responses[len(self.segments) - 1]
        if isinstance(response, Exception):
            raise response
        return response, dict(META)


@pytest.fixture(autouse=True)
def _vocabulary():
    dv.clear_vocabulary_cache()
    dv.refresh_vocabulary(
        "Ring", "Name",
        execute_fn=lambda sql, params: pd.DataFrame({"Name": ["Alpha Hall", "Beta Hall"]}),
    )
    yield
    dv.clear_vocabulary_cache()


@pytest.fixture(autouse=True)
def _check_on():
    with override_settings(filter_value_check=True):
        yield


def _ask(
    responses, execute=None, *, max_corrections=2, denied_columns=None, context=CONTEXT, monkeypatch=None,
):
    executed: list[str] = []
    records: list[AuditRecord] = []

    def run(sql: str) -> pd.DataFrame:
        executed.append(sql)
        return RESULT.copy() if execute is None else execute(len(executed), sql)

    backend = _Recorder(responses)
    engine = TurnEngine(router=LLMRouter(default_chain=[backend]), execute_fn=run, max_corrections=max_corrections)
    record = SessionStore(ttl_seconds=60, max_size=10, max_turns=10).create()
    with patch("session.engine.ContextRetriever.retrieve", return_value=context), patch(
        "session.engine.save_audit_record", records.append,
    ):
        turn = engine.ask(record, QUESTION, SYSTEM_PROMPT, denied_columns=denied_columns)
    return backend, executed, turn, records


def _message(sql: str) -> str:
    return "\n\n" + correction_text(sql, check_filter_values(sql))


class TestUnknownValueIsCorrectedOnce:
    def test_the_corrected_statement_runs_and_the_original_never_does(self):
        backend, executed, turn, _records = _ask([UNKNOWN_SQL, CORRECTED_SQL])

        assert turn.error is None
        assert backend.calls == 2
        assert executed == [CORRECTED_SQL]
        assert turn.sql == CORRECTED_SQL
        assert turn.warnings == []
        assert turn.guard.verdict == "allowed"

    def test_the_message_is_appended_to_the_question_segment_only(self):
        backend, _executed, _turn, _records = _ask([UNKNOWN_SQL, CORRECTED_SQL])

        first, second = backend.segments
        assert second.question == first.question + _message(UNKNOWN_SQL)
        assert "Ring.Name: the value 'Gamma Hall' does not exist in this column." in second.question
        assert "remove this filter" in second.question
        assert second.static_prefix == first.static_prefix
        assert second.static_prefix == build_prompt_segments(QUESTION, SYSTEM_PROMPT, CONTEXT).static_prefix
        assert second.session_context == first.session_context

    def test_it_is_counted_with_the_corrections(self):
        _backend, _executed, turn, _records = _ask([UNKNOWN_SQL, CORRECTED_SQL])

        assert turn.llm["corrections"] == 1

    def test_the_audit_line_carries_the_block_after_the_existing_keys(self):
        _backend, _executed, turn, records = _ask([UNKNOWN_SQL, CORRECTED_SQL])

        (record,) = records
        assert list(record.as_dict()) == [*BASE_KEYS, "value_check"]
        block = record.value_check
        assert block["checked"] == 1 and block["corrected"] is True and block["fallback"] is False
        assert [u["column"] for u in block["unknown"]] == ["Ring.Name"]
        assert record.generated_sql == turn.sql == CORRECTED_SQL
        assert "Gamma" not in str(block) and "Alpha" not in str(block)

    def test_removing_the_filter_is_a_correction_too(self):
        backend, executed, turn, _records = _ask([UNKNOWN_SQL, REMOVED_SQL])

        assert backend.calls == 2
        assert executed == [REMOVED_SQL]
        assert turn.sql == REMOVED_SQL

    def test_the_display_form_is_the_corrected_statements(self):
        _backend, _executed, turn, _records = _ask([UNKNOWN_SQL, CORRECTED_SQL])

        assert "Alpha Hall" in turn.sql_display and "Gamma Hall" not in turn.sql_display

    def test_a_row_cap_is_added_to_the_corrected_statement_like_any_other(self):
        no_top = "SELECT r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Alpha Hall'"

        _backend, executed, turn, _records = _ask([UNKNOWN_SQL, no_top])

        assert executed[0].startswith("SELECT TOP ")
        assert turn.guard.injected_top is not None


class TestEveryOtherOutcomeRunsTheOriginal:
    def test_the_model_keeps_the_value(self):
        backend, executed, turn, records = _ask([UNKNOWN_SQL, UNKNOWN_SQL])

        assert turn.error is None
        assert backend.calls == 2
        assert executed == [UNKNOWN_SQL]
        assert turn.sql == UNKNOWN_SQL
        assert turn.llm["corrections"] == 1
        assert records[0].value_check["corrected"] is False
        assert records[0].value_check["fallback"] is True

    def test_the_model_swaps_one_unknown_value_for_another(self):
        _backend, executed, turn, _records = _ask([UNKNOWN_SQL, OTHER_UNKNOWN_SQL])

        assert executed == [UNKNOWN_SQL]
        assert turn.sql == UNKNOWN_SQL

    def test_the_corrected_statement_is_refused_by_the_guard(self):
        backend, executed, turn, _records = _ask([UNKNOWN_SQL, "SELECT TOP 5 r.[Nope] FROM [ref].[Ring] AS r"])

        assert turn.error is None and turn.guard.verdict == "allowed"
        assert backend.calls == 2
        assert executed == [UNKNOWN_SQL]
        assert turn.sql == UNKNOWN_SQL

    def test_the_corrected_statement_is_not_a_statement(self):
        _backend, executed, turn, _records = _ask([UNKNOWN_SQL, "I cannot help with that."])

        assert executed == [UNKNOWN_SQL]
        assert turn.sql == UNKNOWN_SQL

    def test_the_corrected_statement_fails_at_the_database(self):
        def execute(n, sql):
            if sql == CORRECTED_SQL:
                raise RuntimeError("Database error: boom")
            return RESULT.copy()

        backend, executed, turn, _records = _ask([UNKNOWN_SQL, CORRECTED_SQL], execute)

        assert turn.error is None
        assert backend.calls == 2
        assert executed == [CORRECTED_SQL, UNKNOWN_SQL]
        assert turn.sql == UNKNOWN_SQL

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("model unreachable"), ValueError("OUT_OF_SCOPE"), TimeoutError("slow")],
        ids=["transport", "out-of-scope", "timeout"],
    )
    def test_the_model_cannot_be_asked_again(self, error):
        backend, executed, turn, _records = _ask([UNKNOWN_SQL, error])

        assert turn.error is None
        assert backend.calls == 2
        assert executed == [UNKNOWN_SQL]
        assert turn.sql == UNKNOWN_SQL

    def test_no_correction_budget_no_extra_call(self):
        backend, executed, turn, records = _ask([UNKNOWN_SQL], max_corrections=0)

        assert backend.calls == 1
        assert executed == [UNKNOWN_SQL]
        assert turn.llm["corrections"] == 0
        assert records[0].value_check["fallback"] is True

    def test_the_original_that_then_fails_goes_through_the_ordinary_correction_unchanged(self):
        def execute(n, sql):
            if n == 1:
                raise RuntimeError("Database error: boom")
            return RESULT.copy()

        backend, executed, turn, _records = _ask([UNKNOWN_SQL, UNKNOWN_SQL, CORRECTED_SQL], execute)

        ordinary = (
            "\n\nThe SQL query you generated failed:\n"
            "--- FAILED SQL ---\n"
            f"{UNKNOWN_SQL}\n"
            "--- ERROR ---\n"
            "Database error: boom\n"
            "--- INSTRUCTIONS ---\n"
            "Fix ONLY the error above. Return only the corrected SQL statement.\n\n"
            "SQL:\n"
        )
        assert turn.error is None
        assert backend.calls == 3
        assert backend.segments[2].question == backend.segments[0].question + ordinary
        assert executed == [UNKNOWN_SQL, CORRECTED_SQL]
        assert turn.sql == CORRECTED_SQL
        # the value round and the ordinary round are both counted
        assert turn.llm["corrections"] == 2

    def test_even_at_the_last_round_it_adds_one_call_and_never_a_refusal(self):
        backend, executed, turn, _records = _ask(
            ["SELECT TOP 5 nope FROM", UNKNOWN_SQL, UNKNOWN_SQL], max_corrections=1,
        )

        assert turn.error is None
        assert backend.calls == 3
        assert executed == [UNKNOWN_SQL]

    def test_a_corrected_statement_is_used_even_at_the_last_round(self):
        backend, executed, turn, _records = _ask(
            ["SELECT TOP 5 nope FROM", UNKNOWN_SQL, CORRECTED_SQL], max_corrections=1,
        )

        assert backend.calls == 3
        assert executed == [CORRECTED_SQL]
        assert turn.sql == CORRECTED_SQL

    def test_only_once_per_turn(self):
        def execute(n, sql):
            if n == 1:
                raise RuntimeError("Database error: boom")
            return RESULT.copy()

        backend, executed, turn, _records = _ask(
            [UNKNOWN_SQL, UNKNOWN_SQL, OTHER_UNKNOWN_SQL], execute,
        )

        assert backend.calls == 3
        assert executed == [UNKNOWN_SQL, OTHER_UNKNOWN_SQL]
        assert turn.sql == OTHER_UNKNOWN_SQL


class TestFiltersTheQuestionNamed:
    BROKER_UNKNOWN = (
        "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r, [ref].[Broker] AS b "
        "WHERE r.[Name] = N'Alpha Hall' AND b.[PersianName] = N'Nope'"
    )
    CONTEXT = RetrievalContext(entities=["Ring"], filters={"Ring": "Alpha Hall"})

    @pytest.fixture(autouse=True)
    def _broker(self):
        dv.refresh_vocabulary(
            "Broker", "PersianName", execute_fn=lambda sql, params: pd.DataFrame({"PersianName": ["Broker One"]}),
        )

    def test_a_correction_that_drops_a_named_filter_is_not_used(self):
        drops = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r, [ref].[Broker] AS b WHERE b.[PersianName] = N'Broker One'"

        backend, executed, turn, _records = _ask([self.BROKER_UNKNOWN, drops], context=self.CONTEXT)

        assert backend.calls == 2
        assert executed == [self.BROKER_UNKNOWN]
        assert turn.sql == self.BROKER_UNKNOWN
        assert turn.warnings == []

    def test_a_correction_that_keeps_it_is_used(self):
        keeps = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Alpha Hall'"

        backend, executed, turn, _records = _ask([self.BROKER_UNKNOWN, keeps], context=self.CONTEXT)

        assert backend.calls == 2
        assert executed == [keeps]
        assert turn.sql == keeps

    def test_the_filter_enforcement_runs_first_and_the_value_check_on_what_it_produces(self):
        omits = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r"

        backend, executed, turn, _records = _ask(
            [omits, UNKNOWN_SQL, CORRECTED_SQL], context=RetrievalContext(entities=["Ring"], filters={"Ring": "Alpha Hall"}),
        )

        assert backend.calls == 3
        assert "does not filter on every value" in backend.segments[1].question
        assert "does not exist" in backend.segments[2].question
        assert executed == [CORRECTED_SQL]
        assert turn.llm["corrections"] == 2


class TestNothingToCheck:
    def test_a_known_value(self):
        backend, executed, turn, records = _ask([CORRECTED_SQL])

        assert backend.calls == 1
        assert executed == [CORRECTED_SQL]
        assert turn.llm["corrections"] == 0
        assert records[0].value_check == {"checked": 1, "unknown": [], "corrected": False, "fallback": False}

    def test_a_column_with_no_vocabulary_is_not_checked_and_the_line_has_no_new_key(self):
        dv.clear_vocabulary_cache()

        backend, executed, turn, records = _ask([UNKNOWN_SQL])

        assert backend.calls == 1
        assert executed == [UNKNOWN_SQL]
        assert list(records[0].as_dict()) == BASE_KEYS

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] LIKE N'%Gamma%'",
            "SELECT TOP 5 o.ID FROM [sales].[Order] AS o WHERE o.TotalAmount > 1000 AND o.ID = 7",
        ],
        ids=["like", "numbers"],
    )
    def test_like_and_numbers_are_ignored(self, sql):
        backend, executed, _turn, records = _ask([sql])

        assert backend.calls == 1
        assert executed == [sql]
        assert list(records[0].as_dict()) == BASE_KEYS


class TestWhatTheCallerMaySee:
    def test_a_denied_column_still_ends_the_turn_at_the_guard_with_no_extra_call(self):
        backend, executed, turn, _records = _ask([UNKNOWN_SQL], denied_columns=("Name",))

        assert backend.calls == 1
        assert executed == []
        assert turn.guard.verdict == "rejected"

    def test_another_denied_column_changes_nothing(self):
        backend, executed, _turn, _records = _ask([UNKNOWN_SQL, CORRECTED_SQL], denied_columns=("NationalID",))

        assert backend.calls == 2
        assert executed == [CORRECTED_SQL]


class TestRefinementPathIsLeftAlone:
    def test_a_cte_refinement_is_not_checked(self):
        from session.models import ResultColumn, Turn, TurnResult
        from session.store import TurnMemory

        record = SessionStore(ttl_seconds=60, max_size=10, max_turns=10).create()
        previous = Turn(
            turn_id="t_prev", session_id=record.session_id, index=1, question="q1",
            sql="SELECT TOP 10 r.Name AS Name FROM [ref].[Ring] r",
            result=TurnResult(columns=[ResultColumn(name="Name", type="string")], row_count=0),
        )
        record.turns.append(previous)
        record.memory["t_prev"] = TurnMemory(turn_id="t_prev", filters={})
        backend = _Recorder(["SELECT TOP 10 Name FROM _prev WHERE Name = N'Gamma Hall'"])
        engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]), execute_fn=lambda sql: RESULT.copy(), max_corrections=2,
        )

        with patch("session.engine.save_audit_record", lambda r: None):
            turn = engine.ask(record, "از بین آن‌ها ۱۰ مشتری برتر به لحاظ حجم معامله", SYSTEM_PROMPT)

        assert turn.basis.composition == "cte"
        assert backend.calls == 1
