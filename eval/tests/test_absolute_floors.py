# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for the optional absolute accuracy floors of the release gate.

The relative drop threshold (``EVAL_MAX_ACCURACY_DROP_PCT``) cannot stop a
release whose baseline was already poor. ``EVAL_MIN_ACCURACY`` and
``EVAL_MIN_SOURCE_ACCURACY`` (``--min-accuracy`` / ``--min-source-accuracy``)
are fixed floors that apply whatever the baseline says, and are off unless
set. Covered here: the settings parser, :func:`eval.baseline.check_absolute_floors`,
its use inside :func:`eval.baseline.compare_to_baseline`, and the CLI.
"""

from __future__ import annotations

import json

import pytest

import config as cfg
from config import Settings, _parse_optional_percent
from eval.baseline import (
    BaselineThresholds,
    check_absolute_floors,
    compare_to_baseline,
    exit_code,
)
from eval.cli import build_parser, main
from eval.models import CaseResult
from eval.report import build_report
from schema_data.columns import TABLE_COLUMNS

_ANY_KNOWN_TABLE = next(iter(TABLE_COLUMNS))


def _result(case_id: str, ok: bool, source: str | None = None) -> CaseResult:
    return CaseResult(
        case_id,
        "q",
        [],
        "pass" if ok else "fingerprint_mismatch",
        "SELECT 1",
        "fp",
        None if ok else "mismatch",
        0.1,
        source,
        source,
    )


def _report(passed: int, failed: int, source: str | None = None):
    results = [_result(f"p{i}", True, source) for i in range(passed)]
    results += [_result(f"f{i}", False, source) for i in range(failed)]
    return build_report(results, mode="live")


# ---------------------------------------------------------------------------
# Settings parsing
# ---------------------------------------------------------------------------


class TestParseOptionalPercent:
    def test_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("X_PCT", raising=False)
        assert _parse_optional_percent("X_PCT") is None

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_is_none(self, monkeypatch, raw):
        monkeypatch.setenv("X_PCT", raw)
        assert _parse_optional_percent("X_PCT") is None

    @pytest.mark.parametrize(("raw", "expected"), [("0", 0.0), ("92.5", 92.5), (" 100 ", 100.0)])
    def test_valid_values(self, monkeypatch, raw, expected):
        monkeypatch.setenv("X_PCT", raw)
        assert _parse_optional_percent("X_PCT") == expected

    @pytest.mark.parametrize("raw", ["abc", "-1", "100.1", "nan", "inf"])
    def test_invalid_values_name_the_variable_and_value(self, monkeypatch, raw):
        monkeypatch.setenv("X_PCT", raw)
        with pytest.raises(ValueError, match=rf"X_PCT.*{raw!r}"):
            _parse_optional_percent("X_PCT")


class TestSettingsDefaults:
    def test_floors_are_off_by_default(self, monkeypatch):
        monkeypatch.delenv("EVAL_MIN_ACCURACY", raising=False)
        monkeypatch.delenv("EVAL_MIN_SOURCE_ACCURACY", raising=False)
        settings = Settings()
        assert settings.eval_min_accuracy is None
        assert settings.eval_min_source_accuracy is None

    def test_floors_are_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("EVAL_MIN_ACCURACY", "90")
        monkeypatch.setenv("EVAL_MIN_SOURCE_ACCURACY", "75.5")
        settings = Settings()
        assert settings.eval_min_accuracy == 90.0
        assert settings.eval_min_source_accuracy == 75.5

    def test_thresholds_default_to_the_settings(self):
        with cfg.override_settings(eval_min_accuracy=88.0, eval_min_source_accuracy=70.0):
            thresholds = BaselineThresholds()
        assert thresholds.min_accuracy_pct == 88.0
        assert thresholds.min_source_accuracy_pct == 70.0


# ---------------------------------------------------------------------------
# check_absolute_floors
# ---------------------------------------------------------------------------


class TestCheckAbsoluteFloors:
    def test_nothing_set_means_nothing_checked(self):
        assert check_absolute_floors(_report(0, 10), BaselineThresholds()) == []

    def test_below_overall_floor_names_figure_and_floor(self):
        report = _report(7, 3)  # 70%
        messages = check_absolute_floors(report, BaselineThresholds(min_accuracy_pct=90.0))
        assert messages == ["accuracy 70.00% (7/10) is below the absolute floor of 90.00%"]

    def test_exactly_at_the_floor_passes(self):
        report = _report(9, 1)  # 90%
        assert check_absolute_floors(report, BaselineThresholds(min_accuracy_pct=90.0)) == []

    def test_above_the_floor_passes(self):
        assert check_absolute_floors(_report(10, 0), BaselineThresholds(min_accuracy_pct=99.0)) == []

    def test_empty_run_fails_a_non_zero_floor(self):
        report = build_report([], mode="live")
        assert check_absolute_floors(report, BaselineThresholds(min_accuracy_pct=1.0))

    def test_zero_floor_never_fails(self):
        report = build_report([], mode="live")
        assert check_absolute_floors(report, BaselineThresholds(min_accuracy_pct=0.0)) == []

    def test_per_source_floor_names_the_failing_source(self):
        results = (
            [_result(f"a{i}", True, "alpha") for i in range(9)]
            + [_result("a9", False, "alpha")]
            + [_result("b0", True, "beta"), _result("b1", False, "beta")]
        )
        report = build_report(results, mode="live")
        thresholds = BaselineThresholds(min_source_accuracy_pct=80.0)
        assert check_absolute_floors(report, thresholds) == [
            "source 'beta' accuracy 50.00% (1/2) is below the per-source floor of 80.00%"
        ]

    def test_overall_floor_can_pass_while_a_source_fails(self):
        results = [_result(f"a{i}", True, "alpha") for i in range(18)] + [
            _result("b0", False, "beta"),
            _result("b1", False, "beta"),
        ]
        report = build_report(results, mode="live")  # 90% overall, beta at 0%
        thresholds = BaselineThresholds(min_accuracy_pct=85.0, min_source_accuracy_pct=50.0)
        messages = check_absolute_floors(report, thresholds)
        assert len(messages) == 1
        assert "beta" in messages[0]

    def test_per_source_floor_without_per_source_figures_fails_closed(self):
        report = _report(5, 0, source=None)
        messages = check_absolute_floors(report, BaselineThresholds(min_source_accuracy_pct=50.0))
        assert len(messages) == 1
        assert "no per-source figures" in messages[0]

    def test_both_floors_report_both_failures(self):
        report = _report(1, 3, source="alpha")  # 25% overall and for alpha
        thresholds = BaselineThresholds(min_accuracy_pct=50.0, min_source_accuracy_pct=50.0)
        assert len(check_absolute_floors(report, thresholds)) == 2


# ---------------------------------------------------------------------------
# compare_to_baseline integration
# ---------------------------------------------------------------------------


class TestCompareToBaselineWithFloors:
    def test_poor_baseline_cannot_hide_a_poor_run(self):
        # The relative gate passes (no drop at all) -- the floor must still fail.
        baseline = _report(5, 5)
        current = _report(5, 5)
        loose = BaselineThresholds(max_latency_p95_increase_pct=1e9)
        assert not compare_to_baseline(current, baseline, loose).regressed

        floored = BaselineThresholds(
            max_latency_p95_increase_pct=1e9, min_accuracy_pct=80.0
        )
        comparison = compare_to_baseline(current, baseline, floored)
        assert comparison.regressed
        assert comparison.messages == []
        assert comparison.floor_messages == [
            "accuracy 50.00% (5/10) is below the absolute floor of 80.00%"
        ]
        assert exit_code(comparison) == 1

    def test_floors_met_do_not_regress(self):
        report = _report(9, 1)
        thresholds = BaselineThresholds(
            max_latency_p95_increase_pct=1e9, min_accuracy_pct=90.0
        )
        comparison = compare_to_baseline(report, report, thresholds)
        assert not comparison.regressed
        assert comparison.floor_messages == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_PASSING_CASE = {
    "id": "c1",
    "question": "how many?",
    "tags": ["count"],
    "expected_sql": f"SELECT COUNT(*) AS n FROM {_ANY_KNOWN_TABLE}",
    "expected_rows": [{"n": 3}],
}
# No expected_rows: the offline executor cannot replay it, so the case fails.
_FAILING_CASE = dict(
    _PASSING_CASE,
    id="c2",
    question="how many again?",
    expected_sql=f"SELECT COUNT(*) AS m FROM {_ANY_KNOWN_TABLE}",
    expected_rows=None,
)


def _golden(tmp_path, cases):
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in cases) + "\n", encoding="utf-8")
    return path


class TestCliFlags:
    def test_flags_default_to_off(self):
        args = build_parser().parse_args(["run", "--golden", "g.jsonl"])
        assert args.min_accuracy is None
        assert args.min_source_accuracy is None

    def test_flags_parse_as_floats(self):
        args = build_parser().parse_args(
            ["run", "--golden", "g", "--min-accuracy", "90", "--min-source-accuracy", "80.5"]
        )
        assert args.min_accuracy == 90.0
        assert args.min_source_accuracy == 80.5

    @pytest.mark.parametrize("flag", ["--min-accuracy", "--min-source-accuracy"])
    @pytest.mark.parametrize("value", ["101", "-5", "abc"])
    def test_out_of_range_values_are_rejected(self, flag, value, capsys):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["run", "--golden", "g", flag, value])
        assert excinfo.value.code == 2
        assert "between 0 and 100" in capsys.readouterr().err

    def test_defaults_come_from_the_settings(self):
        with cfg.override_settings(eval_min_accuracy=77.0, eval_min_source_accuracy=66.0):
            args = build_parser().parse_args(["run", "--golden", "g.jsonl"])
        assert args.min_accuracy == 77.0
        assert args.min_source_accuracy == 66.0


class TestCliGate:
    def test_without_floors_a_failing_run_still_exits_zero(self, tmp_path):
        golden = _golden(tmp_path, [_FAILING_CASE])
        assert main(["run", "--golden", str(golden)]) == 0

    def test_below_floor_without_a_baseline_fails_naming_figure_and_floor(
        self, tmp_path, capsys
    ):
        golden = _golden(tmp_path, [_PASSING_CASE, _FAILING_CASE])
        code = main(["run", "--golden", str(golden), "--min-accuracy", "90"])
        out = capsys.readouterr().out
        assert code == 1
        assert "BELOW ABSOLUTE ACCURACY FLOOR" in out
        assert "accuracy 50.00% (1/2) is below the absolute floor of 90.00%" in out

    def test_at_or_above_floor_exits_zero(self, tmp_path, capsys):
        golden = _golden(tmp_path, [_PASSING_CASE, _FAILING_CASE])
        assert main(["run", "--golden", str(golden), "--min-accuracy", "50"]) == 0
        assert "Absolute accuracy floors met." in capsys.readouterr().out

    def test_floor_from_the_environment_setting_applies(self, tmp_path):
        golden = _golden(tmp_path, [_PASSING_CASE, _FAILING_CASE])
        with cfg.override_settings(eval_min_accuracy=90.0):
            assert main(["run", "--golden", str(golden)]) == 1

    def test_flag_overrides_the_environment_setting(self, tmp_path):
        golden = _golden(tmp_path, [_PASSING_CASE, _FAILING_CASE])
        with cfg.override_settings(eval_min_accuracy=90.0):
            assert main(["run", "--golden", str(golden), "--min-accuracy", "10"]) == 0

    def test_per_source_floor_without_source_figures_fails(self, tmp_path, capsys):
        golden = _golden(tmp_path, [_PASSING_CASE])
        code = main(["run", "--golden", str(golden), "--min-source-accuracy", "50"])
        assert code == 1
        assert "no per-source figures" in capsys.readouterr().out

    def test_floor_applies_alongside_a_baseline_that_shows_no_regression(
        self, tmp_path, capsys
    ):
        golden = _golden(tmp_path, [_PASSING_CASE, _FAILING_CASE])
        baseline = tmp_path / "baseline.json"
        main(["run", "--golden", str(golden), "--save-baseline", str(baseline)])
        capsys.readouterr()

        code = main(
            [
                "run",
                "--golden",
                str(golden),
                "--baseline",
                str(baseline),
                "--max-latency-p95-increase-pct",
                "100000",
                "--min-accuracy",
                "90",
            ]
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "No regression versus baseline." in out
        assert "BELOW ABSOLUTE ACCURACY FLOOR" in out
        assert "is below the absolute floor of 90.00%" in out
