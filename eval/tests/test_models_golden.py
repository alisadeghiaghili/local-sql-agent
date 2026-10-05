# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for the golden-set additions in eval/models.py: the ``datasource``
field and the ``pending_review`` / ``reviewed`` statuses.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_models_golden.py -q
"""

from __future__ import annotations

import json

import pytest

from eval.models import NON_RUNNABLE_STATUSES, GoldenCase
from eval.runner import load_golden_cases


class TestStatuses:
    def test_non_runnable_statuses_are_everything_but_active(self):
        assert set(NON_RUNNABLE_STATUSES) == {"pending_expected", "pending_review", "reviewed"}

    @pytest.mark.parametrize("status", ["pending_expected", "pending_review", "reviewed"])
    def test_only_active_is_runnable(self, status):
        case = GoldenCase(id="a", question="q", expected_sql="SELECT 1", status=status)
        assert not case.is_runnable
        assert GoldenCase(id="b", question="q", expected_sql="SELECT 1").is_runnable

    def test_unknown_status_is_rejected(self):
        with pytest.raises(ValueError, match="unknown status"):
            GoldenCase(id="a", question="q", expected_sql="SELECT 1", status="approved")

    def test_pending_review_may_omit_sql(self):
        case = GoldenCase(id="a", question="q", status="pending_review")
        assert case.expected_sql is None

    def test_pending_review_keeps_the_proposed_sql_and_expect(self):
        case = GoldenCase(
            id="a", question="q", expected_sql="SELECT 1", expect="empty", status="pending_review"
        )
        assert (case.expected_sql, case.expect) == ("SELECT 1", "empty")

    def test_pending_review_cannot_carry_row_data(self):
        with pytest.raises(ValueError, match="cannot carry expected_rows"):
            GoldenCase(id="a", question="q", status="pending_review", expected_rows=[{"n": 1}])
        with pytest.raises(ValueError, match="cannot carry expected_rows"):
            GoldenCase(id="a", question="q", status="pending_review", expected_fingerprint="ab")

    def test_reviewed_still_requires_sql_unless_out_of_scope(self):
        with pytest.raises(ValueError, match="expected_sql is required"):
            GoldenCase(id="a", question="q", status="reviewed")
        assert GoldenCase(id="a", question="q", status="reviewed", expect="out_of_scope")

    def test_pending_expected_behaviour_is_unchanged(self):
        case = GoldenCase(id="a", question="q", status="pending_expected")
        assert case.expected_sql is None


class TestDatasource:
    def test_default_is_none_and_absent_key_loads(self):
        case = GoldenCase.from_dict({"id": "a", "question": "q", "expected_sql": "SELECT 1"})
        assert case.datasource is None

    def test_round_trips_through_to_dict(self):
        case = GoldenCase(id="a", question="q", expected_sql="SELECT 1", datasource="sales")
        assert GoldenCase.from_dict(json.loads(json.dumps(case.to_dict()))) == case

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_blank_datasource_is_rejected(self, bad):
        with pytest.raises(ValueError, match="datasource"):
            GoldenCase(id="a", question="q", expected_sql="SELECT 1", datasource=bad)

    def test_the_committed_example_set_still_loads(self):
        cases = load_golden_cases("eval_data.example/golden.jsonl")
        assert len(cases) >= 14
        assert all(c.is_runnable for c in cases)
        assert all(c.datasource is None for c in cases)

    def test_persian_question_survives_a_jsonl_round_trip(self, tmp_path):
        case = GoldenCase(id="a", question="تعداد سفارش‌های ۱۴۰۲", expected_sql="SELECT 1")
        path = tmp_path / "g.jsonl"
        path.write_text(json.dumps(case.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
        assert load_golden_cases(path)[0].question == case.question
