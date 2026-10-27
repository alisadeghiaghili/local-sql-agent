# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The filter value check on the ``/query`` path (``llm.sql_agent.SQLAgent``), setting on.

``tests/test_filter_value_check_pins.py`` pins what the same requests do with
the setting off. Here: one correction message when a literal is not a value of
its column, the corrected statement runs only when it is clean and works, and
every other outcome runs the original statement the way the loop always did.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd
import pytest

from config import override_settings
from core.models import RetrievalContext
from llm.router import PromptSegments, build_prompt_segments
from llm.sql_agent import SQLAgent
from retrieval import dimension_vocabulary as dv
from retrieval.filter_values import check_filter_values, correction_text
from security.sql_guard import PolicyRejection

SYSTEM_PROMPT = "You are a T-SQL expert."
QUESTION = "total value in Gamma Hall"
CONTEXT = RetrievalContext(entities=["Ring"], filters={})
RESULT = pd.DataFrame({"Name": ["x"]})

UNKNOWN_SQL = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Gamma Hall'"
CORRECTED_SQL = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Alpha Hall'"
REMOVED_SQL = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r"
OTHER_UNKNOWN_SQL = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Delta Hall'"
META = {"raw": {}, "endpoint_status": 200, "attempts": 1}


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


def _run(responses, execute=None, *, max_corrections=2, denied_columns=None, sql_cache_lookup=None):
    executed: list[str] = []

    def default_execute(sql: str) -> pd.DataFrame:
        executed.append(sql)
        return RESULT.copy()

    def recording(sql: str) -> pd.DataFrame:
        executed.append(sql)
        return execute(len(executed), sql)

    backend = _Recorder(responses)
    agent = SQLAgent(
        backend=backend, execute_fn=default_execute if execute is None else recording,
        max_corrections=max_corrections,
    )
    with patch("llm.sql_agent.ContextRetriever.retrieve", return_value=CONTEXT):
        df, result = agent.run(
            QUESTION, SYSTEM_PROMPT, denied_columns=denied_columns, sql_cache_lookup=sql_cache_lookup,
        )
    return backend, executed, df, result


def _message(sql: str) -> str:
    return "\n" + correction_text(sql, check_filter_values(sql))


class TestUnknownValueIsCorrectedOnce:
    def test_the_corrected_statement_runs_and_the_original_never_does(self):
        backend, executed, df, result = _run([UNKNOWN_SQL, CORRECTED_SQL])

        assert backend.calls == 2
        assert executed == [CORRECTED_SQL]
        assert result.sql == CORRECTED_SQL
        assert result.raw_response == CORRECTED_SQL
        assert df.equals(RESULT)

    def test_the_message_names_the_column_the_value_and_the_nearest_real_ones(self):
        backend, _executed, _df, _result = _run([UNKNOWN_SQL, CORRECTED_SQL])

        message = backend.segments[1].question[len(backend.segments[0].question):]

        assert message == _message(UNKNOWN_SQL)
        assert "Ring.Name: the value 'Gamma Hall' does not exist in this column." in message
        nearest = [ln for ln in message.splitlines() if ln.startswith("Ring.Name, nearest to 'Gamma Hall': ")]
        assert len(nearest) == 1
        assert "Alpha Hall" in nearest[0] and "Beta Hall" in nearest[0]
        assert "remove this filter" in message

    def test_the_static_prefix_and_the_session_context_are_byte_for_byte_the_first_ones(self):
        backend, _executed, _df, _result = _run([UNKNOWN_SQL, CORRECTED_SQL])

        first, second = backend.segments
        assert second.static_prefix == first.static_prefix
        assert second.static_prefix == build_prompt_segments(QUESTION, SYSTEM_PROMPT, CONTEXT).static_prefix
        assert second.session_context == first.session_context
        assert second.question.startswith(first.question)

    def test_it_is_counted_with_the_corrections(self):
        _backend, _executed, _df, result = _run([UNKNOWN_SQL, CORRECTED_SQL])

        assert result.attempt == 2
        assert result.correction_prompts == [_message(UNKNOWN_SQL)]

    def test_the_audit_block_says_what_happened_without_the_values(self):
        _backend, _executed, _df, result = _run([UNKNOWN_SQL, CORRECTED_SQL])

        block = result.value_check
        assert block["checked"] == 1
        assert block["corrected"] is True and block["fallback"] is False
        assert [(u["column"], u["length"]) for u in block["unknown"]] == [("Ring.Name", len("Gamma Hall"))]
        assert "Gamma" not in json.dumps(block) and "Alpha" not in json.dumps(block)

    def test_removing_the_filter_is_a_correction_too(self):
        backend, executed, _df, result = _run([UNKNOWN_SQL, REMOVED_SQL])

        assert backend.calls == 2
        assert executed == [REMOVED_SQL]
        assert result.sql == REMOVED_SQL

    def test_the_corrected_statement_goes_through_the_sql_cache_hook(self):
        looked_up: list[str] = []

        def lookup(sql: str):
            looked_up.append(sql)
            return RESULT.copy() if sql == CORRECTED_SQL else None

        _backend, executed, _df, result = _run([UNKNOWN_SQL, CORRECTED_SQL], sql_cache_lookup=lookup)

        assert looked_up == [CORRECTED_SQL]
        assert executed == []
        assert result.sql == CORRECTED_SQL

    def test_a_row_cap_is_added_to_the_corrected_statement_like_any_other(self):
        no_top = "SELECT r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Alpha Hall'"

        _backend, executed, _df, result = _run([UNKNOWN_SQL, no_top])

        assert executed[0].startswith("SELECT TOP ")
        assert result.injected_top is not None

    def test_only_once_per_request(self):
        def execute(n, sql):
            if n == 1:
                raise RuntimeError("Database error: boom")
            return RESULT.copy()

        backend, executed, _df, result = _run(
            [UNKNOWN_SQL, UNKNOWN_SQL, OTHER_UNKNOWN_SQL], execute,
        )

        # first statement, the one value correction (kept the value), the
        # execution-failure retry -- whose statement has an unknown value too
        # and is executed as it is
        assert backend.calls == 3
        assert executed == [UNKNOWN_SQL, OTHER_UNKNOWN_SQL]
        assert result.sql == OTHER_UNKNOWN_SQL


class TestEveryOtherOutcomeRunsTheOriginal:
    def test_the_model_keeps_the_value(self):
        backend, executed, _df, result = _run([UNKNOWN_SQL, UNKNOWN_SQL])

        assert backend.calls == 2
        assert executed == [UNKNOWN_SQL]
        assert result.sql == UNKNOWN_SQL
        assert result.attempt == 2
        assert result.value_check["corrected"] is False and result.value_check["fallback"] is True

    def test_the_model_swaps_one_unknown_value_for_another(self):
        _backend, executed, _df, result = _run([UNKNOWN_SQL, OTHER_UNKNOWN_SQL])

        assert executed == [UNKNOWN_SQL]
        assert result.sql == UNKNOWN_SQL

    def test_the_corrected_statement_is_refused_by_the_guard(self):
        bad = "SELECT TOP 5 r.[Nope] FROM [ref].[Ring] AS r"

        backend, executed, _df, result = _run([UNKNOWN_SQL, bad])

        assert backend.calls == 2
        assert executed == [UNKNOWN_SQL]
        assert result.sql == UNKNOWN_SQL
        assert result.value_check["fallback"] is True

    def test_the_corrected_statement_is_not_a_statement(self):
        _backend, executed, _df, result = _run([UNKNOWN_SQL, "I cannot help with that."])

        assert executed == [UNKNOWN_SQL]
        assert result.sql == UNKNOWN_SQL

    def test_the_corrected_statement_fails_at_the_database(self):
        def execute(n, sql):
            if sql == CORRECTED_SQL:
                raise RuntimeError("Database error: boom")
            return RESULT.copy()

        backend, executed, _df, result = _run([UNKNOWN_SQL, CORRECTED_SQL], execute)

        assert backend.calls == 2
        assert executed == [CORRECTED_SQL, UNKNOWN_SQL]
        assert result.sql == UNKNOWN_SQL
        assert result.value_check["fallback"] is True

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("model unreachable"), ValueError("OUT_OF_SCOPE"), TimeoutError("slow")],
        ids=["transport", "out-of-scope", "timeout"],
    )
    def test_the_model_cannot_be_asked_again(self, error):
        backend, executed, _df, result = _run([UNKNOWN_SQL, error])

        assert backend.calls == 2
        assert executed == [UNKNOWN_SQL]
        assert result.sql == UNKNOWN_SQL

    def test_no_correction_budget_no_extra_call(self):
        backend, executed, _df, result = _run([UNKNOWN_SQL], max_corrections=0)

        assert backend.calls == 1
        assert executed == [UNKNOWN_SQL]
        assert result.attempt == 1
        assert result.value_check["fallback"] is True

    def test_the_original_that_then_fails_goes_through_the_ordinary_correction_unchanged(self):
        def execute(n, sql):
            if n == 1:
                raise RuntimeError("Database error: boom")
            return RESULT.copy()

        backend, executed, _df, result = _run([UNKNOWN_SQL, UNKNOWN_SQL, CORRECTED_SQL], execute)

        ordinary = (
            "\nThe SQL query you generated failed to execute.\n\n"
            "--- FAILED SQL ---\n"
            f"{UNKNOWN_SQL}\n\n"
            "--- DATABASE ERROR ---\n"
            "Database error: boom\n\n"
            "--- INSTRUCTIONS ---\n"
            "Fix ONLY the SQL error above. Do not change the intent of the query.\n"
            "Return only the corrected SQL statement with no explanation.\n\n"
            "SQL:\n"
        )
        assert backend.calls == 3
        # what the third call is told is what it would have been told without the check
        assert backend.segments[2].question == backend.segments[0].question + ordinary
        assert executed == [UNKNOWN_SQL, CORRECTED_SQL]
        assert result.sql == CORRECTED_SQL
        assert result.correction_prompts == [ordinary, _message(UNKNOWN_SQL)]

    def test_even_at_the_last_round_it_adds_one_call_and_never_a_refusal(self):
        bad = "SELECT TOP 5 nope FROM"

        backend, executed, _df, result = _run([bad, UNKNOWN_SQL, UNKNOWN_SQL], max_corrections=1)

        assert backend.calls == 3
        assert executed == [UNKNOWN_SQL]
        assert result.sql == UNKNOWN_SQL


class TestNothingToCheck:
    def test_a_known_value(self):
        backend, executed, _df, result = _run([CORRECTED_SQL])

        assert backend.calls == 1
        assert executed == [CORRECTED_SQL]
        assert result.attempt == 1
        assert result.correction_prompts == []
        assert result.value_check == {"checked": 1, "unknown": [], "corrected": False, "fallback": False}

    def test_a_column_with_no_vocabulary_is_not_checked(self):
        dv.clear_vocabulary_cache()

        backend, executed, _df, result = _run([UNKNOWN_SQL])

        assert backend.calls == 1
        assert executed == [UNKNOWN_SQL]
        assert result.value_check is None

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] LIKE N'%Gamma%'",
            "SELECT TOP 5 o.ID FROM [sales].[Order] AS o WHERE o.TotalAmount > 1000 AND o.ID = 7",
        ],
        ids=["like", "numbers"],
    )
    def test_like_and_numbers_are_ignored(self, sql):
        backend, executed, _df, result = _run([sql])

        assert backend.calls == 1
        assert executed == [sql]
        assert result.value_check is None


class TestWhatTheCallerMaySee:
    def test_a_denied_column_still_ends_the_request_at_the_guard_with_no_extra_call(self):
        backend = _Recorder([UNKNOWN_SQL])
        agent = SQLAgent(backend=backend, execute_fn=lambda sql: RESULT.copy(), max_corrections=2)

        with patch("llm.sql_agent.ContextRetriever.retrieve", return_value=CONTEXT):
            with pytest.raises(PolicyRejection):
                agent.run(QUESTION, SYSTEM_PROMPT, denied_columns=["Name"])

        assert backend.calls == 1

    def test_another_denied_column_changes_nothing(self):
        backend, executed, _df, _result = _run([UNKNOWN_SQL, CORRECTED_SQL], denied_columns=["NationalID"])

        assert backend.calls == 2
        assert executed == [CORRECTED_SQL]


class TestOnTheEndpoint:
    @pytest.fixture()
    def audit_file(self, tmp_path):
        path = tmp_path / "audit_log.jsonl"
        with patch("observability.audit._AUDIT_LOG_FILE", str(path)):
            yield path

    @staticmethod
    def _query(agent):
        from api.query_cache import query_cache
        from api.runner import run_query

        query_cache.clear()
        try:
            with patch("api.runner.agent", agent), patch(
                "llm.sql_agent.ContextRetriever.retrieve", return_value=CONTEXT,
            ):
                return run_query(QUESTION, system_prompt=SYSTEM_PROMPT, mode="full", request_id="r_value")
        finally:
            query_cache.clear()

    def test_the_audit_line_carries_the_block_and_counts_the_correction(self, audit_file):
        agent = SQLAgent(
            backend=_Recorder([UNKNOWN_SQL, CORRECTED_SQL]),
            execute_fn=lambda sql: RESULT.copy(), max_corrections=2,
        )

        response = self._query(agent)

        record = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert record["generated_sql"] == CORRECTED_SQL
        assert record["llm"]["corrections"] == 1
        assert record["value_check"]["corrected"] is True
        assert response.correction_attempts == 2
        assert response.sql == CORRECTED_SQL
        # the question and the statement are recorded as ever; the block holds no value
        assert "Gamma" not in json.dumps(record["value_check"])
        assert "Alpha" not in json.dumps(record["value_check"])

    def test_a_request_that_fails_later_still_says_what_the_check_did(self, audit_file):
        from api.errors import NLQError

        def failing(sql: str) -> pd.DataFrame:
            raise RuntimeError("Database error: boom")

        agent = SQLAgent(
            backend=_Recorder([UNKNOWN_SQL, UNKNOWN_SQL, UNKNOWN_SQL]),
            execute_fn=failing, max_corrections=1,
        )

        with pytest.raises(NLQError):
            self._query(agent)

        record = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert record["error_code"] is not None
        assert record["value_check"]["fallback"] is True

    def test_a_known_value_leaves_a_block_with_nothing_unknown(self, audit_file):
        agent = SQLAgent(
            backend=_Recorder([CORRECTED_SQL]), execute_fn=lambda sql: RESULT.copy(), max_corrections=2,
        )

        self._query(agent)

        record = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert record["value_check"] == {"checked": 1, "unknown": [], "corrected": False, "fallback": False}
        assert record["llm"]["corrections"] == 0

    def test_nothing_checked_writes_no_key(self, audit_file):
        dv.clear_vocabulary_cache()
        agent = SQLAgent(
            backend=_Recorder([UNKNOWN_SQL]), execute_fn=lambda sql: RESULT.copy(), max_corrections=2,
        )

        self._query(agent)

        record = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert "value_check" not in record
