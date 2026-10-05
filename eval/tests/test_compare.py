# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for eval/compare.py -- the execution-accuracy comparison.

Each class pins one clause of the semantics documented in the module
docstring: aliases, column order, row order and ties, duplicates, numeric
tolerance, NULLs, empties, dates and the ORDER BY detection.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_compare.py -q
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from eval.compare import (
    ComparisonOptions,
    compare_frames,
    compare_to_reference,
    normalise_cell,
    reference_ordering,
)


def _df(**columns) -> pd.DataFrame:
    return pd.DataFrame(columns)


class TestColumnNamesAndOrder:
    def test_aliases_are_ignored(self):
        a = _df(n=[3])
        b = _df(TotalCount=[3])
        assert compare_frames(a, b).equal

    def test_column_order_as_selected_matters(self):
        a = pd.DataFrame([["x", 1]], columns=["sym", "vol"])
        swapped = pd.DataFrame([[1, "x"]], columns=["vol", "sym"])
        result = compare_frames(a, swapped)
        assert not result.equal
        assert result.reason == "result values differ"

    def test_column_count_must_match(self):
        result = compare_frames(_df(a=[1], b=[2]), _df(a=[1]))
        assert not result.equal
        assert result.reason == "column count differs (expected 2, got 1)"

    def test_duplicate_column_names_do_not_break_it(self):
        a = pd.DataFrame([[1, 1]], columns=["v", "v"])
        b = pd.DataFrame([[1, 1]], columns=["x", "y"])
        assert compare_frames(a, b).equal


class TestRowOrderAndMultiset:
    def test_row_order_ignored_by_default(self):
        a = _df(s=["a", "b", "c"], v=[1, 2, 3])
        b = _df(s=["c", "a", "b"], v=[3, 1, 2])
        assert compare_frames(a, b).equal

    def test_duplicates_are_counted(self):
        assert not compare_frames(_df(n=[1, 1, 2]), _df(n=[1, 2, 2])).equal
        assert compare_frames(_df(n=[1, 1, 2]), _df(n=[2, 1, 1])).equal

    def test_row_count_mismatch_reports_counts_only(self):
        result = compare_frames(_df(n=[1, 2, 3]), _df(n=[1, 2]))
        assert result.reason == "row count differs (expected 3, got 2)"

    def test_one_wrong_value_fails(self):
        assert not compare_frames(_df(s=["a", "b"]), _df(s=["a", "z"])).equal

    def test_mixed_text_and_number_rows_pair_up_correctly(self):
        a = pd.DataFrame({"s": ["x", "x", "y"], "v": [1.0, 2.0, 1.0]})
        b = pd.DataFrame({"s": ["y", "x", "x"], "v": [1.0, 2.0, 1.0]})
        assert compare_frames(a, b).equal
        wrong = pd.DataFrame({"s": ["y", "x", "x"], "v": [2.0, 2.0, 1.0]})
        assert not compare_frames(a, wrong).equal

    def test_reason_never_contains_a_cell_value(self):
        result = compare_frames(_df(s=["secret-value"]), _df(s=["other-value"]))
        assert "secret" not in result.reason and "other" not in result.reason


class TestOrderedComparison:
    TOP = pd.DataFrame({"s": ["a", "b", "c"], "v": [9, 5, 5]})

    def test_wrong_order_fails_when_order_is_part_of_the_answer(self):
        result = compare_frames(
            self.TOP, self.TOP.iloc[::-1], ordered=True, order_keys=[1]
        )
        assert not result.equal
        assert result.ordered
        assert result.reason == "row order differs from the reference ORDER BY"

    def test_same_order_passes(self):
        assert compare_frames(self.TOP, self.TOP.copy(), ordered=True, order_keys=[1]).equal

    def test_ties_on_the_sort_key_may_permute(self):
        tied_swapped = pd.DataFrame({"s": ["a", "c", "b"], "v": [9, 5, 5]})
        assert compare_frames(self.TOP, tied_swapped, ordered=True, order_keys=[1]).equal

    def test_unresolved_sort_key_compares_the_whole_sequence(self):
        tied_swapped = pd.DataFrame({"s": ["a", "c", "b"], "v": [9, 5, 5]})
        assert not compare_frames(self.TOP, tied_swapped, ordered=True).equal

    def test_ordered_still_requires_the_same_rows(self):
        other = pd.DataFrame({"s": ["a", "b", "z"], "v": [9, 5, 5]})
        assert not compare_frames(self.TOP, other, ordered=True, order_keys=[1]).equal

    def test_unordered_comparison_ignores_the_order_of_the_same_frames(self):
        assert compare_frames(self.TOP, self.TOP.iloc[::-1]).equal

    def test_descending_vs_ascending_sort_key_differs(self):
        asc = self.TOP.sort_values("v", kind="stable").reset_index(drop=True)
        assert not compare_frames(self.TOP, asc, ordered=True, order_keys=[1]).equal


class TestNumbers:
    def test_decimal_and_float_agree_within_tolerance(self):
        a = _df(v=[Decimal("10.50")])
        assert compare_frames(a, _df(v=[10.5000001])).equal

    def test_relative_tolerance_scales_with_magnitude(self):
        assert compare_frames(_df(v=[1e9 + 100.0]), _df(v=[1e9 + 200.0])).equal
        assert not compare_frames(_df(v=[10.0]), _df(v=[10.1])).equal

    def test_absolute_floor_near_zero(self):
        assert compare_frames(_df(v=[0.0]), _df(v=[1e-9])).equal
        assert not compare_frames(_df(v=[0.0]), _df(v=[1e-3])).equal

    def test_integers_compare_exactly_whatever_the_magnitude(self):
        a = _df(id=[1_000_000_000_001])
        b = _df(id=[1_000_000_000_002])
        assert not compare_frames(a, b).equal

    def test_int_equals_float_with_the_same_value(self):
        assert compare_frames(_df(v=[3]), _df(v=[3.0])).equal
        assert not compare_frames(_df(v=[3]), _df(v=[3.5])).equal

    def test_bool_counts_as_an_integer(self):
        assert compare_frames(_df(f=[True]), _df(f=[1])).equal
        assert not compare_frames(_df(f=[True]), _df(f=[0])).equal

    def test_tolerance_option_zero_is_exact(self):
        exact = ComparisonOptions(tolerance=0.0)
        assert not compare_frames(_df(v=[1.0]), _df(v=[1.0000001]), options=exact).equal
        assert compare_frames(_df(v=[1.0]), _df(v=[1.0]), options=exact).equal

    def test_tolerance_option_widens(self):
        loose = ComparisonOptions(tolerance=0.01)
        assert compare_frames(_df(v=[100.0]), _df(v=[100.9]), options=loose).equal

    def test_float_noise_in_a_tie_does_not_break_pairing(self):
        a = pd.DataFrame({"x": [1.0000001, 1.0], "k": [5, 3]})
        b = pd.DataFrame({"x": [1.0, 1.0000001], "k": [5, 3]})
        assert compare_frames(a, b).equal

    def test_invalid_tolerance_is_rejected(self):
        with pytest.raises(ValueError):
            ComparisonOptions(tolerance=-1.0)
        with pytest.raises(ValueError):
            ComparisonOptions(tolerance=float("nan"))

    def test_numpy_scalars_are_unwrapped(self):
        assert normalise_cell(np.int64(4)) == ("i", 4)
        assert normalise_cell(np.float32(0.5)) == ("f", 0.5)


class TestNulls:
    def test_null_equals_null_across_sentinels(self):
        a = pd.DataFrame({"v": [None, 1.0]})
        b = pd.DataFrame({"v": [float("nan"), 1.0]})
        assert compare_frames(a, b).equal

    def test_null_is_not_zero_or_empty_string(self):
        assert not compare_frames(_df(v=[None]), _df(v=[0])).equal
        assert not compare_frames(_df(v=[None], w=["x"]), _df(v=[""], w=["x"])).equal

    def test_pandas_na_and_nat(self):
        a = pd.DataFrame({"v": pd.array([1, None], dtype="Int64")})
        b = pd.DataFrame({"v": [1.0, np.nan]})
        assert compare_frames(a, b).equal
        assert normalise_cell(pd.NaT) == ("n", None)

    def test_nulls_in_text_columns(self):
        a = pd.DataFrame({"s": ["a", None]})
        b = pd.DataFrame({"s": [None, "a"]})
        assert compare_frames(a, b).equal


class TestEmpty:
    def test_both_empty_with_same_column_count_is_equal(self):
        a = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        b = pd.DataFrame({"total": pd.Series([], dtype="object")})
        assert compare_frames(a, b).equal

    def test_empty_with_different_column_count_is_not_equal(self):
        a = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        b = pd.DataFrame({"a": pd.Series([], dtype="int64"), "b": pd.Series([], dtype="int64")})
        assert not compare_frames(a, b).equal

    def test_empty_vs_non_empty(self):
        a = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        assert not compare_frames(a, _df(n=[0])).equal


class TestDatesAndText:
    def test_midnight_datetime_equals_plain_date(self):
        a = pd.DataFrame({"d": [pd.Timestamp("2026-10-04")]})
        b = pd.DataFrame({"d": [date(2026, 10, 4)]})
        assert compare_frames(a, b).equal

    def test_time_of_day_is_kept(self):
        a = pd.DataFrame({"d": [datetime(2026, 10, 4, 9, 30)]})
        b = pd.DataFrame({"d": [datetime(2026, 10, 4)]})
        assert not compare_frames(a, b).equal

    def test_text_is_exact_and_persian_survives(self):
        assert compare_frames(_df(s=["مشتری"]), _df(s=["مشتری"])).equal
        assert not compare_frames(_df(s=["a"]), _df(s=["A"])).equal
        assert not compare_frames(_df(s=["a"]), _df(s=["a "])).equal

    def test_text_that_looks_like_a_number_is_not_a_number(self):
        assert not compare_frames(_df(s=["1"]), _df(s=[1])).equal


class TestReferenceOrdering:
    @pytest.mark.parametrize(
        "sql, expected",
        [
            ("SELECT TOP 5 Sym, Vol FROM T ORDER BY Vol DESC", (True, (1,))),
            ("SELECT TOP 5 Sym, Vol AS V FROM T ORDER BY Vol DESC", (True, (1,))),
            ("SELECT TOP 5 Sym AS S, Vol FROM T ORDER BY S, 2 DESC", (True, (0, 1))),
            ("SELECT Sym FROM T ORDER BY Sym OFFSET 0 ROWS FETCH NEXT 3 ROWS ONLY", (True, (0,))),
            ("SELECT Sym FROM T ORDER BY Sym OFFSET 2 ROWS", (True, (0,))),
            ("SELECT TOP 5 * FROM T ORDER BY Vol", (True, None)),
            ("SELECT TOP 3 Sym FROM T ORDER BY Vol", (True, None)),
            ("SELECT Sym FROM T ORDER BY Sym", (False, None)),
            ("SELECT TOP 3 Sym FROM T", (False, None)),
            ("SELECT a FROM T UNION ALL SELECT b FROM U", (False, None)),
            ("this is not sql (", (False, None)),
        ],
    )
    def test_detection(self, sql, expected):
        assert reference_ordering(sql, "tsql") == expected

    def test_a_cte_does_not_hide_the_top_level_clauses(self):
        sql = "WITH c AS (SELECT a FROM T) SELECT TOP 2 a FROM c ORDER BY a"
        assert reference_ordering(sql, "tsql") == (True, (0,))

    def test_order_by_inside_a_subquery_is_not_top_level(self):
        sql = "SELECT a FROM (SELECT TOP 5 a FROM T ORDER BY a) q"
        assert reference_ordering(sql, "tsql") == (False, None)

    def test_defaults_to_the_configured_dialect(self):
        assert reference_ordering("SELECT TOP 1 a FROM T ORDER BY a") == (True, (0,))


class TestCompareToReference:
    def test_unordered_reference_ignores_order(self):
        ref = _df(s=["a", "b"])
        assert compare_to_reference("SELECT s FROM T", ref, ref.iloc[::-1], dialect="tsql").equal

    def test_top_order_by_reference_enforces_order(self):
        ref = _df(s=["a", "b"])
        result = compare_to_reference(
            "SELECT TOP 2 s FROM T ORDER BY s", ref, ref.iloc[::-1], dialect="tsql"
        )
        assert not result.equal and result.ordered

    def test_top_order_by_with_ties_in_a_non_key_column(self):
        ref = pd.DataFrame({"s": ["a", "b", "c"], "v": [9, 5, 5]})
        got = pd.DataFrame({"s": ["a", "c", "b"], "v": [9, 5, 5]})
        sql = "SELECT TOP 3 s, v FROM T ORDER BY v DESC"
        assert compare_to_reference(sql, ref, got, dialect="tsql").equal
