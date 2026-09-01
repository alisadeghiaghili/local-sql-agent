# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for eval/report.py.

Run::

    .venv/Scripts/python.exe -m pytest eval/tests/test_report.py -v --no-cov
"""

from __future__ import annotations

import json

import pytest

from eval.models import CaseResult
from eval.report import (
    ALL_STATUSES,
    _percentile,
    build_report,
    render_json,
    render_text,
    save_json_report,
)


def _result(case_id="a", tags=None, status="pass", latency=0.1, error=None) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        question=f"question for {case_id}",
        tags=tags or [],
        status=status,
        generated_sql="SELECT 1" if status != "generation_error" else None,
        actual_fingerprint="fp" if status == "pass" else None,
        error=error,
        latency_seconds=latency,
    )


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty_returns_zero(self):
        assert _percentile([], 50) == 0.0

    def test_single_value_returns_that_value_at_any_percentile(self):
        assert _percentile([7.0], 0) == 7.0
        assert _percentile([7.0], 50) == 7.0
        assert _percentile([7.0], 99) == 7.0

    def test_p50_of_four_values_interpolates(self):
        assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5

    def test_p0_is_minimum_p100_is_maximum(self):
        values = [3.0, 1.0, 4.0, 1.0, 5.0]
        values.sort()
        assert _percentile(values, 0) == 1.0
        assert _percentile(values, 100) == 5.0

    def test_exact_rank_no_interpolation_needed(self):
        # 5 values -> rank at p50 is exactly index 2, no fractional part.
        assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------


class TestBuildReport:
    def test_empty_results_gives_zero_total_and_zero_accuracy(self):
        report = build_report([], mode="offline")
        assert report.total == 0
        assert report.passed == 0
        assert report.accuracy_pct == 0.0
        assert report.tag_accuracy == {}
        assert report.latency_p50 == 0.0

    def test_all_passing(self):
        results = [_result("a"), _result("b")]
        report = build_report(results, mode="offline")
        assert report.total == 2
        assert report.passed == 2
        assert report.accuracy_pct == 100.0

    def test_mixed_pass_fail_accuracy(self):
        results = [_result("a", status="pass"), _result("b", status="fingerprint_mismatch")]
        report = build_report(results, mode="live")
        assert report.total == 2
        assert report.passed == 1
        assert report.accuracy_pct == 50.0
        assert report.mode == "live"

    def test_tag_accuracy_aggregates_across_cases(self):
        results = [
            _result("a", tags=["count"], status="pass"),
            _result("b", tags=["count", "join"], status="fingerprint_mismatch"),
            _result("c", tags=["join"], status="pass"),
        ]
        report = build_report(results, mode="offline")
        assert report.tag_accuracy["count"] == (1, 2)
        assert report.tag_accuracy["join"] == (1, 2)

    def test_tag_accuracy_keys_are_sorted(self):
        results = [_result("a", tags=["zeta", "alpha"])]
        report = build_report(results, mode="offline")
        assert list(report.tag_accuracy.keys()) == ["alpha", "zeta"]

    def test_status_counts_include_every_known_status_at_zero(self):
        report = build_report([_result("a", status="pass")], mode="offline")
        for status in ALL_STATUSES:
            assert status in report.status_counts
        assert report.status_counts["pass"] == 1
        assert report.status_counts["guard_rejected"] == 0

    def test_guard_rejections_matches_status_count(self):
        results = [
            _result("a", status="guard_rejected"),
            _result("b", status="guard_rejected"),
            _result("c", status="pass"),
        ]
        report = build_report(results, mode="offline")
        assert report.guard_rejections == 2
        assert report.status_counts["guard_rejected"] == 2

    def test_latency_percentiles_reflect_sorted_latencies(self):
        results = [_result("a", latency=0.1), _result("b", latency=0.3), _result("c", latency=0.2)]
        report = build_report(results, mode="offline")
        assert report.latency_p50 == 0.2

    def test_generated_at_is_iso_utc(self):
        report = build_report([_result("a")], mode="offline")
        assert report.generated_at.endswith("+00:00")

    def test_results_are_preserved_verbatim(self):
        results = [_result("a")]
        report = build_report(results, mode="offline")
        assert report.results == results


# ---------------------------------------------------------------------------
# render_text
# ---------------------------------------------------------------------------


class TestRenderText:
    def test_contains_accuracy_header(self):
        report = build_report([_result("a")], mode="offline")
        text = render_text(report)
        assert "Execution accuracy: 100.00% (1/1)" in text

    def test_contains_tag_breakdown_line(self):
        report = build_report([_result("a", tags=["count"])], mode="offline")
        text = render_text(report)
        assert "count" in text

    def test_no_tagged_cases_message(self):
        report = build_report([_result("a", tags=[])], mode="offline")
        text = render_text(report)
        assert "(no tagged cases)" in text

    def test_only_nonzero_statuses_are_listed(self):
        report = build_report([_result("a", status="pass")], mode="offline")
        text = render_text(report)
        assert "pass" in text
        assert "guard_rejected" not in text

    def test_contains_guard_rejections_and_latency_lines(self):
        report = build_report([_result("a")], mode="offline")
        text = render_text(report)
        assert "Guard rejections: 0" in text
        assert "p50=" in text and "p95=" in text and "p99=" in text


# ---------------------------------------------------------------------------
# render_json / save_json_report
# ---------------------------------------------------------------------------


class TestRenderJson:
    def test_round_trips_core_fields(self):
        results = [_result("a", tags=["count"], status="pass")]
        report = build_report(results, mode="live")
        data = json.loads(render_json(report))
        assert data["mode"] == "live"
        assert data["total"] == 1
        assert data["passed"] == 1
        assert data["tag_accuracy"]["count"] == {"passed": 1, "total": 1}
        assert len(data["results"]) == 1
        assert data["results"][0]["case_id"] == "a"

    def test_is_valid_json_and_not_ascii_escaped(self):
        results = [_result("a", tags=[])]
        report = build_report(results, mode="offline")
        rendered = render_json(report)
        json.loads(rendered)  # must not raise


class TestSaveJsonReport:
    def test_writes_file_readable_back_as_json(self, tmp_path):
        report = build_report([_result("a")], mode="offline")
        out_path = tmp_path / "report.json"
        save_json_report(report, out_path)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["total"] == 1

    def test_creates_parent_directories(self, tmp_path):
        report = build_report([_result("a")], mode="offline")
        out_path = tmp_path / "nested" / "dir" / "report.json"
        save_json_report(report, out_path)
        assert out_path.exists()

    def test_accepts_str_path(self, tmp_path):
        report = build_report([_result("a")], mode="offline")
        out_path = str(tmp_path / "report.json")
        save_json_report(report, out_path)
        assert json.loads(open(out_path, encoding="utf-8").read())["total"] == 1
