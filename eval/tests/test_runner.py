# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for eval/runner.py.

Covers every :class:`~eval.models.CaseStatus` branch of
:func:`eval.runner.run_case`, both replay factories
(:func:`~eval.runner.make_offline_generator`,
:func:`~eval.runner.make_offline_executor`), the live generator factory
(:func:`~eval.runner.make_live_generator`, using a stub backend — no
network call), and golden-set file loading.

Run::

    .venv/Scripts/python.exe -m pytest eval/tests/test_runner.py -v --no-cov
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from eval.fingerprint import fingerprint_dataframe
from eval.models import GoldenCase
from eval.runner import (
    load_golden_cases,
    make_live_generator,
    make_offline_executor,
    make_offline_generator,
    run_case,
    run_golden_set,
)

SIMPLE_DF = pd.DataFrame({"n": [3]})
SIMPLE_FP = fingerprint_dataframe(SIMPLE_DF)


def _success_case(**overrides) -> GoldenCase:
    defaults = dict(
        id="c1",
        question="how many?",
        tags=["count"],
        # A real table (Phase 1's validate_sql now enforces a table
        # allowlist against schema_data/columns.py -- "T" is not one of
        # the 12 known tables and would be guard-rejected).
        expected_sql="SELECT COUNT(*) AS n FROM Contract",
        expected_fingerprint=SIMPLE_FP,
    )
    defaults.update(overrides)
    return GoldenCase(**defaults)


# ---------------------------------------------------------------------------
# run_case: success path
# ---------------------------------------------------------------------------


class TestRunCaseSuccess:
    def test_pass_when_fingerprint_matches(self):
        case = _success_case()
        result = run_case(case, lambda q: case.expected_sql, lambda sql: SIMPLE_DF)
        assert result.status == "pass"
        assert result.passed is True
        assert result.generated_sql == case.expected_sql
        assert result.actual_fingerprint == SIMPLE_FP
        assert result.error is None
        assert result.latency_seconds >= 0.0
        assert result.case_id == "c1"
        assert result.question == "how many?"
        assert result.tags == ["count"]

    def test_pass_when_expected_fingerprint_is_none(self):
        """No expected_fingerprint recorded yet -> nothing to compare, passes."""
        case = _success_case(expected_fingerprint=None)
        result = run_case(case, lambda q: case.expected_sql, lambda sql: SIMPLE_DF)
        assert result.status == "pass"
        assert result.actual_fingerprint == SIMPLE_FP

    def test_fingerprint_mismatch(self):
        case = _success_case()
        wrong_df = pd.DataFrame({"n": [999]})
        result = run_case(case, lambda q: case.expected_sql, lambda sql: wrong_df)
        assert result.status == "fingerprint_mismatch"
        assert result.passed is False
        assert result.error is not None
        assert "expected fingerprint" in result.error

    def test_tags_are_copied_not_aliased(self):
        case = _success_case(tags=["a", "b"])
        result = run_case(case, lambda q: case.expected_sql, lambda sql: SIMPLE_DF)
        result.tags.append("mutated")
        assert case.tags == ["a", "b"]


# ---------------------------------------------------------------------------
# run_case: guard rejection
# ---------------------------------------------------------------------------


class TestRunCaseGuardRejection:
    def test_forbidden_keyword_is_guard_rejected(self):
        case = _success_case()
        result = run_case(case, lambda q: "DELETE FROM T", lambda sql: SIMPLE_DF)
        assert result.status == "guard_rejected"
        assert result.generated_sql == "DELETE FROM T"
        assert "Forbidden keyword" in result.error

    def test_guard_rejection_never_calls_execute_fn(self):
        case = _success_case()

        def _boom(sql: str) -> pd.DataFrame:
            raise AssertionError("execute_fn must not be called after guard rejection")

        result = run_case(case, lambda q: "DROP TABLE T", _boom)
        assert result.status == "guard_rejected"


# ---------------------------------------------------------------------------
# run_case: generation errors
# ---------------------------------------------------------------------------


class TestRunCaseGenerationError:
    def test_generic_exception_from_generator(self):
        case = _success_case()

        def _generate(q: str) -> str:
            raise RuntimeError("Ollama endpoint unreachable")

        result = run_case(case, _generate, lambda sql: SIMPLE_DF)
        assert result.status == "generation_error"
        assert result.generated_sql is None
        assert "Ollama endpoint unreachable" in result.error

    def test_value_error_with_other_message_is_generation_error(self):
        case = _success_case()

        def _generate(q: str) -> str:
            raise ValueError("No SELECT / CTE found in model response")

        result = run_case(case, _generate, lambda sql: SIMPLE_DF)
        assert result.status == "generation_error"


# ---------------------------------------------------------------------------
# run_case: execution errors
# ---------------------------------------------------------------------------


class TestRunCaseExecutionError:
    def test_execute_fn_runtime_error(self):
        case = _success_case()

        def _execute(sql: str) -> pd.DataFrame:
            raise RuntimeError("Database error: Invalid column name 'Foo'")

        result = run_case(case, lambda q: case.expected_sql, _execute)
        assert result.status == "execution_error"
        assert result.generated_sql == case.expected_sql
        assert "Invalid column name" in result.error


# ---------------------------------------------------------------------------
# run_case: out-of-scope handling
# ---------------------------------------------------------------------------


class TestRunCaseOutOfScope:
    def test_expected_out_of_scope_passes(self):
        case = GoldenCase(id="oos", question="who won the war?", expect="out_of_scope")

        def _generate(q: str) -> str:
            raise ValueError("OUT_OF_SCOPE")

        result = run_case(case, _generate, lambda sql: SIMPLE_DF)
        assert result.status == "pass"
        assert result.generated_sql is None
        assert result.actual_fingerprint is None

    def test_unexpected_out_of_scope(self):
        """Generator refuses a case that actually expects data."""
        case = _success_case()

        def _generate(q: str) -> str:
            raise ValueError("OUT_OF_SCOPE")

        result = run_case(case, _generate, lambda sql: SIMPLE_DF)
        assert result.status == "unexpected_out_of_scope"
        assert result.passed is False

    def test_missed_out_of_scope(self):
        """Case expects OUT_OF_SCOPE but the generator returned real SQL."""
        case = GoldenCase(id="oos2", question="who won the war?", expect="out_of_scope")
        result = run_case(case, lambda q: "SELECT 1", lambda sql: SIMPLE_DF)
        assert result.status == "missed_out_of_scope"
        assert result.generated_sql == "SELECT 1"
        assert "expected OUT_OF_SCOPE" in result.error

    def test_out_of_scope_never_calls_execute_fn(self):
        case = GoldenCase(id="oos3", question="who won?", expect="out_of_scope")

        def _boom(sql: str) -> pd.DataFrame:
            raise AssertionError("execute_fn must not run for an out-of-scope case")

        result = run_case(case, lambda q: "SELECT 1", _boom)
        assert result.status == "missed_out_of_scope"


# ---------------------------------------------------------------------------
# run_golden_set
# ---------------------------------------------------------------------------


class TestRunGoldenSet:
    def test_preserves_order_and_runs_every_case(self):
        cases = [_success_case(id="a", question="q1"), _success_case(id="b", question="q2")]
        results = run_golden_set(cases, lambda q: cases[0].expected_sql, lambda sql: SIMPLE_DF)
        assert [r.case_id for r in results] == ["a", "b"]
        assert all(r.status == "pass" for r in results)

    def test_empty_golden_set_returns_empty_list(self):
        assert run_golden_set([], lambda q: "SELECT 1", lambda sql: SIMPLE_DF) == []


# ---------------------------------------------------------------------------
# make_offline_generator / make_offline_executor
# ---------------------------------------------------------------------------


class TestOfflineGenerator:
    def test_replays_expected_sql(self):
        case = _success_case()
        generate = make_offline_generator([case])
        assert generate("how many?") == case.expected_sql

    def test_out_of_scope_raises_sentinel(self):
        case = GoldenCase(id="oos", question="who won?", expect="out_of_scope")
        generate = make_offline_generator([case])
        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            generate("who won?")

    def test_unknown_question_raises_value_error(self):
        generate = make_offline_generator([_success_case()])
        with pytest.raises(ValueError, match="no golden case recorded"):
            generate("a question never seen before")

    def test_duplicate_question_rejected_at_construction(self):
        a = _success_case(id="a", question="same?")
        b = _success_case(id="b", question="same?")
        with pytest.raises(ValueError, match="duplicate question"):
            make_offline_generator([a, b])

    def test_full_offline_replay_round_trips_through_run_case(self):
        """Golden-set-level integration: offline generator + executor together."""
        case = _success_case(expected_rows=[{"n": 3}])
        generate = make_offline_generator([case])
        execute = make_offline_executor([case])
        result = run_case(case, generate, execute)
        assert result.status == "pass"


class TestOfflineExecutor:
    def test_replays_expected_rows(self):
        case = _success_case(expected_rows=[{"n": 3}])
        execute = make_offline_executor([case])
        df = execute(case.expected_sql)
        assert df.to_dict("records") == [{"n": 3}]

    def test_empty_expected_rows_replays_empty_frame(self):
        case = _success_case(expect="empty", expected_rows=[], expected_fingerprint=None)
        execute = make_offline_executor([case])
        df = execute(case.expected_sql)
        assert len(df) == 0

    def test_unknown_sql_raises_runtime_error(self):
        execute = make_offline_executor([_success_case(expected_rows=[{"n": 3}])])
        with pytest.raises(RuntimeError, match="no recorded rows"):
            execute("SELECT * FROM Nowhere")

    def test_missing_expected_rows_raises_runtime_error(self):
        case = _success_case(expected_rows=None)
        execute = make_offline_executor([case])
        with pytest.raises(RuntimeError, match="no expected_rows recorded"):
            execute(case.expected_sql)

    def test_duplicate_expected_sql_rejected_at_construction(self):
        a = _success_case(id="a", question="q1")
        b = _success_case(id="b", question="q2")  # same expected_sql as `a`
        with pytest.raises(ValueError, match="duplicate expected_sql"):
            make_offline_executor([a, b])

    def test_out_of_scope_cases_are_skipped_not_indexed(self):
        """Out-of-scope cases have no expected_sql; must not blow up construction."""
        oos = GoldenCase(id="oos", question="who won?", expect="out_of_scope")
        real = _success_case(expected_rows=[{"n": 3}])
        execute = make_offline_executor([oos, real])
        assert execute(real.expected_sql).to_dict("records") == [{"n": 3}]


# ---------------------------------------------------------------------------
# make_live_generator (stub backend; real retrieval/prompt pipeline, no network)
# ---------------------------------------------------------------------------


class _StubBackend:
    """Minimal LLMBackend-shaped stub — returns a fixed response."""

    name = "stub"

    def __init__(self, response: str) -> None:
        self._response = response

    def generate(self, prompt: str) -> str:
        return self._response


class TestLiveGenerator:
    def test_cleans_markdown_fence_from_backend_response(self):
        generate = make_live_generator(
            _StubBackend("```sql\nSELECT COUNT(*) AS n FROM Customer\n```"),
            system_prompt="You are a T-SQL expert.",
        )
        sql = generate("How many customers are there?")
        assert sql == "SELECT COUNT(*) AS n FROM Customer"

    def test_out_of_scope_sentinel_propagates_unchanged(self):
        class _RefusingBackend:
            name = "refuser"

            def generate(self, prompt: str) -> str:
                raise ValueError("OUT_OF_SCOPE")

        generate = make_live_generator(_RefusingBackend(), system_prompt="sys")
        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            generate("what's the weather?")

    def test_generator_does_not_validate_only_cleans(self):
        """Guard application belongs to run_case, not the generator.

        ``clean_sql`` only needs a SELECT/WITH keyword to succeed; it has
        no opinion on forbidden keywords elsewhere in the string. That
        check is ``validate_sql``'s job, applied later by ``run_case``.
        """
        dangerous = "SELECT 1; DROP TABLE Customer"
        generate = make_live_generator(_StubBackend(dangerous), system_prompt="sys")
        assert generate("drop everything") == dangerous


# ---------------------------------------------------------------------------
# load_golden_cases
# ---------------------------------------------------------------------------


class TestLoadGoldenCases:
    def test_loads_every_non_blank_line(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text(
            '{"id": "a", "question": "q1", "expected_sql": "SELECT 1"}\n'
            "\n"
            '{"id": "b", "question": "q2", "expected_sql": "SELECT 2"}\n',
            encoding="utf-8",
        )
        cases = load_golden_cases(path)
        assert [c.id for c in cases] == ["a", "b"]

    def test_accepts_str_path(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text('{"id": "a", "question": "q1", "expected_sql": "SELECT 1"}\n', encoding="utf-8")
        cases = load_golden_cases(str(path))
        assert cases[0].id == "a"

    def test_invalid_json_raises_value_error_with_line_number(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text(
            '{"id": "a", "question": "q1", "expected_sql": "SELECT 1"}\n'
            "not json at all\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=r"golden\.jsonl:2"):
            load_golden_cases(path)

    def test_invalid_case_data_raises_value_error(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        # expected_sql missing and expect defaults to "success" -> GoldenCase validation fails.
        path.write_text(json.dumps({"id": "a", "question": "q1"}) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected_sql is required"):
            load_golden_cases(path)

    def test_empty_file_raises_value_error(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text("\n\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no golden cases found"):
            load_golden_cases(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_golden_cases(tmp_path / "does_not_exist.jsonl")
