"""Thorough unit tests for eval/fingerprint.py — the load-bearing module.

Every case in the Phase 0-A spec is covered explicitly:
* differing column order
* differing row order
* Decimal vs float
* numpy scalars (int64, float64, bool_)
* empty frames (same/different columns)
* None / NaN / NaT normalisation
* genuinely different data must NOT collide

Run::

    .venv/Scripts/python.exe -m pytest eval/tests/test_fingerprint.py -v
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from eval.fingerprint import canonical_repr, fingerprint_dataframe


# ---------------------------------------------------------------------------
# Column order insensitivity
# ---------------------------------------------------------------------------

class TestColumnOrderInsensitivity:
    def test_two_columns_swapped(self):
        a = pd.DataFrame({"name": ["Ali", "Sara"], "count": [3, 5]})
        b = pd.DataFrame({"count": [3, 5], "name": ["Ali", "Sara"]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_many_columns_shuffled(self):
        cols = {c: [i] for i, c in enumerate("edcba")}
        a = pd.DataFrame(cols)
        b = pd.DataFrame({k: cols[k] for k in sorted(cols)})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_canonical_repr_columns_are_sorted(self):
        df = pd.DataFrame({"z": [1], "a": [2]})
        assert canonical_repr(df)["columns"] == ["a", "z"]


# ---------------------------------------------------------------------------
# Row order insensitivity
# ---------------------------------------------------------------------------

class TestRowOrderInsensitivity:
    def test_rows_reversed(self):
        a = pd.DataFrame({"n": [1, 2, 3]})
        b = pd.DataFrame({"n": [3, 2, 1]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_rows_shuffled_multi_column(self):
        a = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
        b = pd.DataFrame({"id": [3, 1, 2], "name": ["c", "a", "b"]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_row_shuffle_does_not_scramble_pairing(self):
        """Shuffling must move whole rows, not columns independently."""
        a = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        # Same values, but pairing broken (id=1 now paired with "b").
        wrong = pd.DataFrame({"id": [1, 2], "name": ["b", "a"]})
        assert fingerprint_dataframe(a) != fingerprint_dataframe(wrong)


# ---------------------------------------------------------------------------
# Decimal vs float vs numpy scalars
# ---------------------------------------------------------------------------

class TestNumericTypeNormalisation:
    def test_decimal_equals_float(self):
        a = pd.DataFrame({"price": [Decimal("19.99")]})
        b = pd.DataFrame({"price": [19.99]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_decimal_equals_plain_python_float_via_object_column(self):
        a = pd.DataFrame({"price": pd.Series([Decimal("100.00")], dtype=object)})
        b = pd.DataFrame({"price": [100.0]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_numpy_int64_equals_python_int(self):
        a = pd.DataFrame({"n": np.array([5], dtype="int64")})
        b = pd.DataFrame({"n": [5]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_numpy_float64_equals_python_float(self):
        a = pd.DataFrame({"n": np.array([1.5], dtype="float64")})
        b = pd.DataFrame({"n": [1.5]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_numpy_bool_equals_python_bool(self):
        a = pd.DataFrame({"flag": np.array([True, False])})
        b = pd.DataFrame({"flag": [True, False]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_int_and_float_of_same_value_are_distinct_types_but_hash_equal(self):
        # 5 and 5.0 normalise to different Python types (int vs float) but
        # json.dumps renders "5" vs "5.0" -- this SHOULD be a real
        # distinction (COUNT(*) returning 5 vs AVG() returning 5.0 are
        # different signals), so we assert they are NOT silently merged.
        a = pd.DataFrame({"n": [5]})
        b = pd.DataFrame({"n": [5.0]})
        assert fingerprint_dataframe(a) != fingerprint_dataframe(b)

    def test_float_rounding_absorbs_noise_within_precision(self):
        a = pd.DataFrame({"n": [1.0000001]})
        b = pd.DataFrame({"n": [1.0000002]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_float_rounding_still_catches_real_differences(self):
        a = pd.DataFrame({"n": [1.01]})
        b = pd.DataFrame({"n": [1.02]})
        assert fingerprint_dataframe(a) != fingerprint_dataframe(b)

    def test_negative_zero_normalised(self):
        a = pd.DataFrame({"n": [-0.0]})
        b = pd.DataFrame({"n": [0.0]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_custom_precision_is_honoured(self):
        a = pd.DataFrame({"n": [1.001]})
        b = pd.DataFrame({"n": [1.009]})
        # At precision=1 both round to 1.0
        assert fingerprint_dataframe(a, float_precision=1) == fingerprint_dataframe(
            b, float_precision=1
        )
        # At precision=2 they diverge
        assert fingerprint_dataframe(a, float_precision=2) != fingerprint_dataframe(
            b, float_precision=2
        )


# ---------------------------------------------------------------------------
# Null-like sentinel normalisation: None / NaN / NaT / pandas.NA
# ---------------------------------------------------------------------------

class TestNullNormalisation:
    def test_none_and_nan_are_equivalent(self):
        a = pd.DataFrame({"x": pd.Series([1, None], dtype=object)})
        b = pd.DataFrame({"x": pd.Series([1, float("nan")], dtype=object)})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_nat_normalised_like_none(self):
        a = pd.DataFrame({"d": [pd.Timestamp("2024-01-01"), pd.NaT]})
        b = pd.DataFrame({"d": pd.Series(["2024-01-01T00:00:00", None], dtype=object)})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_pandas_na_normalised_like_none(self):
        a = pd.DataFrame({"x": pd.array([1, pd.NA], dtype="Int64")})
        b = pd.DataFrame({"x": pd.Series([1, None], dtype=object)})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_null_position_still_matters_via_row_identity(self):
        # A null must attach to the correct row, not just "exist somewhere".
        a = pd.DataFrame({"id": [1, 2], "v": [None, 9]})
        b = pd.DataFrame({"id": [1, 2], "v": [9, None]})
        assert fingerprint_dataframe(a) != fingerprint_dataframe(b)


# ---------------------------------------------------------------------------
# Empty frames
# ---------------------------------------------------------------------------

class TestEmptyFrames:
    def test_empty_frames_same_columns_match(self):
        a = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        b = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_empty_frame_dtype_does_not_matter(self):
        """An empty result set is empty regardless of the column's dtype."""
        a = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        b = pd.DataFrame({"n": pd.Series([], dtype="float64")})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)

    def test_empty_frames_different_columns_do_not_match(self):
        a = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        b = pd.DataFrame({"m": pd.Series([], dtype="int64")})
        assert fingerprint_dataframe(a) != fingerprint_dataframe(b)

    def test_empty_vs_nonempty_never_match(self):
        empty = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        nonempty = pd.DataFrame({"n": [0]})
        assert fingerprint_dataframe(empty) != fingerprint_dataframe(nonempty)

    def test_empty_frame_row_count_is_zero(self):
        empty = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        assert canonical_repr(empty)["row_count"] == 0
        assert canonical_repr(empty)["rows"] == []

    def test_completely_empty_frame_no_columns(self):
        """A DataFrame with zero columns and zero rows must not raise."""
        empty = pd.DataFrame()
        result = fingerprint_dataframe(empty)
        assert isinstance(result, str) and len(result) == 64


# ---------------------------------------------------------------------------
# Genuine differences must NOT collide
# ---------------------------------------------------------------------------

class TestRealDifferencesDetected:
    def test_different_row_count(self):
        a = pd.DataFrame({"n": [1, 2, 3]})
        b = pd.DataFrame({"n": [1, 2]})
        assert fingerprint_dataframe(a) != fingerprint_dataframe(b)

    def test_different_values(self):
        a = pd.DataFrame({"n": [1, 2, 3]})
        b = pd.DataFrame({"n": [1, 2, 4]})
        assert fingerprint_dataframe(a) != fingerprint_dataframe(b)

    def test_different_column_names(self):
        a = pd.DataFrame({"count": [5]})
        b = pd.DataFrame({"total": [5]})
        assert fingerprint_dataframe(a) != fingerprint_dataframe(b)

    def test_duplicate_rows_are_preserved_not_deduplicated(self):
        """COUNT semantics: two identical rows differ from one."""
        a = pd.DataFrame({"n": [1, 1]})
        b = pd.DataFrame({"n": [1]})
        assert fingerprint_dataframe(a) != fingerprint_dataframe(b)

    def test_persian_text_is_hashed_faithfully(self):
        a = pd.DataFrame({"name": ["مشتری"]})  # مشتری
        b = pd.DataFrame({"name": ["مشتریان"]})  # مشتریان
        assert fingerprint_dataframe(a) != fingerprint_dataframe(b)


# ---------------------------------------------------------------------------
# Determinism / type errors
# ---------------------------------------------------------------------------

class TestDeterminismAndErrors:
    def test_repeated_calls_are_deterministic(self):
        df = pd.DataFrame({"b": [2, 1], "a": ["y", "x"]})
        assert fingerprint_dataframe(df) == fingerprint_dataframe(df)

    def test_rejects_non_dataframe(self):
        with pytest.raises(TypeError):
            fingerprint_dataframe([{"n": 1}])  # type: ignore[arg-type]

    def test_datetime_and_timestamp_equivalent(self):
        a = pd.DataFrame({"d": [datetime(2024, 1, 1)]})
        b = pd.DataFrame({"d": [pd.Timestamp("2024-01-01")]})
        assert fingerprint_dataframe(a) == fingerprint_dataframe(b)
