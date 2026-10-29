# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The filter value spelling fix on the two generation paths, ``/query`` and a session turn.

``tests/test_filter_spelling_pins.py`` pins what the same requests do without the
fix. Here, with ``FILTER_VALUE_SPELLING`` on (its default): a literal the model wrote
with Persian letters runs as the column stores it; the statement the user sees is the
one that ran; the audit line carries the count and the column and no value; a statement
the fix does not touch runs as it was written; with the setting off nothing differs; and
with ``FILTER_VALUE_CHECK`` on, the fix runs first, so a spelling difference costs no
model round, and the corrected statement of the check is respelled too.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd
import pytest

from config import override_settings
from core.models import RetrievalContext
from llm.router import LLMRouter, PromptSegments
from llm.sql_agent import SQLAgent
from observability.audit import AuditRecord
from retrieval import dimension_vocabulary as dv
from retrieval.filter_values import check_filter_values, correction_text
from session.engine import TurnEngine
from session.store import SessionStore

SYSTEM_PROMPT = "You are a T-SQL expert."
QUESTION = "total value in the small export hall"
CONTEXT = RetrievalContext(entities=["Ring"], filters={})
RESULT = pd.DataFrame({"Name": ["x"]})
META = {"raw": {}, "endpoint_status": 200, "attempts": 1}


def arabic(text: str) -> str:
    return text.replace("ی", "ي").replace("ک", "ك")


HALL = "تالار صادراتی کوچک"
STORED = arabic(HALL)
OTHER = "تالار صنعتی"
assert HALL != STORED

HEAD = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE "
FIXED_BLOCK = {"fixed": 1, "columns": ["Ring.Name"]}


def eq_sql(value: str) -> str:
    return f"{HEAD}r.[Name] = N'{value}'"


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
    dv.refresh_vocabulary("Ring", "Name", execute_fn=lambda sql, params: pd.DataFrame({"Name": [STORED, OTHER]}))
    yield
    dv.clear_vocabulary_cache()


def run_query_path(responses, *, execute=None, denied_columns=None, max_corrections=2):
    executed: list[str] = []

    def run(sql: str) -> pd.DataFrame:
        executed.append(sql)
        return RESULT.copy() if execute is None else execute(len(executed), sql)

    backend = _Recorder(responses)
    agent = SQLAgent(backend=backend, execute_fn=run, max_corrections=max_corrections)
    with patch("llm.sql_agent.ContextRetriever.retrieve", return_value=CONTEXT):
        df, result = agent.run(QUESTION, SYSTEM_PROMPT, denied_columns=denied_columns)
    return backend, executed, df, result


def run_session_path(responses, *, execute=None, denied_columns=None, max_corrections=2):
    executed: list[str] = []
    records: list[AuditRecord] = []

    def run(sql: str) -> pd.DataFrame:
        executed.append(sql)
        return RESULT.copy() if execute is None else execute(len(executed), sql)

    backend = _Recorder(responses)
    engine = TurnEngine(router=LLMRouter(default_chain=[backend]), execute_fn=run, max_corrections=max_corrections)
    record = SessionStore(ttl_seconds=60, max_size=10, max_turns=10).create()
    with patch("session.engine.ContextRetriever.retrieve", return_value=CONTEXT), patch(
        "session.engine.save_audit_record", records.append,
    ):
        turn = engine.ask(record, QUESTION, SYSTEM_PROMPT, denied_columns=denied_columns)
    return backend, executed, turn, records


# ---------------------------------------------------------------------------
# /query
# ---------------------------------------------------------------------------


class TestQueryPath:
    def test_the_statement_that_runs_and_the_one_reported_are_the_stored_spelling(self):
        backend, executed, df, result = run_query_path([eq_sql(HALL)])

        assert backend.calls == 1
        assert executed == [eq_sql(STORED)]
        assert result.sql == eq_sql(STORED)
        assert result.raw_response == eq_sql(HALL)
        assert result.attempt == 1
        assert result.correction_prompts == []
        assert result.value_spelling == FIXED_BLOCK
        assert result.value_check is None
        assert df.equals(RESULT)

    def test_a_statement_that_needs_nothing_runs_as_written_and_has_no_block(self):
        backend, executed, _df, result = run_query_path([eq_sql(STORED)])

        assert backend.calls == 1
        assert executed == [eq_sql(STORED)]
        assert result.sql == eq_sql(STORED)
        assert result.value_spelling is None

    def test_with_the_setting_off_the_statement_runs_as_written(self):
        with override_settings(filter_value_spelling=False):
            backend, executed, _df, result = run_query_path([eq_sql(HALL)])

        assert backend.calls == 1
        assert executed == [eq_sql(HALL)]
        assert result.sql == eq_sql(HALL)
        assert result.value_spelling is None

    def test_a_like_pattern_gets_the_stored_form(self):
        like = f"{HEAD}r.[Name] LIKE N'%کوچ%'"
        _backend, executed, _df, result = run_query_path([like])

        assert executed == [f"{HEAD}r.[Name] LIKE N'%كوچ%'"]
        assert result.value_spelling == FIXED_BLOCK

    def test_a_denied_column_is_still_refused_and_never_rewritten(self):
        from security.sql_guard import PolicyRejection

        with pytest.raises(PolicyRejection):
            run_query_path([eq_sql(HALL)], denied_columns=["Name"])

    def test_the_fix_is_applied_again_to_the_statement_of_a_correction_round(self):
        def execute(n: int, sql: str) -> pd.DataFrame:
            if n == 1:
                raise RuntimeError("Database error: boom")
            return RESULT.copy()

        backend, executed, _df, result = run_query_path([eq_sql(HALL), eq_sql(HALL)], execute=execute)

        assert backend.calls == 2
        assert executed == [eq_sql(STORED), eq_sql(STORED)]
        assert result.sql == eq_sql(STORED)
        assert result.attempt == 2
        assert result.value_spelling == FIXED_BLOCK
        # the model was shown the statement that failed, which is the one that ran
        assert eq_sql(STORED) in backend.segments[1].question

    def test_a_later_statement_that_needs_nothing_leaves_no_block_behind(self):
        def execute(n: int, sql: str) -> pd.DataFrame:
            if n == 1:
                raise RuntimeError("Database error: boom")
            return RESULT.copy()

        _backend, executed, _df, result = run_query_path([eq_sql(HALL), eq_sql(OTHER)], execute=execute)

        assert executed == [eq_sql(STORED), eq_sql(OTHER)]
        assert result.value_spelling is None

    def test_a_failed_execution_carries_the_block_to_the_audit(self):
        def execute(n: int, sql: str) -> pd.DataFrame:
            raise RuntimeError("Database error: boom")

        with pytest.raises(RuntimeError) as caught:
            run_query_path([eq_sql(HALL)] * 3, execute=execute)

        assert caught.value.value_spelling == FIXED_BLOCK
        assert caught.value.candidate_sql == eq_sql(STORED)

    def test_the_rewritten_statement_is_what_the_result_cache_is_asked_for(self):
        asked: list[str] = []

        def lookup(sql: str):
            asked.append(sql)
            return None

        backend = _Recorder([eq_sql(HALL)])
        agent = SQLAgent(backend=backend, execute_fn=lambda sql: RESULT.copy(), max_corrections=2)
        with patch("llm.sql_agent.ContextRetriever.retrieve", return_value=CONTEXT):
            agent.run(QUESTION, SYSTEM_PROMPT, sql_cache_lookup=lookup)

        assert asked == [eq_sql(STORED)]


class TestQueryPathWithTheValueCheckOn:
    @pytest.fixture(autouse=True)
    def _check_on(self):
        with override_settings(filter_value_check=True):
            yield

    def test_a_spelling_difference_costs_no_model_round_and_the_literal_is_known(self):
        backend, executed, _df, result = run_query_path([eq_sql(HALL)])

        assert backend.calls == 1
        assert executed == [eq_sql(STORED)]
        assert result.attempt == 1
        assert result.value_check == {"checked": 1, "unknown": [], "corrected": False, "fallback": False}
        assert result.value_spelling == FIXED_BLOCK

    def test_the_check_sees_the_fixed_statement(self):
        unknown = f"{HEAD}r.[Name] = N'{HALL}' OR r.[Name] = N'Gamma'"
        fixed = f"{HEAD}r.[Name] = N'{STORED}' OR r.[Name] = N'Gamma'"
        backend, _executed, _df, _result = run_query_path([unknown, eq_sql(OTHER)])

        message = backend.segments[1].question[len(backend.segments[0].question):]
        assert message == "\n" + correction_text(fixed, check_filter_values(fixed))
        assert "Gamma" in message

    def test_the_corrected_statement_is_respelled_before_it_runs(self):
        unknown = f"{HEAD}r.[Name] = N'{HALL}' OR r.[Name] = N'Gamma'"
        # the model answers the correction message with Persian letters again
        backend, executed, _df, result = run_query_path([unknown, eq_sql(HALL)])

        assert backend.calls == 2
        assert executed == [eq_sql(STORED)]
        assert result.sql == eq_sql(STORED)
        assert result.value_check["corrected"] is True
        assert result.value_spelling == FIXED_BLOCK

    def test_the_original_that_runs_after_a_failed_correction_is_the_respelled_one(self):
        unknown = f"{HEAD}r.[Name] = N'{HALL}' OR r.[Name] = N'Gamma'"
        fixed = f"{HEAD}r.[Name] = N'{STORED}' OR r.[Name] = N'Gamma'"
        # the model keeps the value that does not exist
        backend, executed, _df, result = run_query_path([unknown, unknown])

        assert backend.calls == 2
        assert executed == [fixed]
        assert result.sql == fixed
        assert result.value_check["fallback"] is True
        assert result.value_spelling == FIXED_BLOCK


class TestOnTheEndpoint:
    @pytest.fixture()
    def audit_file(self, tmp_path):
        path = tmp_path / "audit_log.jsonl"
        with patch("observability.audit._AUDIT_LOG_FILE", str(path)):
            yield path

    def _ask(self, sql: str):
        from api.query_cache import query_cache
        from api.runner import run_query

        query_cache.clear()
        executed: list[str] = []

        def run(statement: str) -> pd.DataFrame:
            executed.append(statement)
            return RESULT.copy()

        agent = SQLAgent(backend=_Recorder([sql]), execute_fn=run, max_corrections=2)
        with patch("api.runner.agent", agent):
            response = run_query(QUESTION, system_prompt=SYSTEM_PROMPT, mode="full", request_id="r_spell")
        query_cache.clear()
        return response, executed

    def test_the_response_and_the_audit_line_say_what_ran_and_hold_no_value(self, audit_file):
        response, executed = self._ask(eq_sql(HALL))

        record = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert executed == [eq_sql(STORED)]
        assert response.sql == eq_sql(STORED)
        assert record["generated_sql"] == eq_sql(STORED)
        assert record["value_spelling"] == FIXED_BLOCK
        assert "value_check" not in record
        assert record["guard"]["tables_touched"] == ["Ring"]
        assert record["llm"]["corrections"] == 0
        # names, never values: the literal and the stored value appear only in the statement itself
        assert HALL not in json.dumps({**record, "generated_sql": ""}, ensure_ascii=False)
        assert STORED not in json.dumps({**record, "generated_sql": ""}, ensure_ascii=False)

    def test_a_request_that_needed_no_fix_writes_the_line_it_always_did(self, audit_file):
        response, executed = self._ask(eq_sql(STORED))

        record = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert executed == [eq_sql(STORED)]
        assert response.sql == eq_sql(STORED)
        assert "value_spelling" not in record


# ---------------------------------------------------------------------------
# A session turn
# ---------------------------------------------------------------------------


class TestSessionPath:
    def test_the_turn_reports_and_runs_the_stored_spelling(self):
        backend, executed, turn, records = run_session_path([eq_sql(HALL)])

        assert turn.error is None
        assert backend.calls == 1
        assert executed == [eq_sql(STORED)]
        assert turn.sql == eq_sql(STORED)
        assert STORED in turn.sql_display and HALL not in turn.sql_display
        assert turn.guard.verdict == "allowed"
        assert turn.guard.tables_touched == ["Ring"]
        assert turn.llm["corrections"] == 0
        assert turn.warnings == []
        assert len(records) == 1
        assert records[0].generated_sql == eq_sql(STORED)
        assert records[0].value_spelling == FIXED_BLOCK
        assert records[0].value_check is None

    def test_a_statement_that_needs_nothing_has_the_audit_line_it_always_had(self):
        _backend, executed, turn, records = run_session_path([eq_sql(STORED)])

        assert executed == [eq_sql(STORED)]
        assert turn.sql == eq_sql(STORED)
        assert records[0].value_spelling is None
        assert "value_spelling" not in records[0].as_dict()

    def test_with_the_setting_off_the_turn_is_as_it_was(self):
        with override_settings(filter_value_spelling=False):
            _backend, executed, turn, records = run_session_path([eq_sql(HALL)])

        assert executed == [eq_sql(HALL)]
        assert turn.sql == eq_sql(HALL)
        assert records[0].value_spelling is None
        assert "value_spelling" not in records[0].as_dict()

    def test_a_like_pattern_gets_the_stored_form(self):
        like = f"{HEAD}r.[Name] LIKE N'%کوچ%'"
        _backend, executed, turn, _records = run_session_path([like])

        assert executed == [f"{HEAD}r.[Name] LIKE N'%كوچ%'"]
        assert turn.sql == executed[0]

    def test_a_denied_column_is_still_refused(self):
        _backend, executed, turn, _records = run_session_path([eq_sql(HALL)], denied_columns=("Name",))

        assert executed == []
        assert turn.guard.verdict == "rejected"

    def test_the_fix_is_applied_again_in_a_correction_round(self):
        def execute(n: int, sql: str) -> pd.DataFrame:
            if n == 1:
                raise RuntimeError("Database error: boom")
            return RESULT.copy()

        backend, executed, turn, records = run_session_path([eq_sql(HALL), eq_sql(HALL)], execute=execute)

        assert backend.calls == 2
        assert executed == [eq_sql(STORED), eq_sql(STORED)]
        assert turn.sql == eq_sql(STORED)
        assert records[0].value_spelling == FIXED_BLOCK

    def test_a_later_statement_that_needs_nothing_leaves_no_block_behind(self):
        def execute(n: int, sql: str) -> pd.DataFrame:
            if n == 1:
                raise RuntimeError("Database error: boom")
            return RESULT.copy()

        _backend, executed, _turn, records = run_session_path([eq_sql(HALL), eq_sql(OTHER)], execute=execute)

        assert executed == [eq_sql(STORED), eq_sql(OTHER)]
        assert records[0].value_spelling is None


class TestSessionPathWithTheValueCheckOn:
    @pytest.fixture(autouse=True)
    def _check_on(self):
        with override_settings(filter_value_check=True):
            yield

    def test_a_spelling_difference_costs_no_model_round(self):
        backend, executed, turn, records = run_session_path([eq_sql(HALL)])

        assert backend.calls == 1
        assert executed == [eq_sql(STORED)]
        assert turn.llm["corrections"] == 0
        assert records[0].value_check == {"checked": 1, "unknown": [], "corrected": False, "fallback": False}
        assert records[0].value_spelling == FIXED_BLOCK

    def test_the_check_sees_the_fixed_statement(self):
        unknown = f"{HEAD}r.[Name] = N'{HALL}' OR r.[Name] = N'Gamma'"
        fixed = f"{HEAD}r.[Name] = N'{STORED}' OR r.[Name] = N'Gamma'"
        backend, _executed, _turn, _records = run_session_path([unknown, eq_sql(OTHER)])

        suffix = backend.segments[1].question[len(backend.segments[0].question):]
        assert suffix == "\n\n" + correction_text(fixed, check_filter_values(fixed))

    def test_the_corrected_statement_is_respelled_before_it_runs(self):
        unknown = f"{HEAD}r.[Name] = N'{HALL}' OR r.[Name] = N'Gamma'"
        backend, executed, turn, records = run_session_path([unknown, eq_sql(HALL)])

        assert backend.calls == 2
        assert executed == [eq_sql(STORED)]
        assert turn.sql == eq_sql(STORED)
        assert records[0].value_check["corrected"] is True
        assert records[0].value_spelling == FIXED_BLOCK

    def test_the_original_that_runs_after_a_failed_correction_is_the_respelled_one(self):
        unknown = f"{HEAD}r.[Name] = N'{HALL}' OR r.[Name] = N'Gamma'"
        fixed = f"{HEAD}r.[Name] = N'{STORED}' OR r.[Name] = N'Gamma'"
        _backend, executed, turn, records = run_session_path([unknown, unknown])

        assert executed == [fixed]
        assert turn.sql == fixed
        assert records[0].value_check["fallback"] is True
        assert records[0].value_spelling == FIXED_BLOCK
