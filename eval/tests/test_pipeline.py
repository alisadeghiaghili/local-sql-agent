# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The live evaluation pipeline: the server's own loop, and ``--single-shot``.

``--live`` generates through :class:`llm.sql_agent.SQLAgent` (guard,
correction rounds, hints, row cap) so the figure is what an analyst gets;
``--single-shot`` keeps one model call. Here a scripted fake model stands in
for the endpoint and SQLite for the warehouse.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_pipeline.py -q
"""

from __future__ import annotations

import json
import sqlite3

import pandas as pd
import pytest
import sqlglot

import config as cfg
from eval import cli
from eval.baseline import compare_to_baseline
from eval.models import GoldenCase
from eval.pipeline import make_production_pipeline
from eval.report import build_report
from eval.runner import SourceTrace, run_case, run_golden_set
from llm.base import LLMBackend
from llm.router import LLMRouter
from schema_data.columns import TABLE_COLUMNS

_TABLE = next(iter(TABLE_COLUMNS))
T = f"[{_TABLE}]"
GOOD = f"SELECT COUNT(*) AS n FROM {T}"
UNKNOWN_TABLE = "SELECT ID FROM NoSuchTable"
CASE = GoldenCase(id="c1", question="how many?", expected_sql=GOOD, datasource=None)


class ScriptedBackend(LLMBackend):
    """Answers with the scripted texts in order (the last one repeats) and counts calls."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "scripted:test"

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self.answers) - 1)
        answer = self.answers[index]
        if answer == "OUT_OF_SCOPE":  # the real backend raises the sentinel itself
            raise ValueError("OUT_OF_SCOPE")
        return answer


class Warehouse:
    """SQLite behind a T-SQL front; records every statement it was asked to run."""

    def __init__(
        self, fail_first: bool = False, label: str = "v", fail_counts: bool = False,
        cut: bool = False,
    ) -> None:
        self.fail_counts = fail_counts
        self.cut = cut  # emulate the executor's fetchmany(MAX_ROWS_RETURNED)
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute(f"CREATE TABLE {T} (ID INTEGER, Label TEXT)")
        self.conn.executemany(
            f"INSERT INTO {T} VALUES (?, ?)", [(i, f"{label}{i}") for i in range(1, 6)]
        )
        self.calls: list[str] = []
        self.fail_first = fail_first

    def __call__(self, sql: str) -> pd.DataFrame:
        self.calls.append(sql)
        if self.fail_counts and sql.startswith("SELECT COUNT(*) FROM ("):
            raise RuntimeError("Database error: query timeout")
        if self.fail_first and len(self.calls) == 1:
            self.fail_first = False
            raise RuntimeError("Database error: transient failure")
        frame = pd.read_sql_query(
            sqlglot.transpile(sql, read="tsql", write="sqlite")[0], self.conn
        )
        if self.cut and not sql.startswith("SELECT COUNT(*) FROM ("):
            frame = frame.head(cfg.settings.max_rows_returned)
        return frame


def _pipeline(backend, warehouse, trace=None):
    trace = trace if trace is not None else SourceTrace()
    generate, execute = make_production_pipeline(
        "You are a T-SQL expert.", trace, warehouse,
        router=LLMRouter(default_chain=[backend]),
    )
    return generate, execute, trace


def _run(backend, warehouse=None, case=CASE):
    warehouse = warehouse or Warehouse()
    generate, execute, trace = _pipeline(backend, warehouse)
    result = run_case(case, generate, execute, reference="live", source_trace=trace)
    return result, warehouse, trace


class TestCorrectionLoop:
    def test_a_model_that_fails_once_then_succeeds_is_corrected(self):
        backend = ScriptedBackend(UNKNOWN_TABLE, GOOD)
        result, _, _ = _run(backend)
        assert result.status == "pass"
        assert len(backend.prompts) == 2
        assert result.corrections == 1
        assert [(g["round"], g["class"]) for g in result.guard_rejections] == [
            (1, "CorrectableRejection")
        ]
        assert "NoSuchTable" in result.guard_rejections[0]["message"]
        assert result.execution_errors == []
        assert result.diagnosis.mismatch_class == "match"

    def test_the_second_prompt_carries_the_correction_text(self):
        backend = ScriptedBackend(UNKNOWN_TABLE, GOOD)
        _run(backend)
        assert "NoSuchTable" in backend.prompts[1] and "NoSuchTable" not in backend.prompts[0]

    def test_a_repeated_rejection_is_visible_round_by_round(self):
        backend = ScriptedBackend(UNKNOWN_TABLE, UNKNOWN_TABLE, GOOD)
        result, _, _ = _run(backend)
        assert result.status == "pass" and result.corrections == 2
        assert [g["round"] for g in result.guard_rejections] == [1, 2]
        assert result.guard_rejections[0]["message"] == result.guard_rejections[1]["message"]

    def test_running_out_of_rounds_is_a_guard_rejection_with_the_last_candidate(self):
        backend = ScriptedBackend(UNKNOWN_TABLE)
        result, warehouse, _ = _run(backend)
        assert result.status == "guard_rejected"
        assert len(backend.prompts) == 3  # one try and the two configured correction rounds
        assert result.corrections == 2
        assert [g["round"] for g in result.guard_rejections] == [1, 2, 3]
        assert result.generated_sql == UNKNOWN_TABLE
        assert warehouse.calls == []  # nothing rejected is ever run
        assert result.diagnosis is None

    def test_a_policy_rejection_is_not_retried(self):
        backend = ScriptedBackend(f"SELECT ID INTO #copy FROM {T}", GOOD)
        result, warehouse, _ = _run(backend)
        assert result.status == "guard_rejected"
        assert len(backend.prompts) == 1 and result.corrections == 0
        assert result.guard_rejections[0]["class"] == "PolicyRejection"
        assert warehouse.calls == []

    def test_a_statement_that_fails_to_run_is_corrected_too(self):
        backend = ScriptedBackend(GOOD, GOOD)
        result, warehouse, _ = _run(backend, Warehouse(fail_first=True))
        assert result.status == "pass" and result.corrections == 1
        assert result.guard_rejections == []
        assert [(e["round"], e["class"]) for e in result.execution_errors] == [(1, "RuntimeError")]
        assert "transient failure" in result.execution_errors[0]["message"]
        assert len(backend.prompts) == 2

    def test_a_failure_that_outlasts_the_rounds_is_an_execution_error(self):
        class Broken(Warehouse):
            def __call__(self, sql):
                self.calls.append(sql)
                raise RuntimeError("Database error: always")

        result, _, _ = _run(ScriptedBackend(GOOD), Broken())
        assert result.status == "execution_error"
        assert result.corrections == 2 and len(result.execution_errors) == 3

    def test_out_of_scope_passes_through_for_an_out_of_scope_case(self):
        case = GoldenCase(id="oos", question="weather?", expect="out_of_scope")
        result, _, trace = _run(ScriptedBackend("OUT_OF_SCOPE"), case=case)
        assert result.status == "pass" and result.corrections == 0
        assert trace.guard_rejections == []

    def test_a_transport_failure_stays_a_generation_error(self):
        class Down(ScriptedBackend):
            def generate(self, prompt):
                raise RuntimeError("endpoint unreachable")

        result, _, _ = _run(Down())
        assert result.status == "generation_error"

    def test_the_trace_is_reset_for_every_question(self):
        backend = ScriptedBackend(UNKNOWN_TABLE, GOOD, GOOD)
        warehouse = Warehouse()
        generate, execute, trace = _pipeline(backend, warehouse)
        other = GoldenCase(id="c2", question="and again?", expected_sql=GOOD)
        results = run_golden_set([CASE, other], generate, execute, reference="live",
                                 source_trace=trace)
        assert [r.corrections for r in results] == [1, 0]
        assert results[1].guard_rejections == []


class TestRowCapAndExecution:
    def test_the_servers_row_cap_is_injected_into_a_statement_without_one(self):
        backend = ScriptedBackend(f"SELECT ID FROM {T}")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T}")
        with cfg.override_settings(default_top_n=3):
            result, warehouse, _ = _run(backend, case=case)
        assert warehouse.calls[0] == f"SELECT TOP 3 ID FROM {T}"      # what the analyst's query ran
        assert warehouse.calls[1] == f"SELECT ID FROM {T}"            # the reference is not capped
        assert result.generated_sql == f"SELECT TOP 3 ID FROM {T}"
        # 3 of the reference's 5 rows, all of them right, cut by policy: a pass
        assert result.status == "pass"
        diagnosis = result.diagnosis
        assert (diagnosis.mismatch_class, diagnosis.capped, diagnosis.row_cap) == ("match_capped", True, 3)
        assert (diagnosis.expected_rows, diagnosis.actual_rows) == (5, 3)
        # confirmed by a count of the model's SQL without the cap: 5, as in the reference
        assert diagnosis.uncapped_rows == 5 and diagnosis.capped_unverified is False
        counts = [c for c in warehouse.calls if c.startswith("SELECT COUNT(*) FROM (")]
        assert len(counts) == 1 and "TOP" not in counts[0]  # the model's SQL, not the capped one

    def test_the_pre_cap_sql_is_what_the_model_wrote(self):
        generate, execute, trace = _pipeline(ScriptedBackend(f"select id from {T} order by id"),
                                             Warehouse())
        generate("list?")
        assert "TOP" not in trace.precap_sql and "ID" in trace.precap_sql.upper()
        generate2, _, trace2 = _pipeline(ScriptedBackend(f"SELECT TOP 4 ID FROM {T}"), Warehouse())
        generate2("list?")
        assert trace2.precap_sql == f"SELECT TOP 4 ID FROM {T}"  # its own TOP is kept as written

    def test_an_over_filtered_query_with_exactly_the_cap_rows_fails(self):
        # every row it returns is in the reference, and it returns a cap's worth, but it
        # drops one row the reference has: the cap must not hide that
        backend = ScriptedBackend(f"SELECT ID FROM {T} WHERE ID <= 4 ORDER BY ID")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T}")
        with cfg.override_settings(default_top_n=3):
            result, warehouse, _ = _run(backend, case=case)
        assert result.status == "fingerprint_mismatch"
        diagnosis = result.diagnosis
        assert diagnosis.mismatch_class == "actual_is_subset"
        assert (diagnosis.capped, diagnosis.uncapped_rows, diagnosis.capped_unverified) == (False, 4, False)
        counts = [c for c in warehouse.calls if c.startswith("SELECT COUNT(*) FROM (")]
        assert len(counts) == 1 and "ORDER BY" not in counts[0]  # stripped for the derived table

    def test_a_count_that_cannot_be_asked_is_a_failure_not_a_pass(self):
        backend = ScriptedBackend(f"SELECT ID FROM {T}")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T}")
        with cfg.override_settings(default_top_n=3):
            result, _, _ = _run(backend, Warehouse(fail_counts=True), case)
        assert result.status == "fingerprint_mismatch" and not result.passed
        diagnosis = result.diagnosis
        assert diagnosis.mismatch_class == "actual_is_subset"
        assert diagnosis.capped_unverified is True and diagnosis.capped is False
        assert diagnosis.uncapped_rows is None

    def test_the_count_is_not_asked_when_the_result_did_not_qualify(self):
        backend = ScriptedBackend(f"SELECT ID FROM {T} WHERE ID <= 2")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T}")
        with cfg.override_settings(default_top_n=3):
            _, warehouse, _ = _run(backend, case=case)
        assert not any(c.startswith("SELECT COUNT(*) FROM (") for c in warehouse.calls)

    def test_a_reference_cut_by_max_rows_is_counted_and_equal_counts_pass(self):
        backend = ScriptedBackend(f"SELECT ID FROM {T}")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T}")
        with cfg.override_settings(default_top_n=2, max_rows_returned=3):
            result, warehouse, _ = _run(backend, Warehouse(cut=True), case)
        assert result.status == "pass"
        d = result.diagnosis
        assert (d.expected_rows, d.actual_rows, d.row_cap) == (3, 2, 2)
        assert (d.uncapped_rows, d.reference_uncapped_rows) == (5, 5)
        assert sum(c.startswith("SELECT COUNT(*) FROM (") for c in warehouse.calls) == 2

    def test_a_reference_cut_by_max_rows_with_a_different_count_fails(self):
        backend = ScriptedBackend(f"SELECT ID FROM {T} WHERE ID <= 4 ORDER BY ID")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T}")
        with cfg.override_settings(default_top_n=2, max_rows_returned=3):
            result, _, _ = _run(backend, Warehouse(cut=True), case)
        assert result.status == "fingerprint_mismatch"
        d = result.diagnosis
        assert d.mismatch_class == "actual_is_subset" and not d.capped
        assert (d.uncapped_rows, d.reference_uncapped_rows) == (4, 5)

    def test_a_reference_cut_by_max_rows_whose_count_fails_is_unverified(self):
        class RefBroken(Warehouse):
            def __call__(self, sql):
                if sql.startswith("SELECT COUNT(*) FROM (") and "WHERE" not in sql:
                    self.calls.append(sql)
                    raise RuntimeError("Database error: timeout")
                return super().__call__(sql)

        backend = ScriptedBackend(f"SELECT ID FROM {T} WHERE ID > 0")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T}")
        with cfg.override_settings(default_top_n=2, max_rows_returned=3):
            result, _, _ = _run(backend, RefBroken(cut=True), case)
        assert result.status == "fingerprint_mismatch" and not result.passed
        assert result.diagnosis.capped_unverified is True
        assert result.diagnosis.reference_uncapped_rows is None

    def test_a_capped_result_with_a_wrong_row_fails(self):
        backend = ScriptedBackend(f"SELECT ID FROM {T} ORDER BY ID")  # rows 1 and 2
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T} WHERE ID >= 2")
        with cfg.override_settings(default_top_n=2):
            result, _, _ = _run(backend, case=case)
        assert result.status == "fingerprint_mismatch"  # row 1 is not in the reference
        assert result.diagnosis.mismatch_class == "row_count_differs" and not result.diagnosis.capped

    def test_a_subset_below_the_cap_is_still_wrong(self):
        backend = ScriptedBackend(f"SELECT ID FROM {T} WHERE ID <= 2")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T}")
        with cfg.override_settings(default_top_n=3):
            result, _, _ = _run(backend, case=case)
        assert result.status == "fingerprint_mismatch"
        assert result.diagnosis.mismatch_class == "actual_is_subset"
        assert result.diagnosis.capped is False

    def test_max_rows_returned_below_top_n_is_the_cap(self):
        backend = ScriptedBackend(f"SELECT ID FROM {T}")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T}")
        warehouse = Warehouse()
        generate, execute, trace = _pipeline(backend, warehouse)
        with cfg.override_settings(default_top_n=4, max_rows_returned=2):
            generate(case.question)
        assert trace.row_cap == 2

    def test_a_statement_with_its_own_top_is_capped_by_max_rows_returned_only(self):
        backend = ScriptedBackend(f"SELECT TOP 4 ID FROM {T}")
        generate, execute, trace = _pipeline(backend, Warehouse())
        with cfg.override_settings(default_top_n=3, max_rows_returned=10):
            generate("list?")
        assert trace.row_cap == 10  # nothing was injected; the model's own TOP is not a policy cap

    def test_no_row_value_reaches_the_share_file_for_a_capped_match(self):
        from eval.share import build_share_bundle, render_share_json
        from pathlib import Path

        backend = ScriptedBackend(f"SELECT Label FROM {T}")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT Label FROM {T}")
        warehouse = Warehouse(label="SENTINEL_CAP_")
        generate, execute, trace = _pipeline(backend, warehouse)
        with cfg.override_settings(default_top_n=2):
            results = run_golden_set([case], generate, execute, reference="live",
                                     source_trace=trace)
        report = build_report(results, mode="live", reference="live", pipeline="production")
        bundle = build_share_bundle(
            report, [case], golden_path=Path(__file__),
            run_info={"model": "m"}, started_at="a", finished_at="b")
        text = render_share_json(bundle)
        assert "SENTINEL_CAP_" not in text
        record = bundle["cases"][0]
        assert (record["passed"], record["status"]) == (True, "pass")
        assert record["diagnosis"]["mismatch_class"] == "match_capped"
        assert (record["diagnosis"]["capped"], record["diagnosis"]["row_cap"]) == (True, 2)
        assert record["diagnosis"]["uncapped_rows"] == 5
        assert record["diagnosis"]["capped_unverified"] is False
        assert bundle["summary"]["mismatch_classes"]["match_capped"] == 1
        assert bundle["summary"]["passed"] == 1 and bundle["summary"]["execution_accuracy_pct"] == 100.0

    def test_single_shot_has_no_cap_so_the_class_cannot_appear(self):
        frame = pd.DataFrame({"ID": [1, 2, 3, 4, 5]})
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT ID FROM {T}")
        result = run_case(case, lambda q: f"SELECT TOP 3 ID FROM {T}",
                          lambda sql: frame.head(3) if "TOP" in sql else frame, reference="live")
        assert result.status == "fingerprint_mismatch"
        assert result.diagnosis.mismatch_class == "actual_is_subset"

    def test_a_statement_with_its_own_cap_is_left_alone(self):
        backend = ScriptedBackend(f"SELECT TOP 2 ID FROM {T} ORDER BY ID")
        case = GoldenCase(id="rows", question="list?", expected_sql=f"SELECT TOP 2 ID FROM {T} ORDER BY ID")
        with cfg.override_settings(default_top_n=3):
            result, warehouse, _ = _run(backend, case=case)
        assert result.status == "pass"
        assert warehouse.calls[0] == f"SELECT TOP 2 ID FROM {T} ORDER BY ID"

    def test_the_generated_statement_is_executed_once_and_the_reference_for_real(self):
        result, warehouse, _ = _run(ScriptedBackend(GOOD))
        assert result.status == "pass"
        capped = f"SELECT TOP 1000 COUNT(*) AS n FROM {T}"
        assert warehouse.calls == [capped, GOOD]  # the generated run (capped), then the reference

    def test_the_same_executor_the_server_uses_is_the_default(self):
        import database.executor as executor

        assert cli.build_parser().parse_args(
            ["run", "--golden", "g", "--live"]).single_shot is False
        # the production pipeline is handed database.executor.execute_sql, which owns the
        # query timeout, the rolled-back transaction and max_rows_returned
        assert "max_rows_returned" in executor.execute_sql.__doc__


class TestSingleShot:
    @pytest.fixture
    def patched(self, monkeypatch):
        backend = ScriptedBackend(UNKNOWN_TABLE, GOOD)
        from llm.providers import OpenAIBackend

        monkeypatch.setattr(OpenAIBackend, "from_settings", classmethod(lambda cls: backend))
        monkeypatch.setattr(LLMRouter, "from_settings", classmethod(
            lambda cls: LLMRouter(default_chain=[backend])))
        return backend

    def test_single_shot_makes_one_call_and_no_correction(self, patched):
        trace = SourceTrace()
        generate, execute = cli._build_live_callables(trace=trace, single_shot=True)
        result = run_case(CASE, generate, lambda sql: pd.DataFrame({"n": [5]}), reference="live",
                          source_trace=trace)
        assert len(patched.prompts) == 1
        assert result.status == "guard_rejected" and result.corrections == 0
        assert [g["round"] for g in result.guard_rejections] == [1]

    def test_the_default_runs_the_correction_loop_on_the_same_model(self, patched, monkeypatch):
        import database.executor as executor

        warehouse = Warehouse()
        monkeypatch.setattr(executor, "execute_sql", lambda sql, **kw: warehouse(sql))
        trace = SourceTrace()
        generate, execute = cli._build_live_callables(trace=trace)  # single_shot defaults to False
        result = run_case(CASE, generate, execute, reference="live", source_trace=trace)
        assert len(patched.prompts) == 2
        assert result.status == "pass" and result.corrections == 1


class TestCliModes:
    @pytest.fixture
    def recorded(self, monkeypatch):
        seen = {}

        def build(structured=False, trace=None, single_shot=False):
            seen["single_shot"] = single_shot
            warehouse = Warehouse()
            return (lambda q: GOOD), warehouse

        monkeypatch.setattr(cli, "_build_live_callables", build)
        monkeypatch.setattr(cli, "_print_prefix_cache_probe", lambda question: None)
        monkeypatch.setattr(cli, "_collect_run_info", lambda args: {
            "app_version": "1", "prefix_version": "p", "model": "m",
            "prompt_retrieval_token_budget": 1, "sources": {}, "structured": False,
            "float_tolerance": 1e-6})
        return seen

    def _golden(self, tmp_path):
        path = tmp_path / "g.jsonl"
        path.write_text(json.dumps({"id": "c1", "question": "how many?", "expected_sql": GOOD,
                                    "tags": ["t"]}) + "\n", encoding="utf-8")
        return path

    def test_live_is_the_production_pipeline_by_default_and_says_so_everywhere(
        self, tmp_path, recorded, capsys
    ):
        out, share = tmp_path / "r.json", tmp_path / "out" / "s.json"
        code = cli.main(["run", "--golden", str(self._golden(tmp_path)), "--live",
                         "--reference", "live", "--out", str(out), "--share", str(share)])
        assert code == 0 and recorded["single_shot"] is False
        assert json.loads(out.read_text(encoding="utf-8"))["pipeline"] == "production"
        bundle = json.loads(share.read_text(encoding="utf-8"))
        assert bundle["run"]["pipeline"] == "production"
        assert any("SQLAgent" in note for note in bundle["notes"])
        printed = capsys.readouterr().out
        assert "Pipeline: production" in printed and "pipeline production" in printed

    def test_single_shot_flag_is_recorded(self, tmp_path, recorded):
        out, share = tmp_path / "r.json", tmp_path / "out" / "s.json"
        cli.main(["run", "--golden", str(self._golden(tmp_path)), "--live", "--single-shot",
                  "--reference", "live", "--out", str(out), "--share", str(share)])
        assert recorded["single_shot"] is True
        assert json.loads(out.read_text(encoding="utf-8"))["pipeline"] == "single_shot"
        bundle = json.loads(share.read_text(encoding="utf-8"))
        assert bundle["run"]["pipeline"] == "single_shot"
        assert any("corrections is 0 by construction" in note for note in bundle["notes"])

    def test_offline_runs_have_no_pipeline(self, tmp_path):
        golden = tmp_path / "g.jsonl"
        golden.write_text(json.dumps({
            "id": "c1", "question": "how many?", "expected_sql": GOOD,
            "expected_rows": [{"n": 3}]}) + "\n", encoding="utf-8")
        out = tmp_path / "r.json"
        assert cli.main(["run", "--golden", str(golden), "--out", str(out)]) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["pipeline"] is None

    def test_refusals(self, tmp_path, capsys):
        golden = str(self._golden(tmp_path))
        assert cli.main(["run", "--golden", golden, "--single-shot"]) == 2
        assert "--single-shot only applies with --live" in capsys.readouterr().err
        assert cli.main(["run", "--golden", golden, "--live", "--structured"]) == 2
        assert "--structured needs --single-shot" in capsys.readouterr().err

    def test_no_ci_job_runs_live(self):
        from pathlib import Path

        workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
        for path in workflows.glob("*.yml"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "eval.cli" in line:
                    assert "--live" not in line, f"{path.name}: {line.strip()}"


class TestBaselineAcrossPipelines:
    def _report(self, pipeline):
        from eval.models import CaseResult

        results = [CaseResult("a", "q", [], "pass", "SELECT 1", "f", None, 0.1)]
        return build_report(results, mode="live", reference="live", pipeline=pipeline)

    def test_a_baseline_from_the_other_pipeline_is_refused(self):
        with pytest.raises(ValueError, match="'production' run with a 'single_shot'"):
            compare_to_baseline(self._report("production"), self._report("single_shot"))

    def test_a_baseline_written_before_the_field_existed_was_single_shot(self):
        compare_to_baseline(self._report("single_shot"), self._report(None))
        with pytest.raises(ValueError, match="pipeline|single-shot"):
            compare_to_baseline(self._report("production"), self._report(None))

    def test_same_pipeline_compares(self):
        assert not compare_to_baseline(self._report("production"), self._report("production")).regressed
