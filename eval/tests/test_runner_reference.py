# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for the live-reference mode, source tracing and the new case statuses.

Covers :func:`eval.runner.run_case` with ``reference="live"`` (same-run
comparison of the case's ``expected_sql`` with the generated SQL's result),
:class:`eval.runner.SourceTrace`, and the runner skipping every non-active
case. All with fake generators/executors -- no database, no model.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_runner_reference.py -q
"""

from __future__ import annotations

import pandas as pd
import pytest

from eval.compare import ComparisonOptions
from eval.fingerprint import fingerprint_dataframe
from eval.models import GoldenCase
from eval.runner import (
    SourceTrace,
    make_live_generator,
    make_offline_executor,
    make_offline_generator,
    run_case,
    run_golden_set,
)
from schema_data.columns import TABLE_COLUMNS

_TABLE = next(iter(TABLE_COLUMNS))
REF_SQL = f"SELECT COUNT(*) AS n FROM {_TABLE}"
GEN_SQL = f"SELECT COUNT(*) AS total FROM {_TABLE}"


def _case(**overrides) -> GoldenCase:
    fields = dict(
        id="c1",
        question="how many?",
        expected_sql=REF_SQL,
        expected_fingerprint=fingerprint_dataframe(pd.DataFrame({"n": [3]})),
    )
    fields.update(overrides)
    return GoldenCase(**fields)


class _Warehouse:
    """A fake executor that answers per SQL text and records the calls."""

    def __init__(self, answers: dict[str, pd.DataFrame | Exception]):
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, sql: str) -> pd.DataFrame:
        self.calls.append(sql)
        answer = self.answers[sql]
        if isinstance(answer, Exception):
            raise answer
        return answer


class TestLiveReference:
    def test_stale_stored_fingerprint_does_not_cause_a_false_failure(self):
        """The warehouse moved on: the recorded fingerprint says 3, today it is 4."""
        today = pd.DataFrame({"n": [4]})
        wh = _Warehouse({REF_SQL: today, GEN_SQL: pd.DataFrame({"total": [4]})})
        stored = run_case(_case(), lambda q: GEN_SQL, wh)
        live = run_case(_case(), lambda q: GEN_SQL, wh, reference="live")
        assert stored.status == "fingerprint_mismatch"
        assert live.status == "pass"
        assert live.reference_fingerprint == fingerprint_dataframe(today)

    def test_wrong_answer_is_a_mismatch_with_a_value_free_reason(self):
        wh = _Warehouse({REF_SQL: pd.DataFrame({"n": [4]}), GEN_SQL: pd.DataFrame({"total": [5]})})
        result = run_case(_case(), lambda q: GEN_SQL, wh, reference="live")
        assert result.status == "fingerprint_mismatch"
        assert result.error == "result differs from the live reference: result values differ"
        assert not any(ch.isdigit() for ch in result.error)  # no cell value leaks

    def test_reference_is_executed_after_the_generated_sql_in_the_same_run(self):
        wh = _Warehouse({REF_SQL: pd.DataFrame({"n": [4]}), GEN_SQL: pd.DataFrame({"x": [4]})})
        run_case(_case(), lambda q: GEN_SQL, wh, reference="live")
        assert wh.calls == [GEN_SQL, REF_SQL]

    def test_stored_mode_never_executes_the_reference(self):
        wh = _Warehouse({GEN_SQL: pd.DataFrame({"n": [3]})})
        run_case(_case(), lambda q: GEN_SQL, wh)
        assert wh.calls == [GEN_SQL]

    def test_reference_failure_is_reported_as_the_cases_fault(self):
        wh = _Warehouse({
            REF_SQL: RuntimeError("Database error: timeout"),
            GEN_SQL: pd.DataFrame({"x": [4]}),
        })
        result = run_case(_case(), lambda q: GEN_SQL, wh, reference="live")
        assert result.status == "reference_error"
        assert not result.passed
        assert "reference expected_sql failed" in (result.error or "")
        assert result.actual_fingerprint is not None

    def test_reference_rejected_by_the_guard_is_never_executed(self):
        case = _case(expected_sql="DROP TABLE x")
        wh = _Warehouse({GEN_SQL: pd.DataFrame({"x": [4]})})
        result = run_case(case, lambda q: GEN_SQL, wh, reference="live")
        assert result.status == "reference_error"
        assert "rejected by the guard" in (result.error or "")
        assert wh.calls == [GEN_SQL]

    def test_generation_failures_still_win_over_the_reference(self):
        wh = _Warehouse({})

        def boom(q: str) -> str:
            raise RuntimeError("model down")

        assert run_case(_case(), boom, wh, reference="live").status == "generation_error"
        assert wh.calls == []

    def test_execution_error_of_the_generated_sql_skips_the_reference(self):
        wh = _Warehouse({GEN_SQL: RuntimeError("bad column")})
        result = run_case(_case(), lambda q: GEN_SQL, wh, reference="live")
        assert result.status == "execution_error"
        assert wh.calls == [GEN_SQL]

    def test_out_of_scope_case_needs_no_reference(self):
        case = GoldenCase(id="oos", question="weather?", expect="out_of_scope")

        def decline(q: str) -> str:
            raise ValueError("OUT_OF_SCOPE")

        assert run_case(case, decline, _Warehouse({}), reference="live").status == "pass"

    def test_empty_expectation_compares_two_empty_results(self):
        case = _case(expect="empty", expected_fingerprint=None)
        empty = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        wh = _Warehouse({REF_SQL: empty, GEN_SQL: pd.DataFrame({"other": pd.Series([], dtype="object")})})
        assert run_case(case, lambda q: GEN_SQL, wh, reference="live").status == "pass"

    def test_row_order_matters_only_for_a_top_order_by_reference(self):
        ref = f"SELECT TOP 2 n FROM {_TABLE} ORDER BY n"
        case = _case(expected_sql=ref)
        asc = pd.DataFrame({"n": [1, 2]})
        desc = pd.DataFrame({"n": [2, 1]})
        wh = _Warehouse({ref: asc, GEN_SQL: desc})
        assert run_case(case, lambda q: GEN_SQL, wh, reference="live").status == "fingerprint_mismatch"
        unordered = _case(expected_sql=REF_SQL)
        wh2 = _Warehouse({REF_SQL: asc, GEN_SQL: desc})
        assert run_case(unordered, lambda q: GEN_SQL, wh2, reference="live").status == "pass"

    def test_tolerance_option_reaches_the_comparison(self):
        wh = _Warehouse({REF_SQL: pd.DataFrame({"n": [100.0]}), GEN_SQL: pd.DataFrame({"n": [100.4]})})
        strict = run_case(_case(), lambda q: GEN_SQL, wh, reference="live")
        loose = run_case(
            _case(), lambda q: GEN_SQL, wh, reference="live",
            options=ComparisonOptions(tolerance=0.01),
        )
        assert strict.status == "fingerprint_mismatch"
        assert loose.status == "pass"

    def test_generated_sql_with_duplicate_output_names_does_not_abort_the_run(self):
        dup = pd.DataFrame([[4, 4]], columns=["id", "id"])
        wh = _Warehouse({REF_SQL: pd.DataFrame({"a": [4], "b": [4]}), GEN_SQL: dup})
        live = run_case(_case(), lambda q: GEN_SQL, wh, reference="live")
        assert live.status == "pass"
        assert live.actual_fingerprint is None
        stored = run_case(_case(), lambda q: GEN_SQL, wh)
        assert stored.status == "fingerprint_mismatch"

    def test_latency_excludes_the_reference_run(self):
        import time

        class Slow(_Warehouse):
            def __call__(self, sql: str) -> pd.DataFrame:
                if sql == REF_SQL:
                    time.sleep(0.2)
                return super().__call__(sql)

        wh = Slow({REF_SQL: pd.DataFrame({"n": [3]}), GEN_SQL: pd.DataFrame({"n": [3]})})
        result = run_case(_case(), lambda q: GEN_SQL, wh, reference="live")
        assert result.status == "pass"
        assert result.latency_seconds < 0.15


class TestSourceTrace:
    def test_selected_datasource_comes_from_the_trace(self):
        trace = SourceTrace()

        def generate(q: str) -> str:
            trace.record("sales")
            return GEN_SQL

        wh = _Warehouse({GEN_SQL: pd.DataFrame({"n": [3]})})
        result = run_case(_case(datasource="inventory"), generate, wh, source_trace=trace)
        assert result.expected_datasource == "inventory"
        assert result.selected_datasource == "sales"

    def test_without_a_selection_the_routed_source_of_the_sql_is_used(self):
        from database.routing import target_datasource_or_none

        trace = SourceTrace()
        wh = _Warehouse({GEN_SQL: pd.DataFrame({"n": [3]})})
        result = run_case(_case(), lambda q: GEN_SQL, wh, source_trace=trace)
        assert result.selected_datasource == target_datasource_or_none(GEN_SQL)

    def test_the_trace_is_reset_between_cases(self):
        trace = SourceTrace()
        trace.record("stale")
        wh = _Warehouse({GEN_SQL: pd.DataFrame({"n": [3]})})
        result = run_case(_case(), lambda q: GEN_SQL, wh, source_trace=trace)
        assert result.selected_datasource != "stale"

    def test_no_trace_means_no_selected_datasource(self):
        wh = _Warehouse({GEN_SQL: pd.DataFrame({"n": [3]})})
        assert run_case(_case(), lambda q: GEN_SQL, wh).selected_datasource is None

    def test_selection_is_recorded_even_when_generation_fails(self):
        trace = SourceTrace()

        def generate(q: str) -> str:
            trace.record("sales")
            raise ValueError("OUT_OF_SCOPE")

        result = run_case(_case(), generate, _Warehouse({}), source_trace=trace)
        assert result.status == "unexpected_out_of_scope"
        assert result.selected_datasource == "sales"

    def test_live_generator_records_the_chosen_source(self, monkeypatch):
        import llm.source_routing as routing

        monkeypatch.setattr(routing, "choose_source", lambda question, context: "inventory")

        class Backend:
            name = "stub"

            def generate(self, prompt: str) -> str:
                return "SELECT 1"

        trace = SourceTrace()
        make_live_generator(Backend(), "You are a T-SQL expert.", trace)("how many?")
        assert trace.chosen == "inventory"


class TestNonActiveCasesAreNeverRun:
    @pytest.mark.parametrize("status", ["pending_expected", "pending_review", "reviewed"])
    def test_skipped_without_touching_generator_or_executor(self, status):
        case = GoldenCase(id="p", question="q", expected_sql=REF_SQL, status=status)
        calls: list[str] = []

        def generate(q: str) -> str:
            calls.append(q)
            return GEN_SQL

        assert run_golden_set([case], generate, _Warehouse({})) == []
        assert calls == []

    def test_offline_fixtures_ignore_non_active_duplicates(self):
        active = _case(id="a", question="same?", expected_rows=[{"n": 3}])
        candidate = GoldenCase(
            id="b", question="same?", expected_sql=REF_SQL, status="pending_review"
        )
        generate = make_offline_generator([active, candidate])
        execute = make_offline_executor([active, candidate])
        assert generate("same?") == REF_SQL
        assert execute(REF_SQL).to_dict("records") == [{"n": 3}]


class TestOfflineExecutorSharedReference:
    def test_two_questions_may_share_a_reference_with_identical_rows(self):
        a = _case(id="a", question="q1", expected_rows=[{"n": 3}])
        b = _case(id="b", question="q2", expected_rows=[{"n": 3}])
        assert make_offline_executor([a, b])(REF_SQL).to_dict("records") == [{"n": 3}]

    def test_conflicting_recorded_rows_are_still_rejected(self):
        a = _case(id="a", question="q1", expected_rows=[{"n": 3}])
        b = _case(id="b", question="q2", expected_rows=[{"n": 4}])
        with pytest.raises(ValueError, match="duplicate expected_sql"):
            make_offline_executor([a, b])
