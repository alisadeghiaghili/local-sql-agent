# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for eval/baseline.py.

Run::

    .venv/Scripts/python.exe -m pytest eval/tests/test_baseline.py -v --no-cov
"""

from __future__ import annotations

import json

import pytest

from eval.baseline import (
    BaselineThresholds,
    ComparisonResult,
    compare_to_baseline,
    exit_code,
    load_baseline,
    save_baseline,
)
from eval.models import CaseResult
from eval.report import build_report


def _result(case_id="a", status="pass", latency=1.0, tags=None) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        question=f"q-{case_id}",
        tags=tags or [],
        status=status,
        generated_sql="SELECT 1",
        actual_fingerprint="fp" if status == "pass" else None,
        error=None if status == "pass" else "boom",
        latency_seconds=latency,
    )


# ---------------------------------------------------------------------------
# BaselineThresholds
# ---------------------------------------------------------------------------


class TestBaselineThresholds:
    def test_defaults(self):
        t = BaselineThresholds()
        assert t.max_accuracy_drop_pct == 5.0
        assert t.max_latency_p95_increase_pct == 20.0
        assert t.max_guard_rejection_increase == 0

    def test_is_frozen(self):
        t = BaselineThresholds()
        with pytest.raises((AttributeError, TypeError)):
            t.max_accuracy_drop_pct = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# save_baseline / load_baseline round trip
# ---------------------------------------------------------------------------


class TestSaveLoadRoundTrip:
    def test_round_trip_preserves_scalar_fields(self, tmp_path):
        results = [_result("a", status="pass"), _result("b", status="fingerprint_mismatch")]
        report = build_report(results, mode="live")
        path = tmp_path / "baseline.json"

        save_baseline(report, path)
        loaded = load_baseline(path)

        assert loaded.mode == report.mode
        assert loaded.total == report.total
        assert loaded.passed == report.passed
        assert loaded.accuracy_pct == report.accuracy_pct
        assert loaded.guard_rejections == report.guard_rejections
        assert loaded.latency_p50 == report.latency_p50
        assert loaded.latency_p95 == report.latency_p95
        assert loaded.latency_p99 == report.latency_p99
        assert loaded.generated_at == report.generated_at

    def test_round_trip_preserves_tag_accuracy(self, tmp_path):
        results = [_result("a", tags=["count"], status="pass")]
        report = build_report(results, mode="offline")
        path = tmp_path / "baseline.json"
        save_baseline(report, path)
        loaded = load_baseline(path)
        assert loaded.tag_accuracy["count"] == (1, 1)

    def test_round_trip_preserves_results(self, tmp_path):
        results = [_result("a", status="pass"), _result("b", status="guard_rejected")]
        report = build_report(results, mode="offline")
        path = tmp_path / "baseline.json"
        save_baseline(report, path)
        loaded = load_baseline(path)
        assert [r.case_id for r in loaded.results] == ["a", "b"]
        assert [r.status for r in loaded.results] == ["pass", "guard_rejected"]

    def test_creates_parent_directories(self, tmp_path):
        report = build_report([_result("a")], mode="offline")
        path = tmp_path / "nested" / "baseline.json"
        save_baseline(report, path)
        assert path.exists()

    def test_accepts_str_path(self, tmp_path):
        report = build_report([_result("a")], mode="offline")
        path = str(tmp_path / "baseline.json")
        save_baseline(report, path)
        loaded = load_baseline(path)
        assert loaded.total == 1


class TestLoadBaselineErrors:
    def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_baseline(tmp_path / "nope.json")

    def test_invalid_json_raises_value_error(self, tmp_path):
        path = tmp_path / "baseline.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid JSON"):
            load_baseline(path)

    def test_missing_key_raises_value_error(self, tmp_path):
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps({"mode": "offline"}), encoding="utf-8")
        with pytest.raises(ValueError, match="missing expected key"):
            load_baseline(path)


# ---------------------------------------------------------------------------
# compare_to_baseline
# ---------------------------------------------------------------------------


class TestCompareToBaseline:
    def _report(self, statuses, latency=1.0, mode="live"):
        results = [_result(f"c{i}", status=s, latency=latency) for i, s in enumerate(statuses)]
        return build_report(results, mode=mode)

    def test_identical_reports_do_not_regress(self):
        baseline = self._report(["pass", "pass"])
        current = self._report(["pass", "pass"])
        comparison = compare_to_baseline(current, baseline)
        assert comparison.regressed is False
        assert comparison.accuracy_delta_pct == 0.0
        assert comparison.messages == []

    def test_accuracy_drop_within_threshold_does_not_regress(self):
        baseline = self._report(["pass"] * 10)  # 100%
        current = self._report(["pass"] * 9 + ["fingerprint_mismatch"])  # 90%
        comparison = compare_to_baseline(current, baseline, BaselineThresholds(max_accuracy_drop_pct=10.0))
        assert comparison.regressed is False

    def test_accuracy_drop_beyond_threshold_regresses(self):
        baseline = self._report(["pass"] * 10)  # 100%
        current = self._report(["pass"] * 5 + ["fingerprint_mismatch"] * 5)  # 50%
        comparison = compare_to_baseline(current, baseline, BaselineThresholds(max_accuracy_drop_pct=10.0))
        assert comparison.regressed is True
        assert comparison.accuracy_delta_pct == -50.0
        assert any("accuracy dropped" in m for m in comparison.messages)

    def test_accuracy_improvement_never_regresses(self):
        baseline = self._report(["pass", "fingerprint_mismatch"])  # 50%
        current = self._report(["pass", "pass"])  # 100%
        comparison = compare_to_baseline(current, baseline)
        assert comparison.regressed is False
        assert comparison.accuracy_delta_pct == 50.0

    def test_latency_increase_beyond_threshold_regresses(self):
        baseline = self._report(["pass"], latency=1.0)
        current = self._report(["pass"], latency=2.0)  # +100%
        comparison = compare_to_baseline(current, baseline, BaselineThresholds(max_latency_p95_increase_pct=20.0))
        assert comparison.regressed is True
        assert comparison.latency_p95_delta_pct == pytest.approx(100.0)
        assert any("latency p95 increased" in m for m in comparison.messages)

    def test_latency_increase_within_threshold_does_not_regress(self):
        baseline = self._report(["pass"], latency=1.0)
        current = self._report(["pass"], latency=1.1)  # +10%
        comparison = compare_to_baseline(current, baseline, BaselineThresholds(max_latency_p95_increase_pct=20.0))
        assert comparison.regressed is False

    def test_zero_baseline_latency_skips_latency_check(self):
        baseline = self._report(["pass"], latency=0.0)
        current = self._report(["pass"], latency=5.0)
        comparison = compare_to_baseline(current, baseline)
        assert comparison.latency_p95_delta_pct is None
        assert comparison.regressed is False

    def test_guard_rejection_increase_regresses_by_default(self):
        baseline = self._report(["pass", "pass"])
        current = self._report(["pass", "guard_rejected"])
        comparison = compare_to_baseline(current, baseline)
        assert comparison.regressed is True
        assert comparison.guard_rejection_delta == 1
        assert any("guard rejections increased" in m for m in comparison.messages)

    def test_guard_rejection_increase_allowed_when_threshold_raised(self):
        baseline = self._report(["pass", "pass"])
        current = self._report(["pass", "guard_rejected"])
        comparison = compare_to_baseline(
            current,
            baseline,
            BaselineThresholds(max_guard_rejection_increase=1, max_accuracy_drop_pct=100.0),
        )
        assert comparison.regressed is False

    def test_multiple_violations_all_reported(self):
        baseline = self._report(["pass"] * 10, latency=1.0)
        current = self._report(["fingerprint_mismatch"] * 10, latency=5.0)
        comparison = compare_to_baseline(current, baseline)
        assert comparison.regressed is True
        assert len(comparison.messages) >= 2


# ---------------------------------------------------------------------------
# exit_code
# ---------------------------------------------------------------------------


class TestExitCode:
    def test_zero_when_not_regressed(self):
        comparison = ComparisonResult(False, 0.0, 0.0, 0, [])
        assert exit_code(comparison) == 0

    def test_one_when_regressed(self):
        comparison = ComparisonResult(True, -10.0, None, 0, ["accuracy dropped"])
        assert exit_code(comparison) == 1

class TestModeMismatchGuard:
    """A baseline recorded in one mode must never gate a run from the other.

    Offline accuracy is 100% by construction, so gating a live run against
    an offline baseline would report "no regression" no matter how far the
    engine had degraded. That is a false pass on the gate every later phase
    depends on, so the comparison refuses rather than guesses.
    """

    def test_live_run_against_offline_baseline_raises(self):
        offline_baseline = self._report(["pass"], latency=0.00005, mode="offline")
        live_current = self._report(["fail"], latency=2.0, mode="live")
        with pytest.raises(ValueError, match="Cannot compare"):
            compare_to_baseline(live_current, offline_baseline)

    def test_offline_run_against_live_baseline_raises(self):
        live_baseline = self._report(["pass"], latency=2.0, mode="live")
        offline_current = self._report(["pass"], latency=0.00005, mode="offline")
        with pytest.raises(ValueError, match="Cannot compare"):
            compare_to_baseline(offline_current, live_baseline)

    def test_same_mode_still_compares(self):
        baseline = self._report(["pass"], latency=1.0, mode="live")
        current = self._report(["pass"], latency=1.0, mode="live")
        assert compare_to_baseline(current, baseline).regressed is False

    def _report(self, statuses, latency=1.0, mode="live"):
        results = [_result(f"c{i}", status=s, latency=latency) for i, s in enumerate(statuses)]
        return build_report(results, mode=mode)
