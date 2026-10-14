# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""How a generated query's result differs from the reference -- without any value.

:func:`eval.compare.compare_frames` answers *whether* two results are the
same answer. A reviewer who cannot see the warehouse needs to know *how* they
differ: too few rows, too many, the right rows in the wrong order, the right
numbers in the wrong columns, or a number that is off by two percent. This
module computes that on the machine that ran both queries and returns a
:class:`~eval.models.MismatchDiagnosis` that holds only counts, column names
and, for one numeric cell, a coarse error bucket. It never keeps, returns or
logs a cell value, so the diagnosis can leave the server in a share bundle
(:mod:`eval.share`).

The decision order (the first rule that applies wins):

1. ``match`` -- :func:`~eval.compare.compare_to_reference` says equal.
2. ``column_count_differs`` -- a different number of columns.
3. ``actual_empty`` / ``expected_empty`` -- no rows on one side only.
4. different row counts: ``actual_is_subset`` / ``actual_is_superset`` when
   the smaller result's rows are all in the larger one, else
   ``row_count_differs``.
5. same shape: ``same_rows_different_order`` when the same rows are present
   (so only the order the reference's ``ORDER BY ... TOP`` demands is wrong),
   ``column_names_differ`` when the data agrees once the columns are lined up
   (by name, else by content), else ``values_differ_same_shape``.

``not_executed`` is for a query that produced no result: see
:func:`diagnose_not_executed`.

Column names are schema, not data, and are exported -- with one limit: a
``PIVOT`` turns values into column names. Names are cut at 128 characters and
the list at 64 names.
"""

from __future__ import annotations

import math

import pandas as pd

from eval.compare import (
    ComparisonOptions,
    align_columns,
    compare_frames,
    multiset_contains,
    multiset_equal,
    normalise_cell,
    reference_ordering,
)
from eval.models import MismatchDiagnosis

__all__ = [
    "NOT_EXECUTED_STATUSES",
    "diagnose_not_executed",
    "diagnose_results",
    "mismatch_class_for",
    "relative_error_bucket",
]

#: Statuses where the generated SQL (or the reference) never produced a result.
NOT_EXECUTED_STATUSES = frozenset({
    "guard_rejected",
    "generation_error",
    "execution_error",
    "reference_error",
    "unexpected_out_of_scope",
    "missed_out_of_scope",
})

_MAX_COLUMNS = 64
_MAX_NAME_CHARS = 128


def _names(df: pd.DataFrame | None) -> list[str] | None:
    if df is None:
        return None
    return [str(c)[:_MAX_NAME_CHARS] for c in list(df.columns)[:_MAX_COLUMNS]]


def relative_error_bucket(expected: float, actual: float) -> str:
    """Bucket the relative error of *actual* against *expected*: ``<1%``, ``<10%`` or ``>=10%``.

    A zero reference with a non-zero answer is infinitely far off, so it is
    ``>=10%``; two zeros are ``<1%``.

    Examples
    --------
    >>> relative_error_bucket(100.0, 100.5)
    '<1%'
    >>> relative_error_bucket(100.0, 95.0)
    '<10%'
    >>> relative_error_bucket(100.0, 50.0)
    '>=10%'
    >>> relative_error_bucket(0.0, 3.0), relative_error_bucket(0.0, 0.0)
    ('>=10%', '<1%')
    """
    if expected == 0:
        return "<1%" if actual == 0 else ">=10%"
    ratio = abs(actual - expected) / abs(expected)
    if math.isnan(ratio):
        return ">=10%"
    if ratio < 0.01:
        return "<1%"
    if ratio < 0.10:
        return "<10%"
    return ">=10%"


def _single_number(df: pd.DataFrame) -> float | None:
    """The one cell of a 1x1 frame if it is a number, else ``None``."""
    if df.shape != (1, 1):
        return None
    kind, value = normalise_cell(df.iat[0, 0])
    return float(value) if kind in ("i", "f") else None


def mismatch_class_for(status: str, passed: bool, diagnosed: str | None) -> str | None:
    """The class to report for a case: its diagnosis, else one derived from the status.

    A case that passed without a diagnosis (an out-of-scope question
    correctly declined) is a ``match``; one that never got a result is
    ``not_executed``; anything else without a diagnosis (a report written
    before diagnoses existed) is unknown, ``None``.

    Examples
    --------
    >>> mismatch_class_for("pass", True, None)
    'match'
    >>> mismatch_class_for("guard_rejected", False, None)
    'not_executed'
    >>> mismatch_class_for("fingerprint_mismatch", False, None) is None
    True
    >>> mismatch_class_for("fingerprint_mismatch", False, "actual_empty")
    'actual_empty'
    """
    if diagnosed is not None:
        return diagnosed
    if passed:
        return "match"
    if status in NOT_EXECUTED_STATUSES:
        return "not_executed"
    return None


def _is_capped_match(
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    row_cap: int | None,
    ordered: bool,
    keys: tuple[int, ...] | None,
    options: ComparisonOptions,
) -> bool:
    """Whether *actual* is *expected* cut at exactly *row_cap* rows, and nothing else.

    The row count must equal the cap and be below the reference's (equal
    counts that are not the same answer are a wrong answer, not a cut), and
    every row of *actual* must be a row of *expected* under the comparison's
    own matching rules. A subset that stops short of the cap is not capped.
    """
    if not row_cap or len(actual) != row_cap or len(expected) <= row_cap:
        return False
    if ordered:
        return compare_frames(
            expected.iloc[:row_cap], actual, ordered=True, order_keys=keys, options=options
        ).equal
    return multiset_contains(expected, actual, options=options)


def diagnose_not_executed(
    expected: pd.DataFrame | None = None, actual: pd.DataFrame | None = None
) -> MismatchDiagnosis:
    """The diagnosis of a case where at least one side produced no result.

    Whatever shape is known (a reference that ran when the generated SQL did
    not, or the reverse) is kept.

    Examples
    --------
    >>> d = diagnose_not_executed(expected=pd.DataFrame({"n": [1, 2]}))
    >>> (d.mismatch_class, d.expected_rows, d.actual_rows, d.expected_columns)
    ('not_executed', 2, None, ['n'])
    """
    return MismatchDiagnosis(
        mismatch_class="not_executed",
        expected_rows=None if expected is None else len(expected),
        actual_rows=None if actual is None else len(actual),
        expected_columns=_names(expected),
        actual_columns=_names(actual),
    )


def diagnose_results(
    expected_sql: str,
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    *,
    options: ComparisonOptions | None = None,
    dialect: str | None = None,
    row_cap: int | None = None,
) -> MismatchDiagnosis:
    """Classify how *actual* differs from the reference *expected*.

    Parameters
    ----------
    expected_sql:
        The case's reference SQL; its ``ORDER BY`` decides whether row order
        is part of the answer (as in :func:`~eval.compare.compare_to_reference`).
    expected, actual:
        The reference result and the generated SQL's result.
    options:
        Comparison tolerance; must be the one the verdict was reached with.
    dialect:
        sqlglot dialect used to read *expected_sql*.
    row_cap:
        The row cap the server applied to *actual* (the injected ``TOP n``
        or ``MAX_ROWS_RETURNED``, whichever is lower), or ``None`` when
        there was none (``--single-shot``). A result that is not the same
        answer but has exactly *row_cap* rows, every one of them a row of
        the reference, is ``match_capped``: the analyst saw the right rows,
        cut short by policy. For a reference that orders its rows, *actual*
        must also be the reference's first *row_cap* rows in that order.

    Returns
    -------
    MismatchDiagnosis
        Counts, column names and an optional error bucket; never a value.

    Examples
    --------
    >>> ref = pd.DataFrame({"n": [100]})
    >>> diagnose_results("SELECT 1", ref, pd.DataFrame({"x": [100]}), dialect="tsql").mismatch_class
    'match'
    >>> d = diagnose_results("SELECT 1", ref, pd.DataFrame({"x": [93]}), dialect="tsql")
    >>> (d.mismatch_class, d.relative_error_bucket)
    ('values_differ_same_shape', '<10%')
    >>> top = pd.DataFrame({"s": ["a", "b"], "v": [2, 1]})
    >>> diagnose_results("SELECT TOP 2 s, v FROM T ORDER BY v DESC", top, top.iloc[::-1],
    ...                  dialect="tsql").mismatch_class
    'same_rows_different_order'
    >>> diagnose_results("SELECT s, v FROM T", top, top.iloc[:1], dialect="tsql").mismatch_class
    'actual_is_subset'
    >>> capped = diagnose_results("SELECT s, v FROM T", top, top.iloc[:1], dialect="tsql", row_cap=1)
    >>> (capped.mismatch_class, capped.capped, capped.row_cap)
    ('match_capped', True, 1)
    >>> diagnose_results("SELECT s, v FROM T", top, top[["v", "s"]], dialect="tsql").mismatch_class
    'column_names_differ'
    """
    opts = options or ComparisonOptions()
    ordered, keys = reference_ordering(expected_sql, dialect)
    verdict = compare_frames(
        expected, actual, ordered=ordered, order_keys=keys, options=opts
    )
    expected_rows, actual_rows = len(expected), len(actual)
    expected_cols, actual_cols = len(expected.columns), len(actual.columns)

    bucket: str | None = None
    mismatch_class: str
    if verdict.equal:
        mismatch_class = "match"
    elif expected_cols != actual_cols:
        mismatch_class = "column_count_differs"
    elif _is_capped_match(expected, actual, row_cap, ordered, keys, opts):
        mismatch_class = "match_capped"
    elif actual_rows == 0 and expected_rows > 0:
        mismatch_class = "actual_empty"
    elif expected_rows == 0 and actual_rows > 0:
        mismatch_class = "expected_empty"
    elif expected_rows != actual_rows:
        if actual_rows < expected_rows and multiset_contains(expected, actual, options=opts):
            mismatch_class = "actual_is_subset"
        elif actual_rows > expected_rows and multiset_contains(actual, expected, options=opts):
            mismatch_class = "actual_is_superset"
        else:
            mismatch_class = "row_count_differs"
    elif multiset_equal(expected, actual, options=opts):
        mismatch_class = "same_rows_different_order"
    elif align_columns(expected, actual, options=opts) is not None:
        mismatch_class = "column_names_differ"
    else:
        mismatch_class = "values_differ_same_shape"

    if mismatch_class not in ("match", "match_capped"):
        want, got = _single_number(expected), _single_number(actual)
        if want is not None and got is not None:
            bucket = relative_error_bucket(want, got)

    return MismatchDiagnosis(
        mismatch_class=mismatch_class,  # type: ignore[arg-type]
        expected_rows=expected_rows,
        actual_rows=actual_rows,
        expected_columns=_names(expected),
        actual_columns=_names(actual),
        relative_error_bucket=bucket,
        capped=mismatch_class == "match_capped",
        row_cap=row_cap if row_cap is not None else None,
    )
