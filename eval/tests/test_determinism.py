# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for eval/determinism.py.

Every test here drives :func:`~eval.determinism.probe_determinism` with a
plain stub ``generate_fn`` -- no real Ollama, no real database, exactly
like ``eval/tests/test_runner.py``'s ``TestLiveGenerator`` tests
:func:`~eval.runner.make_live_generator` with a stub backend. There is no
live endpoint available in this environment, so the actual "is a real
server deterministic" question is left unanswered here by design; what
this file proves is that the probe itself correctly classifies stable vs.
varying output once it is pointed at *something*.

Run::

    .venv/Scripts/python.exe -m pytest eval/tests/test_determinism.py -v --no-cov
"""

from __future__ import annotations

import json

import pytest

from eval.determinism import (
    MIN_REPEATS,
    DeterminismReport,
    QuestionDeterminism,
    _generate_variant,
    _variant_diff,
    probe_determinism,
    render_determinism_text,
    save_determinism_json,
)
from eval.models import GoldenCase

ONE_CASE = [GoldenCase(id="a", question="how many?", expected_sql="SELECT 1")]
TWO_CASES = [
    GoldenCase(id="a", question="how many?", expected_sql="SELECT 1"),
    GoldenCase(id="b", question="how many more?", expected_sql="SELECT 2"),
]


def _cycle(*responses: str):
    """Build a stub generate_fn that yields *responses* in order, looping."""
    state = {"i": 0}

    def _generate(question: str) -> str:
        value = responses[state["i"] % len(responses)]
        state["i"] += 1
        return value

    return _generate


# ---------------------------------------------------------------------------
# _generate_variant
# ---------------------------------------------------------------------------


class TestGenerateVariant:
    def test_returns_generated_sql_unchanged(self):
        assert _generate_variant(lambda q: "SELECT 1", "how many?") == "SELECT 1"

    def test_out_of_scope_sentinel_becomes_variant_string(self):
        def _refuse(q: str) -> str:
            raise ValueError("OUT_OF_SCOPE")

        assert _generate_variant(_refuse, "who won?") == "OUT_OF_SCOPE"

    def test_other_value_error_propagates(self):
        def _bad(q: str) -> str:
            raise ValueError("No SELECT / CTE found in model response")

        with pytest.raises(ValueError, match="No SELECT"):
            _generate_variant(_bad, "how many?")

    def test_non_value_error_propagates(self):
        def _boom(q: str) -> str:
            raise RuntimeError("endpoint unreachable")

        with pytest.raises(RuntimeError, match="endpoint unreachable"):
            _generate_variant(_boom, "how many?")


# ---------------------------------------------------------------------------
# QuestionDeterminism
# ---------------------------------------------------------------------------


class TestQuestionDeterminism:
    def test_single_variant_is_deterministic(self):
        result = QuestionDeterminism("a", "q", runs=3, variants=["SELECT 1"])
        assert result.is_deterministic is True

    def test_multiple_variants_is_not_deterministic(self):
        result = QuestionDeterminism("a", "q", runs=3, variants=["SELECT 1", "SELECT 2"])
        assert result.is_deterministic is False

    def test_to_dict_shape(self):
        result = QuestionDeterminism("a", "q", runs=3, variants=["SELECT 1", "SELECT 2"])
        data = result.to_dict()
        assert data == {
            "case_id": "a",
            "question": "q",
            "runs": 3,
            "is_deterministic": False,
            "variant_count": 2,
            "variants": ["SELECT 1", "SELECT 2"],
        }

    def test_zero_runs_rejected(self):
        with pytest.raises(ValueError, match="runs must be >= 1"):
            QuestionDeterminism("a", "q", runs=0, variants=["SELECT 1"])

    def test_empty_variants_rejected(self):
        with pytest.raises(ValueError, match="variants must be non-empty"):
            QuestionDeterminism("a", "q", runs=3, variants=[])

    def test_more_variants_than_runs_rejected(self):
        with pytest.raises(ValueError, match="cannot exceed"):
            QuestionDeterminism("a", "q", runs=1, variants=["SELECT 1", "SELECT 2"])


# ---------------------------------------------------------------------------
# DeterminismReport
# ---------------------------------------------------------------------------


class TestDeterminismReport:
    def test_varied_filters_to_non_deterministic_only(self):
        stable = QuestionDeterminism("a", "q1", runs=2, variants=["SELECT 1"])
        varied = QuestionDeterminism("b", "q2", runs=2, variants=["SELECT 1", "SELECT 2"])
        report = DeterminismReport(
            endpoint="stub:v1",
            repeats=2,
            total=2,
            deterministic=1,
            determinism_rate_pct=50.0,
            results=[stable, varied],
            generated_at="2026-08-30T00:00:00+00:00",
        )
        assert report.varied == [varied]

    def test_to_dict_shape(self):
        stable = QuestionDeterminism("a", "q1", runs=2, variants=["SELECT 1"])
        report = DeterminismReport(
            endpoint="stub:v1",
            repeats=2,
            total=1,
            deterministic=1,
            determinism_rate_pct=100.0,
            results=[stable],
            generated_at="2026-08-30T00:00:00+00:00",
        )
        data = report.to_dict()
        assert data["endpoint"] == "stub:v1"
        assert data["repeats"] == 2
        assert data["determinism_rate_pct"] == 100.0
        assert data["results"] == [stable.to_dict()]
        assert "varied" not in data  # derived, not persisted redundantly


# ---------------------------------------------------------------------------
# probe_determinism
# ---------------------------------------------------------------------------


class TestProbeDeterminism:
    def test_stable_generator_reports_100_percent(self):
        report = probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="stub:v1")
        assert report.determinism_rate_pct == 100.0
        assert report.deterministic == 1
        assert report.total == 1
        assert report.varied == []
        assert report.results[0].variants == ["SELECT 1"]

    def test_alternating_generator_reports_variation_and_both_variants(self):
        generate = _cycle("SELECT 1", "SELECT 2")
        report = probe_determinism(generate, ONE_CASE, endpoint="stub:v1", repeats=3)
        assert report.determinism_rate_pct == 0.0
        assert report.deterministic == 0
        assert len(report.varied) == 1
        varied_result = report.varied[0]
        assert varied_result.case_id == "a"
        assert set(varied_result.variants) == {"SELECT 1", "SELECT 2"}
        assert varied_result.runs == 3

    def test_mixed_stable_and_varied_cases(self):
        responses_by_question = {
            "how many?": _cycle("SELECT 1"),
            "how many more?": _cycle("SELECT 2", "SELECT 2 "),
        }

        def _generate(question: str) -> str:
            return responses_by_question[question](question)

        report = probe_determinism(_generate, TWO_CASES, endpoint="stub:v1", repeats=3)
        assert report.total == 2
        assert report.deterministic == 1
        assert report.determinism_rate_pct == 50.0
        assert [r.case_id for r in report.varied] == ["b"]

    def test_out_of_scope_flip_flop_counts_as_varied(self):
        """A generator that answers sometimes and refuses (OUT_OF_SCOPE) other
        times is exactly the instability this probe exists to catch."""
        state = {"i": 0}

        def _generate(question: str) -> str:
            state["i"] += 1
            if state["i"] % 2 == 0:
                raise ValueError("OUT_OF_SCOPE")
            return "SELECT 1"

        report = probe_determinism(_generate, ONE_CASE, endpoint="stub:v1", repeats=4)
        assert report.determinism_rate_pct == 0.0
        assert set(report.varied[0].variants) == {"SELECT 1", "OUT_OF_SCOPE"}

    def test_consistent_out_of_scope_is_deterministic(self):
        def _refuse(question: str) -> str:
            raise ValueError("OUT_OF_SCOPE")

        report = probe_determinism(_refuse, ONE_CASE, endpoint="stub:v1")
        assert report.determinism_rate_pct == 100.0
        assert report.results[0].variants == ["OUT_OF_SCOPE"]

    def test_transport_error_propagates_uncaught(self):
        def _boom(question: str) -> str:
            raise RuntimeError("connection refused")

        with pytest.raises(RuntimeError, match="connection refused"):
            probe_determinism(_boom, ONE_CASE, endpoint="stub:v1")

    def test_preserves_case_order(self):
        report = probe_determinism(lambda q: "SELECT 1", TWO_CASES, endpoint="stub:v1")
        assert [r.case_id for r in report.results] == ["a", "b"]

    def test_repeats_below_minimum_rejected(self):
        with pytest.raises(ValueError, match=f"repeats >= {MIN_REPEATS}"):
            probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="stub:v1", repeats=1)

    def test_repeats_of_zero_rejected(self):
        with pytest.raises(ValueError, match="repeats >= 2"):
            probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="stub:v1", repeats=0)

    def test_empty_endpoint_rejected(self):
        with pytest.raises(ValueError, match="non-empty endpoint"):
            probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="")

    def test_blank_endpoint_rejected(self):
        with pytest.raises(ValueError, match="non-empty endpoint"):
            probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="   ")

    def test_empty_cases_rejected(self):
        with pytest.raises(ValueError, match="at least one golden case"):
            probe_determinism(lambda q: "SELECT 1", [], endpoint="stub:v1")

    def test_endpoint_is_recorded_on_report(self):
        report = probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="ollama:gpt-oss")
        assert report.endpoint == "ollama:gpt-oss"

    def test_repeats_is_recorded_on_report(self):
        report = probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="stub:v1", repeats=5)
        assert report.repeats == 5
        assert report.results[0].runs == 5


# ---------------------------------------------------------------------------
# _variant_diff
# ---------------------------------------------------------------------------


class TestVariantDiff:
    def test_identical_strings_produce_empty_diff(self):
        assert _variant_diff("SELECT 1", "SELECT 1") == ""

    def test_different_strings_produce_nonempty_diff(self):
        diff = _variant_diff("SELECT 1", "SELECT 2")
        assert "SELECT 1" in diff
        assert "SELECT 2" in diff


# ---------------------------------------------------------------------------
# render_determinism_text
# ---------------------------------------------------------------------------


class TestRenderDeterminismText:
    def test_fully_deterministic_report(self):
        report = probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="stub:v1")
        text = render_determinism_text(report)
        assert "endpoint='stub:v1'" in text
        assert "Determinism rate: 100.00%" in text
        assert "No variation detected" in text

    def test_varied_report_lists_both_variants_and_diff(self):
        generate = _cycle("SELECT 1", "SELECT 2")
        report = probe_determinism(generate, ONE_CASE, endpoint="stub:v1", repeats=3)
        text = render_determinism_text(report)
        assert "Determinism rate: 0.00%" in text
        assert "variant 1: SELECT 1" in text
        assert "variant 2: SELECT 2" in text
        assert "diff (variant 1 vs variant 2):" in text

    def test_varied_report_names_the_question(self):
        generate = _cycle("SELECT 1", "SELECT 2")
        report = probe_determinism(generate, ONE_CASE, endpoint="stub:v1", repeats=2)
        text = render_determinism_text(report)
        assert "how many?" in text


# ---------------------------------------------------------------------------
# save_determinism_json
# ---------------------------------------------------------------------------


class TestSaveDeterminismJson:
    def test_round_trips_through_json(self, tmp_path):
        report = probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="stub:v1")
        out_path = tmp_path / "determinism.json"
        save_determinism_json(report, out_path)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["endpoint"] == "stub:v1"
        assert data["determinism_rate_pct"] == 100.0
        assert data["results"][0]["case_id"] == "a"

    def test_creates_parent_directories(self, tmp_path):
        report = probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="stub:v1")
        out_path = tmp_path / "nested" / "dir" / "determinism.json"
        save_determinism_json(report, out_path)
        assert out_path.exists()

    def test_accepts_str_path(self, tmp_path):
        report = probe_determinism(lambda q: "SELECT 1", ONE_CASE, endpoint="stub:v1")
        out_path = tmp_path / "determinism.json"
        save_determinism_json(report, str(out_path))
        assert out_path.exists()
