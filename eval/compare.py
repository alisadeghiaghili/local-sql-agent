# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Execution-accuracy comparison of two result sets (live reference mode).

Why this exists
---------------
A stored :func:`~eval.fingerprint.fingerprint_dataframe` records one
answer at one moment. A warehouse that changes every day ("trades
yesterday") makes that hash stale within hours, and the regression gate
then reports a false failure for a perfectly correct query. The remedy is
to run the case's own ``expected_sql`` *in the same run*, against the same
data, and compare the two results. This module decides when two results
are "the same answer".

The rule follows execution accuracy (EX) as used by BIRD and Spider,
adapted to a T-SQL warehouse:

* **Rows are compared as a multiset of tuples.** Duplicates matter (a
  ``UNION ALL`` that doubles a row is wrong), so ``[a, a, b]`` is not
  ``[a, b]``; but the order the server happened to return them in is
  ignored.
* **Column names are ignored** -- ``COUNT(*) AS n`` and
  ``COUNT(*) AS TotalCount`` are the same answer. **Column order is kept
  as selected**: the tuple ``(symbol, volume)`` is not ``(volume,
  symbol)``. The column *count* must match even when both results are
  empty.
* **Row order is ignored unless the reference decides it.** When the
  reference SQL has a top-level ``ORDER BY`` together with ``TOP`` /
  ``OFFSET`` / ``FETCH`` the order is part of the answer ("the five
  largest, largest first"), so :func:`reference_ordering` flags it and
  the rows must come back in that order. Ties on the sort key may appear
  in any order (the sort key columns are compared position by position,
  the remaining columns of tied rows may permute); when the sort key
  cannot be located among the selected columns the whole row sequence is
  compared strictly. An ``ORDER BY`` without ``TOP`` / ``OFFSET`` only
  orders a result whose rows are the same either way, so it is ignored.
* **Numbers are compared with a tolerance.** Two numbers that are not both
  integers match when ``|a - b| <= tolerance * max(1, |a|, |b|)`` (a
  relative tolerance, with an absolute floor of ``tolerance`` near zero),
  default ``1e-6`` -- the same order as the six digits the stored
  fingerprint rounds to, enough to absorb ``SUM`` accumulation order and
  ``Decimal`` vs ``float`` conversions. **Two integers always compare
  exactly**: an identifier or a count that is off by one is a different
  answer, whatever its magnitude. ``bool`` counts as an integer
  (``True == 1``) and ``3`` equals ``3.0``.
* **NULL equals NULL** (``None``, ``NaN``, ``NaT``, ``pandas.NA``) and
  nothing else: not ``0``, not the empty string.
* **Dates**: a datetime at exactly midnight equals the plain date, so
  ``CAST(d AS date)`` and a ``datetime`` column that holds the same day
  agree; any other time of day is kept.
* **Strings are compared exactly** (no case folding, no trimming).

Known limit: when a ``TOP n`` cut falls inside a group of rows that tie on
the ``ORDER BY`` key, which of the tied rows are returned is the server's
choice and a correct answer can differ from the reference. Give such a
reference a tie-breaking second sort key; ``eval.cli verify`` cannot see
the ambiguity.

Nothing here prints or returns a cell value: a :class:`ComparisonResult`
reason carries counts and positions only, so it is safe in a report.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pandas as pd
import sqlglot
from sqlglot import exp

import config as cfg

#: A normalised cell: ``(kind, value)`` where kind is one of ``"n"``
#: (NULL), ``"i"`` (integer), ``"f"`` (inexact number), ``"t"`` (date /
#: datetime / time as text) or ``"s"`` (text).
Cell = tuple[str, Any]


@dataclass(frozen=True, slots=True)
class ComparisonOptions:
    """Knobs of :func:`compare_frames`.

    Parameters
    ----------
    tolerance:
        Relative tolerance for numbers that are not both integers, with an
        absolute floor of the same size near zero (see the module
        docstring). ``0.0`` demands exact equality. Exposed on the CLI as
        ``--float-tolerance``.

    Examples
    --------
    >>> ComparisonOptions().tolerance
    1e-06
    """

    tolerance: float = 1e-6

    def __post_init__(self) -> None:
        if not (self.tolerance >= 0.0) or math.isinf(self.tolerance):
            raise ValueError(f"tolerance must be a finite number >= 0, got {self.tolerance!r}")


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Verdict of :func:`compare_frames`.

    Parameters
    ----------
    equal:
        ``True`` when the two results are the same answer.
    reason:
        ``"match"`` when equal; otherwise a short, value-free description
        (counts and positions only, never a cell).
    ordered:
        Whether row order was part of the comparison.
    """

    equal: bool
    reason: str
    ordered: bool = False


def _is_null(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def normalise_cell(value: Any) -> Cell:
    """Reduce one cell to the ``(kind, value)`` form the comparison uses.

    Parameters
    ----------
    value:
        A DataFrame cell: Python or numpy scalar, ``Decimal``, datetime,
        string, or a null-like sentinel.

    Returns
    -------
    tuple[str, Any]

    Examples
    --------
    >>> normalise_cell(None), normalise_cell(float("nan"))
    (('n', None), ('n', None))
    >>> normalise_cell(True), normalise_cell(7)
    (('i', 1), ('i', 7))
    >>> from decimal import Decimal
    >>> normalise_cell(Decimal("2.50"))
    ('f', 2.5)
    >>> normalise_cell(datetime(2024, 1, 1)) == normalise_cell(date(2024, 1, 1))
    True
    >>> normalise_cell(datetime(2024, 1, 1, 9, 30))
    ('t', '2024-01-01T09:30:00')
    >>> normalise_cell("abc")
    ('s', 'abc')
    """
    if _is_null(value):
        return ("n", None)
    if hasattr(value, "item") and type(value).__module__ == "numpy":
        value = value.item()
        if _is_null(value):
            return ("n", None)
    if isinstance(value, bool):
        return ("i", int(value))
    if isinstance(value, int):
        return ("i", value)
    if isinstance(value, (float, Decimal)):
        return ("f", float(value))
    if isinstance(value, datetime):
        if value.time() == time(0, 0) and value.tzinfo is None:
            return ("t", value.date().isoformat())
        return ("t", value.isoformat())
    if isinstance(value, (date, time)):
        return ("t", value.isoformat())
    if isinstance(value, (bytes, bytearray)):
        return ("s", bytes(value).hex())
    return ("s", value if isinstance(value, str) else str(value))


def _cells_equal(a: Cell, b: Cell, tolerance: float) -> bool:
    kind_a, val_a = a
    kind_b, val_b = b
    numeric = ("i", "f")
    if kind_a in numeric and kind_b in numeric:
        if kind_a == "i" and kind_b == "i":
            return bool(val_a == val_b)
        fa, fb = float(val_a), float(val_b)
        if fa == fb:
            return True
        return abs(fa - fb) <= tolerance * max(1.0, abs(fa), abs(fb))
    return kind_a == kind_b and bool(val_a == val_b)


def _rows_of(df: pd.DataFrame) -> list[tuple[Cell, ...]]:
    """Every row as a tuple of normalised cells, in column order as selected."""
    return [
        tuple(normalise_cell(v) for v in row)
        for row in df.astype(object).itertuples(index=False, name=None)
    ]


def _rows_equal(a: tuple[Cell, ...], b: tuple[Cell, ...], tolerance: float) -> bool:
    return len(a) == len(b) and all(_cells_equal(x, y, tolerance) for x, y in zip(a, b))


def _signature(row: tuple[Cell, ...]) -> tuple[Any, ...]:
    """The part of a row that must match exactly: every non-numeric cell
    and the positions of the numeric ones."""
    return tuple(("num",) if cell[0] in ("i", "f") else cell for cell in row)


def _numeric_part(row: tuple[Cell, ...]) -> tuple[Cell, ...]:
    return tuple(cell for cell in row if cell[0] in ("i", "f"))


def _sort_key(digits: int | None) -> Callable[[tuple[Cell, ...]], tuple[float, ...]]:
    def key(vector: tuple[Cell, ...]) -> tuple[float, ...]:
        if digits is None:
            return tuple(float(v) for _, v in vector)
        return tuple(round(float(v), digits) for _, v in vector)

    return key


def _multisets_equal(
    expected: list[tuple[Cell, ...]],
    actual: list[tuple[Cell, ...]],
    tolerance: float,
) -> bool:
    """Whether *expected* and *actual* hold the same rows, order ignored."""
    groups_e: dict[tuple[Any, ...], list[tuple[Cell, ...]]] = defaultdict(list)
    groups_a: dict[tuple[Any, ...], list[tuple[Cell, ...]]] = defaultdict(list)
    for row in expected:
        groups_e[_signature(row)].append(_numeric_part(row))
    for row in actual:
        groups_a[_signature(row)].append(_numeric_part(row))
    if groups_e.keys() != groups_a.keys():
        return False
    # Sorting by numbers rounded to the tolerance's precision puts rows that
    # differ only by float noise next to their counterpart on the other side.
    digits = None if tolerance <= 0.0 else max(0, math.ceil(-math.log10(tolerance)))
    key = _sort_key(digits)
    for signature, vectors_e in groups_e.items():
        vectors_a = groups_a[signature]
        if len(vectors_e) != len(vectors_a):
            return False
        if not vectors_e[0]:
            continue  # no numeric cells: the signature already matched
        for ve, va in zip(sorted(vectors_e, key=key), sorted(vectors_a, key=key)):
            if not _rows_equal(ve, va, tolerance):
                return False
    return True


def compare_frames(
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    *,
    ordered: bool = False,
    order_keys: Sequence[int] | None = None,
    options: ComparisonOptions | None = None,
) -> ComparisonResult:
    """Decide whether *actual* is the same answer as *expected*.

    See the module docstring for the full semantics.

    Parameters
    ----------
    expected:
        The reference result.
    actual:
        The result under test.
    ordered:
        Compare row order too. Normally taken from
        :func:`reference_ordering` of the reference SQL.
    order_keys:
        With *ordered*, the zero-based positions of the ``ORDER BY`` key
        among the selected columns. Rows tied on those positions may come
        back in any order. ``None`` compares the whole row sequence
        strictly.
    options:
        Tolerance settings; defaults to :class:`ComparisonOptions`.

    Returns
    -------
    ComparisonResult

    Examples
    --------
    Column names do not matter, row order does not matter:

    >>> a = pd.DataFrame({"sym": ["x", "y"], "n": [1, 2]})
    >>> b = pd.DataFrame({"s": ["y", "x"], "total": [2, 1]})
    >>> compare_frames(a, b).equal
    True

    Column order does:

    >>> compare_frames(a, b[["total", "s"]]).equal
    False

    Duplicate rows count:

    >>> compare_frames(pd.DataFrame({"n": [1, 1]}), pd.DataFrame({"n": [1]})).reason
    'row count differs (expected 2, got 1)'

    Decimals and floats agree within the tolerance, integers never do:

    >>> from decimal import Decimal
    >>> compare_frames(pd.DataFrame({"v": [Decimal("10.50")]}),
    ...                pd.DataFrame({"v": [10.5000001]})).equal
    True
    >>> compare_frames(pd.DataFrame({"id": [1000000000001]}),
    ...                pd.DataFrame({"id": [1000000000002]})).equal
    False

    Order is part of the answer when asked for; ties may permute:

    >>> top = pd.DataFrame({"s": ["a", "b", "c"], "v": [9, 5, 5]})
    >>> swapped = pd.DataFrame({"s": ["a", "c", "b"], "v": [9, 5, 5]})
    >>> compare_frames(top, swapped, ordered=True, order_keys=[1]).equal
    True
    >>> compare_frames(top, swapped, ordered=True).equal
    False
    >>> compare_frames(top, top.iloc[::-1], ordered=True, order_keys=[1]).reason
    'row order differs from the reference ORDER BY'
    """
    opts = options or ComparisonOptions()
    if len(expected.columns) != len(actual.columns):
        return ComparisonResult(
            False,
            f"column count differs (expected {len(expected.columns)}, got {len(actual.columns)})",
            ordered,
        )
    rows_e = _rows_of(expected)
    rows_a = _rows_of(actual)
    if len(rows_e) != len(rows_a):
        return ComparisonResult(
            False, f"row count differs (expected {len(rows_e)}, got {len(rows_a)})", ordered
        )
    if not _multisets_equal(rows_e, rows_a, opts.tolerance):
        return ComparisonResult(False, "result values differ", ordered)
    if ordered and rows_e:
        if order_keys:
            for position, (re_, ra_) in enumerate(zip(rows_e, rows_a)):
                if not all(_cells_equal(re_[k], ra_[k], opts.tolerance) for k in order_keys):
                    return ComparisonResult(
                        False, "row order differs from the reference ORDER BY", True
                    )
        else:
            for re_, ra_ in zip(rows_e, rows_a):
                if not _rows_equal(re_, ra_, opts.tolerance):
                    return ComparisonResult(
                        False, "row order differs from the reference ORDER BY", True
                    )
    return ComparisonResult(True, "match", ordered)


# ---------------------------------------------------------------------------
# Does the reference SQL make row order part of the answer?
# ---------------------------------------------------------------------------


def _projection_names(select: exp.Select) -> list[set[str]] | None:
    """The names each selected column answers to, or ``None`` with a ``*``."""
    names: list[set[str]] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            return None
        aliases = {projection.alias_or_name.lower()} - {""}
        if isinstance(projection, exp.Alias) and isinstance(projection.this, exp.Column):
            aliases.add(projection.this.name.lower())
        names.append(aliases)
    return names


def reference_ordering(
    sql: str, dialect: str | None = None
) -> tuple[bool, tuple[int, ...] | None]:
    """Whether *sql* makes row order part of the answer, and by which columns.

    Row order is part of the answer only for a top-level ``ORDER BY``
    together with ``TOP`` / ``OFFSET`` / ``FETCH`` (see the module
    docstring). A statement that cannot be parsed, a set operation, and a
    query without those clauses are unordered.

    Parameters
    ----------
    sql:
        The reference SQL.
    dialect:
        sqlglot dialect; defaults to ``config.settings.sql_dialect``.

    Returns
    -------
    tuple[bool, tuple[int, ...] | None]
        ``(ordered, keys)``. *keys* are the zero-based positions of the
        ``ORDER BY`` expressions among the selected columns (by alias,
        column name or ordinal), or ``None`` when any of them cannot be
        located (a ``*`` projection, an expression that is not selected).

    Examples
    --------
    >>> reference_ordering("SELECT TOP 5 Symbol, Volume AS V FROM T ORDER BY Volume DESC", "tsql")
    (True, (1,))
    >>> reference_ordering("SELECT a, b FROM T ORDER BY b, 1", "tsql")
    (False, None)
    >>> reference_ordering("SELECT a FROM T ORDER BY a OFFSET 0 ROWS FETCH NEXT 3 ROWS ONLY", "tsql")
    (True, (0,))
    >>> reference_ordering("SELECT TOP 3 a FROM T ORDER BY a + 1", "tsql")
    (True, None)
    >>> reference_ordering("SELECT TOP 3 a FROM T", "tsql")
    (False, None)
    >>> reference_ordering("not sql at all (", "tsql")
    (False, None)
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect or cfg.settings.sql_dialect)
    except Exception:  # noqa: BLE001 - an unparsable reference is simply unordered
        return (False, None)
    if not isinstance(tree, exp.Select):
        return (False, None)
    order = tree.args.get("order")
    if not order:
        return (False, None)
    if not (tree.args.get("limit") or tree.args.get("offset") or tree.args.get("fetch")):
        return (False, None)

    names = _projection_names(tree)
    if names is None:
        return (True, None)
    keys: list[int] = []
    for ordered_expr in order.expressions:
        target = ordered_expr.this
        position: int | None = None
        if isinstance(target, exp.Literal) and not target.is_string:
            try:
                candidate = int(target.name) - 1
            except ValueError:
                candidate = -1
            if 0 <= candidate < len(names):
                position = candidate
        elif isinstance(target, exp.Column):
            wanted = target.name.lower()
            for index, aliases in enumerate(names):
                if wanted in aliases:
                    position = index
                    break
        if position is None:
            return (True, None)
        keys.append(position)
    return (True, tuple(keys))


def compare_to_reference(
    reference_sql: str,
    reference: pd.DataFrame,
    actual: pd.DataFrame,
    *,
    options: ComparisonOptions | None = None,
    dialect: str | None = None,
) -> ComparisonResult:
    """:func:`compare_frames` with the ordering rule taken from *reference_sql*.

    Parameters
    ----------
    reference_sql:
        The case's ``expected_sql``.
    reference:
        Its freshly executed result.
    actual:
        The result of the SQL under test.
    options, dialect:
        See :func:`compare_frames` and :func:`reference_ordering`.

    Returns
    -------
    ComparisonResult

    Examples
    --------
    >>> ref = pd.DataFrame({"s": ["a", "b"]})
    >>> compare_to_reference("SELECT s FROM T", ref, ref.iloc[::-1], dialect="tsql").equal
    True
    >>> compare_to_reference("SELECT TOP 2 s FROM T ORDER BY s", ref, ref.iloc[::-1],
    ...                      dialect="tsql").equal
    False
    """
    ordered, keys = reference_ordering(reference_sql, dialect)
    return compare_frames(
        reference, actual, ordered=ordered, order_keys=keys, options=options
    )
