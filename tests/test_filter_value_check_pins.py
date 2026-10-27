# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pins: what a request does today when a filter literal is not a real value.

The filter-value check (``FILTER_VALUE_CHECK``) is an opt-in addition. These
tests describe the behaviour of a deployment that never turns it on, and
were written against the code as it stood before the check existed: they
must pass there and keep passing, byte for byte, after it. They cover

* the ``/query`` path (``llm.sql_agent.SQLAgent`` through ``api.runner``):
  the prompt segments, the statement that is executed, the result's fields,
  the correction prompts of a rejected and of a failed statement, and the
  audit record's exact key list;
* the session path (``session.engine.TurnEngine``): the same, for one turn;
* the guard's decisions, which the check never touches;
* the list of preflight checks.

The scenario is the one the check exists for: the vocabulary of ``Ring.Name``
is cached, and the model writes a literal that is not in it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd
import pytest

from core.models import RetrievalContext
from llm.router import PromptSegments, build_prompt_segments
from llm.sql_agent import SQLAgent
from observability.audit import AuditRecord
from retrieval import dimension_vocabulary as dv
from security.sql_guard import PolicyRejection, validate_sql
from session.engine import TurnEngine
from session.store import SessionStore

SYSTEM_PROMPT = "You are a T-SQL expert."
QUESTION = "total value in Gamma Hall"
RING_VALUES = ["Alpha Hall", "Beta Hall"]
UNKNOWN_SQL = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Gamma Hall'"
KNOWN_SQL = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Alpha Hall'"
CONTEXT = RetrievalContext(entities=["Ring"], filters={})
RESULT = pd.DataFrame({"Name": ["x"]})

#: The keys of one audit line, in the order they are written.
AUDIT_KEYS = [
    "timestamp", "request_id", "question", "generated_sql", "guard", "row_count", "tier",
    "error_code", "error_message", "timings", "llm", "columns", "principal_id",
    "resolved_columns", "session_id", "turn_id", "config_version_id", "assumptions",
    "datasource", "datasource_selection",
]

META = {"raw": {}, "endpoint_status": 200, "attempts": 1}


class _Recorder:
    """A backend that answers from a list and keeps every prompt it was given."""

    name = "recorder"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.segments: list[PromptSegments] = []

    @property
    def calls(self) -> int:
        return len(self.segments)

    def generate_with_meta_segments(self, segments: PromptSegments):
        self.segments.append(segments)
        return self._responses[len(self.segments) - 1], dict(META)


@pytest.fixture(autouse=True)
def _ring_vocabulary():
    """``Ring.Name`` cached with two halls; flushed afterwards."""
    dv.clear_vocabulary_cache()
    dv.refresh_vocabulary(
        "Ring", "Name", execute_fn=lambda sql, params: pd.DataFrame({"Name": RING_VALUES}),
    )
    yield
    dv.clear_vocabulary_cache()


@pytest.fixture()
def executed() -> list[str]:
    return []


def _execute_into(executed: list[str], *, fail_first: bool = False):
    def execute(sql: str) -> pd.DataFrame:
        executed.append(sql)
        if fail_first and len(executed) == 1:
            raise RuntimeError("Database error: boom")
        return RESULT.copy()
    return execute


def _fresh_segments() -> PromptSegments:
    return build_prompt_segments(QUESTION, SYSTEM_PROMPT, CONTEXT)


def _run_agent(responses: list[str], executed: list[str], *, fail_first: bool = False):
    backend = _Recorder(responses)
    agent = SQLAgent(
        backend=backend, execute_fn=_execute_into(executed, fail_first=fail_first), max_corrections=2,
    )
    with patch("llm.sql_agent.ContextRetriever.retrieve", return_value=CONTEXT):
        df, result = agent.run(QUESTION, SYSTEM_PROMPT)
    return backend, df, result


# ---------------------------------------------------------------------------
# /query path
# ---------------------------------------------------------------------------


class TestQueryPathToday:
    @pytest.mark.parametrize("sql", [UNKNOWN_SQL, KNOWN_SQL], ids=["unknown-literal", "known-literal"])
    def test_one_generation_one_execution_of_the_statement_as_written(self, sql, executed):
        backend, df, result = _run_agent([sql], executed)

        assert backend.calls == 1
        assert executed == [sql]
        assert result.sql == sql
        assert result.raw_response == sql
        assert result.attempt == 1
        assert result.correction_prompts == []
        assert result.injected_top is None
        assert df.equals(RESULT)

    def test_the_prompt_is_the_one_the_builder_makes(self, executed):
        backend, _df, _result = _run_agent([UNKNOWN_SQL], executed)

        fresh = _fresh_segments()
        assert backend.segments == [fresh]
        assert backend.segments[0].static_prefix == fresh.static_prefix
        assert backend.segments[0].question == fresh.question

    def test_execution_failure_correction_text_is_unchanged(self, executed):
        backend, _df, result = _run_agent([UNKNOWN_SQL, KNOWN_SQL], executed, fail_first=True)

        expected = (
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
        assert backend.calls == 2
        assert backend.segments[1].static_prefix == backend.segments[0].static_prefix
        assert backend.segments[1].question == backend.segments[0].question + expected
        assert result.correction_prompts == [expected]
        assert result.attempt == 2
        assert executed == [UNKNOWN_SQL, KNOWN_SQL]

    def test_unknown_column_correction_text_with_its_hint_is_unchanged(self, executed):
        bad = "SELECT TOP 5 r.[Nope] FROM [ref].[Ring] AS r"
        backend, _df, result = _run_agent([bad, KNOWN_SQL], executed)

        expected = (
            "\nThe SQL query you generated failed to execute.\n\n"
            "--- FAILED SQL ---\n"
            f"{bad}\n\n"
            "--- DATABASE ERROR ---\n"
            "Unknown column 'Nope' on table 'Ring'\n\n"
            "--- COLUMNS THAT EXIST ON Ring ---\n"
            "ID, Name, Code\n"
            "Use only these names for Ring; any other column does not exist there.\n\n"
            "--- INSTRUCTIONS ---\n"
            "Fix ONLY the SQL error above. Do not change the intent of the query.\n"
            "Return only the corrected SQL statement with no explanation.\n\n"
            "SQL:\n"
        )
        assert backend.segments[1].question == backend.segments[0].question + expected
        assert result.correction_prompts == [expected]
        assert executed == [KNOWN_SQL]


class TestQueryEndpointAuditToday:
    @pytest.fixture()
    def audit_file(self, tmp_path):
        path = tmp_path / "audit_log.jsonl"
        with patch("observability.audit._AUDIT_LOG_FILE", str(path)):
            yield path

    def test_the_audit_line_and_the_response_are_unchanged(self, audit_file, executed):
        from api.query_cache import query_cache
        from api.runner import run_query

        query_cache.clear()
        agent = SQLAgent(
            backend=_Recorder([UNKNOWN_SQL]), execute_fn=_execute_into(executed), max_corrections=2,
        )
        with patch("api.runner.agent", agent):
            response = run_query(QUESTION, system_prompt=SYSTEM_PROMPT, mode="full", request_id="r_pin")
        query_cache.clear()

        record = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert list(record) == AUDIT_KEYS
        assert record["generated_sql"] == UNKNOWN_SQL
        assert record["guard"] == {
            "verdict": "allowed", "rule": None, "injected_top": None, "tables_touched": ["Ring"],
        }
        assert record["llm"]["corrections"] == 0
        assert record["error_code"] is None
        assert record["row_count"] == 1
        assert response.sql == UNKNOWN_SQL
        assert response.correction_attempts == 1
        assert executed == [UNKNOWN_SQL]


def test_an_audit_record_has_exactly_these_keys_in_this_order():
    from datetime import datetime

    record = AuditRecord(
        timestamp=datetime(2026, 1, 1), request_id="r", question="q", generated_sql="SELECT 1",
        guard={"verdict": "allowed"},
    )
    assert list(record.as_dict()) == AUDIT_KEYS


# ---------------------------------------------------------------------------
# Session path
# ---------------------------------------------------------------------------


def _ask(responses: list[str], executed: list[str], monkeypatch, *, fail_first: bool = False):
    records: list[AuditRecord] = []
    monkeypatch.setattr("session.engine.save_audit_record", records.append)
    backend = _Recorder(responses)
    from llm.router import LLMRouter

    engine = TurnEngine(
        router=LLMRouter(default_chain=[backend]),
        execute_fn=_execute_into(executed, fail_first=fail_first),
        max_corrections=2,
    )
    record = SessionStore(ttl_seconds=60, max_size=10, max_turns=10).create()
    with patch("session.engine.ContextRetriever.retrieve", return_value=CONTEXT):
        turn = engine.ask(record, QUESTION, SYSTEM_PROMPT)
    return backend, turn, records


class TestSessionTurnToday:
    @pytest.mark.parametrize("sql", [UNKNOWN_SQL, KNOWN_SQL], ids=["unknown-literal", "known-literal"])
    def test_one_generation_one_execution_and_the_audit_line(self, sql, executed, monkeypatch):
        backend, turn, records = _ask([sql], executed, monkeypatch)

        assert turn.error is None
        assert backend.calls == 1
        assert executed == [sql]
        assert turn.sql == sql
        assert turn.warnings == []
        assert turn.llm["corrections"] == 0
        assert turn.guard.verdict == "allowed"
        assert len(records) == 1
        assert list(records[0].as_dict()) == AUDIT_KEYS
        assert records[0].generated_sql == sql

    def test_the_static_prefix_is_the_builders(self, executed, monkeypatch):
        backend, _turn, _records = _ask([UNKNOWN_SQL], executed, monkeypatch)

        assert backend.segments[0].static_prefix == _fresh_segments().static_prefix

    def test_execution_failure_correction_text_is_unchanged(self, executed, monkeypatch):
        backend, turn, _records = _ask([UNKNOWN_SQL, KNOWN_SQL], executed, monkeypatch, fail_first=True)

        suffix = (
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
        assert backend.calls == 2
        assert backend.segments[1].question == backend.segments[0].question + suffix
        assert backend.segments[1].static_prefix == backend.segments[0].static_prefix
        assert turn.llm["corrections"] == 1
        assert executed == [UNKNOWN_SQL, KNOWN_SQL]


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class TestGuardDecisionsToday:
    def test_it_does_not_look_at_the_value_of_a_literal(self):
        validate_sql(UNKNOWN_SQL)
        validate_sql(KNOWN_SQL)

    def test_a_denied_column_is_still_refused_whatever_the_literal(self):
        for sql in (UNKNOWN_SQL, KNOWN_SQL):
            with pytest.raises(PolicyRejection):
                validate_sql(sql, denied_columns=["Name"])

    def test_a_join_only_column_in_a_where_is_still_refused(self):
        with pytest.raises(ValueError, match="restricted"):
            validate_sql(UNKNOWN_SQL, denied_columns=["ref.Ring.Name"])


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def test_the_preflight_check_list_is_unchanged():
    from scripts.verify_deployment import build_checks

    assert [check.__name__ for check in build_checks()] == [
        "check_settings_valid",
        "check_table_datasources",
        "check_db_connectivity",
        "check_login_is_read_only",
        "check_row_cap",
        "check_query_timeout",
        "check_tables_in_assigned_sources",
        "check_schema_structure_matches_databases",
        "check_openai_model_exists",
        "check_model_answers_each_prompt",
        "check_api_key_authenticates",
        "check_column_restrictions_name_real_columns",
        "check_audit_log_writable",
        "check_session_store_writable",
        "check_project_config_loads",
        "check_project_config_is_curated",
        "check_aliases_are_curated",
        "check_every_data_source_is_described",
        "check_no_staging_tables",
        "check_no_sample_values_in_descriptions",
        "check_no_table_in_excluded_schema",
        "check_aliases_hold_no_raw_values",
        "check_entities_match_schema_keys",
        "check_rate_limit_sane_for_deployment",
        "check_web_ui_origin_allowed",
    ]
