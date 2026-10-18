# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for the ``excluded`` status, ``exclusion_reason`` and ``match`` of a golden case.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_models_excluded.py -q
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from eval.models import GoldenCase
from eval.runner import run_case, run_golden_set


class TestExcludedStatus:
    def test_excluded_needs_a_reason_and_is_never_runnable(self):
        with pytest.raises(ValueError, match="needs an exclusion_reason"):
            GoldenCase(id="a", question="q", status="excluded")
        case = GoldenCase(id="a", question="q", status="excluded", exclusion_reason="load_fidelity")
        assert not case.is_runnable
        assert case.expected_sql is None

    def test_a_reason_is_only_for_an_excluded_case(self):
        with pytest.raises(ValueError, match="only applies to an 'excluded' case"):
            GoldenCase(id="a", question="q", expected_sql="SELECT 1", exclusion_reason="x")

    def test_an_excluded_case_is_skipped_by_the_runner(self):
        excluded = GoldenCase(
            id="x", question="q", expected_sql="SELECT 1", status="excluded",
            exclusion_reason="reference_empty",
        )
        assert run_golden_set([excluded], lambda q: "SELECT 1", lambda sql: pd.DataFrame()) == []

    def test_a_line_written_before_these_fields_loads_unchanged(self):
        case = GoldenCase.from_dict({"id": "a", "question": "q", "expected_sql": "SELECT 1"})
        assert (case.match, case.exclusion_reason, case.status) == ("fingerprint", None, "active")


class TestExecutionMatch:
    def test_needs_recorded_rows_and_a_known_value(self):
        with pytest.raises(ValueError, match="non-empty list"):
            GoldenCase(id="a", question="q", expected_sql="SELECT 1", match="execution")
        with pytest.raises(ValueError, match="unknown match"):
            GoldenCase(id="a", question="q", expected_sql="SELECT 1", match="fuzzy")

    def test_round_trips_through_json(self):
        case = GoldenCase(
            id="a", question="q", expected_sql="SELECT 1", match="execution",
            expected_rows=[{"n": 1}], exclusion_reason=None,
        )
        assert GoldenCase.from_dict(json.loads(json.dumps(case.to_dict()))) == case

    def _case(self) -> GoldenCase:
        return GoldenCase(
            id="a", question="q", expected_sql="SELECT Name, COUNT(*) AS n FROM Customer GROUP BY Name",
            match="execution", expected_fingerprint="0" * 64,
            expected_rows=[{"name": "x", "c1": 2}, {"name": "y", "c1": 5}],
        )

    def test_column_names_and_row_order_do_not_matter(self):
        # The fingerprint is stale on purpose: execution matching never reads it.
        actual = pd.DataFrame({"label": ["y", "x"], "total": [5, 2]})
        result = run_case(self._case(), lambda q: self._case().expected_sql, lambda sql: actual)
        assert result.status == "pass"

    def test_a_different_value_fails_with_a_diagnosis(self):
        actual = pd.DataFrame({"label": ["x", "y"], "total": [2, 6]})
        result = run_case(self._case(), lambda q: self._case().expected_sql, lambda sql: actual)
        assert result.status == "fingerprint_mismatch"
        assert "recorded reference rows" in (result.error or "")
        assert result.diagnosis is not None

    def test_the_default_match_is_still_the_fingerprint(self):
        case = GoldenCase(
            id="a", question="q", expected_sql="SELECT COUNT(*) AS n FROM Customer", expected_fingerprint="0" * 64,
            expected_rows=[{"n": 1}],
        )
        result = run_case(case, lambda q: "SELECT COUNT(*) AS n FROM Customer", lambda sql: pd.DataFrame({"n": [1]}))
        assert result.status == "fingerprint_mismatch"
