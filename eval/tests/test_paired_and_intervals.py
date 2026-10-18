# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Intervals in a report, the paired block of ``compare``, and old-report compatibility.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_paired_and_intervals.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import cli
from eval.baseline import load_baseline
from eval.models import CaseResult
from eval.report import benchmark_from_results, build_report, render_text
from eval.rounds import compare_runs, load_run, render_comparison


def _result(i: int, passed: bool, tags: tuple[str, ...] = ("t",)) -> CaseResult:
    return CaseResult(
        f"c{i:03d}", "q", list(tags), "pass" if passed else "fingerprint_mismatch",
        "SELECT 1", "f", None, 0.1,
    )


class TestReportIntervals:
    def test_overall_and_per_tag_carry_an_interval_and_n(self):
        results = [_result(i, i % 4 != 0, ("db:x",)) for i in range(52)]  # 39 of 52
        report = build_report(results, mode="live")
        text = render_text(report)
        assert "Execution accuracy: 75.00% (39/52)" in text
        assert "95% interval (Wilson) [61.8, 84.8], n=52" in text
        line = next(ln for ln in text.splitlines() if ln.strip().startswith("db:x"))
        assert "39/52" in line and "[61.8, 84.8]" in line and "not interpretable" not in line

    def test_a_small_stratum_is_marked_not_interpretable(self):
        results = [_result(i, True, ("big",)) for i in range(30)] + [
            _result(100 + i, i == 0, ("small",)) for i in range(5)
        ]
        text = render_text(build_report(results, mode="live"))
        small = next(ln for ln in text.splitlines() if ln.strip().startswith("small"))
        big = next(ln for ln in text.splitlines() if ln.strip().startswith("big"))
        assert "n<20: not interpretable" in small
        assert "not interpretable" not in big

    def test_the_json_carries_the_intervals_additively(self):
        data = build_report([_result(i, True) for i in range(25)], mode="live").to_dict()
        assert data["intervals"]["overall"]["n"] == 25
        assert data["intervals"]["per_tag"]["t"]["interpretable"] is True
        assert data["intervals"]["overall"]["wilson95_high_pct"] == 100.0
        assert data["benchmark"] is None

    def test_source_strata_have_intervals_too(self):
        results = [
            CaseResult("a", "q", [], "pass", "S", "f", None, 0.1, expected_datasource="sales"),
        ]
        data = build_report(results, mode="live").to_dict()
        assert data["intervals"]["per_source"]["sales"]["interpretable"] is False


class TestShareSummaryIntervals:
    def test_the_summary_carries_the_intervals_and_keeps_its_old_keys(self, tmp_path):
        from eval.models import GoldenCase
        from eval.share import build_share_bundle

        golden = tmp_path / "g.jsonl"
        golden.write_text("{}\n", encoding="utf-8")
        results = [_result(i, i % 4 != 0, ("db:x",)) for i in range(52)]
        report = build_report(results, mode="live", reference="live", pipeline="production")
        cases = [GoldenCase(id=r.case_id, question="q", expected_sql="SELECT 1") for r in results]
        info = {"app_version": "1", "prefix_version": "p", "model": "m",
                "prompt_retrieval_token_budget": 6000, "sources": {}, "structured": False,
                "float_tolerance": 1e-06}
        summary = build_share_bundle(
            report, cases, golden_path=golden, run_info=info, started_at="t0", finished_at="t1",
        )["summary"]
        assert summary["per_tag"]["db:x"] == {"passed": 39, "total": 52, "accuracy_pct": 75.0}
        assert summary["intervals"]["per_tag"]["db:x"]["wilson95_low_pct"] == 61.79
        assert summary["intervals"]["overall"]["n"] == 52


class TestBenchmarkHeader:
    def test_cases_tagged_bench_get_the_not_this_deployment_header(self):
        results = [_result(i, True, ("bench:bird", "db:x")) for i in range(3)]
        report = build_report(results, mode="live")
        assert report.benchmark == {"name": "bird"}
        first = render_text(report).splitlines()[0]
        assert first.startswith("BENCHMARK RUN (bird)")
        assert "NOT this deployment's accuracy" in first

    def test_a_deployments_own_golden_set_gets_no_header(self):
        report = build_report([_result(1, True)], mode="live")
        assert report.benchmark is None
        assert "BENCHMARK" not in render_text(report)

    def test_a_mixture_is_not_a_benchmark_figure(self):
        assert benchmark_from_results(
            [_result(1, True, ("bench:bird",)), _result(2, True, ("count",))]
        ) is None

    def test_the_header_survives_a_baseline_round_trip(self, tmp_path):
        report = build_report([_result(1, True, ("bench:bird",))], mode="live")
        path = tmp_path / "r.json"
        path.write_text(json.dumps(report.to_dict()), encoding="utf-8")
        assert load_baseline(path).benchmark == {"name": "bird"}


def _records(spec: dict[str, tuple[bool, bool]], which: int) -> list[dict]:
    return [
        {"case_id": cid, "tags": ["t"], "status": "pass" if flags[which] else "fingerprint_mismatch",
         "passed": flags[which]}
        for cid, flags in spec.items()
    ]


def _report_file(path: Path, records: list[dict]) -> Path:
    path.write_text(json.dumps({"results": records}), encoding="utf-8")
    return path


class TestPairedComparison:
    # 1 regressed, 5 fixed, 4 both pass, 12 both fail (22 cases)
    SPEC = {
        **{f"fix{i}": (False, True) for i in range(5)},
        "reg0": (True, False),
        **{f"keep{i}": (True, True) for i in range(4)},
        **{f"fail{i}": (False, False) for i in range(12)},
    }

    def _compare(self, tmp_path):
        old = _report_file(tmp_path / "old.json", _records(self.SPEC, 0))
        new = _report_file(tmp_path / "new.json", _records(self.SPEC, 1))
        return compare_runs(load_run(old), load_run(new))

    def test_the_counts_and_the_exact_p_value(self, tmp_path):
        paired = self._compare(tmp_path).paired
        assert (paired["fixed"], paired["regressed"], paired["unchanged"]) == (5, 1, 16)
        assert paired["n"] == 22
        assert paired["mcnemar_p"] == pytest.approx(14 / 64)   # 2 * (1 + 6) / 2**6
        assert paired["diff_points"] == pytest.approx(100 * 4 / 22, abs=0.01)
        low, high = paired["diff_ci95_points"]
        assert low < paired["diff_points"] < high
        assert paired["interpretable"] is True

    def test_the_text_and_the_json_report_it(self, tmp_path, capsys):
        comparison = self._compare(tmp_path)
        text = render_comparison(comparison)
        assert "Paired comparison on the 22 cases both rounds scored" in text
        assert "fixed 5, regressed 1, unchanged 16" in text
        assert "McNemar exact two-sided p = 0.2188" in text
        assert "Newcombe, paired" in text
        assert comparison.to_dict()["paired"]["mcnemar_p"] == pytest.approx(14 / 64)

    def test_only_cases_both_rounds_scored_are_paired(self, tmp_path):
        old = _report_file(tmp_path / "o.json", _records({"a": (True, True), "gone": (True, True)}, 0))
        new = _report_file(tmp_path / "n.json", _records({"a": (True, True), "fresh": (True, True)}, 1))
        paired = compare_runs(load_run(old), load_run(new)).paired
        assert paired["n"] == 1

    def test_a_small_comparison_is_flagged(self, tmp_path):
        old = _report_file(tmp_path / "o.json", _records({"a": (False, True)}, 0))
        new = _report_file(tmp_path / "n.json", _records({"a": (False, True)}, 1))
        comparison = compare_runs(load_run(old), load_run(new))
        assert comparison.paired["interpretable"] is False
        assert "not interpretable" in render_comparison(comparison)

    def test_no_change_reads_as_no_change(self, tmp_path):
        spec = {f"k{i}": (True, True) for i in range(25)}
        old = _report_file(tmp_path / "o.json", _records(spec, 0))
        new = _report_file(tmp_path / "n.json", _records(spec, 1))
        comparison = compare_runs(load_run(old), load_run(new))
        assert comparison.paired["mcnemar_p"] == 1.0
        assert "(no case changed)" in render_comparison(comparison)

    def test_the_cli_prints_the_paired_block_as_json(self, tmp_path, capsys):
        old = _report_file(tmp_path / "old.json", _records(self.SPEC, 0))
        new = _report_file(tmp_path / "new.json", _records(self.SPEC, 1))
        assert cli.main(["compare", str(old), str(new), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["paired"]["fixed"] == 5


class TestOldReportsStillLoad:
    """A report written before intervals, ``benchmark`` and the paired block existed."""

    OLD_REPORT = {
        "mode": "live", "total": 2, "passed": 1, "accuracy_pct": 50.0,
        "tag_accuracy": {"count": {"passed": 1, "total": 2}},
        "status_counts": {"pass": 1, "fingerprint_mismatch": 1},
        "guard_rejections": 0, "latency_p50": 0.1, "latency_p95": 0.2, "latency_p99": 0.2,
        "generated_at": "2025-01-01T00:00:00+00:00",
        "results": [
            {"case_id": "a", "question": "q", "tags": ["count"], "status": "pass", "passed": True,
             "generated_sql": "SELECT 1", "actual_fingerprint": "f", "error": None,
             "latency_seconds": 0.1},
            {"case_id": "b", "question": "q", "tags": ["count"], "status": "fingerprint_mismatch",
             "passed": False, "generated_sql": "SELECT 2", "actual_fingerprint": "g",
             "error": "x", "latency_seconds": 0.2},
        ],
    }

    def test_compare_reads_it(self, tmp_path):
        path = tmp_path / "old.json"
        path.write_text(json.dumps(self.OLD_REPORT), encoding="utf-8")
        comparison = compare_runs(load_run(path), load_run(path))
        assert comparison.paired["unchanged"] == 2
        assert comparison.paired["interpretable"] is False

    def test_a_baseline_reads_it(self, tmp_path):
        path = tmp_path / "old.json"
        path.write_text(json.dumps(self.OLD_REPORT), encoding="utf-8")
        report = load_baseline(path)
        assert report.benchmark is None
        assert "95% interval (Wilson)" in render_text(report)
