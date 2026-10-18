# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``eval.stats``: Wilson, McNemar and Newcombe against values worked out by hand.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_stats.py -q
"""

from __future__ import annotations

import math

import pytest

from eval.stats import (
    MIN_STRATUM_N,
    Z_95,
    is_interpretable,
    mcnemar_exact_p,
    newcombe_paired_interval,
    repeat_consistency,
    repeat_mean_accuracy,
    stratum_summary,
    wilson_interval,
)


class TestWilson:
    def test_39_of_52_by_hand(self):
        n, p, z2 = 52, 39 / 52, Z_95 ** 2
        centre = (p + z2 / (2 * n)) / (1 + z2 / n)
        half = Z_95 * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
        low, high = wilson_interval(39, 52)
        assert low == pytest.approx(centre - half)
        assert high == pytest.approx(centre + half)
        # the same figures, rounded the way the documentation quotes them
        assert (round(low, 3), round(high, 3)) == (0.618, 0.848)

    def test_the_interval_is_inside_the_unit_interval_at_the_extremes(self):
        assert wilson_interval(0, 30)[0] == 0.0
        assert wilson_interval(30, 30)[1] == 1.0
        assert 0.0 < wilson_interval(0, 30)[1] < 0.2

    def test_known_value_for_a_half(self):
        # 5 of 10: centre is exactly 0.5 by symmetry
        low, high = wilson_interval(5, 10)
        assert (low + high) / 2 == pytest.approx(0.5)
        assert (round(low, 4), round(high, 4)) == (0.2366, 0.7634)

    def test_no_cases_is_uninformative(self):
        assert wilson_interval(0, 0) == (0.0, 1.0)

    def test_it_narrows_with_more_cases(self):
        small = wilson_interval(15, 20)
        large = wilson_interval(150, 200)
        assert (large[1] - large[0]) < (small[1] - small[0])

    @pytest.mark.parametrize("successes, n", [(-1, 5), (6, 5)])
    def test_impossible_counts_are_refused(self, successes, n):
        with pytest.raises(ValueError):
            wilson_interval(successes, n)

    def test_the_minimum_stratum_size(self):
        assert MIN_STRATUM_N == 20
        assert not is_interpretable(19) and is_interpretable(20)
        assert stratum_summary(3, 4)["interpretable"] is False
        summary = stratum_summary(39, 52)
        assert (summary["n"], summary["pct"], summary["interpretable"]) == (52, 75.0, True)


class TestMcNemar:
    # Each value is 2 * P(X <= min(b, c)) for X ~ Binomial(b + c, 1/2), by hand.
    @pytest.mark.parametrize(
        "regressed, fixed, expected",
        [
            (1, 5, 14 / 64),          # 2 * (1 + 6) / 2**6
            (0, 5, 2 / 32),           # 2 * 1 / 2**5
            (0, 6, 2 / 64),
            (5, 0, 2 / 32),           # symmetric in its arguments
            (2, 9, 2 * 67 / 2048),    # 2 * (1 + 11 + 55) / 2**11
            (3, 3, 1.0),              # a tie is capped at 1
            (1, 1, 1.0),
            (0, 1, 1.0),              # 2 * 1/2 = 1
            (0, 0, 1.0),              # nothing changed
            (4, 12, 2 * 2517 / 65536),  # 2 * (1+16+120+560+1820) / 2**16
        ],
    )
    def test_small_tables(self, regressed, fixed, expected):
        assert mcnemar_exact_p(regressed, fixed) == pytest.approx(expected)

    def test_a_large_table_does_not_overflow(self):
        assert 0.0 <= mcnemar_exact_p(400, 450) <= 1.0
        assert mcnemar_exact_p(0, 2000) == pytest.approx(0.0)

    def test_negative_counts_are_refused(self):
        with pytest.raises(ValueError):
            mcnemar_exact_p(-1, 3)


class TestNewcombe:
    def test_worked_example(self):
        # 20 both, 3 regressed, 9 fixed, 18 neither: old 23/50, new 29/50.
        low, high = newcombe_paired_interval(20, 3, 9, 18)
        assert (round(low, 3), round(high, 3)) == (-0.017, 0.248)

    def test_the_interval_contains_the_observed_difference(self):
        for table in [(20, 3, 9, 18), (5, 1, 7, 2), (50, 10, 2, 30), (0, 5, 5, 0)]:
            both, regressed, fixed, neither = table
            n = sum(table)
            low, high = newcombe_paired_interval(*table)
            assert low <= (fixed - regressed) / n <= high

    def test_correlated_rounds_have_a_narrower_interval_than_independent_ones(self):
        paired = newcombe_paired_interval(30, 0, 0, 20)
        wilson_old = wilson_interval(30, 50)
        independent_width = math.sqrt(2) * (wilson_old[1] - wilson_old[0])
        assert (paired[1] - paired[0]) < independent_width / 2

    def test_a_clear_improvement_excludes_zero(self):
        low, high = newcombe_paired_interval(60, 0, 25, 15)
        assert low > 0

    def test_no_cases_is_uninformative(self):
        assert newcombe_paired_interval(0, 0, 0, 0) == (-1.0, 1.0)

    def test_the_interval_stays_within_minus_one_and_one(self):
        low, high = newcombe_paired_interval(0, 0, 30, 0)
        assert -1.0 <= low and high <= 1.0


class TestRepeats:
    OUTCOMES = {
        "a": [True, True, True],
        "b": [False, False, False],
        "c": [True, False, True],
        "d": [True, True, False],
    }

    def test_consistency_is_the_share_with_one_outcome_on_every_repeat(self):
        assert repeat_consistency(self.OUTCOMES) == 0.5

    def test_mean_accuracy_over_repeats(self):
        # repeat 1: a, c, d pass (3/4); repeat 2: a, d (2/4); repeat 3: a, c (2/4)
        assert repeat_mean_accuracy(self.OUTCOMES) == pytest.approx((3 + 2 + 2) / 12)

    def test_empty_input(self):
        assert repeat_consistency({}) == 0.0
        assert repeat_mean_accuracy({}) == 0.0
