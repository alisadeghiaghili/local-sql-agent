# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A determinism probe with a judge: outcome consistency and mean accuracy over the repeats.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_determinism_judged.py -q
"""

from __future__ import annotations

import pandas as pd

from eval.determinism import (
    OUT_OF_SCOPE_VARIANT,
    make_judge,
    probe_determinism,
    render_determinism_text,
)
from eval.models import GoldenCase

RIGHT = "SELECT COUNT(*) AS n FROM Customer"
WRONG = "SELECT COUNT(*) AS n FROM Customer WHERE Name = 'x'"


def _case(case_id: str = "a") -> GoldenCase:
    return GoldenCase(
        id=case_id, question=f"how many {case_id}?", expected_sql=RIGHT,
        match="execution", expected_rows=[{"n": 3}],
    )


def _execute(sql: str) -> pd.DataFrame:
    return pd.DataFrame({"n": [3 if sql == RIGHT else 0]})


class TestJudge:
    def test_a_repeat_is_right_by_the_rule_a_run_uses(self):
        judge = make_judge(_execute)
        assert judge(_case(), RIGHT) is True
        assert judge(_case(), WRONG) is False
        assert judge(_case(), OUT_OF_SCOPE_VARIANT) is False


class TestJudgedProbe:
    def test_consistency_and_mean_accuracy(self):
        # case a: right, wrong, right (inconsistent); case b: always wrong (consistent)
        answers = {"how many a?": iter([RIGHT, WRONG, RIGHT]), "how many b?": iter([WRONG] * 3)}
        report = probe_determinism(
            lambda q: next(answers[q]), [_case("a"), _case("b")], endpoint="stub:v1",
            repeats=3, judge=make_judge(_execute),
        )
        assert [r.outcomes for r in report.results] == [[True, False, True], [False] * 3]
        assert report.consistency_pct == 50.0
        # repeat 1: a right (1/2); repeat 2: none (0/2); repeat 3: a right (1/2)
        assert round(report.mean_accuracy_pct, 2) == round(100 * (0.5 + 0 + 0.5) / 3, 2)
        data = report.to_dict()
        assert data["consistency_pct"] == 50.0 and data["results"][0]["outcomes"] == [True, False, True]
        text = render_determinism_text(report)
        assert "Outcome consistency: 50.00%" in text and "mean accuracy over the 3 repeats" in text

    def test_without_a_judge_the_report_is_unchanged(self):
        report = probe_determinism(lambda q: RIGHT, [_case()], endpoint="stub:v1")
        assert report.consistency_pct is None
        data = report.to_dict()
        assert "consistency_pct" not in data and "outcomes" not in data["results"][0]
        assert "Outcome consistency" not in render_determinism_text(report)
