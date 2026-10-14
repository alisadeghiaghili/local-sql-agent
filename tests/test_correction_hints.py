# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A correction prompt names what exists when the error is a name that does not.

An unknown column or table made the model try another guess with only the
guard's message to go on; in a production audit log every request that began
that way used all its correction rounds and was refused. The correction
prompt now lists the table's real column names (an unknown column) or the
closest real table references (an unknown table), from ``schema.yaml``, and
only then. Pinned here:

* the list is what ``schema.yaml`` holds, in its order, names only;
* **a fully denied column never appears; a join-only column appears only
  marked as a join key** (the policy matches the guard: join-only is allowed
  in ``JOIN ... ON``, and the first-attempt prompt already says so);
* an unreadable policy yields no hint at all;
* the hint is in the question segment of the retry, the static prefix is
  byte-identical across rounds, and a block already shown is not repeated;
* every other error's correction prompt is byte-for-byte what it was.

Expectations are derived from whichever schema is loaded; no table or column
name is written in this file.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from llm.base import LLMBackend
from llm.correction_hints import JOIN_KEY_MARK, correction_hint
from llm.router import LLMRouter, PromptSegments
from llm.sql_agent import SQLAgent
from schema_data.registry import (
    get_table_columns,
    get_table_descriptions,
    get_table_schema_qualifiers,
    table_reference_sql,
)
from security.sql_guard import CorrectableRejection, PolicyRejection, validate_sql
from tests._source_fixtures import configured_sources, tables_of


def _wide_table() -> str:
    columns = get_table_columns()
    return max(columns, key=lambda key: len(columns[key]))


def _reference(table: str) -> str:
    return table_reference_sql(table, get_table_schema_qualifiers().get(table, ""))


def _bad_column_sql(table: str, column: str = "NoSuchColumnAnywhere") -> str:
    return f"SELECT t.{column} FROM {_reference(table)} t"


def _rejection(sql: str) -> Exception:
    with pytest.raises(CorrectableRejection) as caught:
        validate_sql(sql)
    return caught.value


def _listing(hint: str) -> list[str]:
    """The comma-separated names of a columns hint."""
    return [item.strip() for item in hint.splitlines()[1].split(",")]


# ---------------------------------------------------------------------------
# The guard says which table
# ---------------------------------------------------------------------------

class TestGuardNamesTheTable:
    def test_an_unknown_column_rejection_carries_its_table_key(self):
        table = _wide_table()
        exc = _rejection(_bad_column_sql(table))
        assert exc.table == table
        assert exc.subject == "NoSuchColumnAnywhere"

    def test_other_rejections_carry_no_table(self):
        exc = _rejection("SELECT * FROM TableThatIsNotInTheSchemaAtAll")
        assert exc.table is None
        assert PolicyRejection("x").table is None


# ---------------------------------------------------------------------------
# Unknown column: the table's real columns
# ---------------------------------------------------------------------------

class TestUnknownColumn:
    def test_lists_the_real_columns_in_schema_order(self):
        table = _wide_table()
        hint = correction_hint(_rejection(_bad_column_sql(table)))
        lines = hint.splitlines()
        assert lines[0] == f"--- COLUMNS THAT EXIST ON {table} ---"
        assert _listing(hint) == list(get_table_columns()[table])
        assert table in lines[2]

    def test_names_only_no_descriptions(self):
        table = _wide_table()
        hint = correction_hint(_rejection(_bad_column_sql(table)))
        for text in list(get_table_columns()[table].values()) + list(get_table_descriptions().values()):
            if text and len(text) > 12:
                assert text not in hint

    def test_it_is_deterministic(self):
        exc = _rejection(_bad_column_sql(_wide_table()))
        assert correction_hint(exc) == correction_hint(exc)

    def test_a_long_list_is_bounded(self):
        table = _wide_table()
        total = len(get_table_columns()[table])
        hint = correction_hint(_rejection(_bad_column_sql(table)), max_columns=2)
        assert f"... and {total - 2} more" in hint
        assert len(_listing(hint)) == 3

    def test_no_hint_for_an_error_that_is_not_a_guard_rejection(self):
        assert correction_hint(RuntimeError("Invalid column name 'x'")) == ""
        assert correction_hint(ValueError("syntax error")) == ""

    def test_no_hint_when_the_table_is_not_in_the_schema(self):
        exc = CorrectableRejection("Unknown column 'x' on table 'Nope'", reason="other", subject="x")
        exc.table = "Nope"
        assert correction_hint(exc) == ""


class TestAccessPolicy:
    def test_a_denied_column_is_left_out(self):
        table = _wide_table()
        columns = list(get_table_columns()[table])
        denied = columns[1]
        hint = correction_hint(_rejection(_bad_column_sql(table)), denied_columns=[denied.lower()])
        listing = _listing(hint)
        assert denied not in listing
        assert listing == [c for c in columns if c != denied]
        assert denied not in hint.split("\n", 1)[1]

    def test_a_join_only_column_is_shown_only_as_a_join_key(self):
        table = _wide_table()
        columns = list(get_table_columns()[table])
        join_key = columns[0]
        hint = correction_hint(
            _rejection(_bad_column_sql(table)), denied_columns=[f"{table}.{join_key}"],
        )
        listing = _listing(hint)
        assert listing[0] == join_key + JOIN_KEY_MARK
        assert listing[1:] == columns[1:]

    def test_denied_and_join_only_together(self):
        table = _wide_table()
        columns = list(get_table_columns()[table])
        hint = correction_hint(
            _rejection(_bad_column_sql(table)),
            denied_columns=[columns[2].lower(), f"{table}.{columns[0]}"],
        )
        listing = _listing(hint)
        assert columns[2] not in listing
        assert listing[0] == columns[0] + JOIN_KEY_MARK

    def test_an_unreadable_policy_gives_no_hint(self):
        table = _wide_table()
        assert correction_hint(
            _rejection(_bad_column_sql(table)), denied_columns=["a:b:c"],
        ) == ""

    def test_a_policy_naming_nothing_here_changes_nothing(self):
        table = _wide_table()
        plain = correction_hint(_rejection(_bad_column_sql(table)))
        assert correction_hint(
            _rejection(_bad_column_sql(table)), denied_columns=["ColumnNobodyHas"],
        ) == plain


# ---------------------------------------------------------------------------
# Unknown table: the closest real ones
# ---------------------------------------------------------------------------

class TestUnknownTable:
    def test_a_misspelt_table_gets_the_real_one(self):
        table = _wide_table()
        bare = table.rsplit(".", 1)[-1]
        misspelt = bare[:-1] + "x" if len(bare) > 3 else bare + "z"
        exc = _rejection(f"SELECT * FROM {misspelt}")
        assert exc.reason == "unknown_table"
        hint = correction_hint(exc)
        assert hint.startswith(f"--- TABLES THAT EXIST, CLOSEST TO {misspelt} ---")
        assert _reference(table) in hint

    def test_the_suggestions_are_references_to_write(self):
        table = _wide_table()
        bare = table.rsplit(".", 1)[-1]
        hint = correction_hint(_rejection(f"SELECT * FROM {bare}s"))
        assert _reference(table) in hint

    def test_a_name_resembling_nothing_gets_no_suggestion(self):
        assert correction_hint(_rejection("SELECT * FROM Qzxjvkwpq")) == ""

    def test_a_rejection_that_already_says_did_you_mean_gets_nothing_more(self):
        exc = CorrectableRejection(
            "Forbidden keyword detected: table 'x.T' is not in the schema allowlist; "
            "did you mean [a].[T]?", reason="unknown_table", subject="T",
        )
        assert correction_hint(exc) == ""

    def test_with_several_sources_only_that_sources_tables_are_suggested(self):
        with configured_sources() as sets:
            first = next(t for t, s in sets.items() if s == ("inventory",))
            other = next(t for t, s in sets.items() if "inventory" not in s)
            exc = CorrectableRejection(
                "Forbidden keyword detected: unknown table 'x' is not in the schema allowlist",
                reason="unknown_table", subject=other.rsplit(".", 1)[-1] + "x",
            )
            assert other not in tables_of("inventory", sets)
            hint = correction_hint(exc, source="inventory")
            assert _reference(other) not in hint
            exc.subject = first.rsplit(".", 1)[-1] + "x"
            assert _reference(first) in correction_hint(exc, source="inventory")


# ---------------------------------------------------------------------------
# In the correction prompt
# ---------------------------------------------------------------------------

class _ScriptedBackend(LLMBackend):
    """Answers from a script and records the segments of every call."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.segments: list[PromptSegments] = []

    @property
    def name(self) -> str:
        return "scripted:test"

    def generate(self, prompt: str) -> str:  # pragma: no cover - segments path is used
        raise AssertionError("expected the segment-aware path")

    def generate_with_meta_segments(self, segments: Any) -> tuple[str, dict[str, Any]]:
        self.segments.append(segments)
        index = min(len(self.segments) - 1, len(self._answers) - 1)
        return self._answers[index], {}


def _agent(backend: LLMBackend, *, execute: Any = None) -> SQLAgent:
    return SQLAgent(
        router=LLMRouter(default_chain=[backend]),
        execute_fn=execute or (lambda sql: pd.DataFrame({"n": [1]})),
    )


def _good_sql(index: int = 0) -> str:
    table = _wide_table()
    return f"SELECT t.{list(get_table_columns()[table])[index]} FROM {_reference(table)} t"


class TestSQLAgentCorrectionPrompt:
    def test_the_retry_carries_the_columns_in_the_question_segment_only(self):
        table = _wide_table()
        backend = _ScriptedBackend([_bad_column_sql(table), _good_sql()])
        _agent(backend).run("how many?", "You are a T-SQL expert.")

        first, retry = backend.segments
        hint_heading = f"--- COLUMNS THAT EXIST ON {table} ---"
        assert hint_heading not in first.question
        assert hint_heading in retry.question
        assert hint_heading not in retry.static_prefix
        assert hint_heading not in retry.session_context
        assert retry.static_prefix == first.static_prefix, "the cached prefix must not move"
        assert retry.session_context == first.session_context
        # Between the error and the instructions, where the model reads it.
        text = retry.question
        assert text.index("--- DATABASE ERROR ---") < text.index(hint_heading) < text.index("--- INSTRUCTIONS ---")
        assert ", ".join(get_table_columns()[table]) in text

    def test_a_denied_column_is_not_in_the_retry(self):
        table = _wide_table()
        columns = list(get_table_columns()[table])
        backend = _ScriptedBackend([_bad_column_sql(table), _good_sql()])
        _agent(backend).run("q", "sys", denied_columns=[columns[1].lower()])
        retry = backend.segments[1].question
        assert ", ".join(c for c in columns if c != columns[1]) in retry
        assert columns[1] not in retry.split("--- COLUMNS THAT EXIST ON", 1)[1].split("--- INSTRUCTIONS", 1)[0]

    def test_a_join_only_column_is_marked_in_the_retry(self):
        table = _wide_table()
        columns = list(get_table_columns()[table])
        backend = _ScriptedBackend([_bad_column_sql(table), _good_sql()])
        backend = _ScriptedBackend([_bad_column_sql(table), _good_sql(1)])
        _agent(backend).run("q", "sys", denied_columns=[f"{table}.{columns[0]}"])
        assert columns[0] + JOIN_KEY_MARK in backend.segments[1].question

    def test_the_same_block_is_not_repeated_in_a_later_round(self):
        table = _wide_table()
        backend = _ScriptedBackend([_bad_column_sql(table)] * 3)
        with pytest.raises(ValueError):
            _agent(backend).run("q", "sys")
        heading = f"--- COLUMNS THAT EXIST ON {table} ---"
        assert [s.question.count(heading) for s in backend.segments] == [0, 1, 1]

    def test_a_different_table_in_a_later_round_gets_its_own_block(self):
        columns = get_table_columns()
        first, second = sorted(columns, key=lambda k: -len(columns[k]))[:2]
        backend = _ScriptedBackend([_bad_column_sql(first), _bad_column_sql(second), _good_sql()])
        _agent(backend).run("q", "sys")
        last = backend.segments[2].question
        assert f"COLUMNS THAT EXIST ON {first} ---" in last and f"COLUMNS THAT EXIST ON {second} ---" in last

    def test_an_unknown_table_retry_names_the_closest_real_one(self):
        table = _wide_table()
        bare = table.rsplit(".", 1)[-1]
        backend = _ScriptedBackend([f"SELECT * FROM {bare}s", _good_sql()])
        _agent(backend).run("q", "sys")
        assert _reference(table) in backend.segments[1].question

    def test_any_other_error_gets_exactly_the_old_prompt(self):
        from security.sql_guard import clean_sql

        bad = "SELECT FROM WHERE"
        with pytest.raises(ValueError) as caught:
            validate_sql(clean_sql(bad))
        backend = _ScriptedBackend([bad, _good_sql()])
        _agent(backend).run("q", "sys")
        first, retry = backend.segments
        # The correction template as it was before hints existed.
        old_template = (
            "\nThe SQL query you generated failed to execute.\n\n--- FAILED SQL ---\n{sql}\n\n"
            "--- DATABASE ERROR ---\n{error}\n\n--- INSTRUCTIONS ---\n"
            "Fix ONLY the SQL error above. Do not change the intent of the query.\n"
            "Return only the corrected SQL statement with no explanation.\n\nSQL:\n"
        )
        assert retry.question == first.question + old_template.format(sql=bad, error=str(caught.value))

    def test_an_execution_error_gets_no_hint(self):
        calls = {"n": 0}

        def execute(sql: str) -> pd.DataFrame:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Invalid column name 'x'.")
            return pd.DataFrame({"n": [1]})

        backend = _ScriptedBackend([_good_sql(), _good_sql()])
        _agent(backend, execute=execute).run("q", "sys")
        assert "THAT EXIST" not in backend.segments[1].question

    def test_a_stale_rejection_does_not_leak_into_a_later_execution_error(self):
        table = _wide_table()
        first_column = list(get_table_columns()[table])[0]
        valid = f"SELECT t.{first_column} FROM {_reference(table)} t"
        calls = {"n": 0}

        def execute(sql: str) -> pd.DataFrame:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("deadlock victim")
            return pd.DataFrame({"n": [1]})

        backend = _ScriptedBackend([_bad_column_sql(table), valid, valid])
        _agent(backend, execute=execute).run("q", "sys")
        third = backend.segments[2].question
        assert third.count("--- COLUMNS THAT EXIST ON") == 1, "only the first round's block, not a new one"
        assert "deadlock victim" in third


class TestTurnEngineCorrectionPrompt:
    def test_the_session_engine_retry_carries_the_columns_too(self, auth_settings):
        from fastapi.testclient import TestClient

        import api.server as server_module
        import api.v2_routes as v2_routes
        from session.engine import TurnEngine

        table = _wide_table()
        backend = _ScriptedBackend([_bad_column_sql(table), _good_sql()])
        server_module._system_prompt = "stub system prompt"
        v2_routes._system_prompt = "stub system prompt"
        v2_routes._reset_for_testing()
        try:
            v2_routes._turn_engine = TurnEngine(
                router=LLMRouter(default_chain=[backend]),
                execute_fn=lambda sql: pd.DataFrame({"n": [1]}),
            )
            client = TestClient(server_module.app, raise_server_exceptions=False, headers=auth_settings)
            sid = client.post("/v2/sessions").json()["session_id"]
            resp = client.post(f"/v2/sessions/{sid}/turns", json={"question": "how many?"})
            assert resp.status_code == 200
        finally:
            v2_routes._reset_for_testing()

        assert len(backend.segments) == 2
        first, retry = backend.segments
        heading = f"--- COLUMNS THAT EXIST ON {table} ---"
        assert heading in retry.question and heading not in first.question
        assert retry.static_prefix == first.static_prefix
        assert heading not in retry.static_prefix
        text = retry.question
        assert text.index("--- ERROR ---") < text.index(heading) < text.index("--- INSTRUCTIONS ---")
