# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Every ``mismatch_class`` of :mod:`eval.diagnosis`, from two result frames.

The diagnosis says *how* a generated result differs from its reference and
must never carry a value: these tests pin one frame pair per class, the
error bucket of a single number, and that the serialised diagnosis holds
only counts and column names.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_diagnosis.py -q
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from eval.compare import ComparisonOptions
from eval.diagnosis import (
    diagnose_not_executed,
    diagnose_results,
    mismatch_class_for,
    relative_error_bucket,
)
from eval.models import MISMATCH_CLASSES, MismatchDiagnosis

SQL = "SELECT a, b FROM T"
RANKED = "SELECT TOP 3 label, score FROM T ORDER BY score DESC"

TABLE = pd.DataFrame({"label": ["p", "q", "r"], "score": [30, 20, 10]})


def _diagnose(expected, actual, sql=SQL, **kwargs):
    return diagnose_results(sql, expected, actual, dialect="tsql", **kwargs)


class TestClasses:
    def test_match_ignores_column_names(self):
        d = _diagnose(TABLE, TABLE.rename(columns={"label": "x", "score": "y"}))
        assert d.mismatch_class == "match"
        assert (d.expected_rows, d.actual_rows) == (3, 3)
        assert d.actual_columns == ["x", "y"]

    def test_row_count_differs_when_neither_contains_the_other(self):
        expected = pd.DataFrame({"n": [1, 2]})
        actual = pd.DataFrame({"n": [3, 4, 5]})
        d = _diagnose(expected, actual)
        assert d.mismatch_class == "row_count_differs"
        assert (d.expected_rows, d.actual_rows) == (2, 3)

    def test_column_count_differs(self):
        d = _diagnose(TABLE, TABLE[["label"]])
        assert d.mismatch_class == "column_count_differs"
        assert d.expected_columns == ["label", "score"]
        assert d.actual_columns == ["label"]

    def test_column_names_differ_when_the_columns_are_in_another_order(self):
        d = _diagnose(TABLE, TABLE[["score", "label"]])
        assert d.mismatch_class == "column_names_differ"

    def test_column_names_differ_is_found_by_content_when_names_differ(self):
        swapped = TABLE[["score", "label"]].rename(columns={"score": "total", "label": "name"})
        assert _diagnose(TABLE, swapped).mismatch_class == "column_names_differ"

    def test_same_rows_different_order_when_the_reference_orders(self):
        d = _diagnose(TABLE, TABLE.iloc[::-1], sql=RANKED)
        assert d.mismatch_class == "same_rows_different_order"

    def test_order_is_ignored_when_the_reference_does_not_order(self):
        assert _diagnose(TABLE, TABLE.iloc[::-1]).mismatch_class == "match"

    def test_values_differ_same_shape(self):
        other = pd.DataFrame({"label": ["p", "q", "z"], "score": [30, 20, 11]})
        d = _diagnose(TABLE, other)
        assert d.mismatch_class == "values_differ_same_shape"
        assert d.relative_error_bucket is None  # not a single cell

    def test_actual_is_subset(self):
        d = _diagnose(TABLE, TABLE.iloc[:2])
        assert d.mismatch_class == "actual_is_subset"
        assert (d.expected_rows, d.actual_rows) == (3, 2)

    def test_actual_is_superset(self):
        extra = pd.concat([TABLE, pd.DataFrame({"label": ["s"], "score": [5]})], ignore_index=True)
        assert _diagnose(TABLE, extra).mismatch_class == "actual_is_superset"

    def test_subset_counts_duplicates(self):
        expected = pd.DataFrame({"n": [1, 2]})
        actual = pd.DataFrame({"n": [1, 1]})  # a row the reference has once, twice
        assert _diagnose(expected, actual).mismatch_class == "values_differ_same_shape"
        assert _diagnose(pd.DataFrame({"n": [1, 1, 2]}), pd.DataFrame({"n": [1, 1]})
                         ).mismatch_class == "actual_is_subset"

    def test_actual_empty(self):
        d = _diagnose(TABLE, TABLE.iloc[0:0])
        assert d.mismatch_class == "actual_empty"
        assert (d.expected_rows, d.actual_rows) == (3, 0)

    def test_expected_empty(self):
        d = _diagnose(TABLE.iloc[0:0], TABLE)
        assert d.mismatch_class == "expected_empty"
        assert (d.expected_rows, d.actual_rows) == (0, 3)

    def test_two_empty_results_match_unless_the_columns_differ(self):
        assert _diagnose(TABLE.iloc[0:0], TABLE.iloc[0:0]).mismatch_class == "match"
        assert _diagnose(TABLE.iloc[0:0], TABLE[["label"]].iloc[0:0]).mismatch_class == \
            "column_count_differs"

    def test_not_executed_keeps_whatever_shape_is_known(self):
        d = diagnose_not_executed(expected=TABLE)
        assert d.mismatch_class == "not_executed"
        assert (d.expected_rows, d.actual_rows, d.actual_columns) == (3, None, None)
        assert diagnose_not_executed().expected_columns is None

    def test_every_class_is_produced_somewhere_in_this_module(self):
        produced = {
            "match": _diagnose(TABLE, TABLE).mismatch_class,
            "row_count_differs": _diagnose(
                pd.DataFrame({"n": [1, 2]}), pd.DataFrame({"n": [3, 4, 5]})).mismatch_class,
            "column_count_differs": _diagnose(TABLE, TABLE[["label"]]).mismatch_class,
            "column_names_differ": _diagnose(TABLE, TABLE[["score", "label"]]).mismatch_class,
            "same_rows_different_order": _diagnose(TABLE, TABLE.iloc[::-1], sql=RANKED).mismatch_class,
            "values_differ_same_shape": _diagnose(
                TABLE, TABLE.assign(score=[1, 2, 3])).mismatch_class,
            "actual_is_subset": _diagnose(TABLE, TABLE.iloc[:1]).mismatch_class,
            "actual_is_superset": _diagnose(TABLE.iloc[:1], TABLE).mismatch_class,
            "actual_empty": _diagnose(TABLE, TABLE.iloc[0:0]).mismatch_class,
            "expected_empty": _diagnose(TABLE.iloc[0:0], TABLE).mismatch_class,
            "not_executed": diagnose_not_executed().mismatch_class,
        }
        assert set(produced) == set(MISMATCH_CLASSES)
        assert all(key == value for key, value in produced.items())


class TestRelativeErrorBucket:
    @pytest.mark.parametrize(
        ("expected", "actual", "bucket"),
        [
            (100, 100.4, "<1%"),
            (100, 95, "<10%"),
            (100, 90, ">=10%"),
            (100, 1000, ">=10%"),
            (0, 0, "<1%"),
            (0, 7, ">=10%"),
            (-100, -95, "<10%"),
        ],
    )
    def test_buckets(self, expected, actual, bucket):
        assert relative_error_bucket(expected, actual) == bucket

    def test_single_number_answers_get_a_bucket_only_when_they_differ(self):
        ref = pd.DataFrame({"n": [200]})
        assert _diagnose(ref, pd.DataFrame({"n": [200]})).relative_error_bucket is None
        assert _diagnose(ref, pd.DataFrame({"n": [199]})).relative_error_bucket == "<1%"
        assert _diagnose(ref, pd.DataFrame({"n": [190]})).relative_error_bucket == "<10%"
        assert _diagnose(ref, pd.DataFrame({"n": [100]})).relative_error_bucket == ">=10%"

    def test_no_bucket_for_text_or_wider_answers(self):
        assert _diagnose(pd.DataFrame({"s": ["a"]}), pd.DataFrame({"s": ["b"]})
                         ).relative_error_bucket is None
        assert _diagnose(pd.DataFrame({"a": [1], "b": [2]}), pd.DataFrame({"a": [1], "b": [3]})
                         ).relative_error_bucket is None

    def test_the_tolerance_the_verdict_used_is_the_one_diagnosed_with(self):
        ref, near = pd.DataFrame({"x": [1000.0]}), pd.DataFrame({"x": [1000.4]})
        assert _diagnose(ref, near).mismatch_class == "values_differ_same_shape"
        loose = ComparisonOptions(tolerance=0.01)
        assert _diagnose(ref, near, options=loose).mismatch_class == "match"


class TestNoValueEverLeaks:
    def test_the_serialised_diagnosis_holds_no_cell_value(self):
        secret = pd.DataFrame({
            "label": ["SENTINEL_TEXT_A", "SENTINEL_TEXT_B"], "score": [918273645, 546372819],
        })
        other = pd.DataFrame({
            "label": ["SENTINEL_TEXT_A", "SENTINEL_TEXT_C"], "score": [918273645, 111222333],
        })
        for pair in ((secret, other), (secret, secret.iloc[:1]), (secret, secret.iloc[0:0]),
                     (secret.iloc[:1], secret), (secret, secret[["score", "label"]])):
            text = json.dumps(_diagnose(*pair).to_dict(), ensure_ascii=False)
            assert "SENTINEL" not in text
            assert "918273645" not in text and "546372819" not in text and "111222333" not in text

    def test_long_column_names_and_wide_results_are_bounded(self):
        wide = pd.DataFrame([[1] * 100], columns=["c" * 300 + str(i) for i in range(100)])
        d = _diagnose(wide, wide)
        assert len(d.expected_columns) == 64
        assert all(len(name) <= 128 for name in d.expected_columns)


class TestSerialisation:
    def test_round_trip_and_unknown_class(self):
        d = MismatchDiagnosis("actual_empty", 3, 0, ["a"], ["a"])
        assert MismatchDiagnosis.from_dict(d.to_dict()) == d
        with pytest.raises(ValueError, match="unknown mismatch_class"):
            MismatchDiagnosis.from_dict({"mismatch_class": "nope"})

    def test_class_derived_from_status_when_there_is_no_diagnosis(self):
        assert mismatch_class_for("pass", True, None) == "match"
        assert mismatch_class_for("generation_error", False, None) == "not_executed"
        assert mismatch_class_for("fingerprint_mismatch", False, None) is None
