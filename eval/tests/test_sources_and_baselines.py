# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Per-source reporting, the report's ``reference`` field and baseline compatibility.

Covers the additive report fields (``reference``, ``source_accuracy``,
``source_selection``), that a baseline written before they existed still
loads and still gates, and the refusal to compare runs that judged results
differently.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_sources_and_baselines.py -q
"""

from __future__ import annotations

import json

import pytest

from eval.baseline import (
    BaselineThresholds,
    compare_to_baseline,
    load_baseline,
    save_baseline,
)
from eval.models import CaseResult
from eval.report import build_report, render_text


def _r(case_id, status="pass", expected=None, selected=None, latency=1.0):
    return CaseResult(
        case_id=case_id, question=f"q-{case_id}", tags=[], status=status,
        generated_sql="SELECT 1", actual_fingerprint="fp", error=None if status == "pass" else "x",
        latency_seconds=latency, expected_datasource=expected, selected_datasource=selected,
    )


MULTI = [
    _r("a", "pass", "sales", "sales"),
    _r("b", "fingerprint_mismatch", "sales", "sales"),
    _r("c", "pass", "inventory", "inventory"),
    _r("d", "fingerprint_mismatch", "inventory", "sales"),
    _r("e", "pass"),  # a case that names no source
]


class TestSourceAccuracy:
    def test_per_source_execution_accuracy(self):
        report = build_report(MULTI, mode="live")
        assert report.source_accuracy == {"inventory": (1, 2), "sales": (1, 2)}

    def test_source_selection_accuracy_is_live_only(self):
        live = build_report(MULTI, mode="live")
        assert live.source_selection == (3, 4)
        assert build_report(MULTI, mode="offline").source_selection is None

    def test_no_named_sources_means_no_source_figures(self):
        report = build_report([_r("x")], mode="live")
        assert report.source_accuracy == {}
        assert report.source_selection is None

    def test_a_case_that_never_selected_counts_as_a_selection_miss(self):
        report = build_report([_r("a", "generation_error", "sales", None)], mode="live")
        assert report.source_selection == (0, 1)

    def test_render_text_shows_both_figures(self):
        text = render_text(build_report(MULTI, mode="live"))
        assert "Per-source execution accuracy:" in text
        assert "sales" in text and "inventory" in text
        assert "Source-selection accuracy: 75.00% (3/4)" in text

    def test_render_text_without_sources_has_no_source_section(self):
        assert "Per-source" not in render_text(build_report([_r("x")], mode="live"))

    def test_render_text_names_the_reference_for_live_runs_only(self):
        assert "Reference: live" in render_text(build_report(MULTI, "live", reference="live"))
        assert "Reference: stored" in render_text(build_report(MULTI, "live"))
        assert "Reference:" not in render_text(build_report(MULTI, "offline"))


class TestRoundTripAndOldBaselines:
    def test_new_fields_survive_save_and_load(self, tmp_path):
        report = build_report(MULTI, mode="live", reference="live")
        save_baseline(report, tmp_path / "b.json")
        loaded = load_baseline(tmp_path / "b.json")
        assert loaded.reference == "live"
        assert loaded.source_accuracy == report.source_accuracy
        assert loaded.source_selection == (3, 4)
        assert loaded.results[3].selected_datasource == "sales"
        assert loaded.results[3].expected_datasource == "inventory"

    def test_a_baseline_written_before_these_fields_still_loads(self, tmp_path):
        old = {
            "mode": "live", "total": 2, "passed": 1, "accuracy_pct": 50.0,
            "tag_accuracy": {"t": {"passed": 1, "total": 2}},
            "status_counts": {"pass": 1, "fingerprint_mismatch": 1},
            "guard_rejections": 0, "latency_p50": 1.0, "latency_p95": 2.0, "latency_p99": 2.0,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "results": [
                {"case_id": "a", "question": "q", "tags": ["t"], "status": "pass", "passed": True,
                 "generated_sql": "SELECT 1", "actual_fingerprint": "f", "error": None,
                 "latency_seconds": 1.0},
                {"case_id": "b", "question": "q2", "tags": ["t"], "status": "fingerprint_mismatch",
                 "passed": False, "generated_sql": "SELECT 2", "actual_fingerprint": "g",
                 "error": "x", "latency_seconds": 2.0},
            ],
        }
        path = tmp_path / "old.json"
        path.write_text(json.dumps(old), encoding="utf-8")
        loaded = load_baseline(path)
        assert loaded.reference == "stored"
        assert loaded.source_accuracy == {}
        assert loaded.source_selection is None
        assert loaded.results[0].expected_datasource is None

    def test_an_old_baseline_still_gates_a_new_stored_run(self, tmp_path):
        old = build_report([_r("a"), _r("b")], mode="live")
        data = old.to_dict()
        for key in ("reference", "source_accuracy", "source_selection"):
            data.pop(key)
        for result in data["results"]:
            for key in ("expected_datasource", "selected_datasource", "reference_fingerprint"):
                result.pop(key)
        path = tmp_path / "old.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        current = build_report([_r("a"), _r("b", "fingerprint_mismatch")], mode="live")
        comparison = compare_to_baseline(
            current, load_baseline(path), BaselineThresholds(max_accuracy_drop_pct=10.0)
        )
        assert comparison.regressed
        assert comparison.source_deltas_pct == {}
        assert comparison.source_selection_delta_pct is None

    def test_an_unknown_reference_value_is_rejected(self, tmp_path):
        data = build_report([_r("a")], mode="live").to_dict()
        data["reference"] = "sometimes"
        path = tmp_path / "b.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="unknown reference"):
            load_baseline(path)


class TestReferenceGuard:
    def test_stored_run_against_live_baseline_is_refused(self):
        stored = build_report([_r("a")], mode="live", reference="stored")
        live = build_report([_r("a")], mode="live", reference="live")
        with pytest.raises(ValueError, match="--reference live"):
            compare_to_baseline(live, stored)
        with pytest.raises(ValueError, match="--reference stored"):
            compare_to_baseline(stored, live)

    def test_same_reference_compares_normally(self):
        a = build_report([_r("a")], mode="live", reference="live")
        assert not compare_to_baseline(a, a).regressed

    def test_mode_mismatch_is_still_refused_first(self):
        with pytest.raises(ValueError, match="not commensurable"):
            compare_to_baseline(
                build_report([_r("a")], mode="live"), build_report([_r("a")], mode="offline")
            )


class TestSourceDeltas:
    def test_deltas_are_informational_and_do_not_gate(self):
        baseline = build_report(
            [_r("a", "pass", "sales", "sales"), _r("b", "pass", "inventory", "inventory"),
             _r("c", "pass", "inventory", "inventory"), _r("d", "pass", "sales", "sales")],
            mode="live",
        )
        current = build_report(
            [_r("a", "pass", "sales", "sales"), _r("b", "pass", "inventory", "sales"),
             _r("c", "pass", "inventory", "inventory"), _r("d", "pass", "sales", "sales")],
            mode="live",
        )
        comparison = compare_to_baseline(current, baseline)
        assert not comparison.regressed
        assert comparison.source_deltas_pct == {"inventory": 0.0, "sales": 0.0}
        assert comparison.source_selection_delta_pct == pytest.approx(-25.0)

    def test_a_source_that_broke_shows_in_its_delta(self):
        good = build_report([_r("a", "pass", "sales", "sales"), _r("b", "pass", "sales", "sales")], "live")
        bad = build_report(
            [_r("a", "pass", "sales", "sales"), _r("b", "fingerprint_mismatch", "sales", "sales")], "live"
        )
        assert compare_to_baseline(bad, good).source_deltas_pct == {"sales": -50.0}
