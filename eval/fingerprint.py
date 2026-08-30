"""Canonical, order-insensitive fingerprint of a pandas result set.

Why this exists
----------------
Two SQL queries can be *semantically* identical while producing DataFrames
that differ in every superficial way: column order, row order, ``Decimal``
vs ``float``, ``numpy.int64`` vs plain ``int``.  Comparing SQL strings
cannot tell "the same answer" from "a different answer" — only comparing
the *data* can.  This module turns a DataFrame into a single hash such
that two DataFrames a human would call "the same answer" always hash to
the same value, and two that differ in actual content never do.

Algorithm
---------
1. Sort **columns** alphabetically by name — column order carries no
   meaning in a result set.
2. Normalise every cell:

   * ``None`` / ``NaN`` / ``NaT`` / ``pandas.NA`` -> the sentinel ``None``.
   * ``Decimal`` -> ``float``, rounded to ``float_precision`` digits.
   * ``numpy`` integer scalars -> plain ``int``.
   * ``numpy`` floating scalars -> plain ``float``, rounded.
   * ``numpy`` bool scalars -> plain ``bool``.
   * plain ``float`` -> rounded; ``-0.0`` normalised to ``0.0``.
   * ``pandas.Timestamp`` / ``datetime`` -> ISO-8601 string.
   * everything else -> passed through unchanged (``str``, ``int``, ``bool``).
3. Encode each row as a canonical JSON array (values in the sorted-column
   order from step 1).
4. Sort the resulting list of row-JSON strings lexicographically — row
   order carries no meaning in a result set either.
5. Hash ``{"columns": [...], "row_count": n, "rows": [...]}`` with SHA-256
   and return the hex digest.

The intermediate, human-readable structure (steps 1-4) is exposed as
:func:`canonical_repr` so callers/tests can inspect *why* two frames
differ instead of only seeing that two hashes disagree.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

#: Default number of digits floats are rounded to before hashing. Chosen to
#: absorb ordinary floating-point noise (e.g. SUM() accumulation order)
#: while still catching real discrepancies.
DEFAULT_FLOAT_PRECISION = 6


def _normalise_scalar(value: Any, float_precision: int) -> Any:
    """Convert one cell value to a canonical, JSON-serialisable Python value.

    Parameters
    ----------
    value:
        A single DataFrame cell value — may be a numpy scalar, ``Decimal``,
        ``pandas.Timestamp``, plain Python scalar, or a null-like sentinel.
    float_precision:
        Number of digits to round floating-point values to.

    Returns
    -------
    Any
        One of ``None``, ``bool``, ``int``, ``float``, or ``str`` — the
        canonical forms this module hashes.

    Examples
    --------
    >>> _normalise_scalar(None, 6) is None
    True
    >>> import numpy as np
    >>> _normalise_scalar(np.int64(5), 6)
    5
    >>> _normalise_scalar(np.float64(1.0000001), 6)
    1.0
    >>> from decimal import Decimal
    >>> _normalise_scalar(Decimal("3.50"), 6)
    3.5
    >>> _normalise_scalar(-0.0, 6)
    0.0
    """
    # Null-like values first: pd.isna() handles None, NaN, NaT, pandas.NA
    # in one call, but it raises/misbehaves on some container types, so we
    # only call it on scalars (which every DataFrame cell is).
    try:
        is_null = pd.isna(value)
    except (TypeError, ValueError):
        is_null = False
    if is_null is True:
        return None

    if isinstance(value, bool):
        return value
    if hasattr(value, "item") and type(value).__module__ == "numpy":
        # numpy scalar (int64, float64, bool_, ...) -> native Python type.
        value = value.item()
        if isinstance(value, bool):
            return value

    if isinstance(value, Decimal):
        value = float(value)

    if isinstance(value, int):
        return int(value)

    if isinstance(value, float):
        if math.isnan(value):
            return None
        rounded = round(value, float_precision)
        if rounded == 0.0:
            rounded = 0.0  # normalise -0.0 -> 0.0
        return rounded

    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()

    return str(value) if not isinstance(value, str) else value


def canonical_repr(
    df: pd.DataFrame, float_precision: int = DEFAULT_FLOAT_PRECISION
) -> dict[str, Any]:
    """Build the canonical, order-insensitive structure hashed by :func:`fingerprint_dataframe`.

    Exposed separately from the hash so tests and debugging tools can
    inspect *what* differs between two frames, not just *whether* it does.

    Parameters
    ----------
    df:
        The result set to canonicalise.
    float_precision:
        Number of digits floats are rounded to (see module docstring).

    Returns
    -------
    dict
        ``{"columns": [str, ...], "row_count": int, "rows": [str, ...]}``
        where ``rows`` is a list of canonical per-row JSON strings, sorted
        lexicographically, and ``columns`` is sorted alphabetically.

    Examples
    --------
    >>> import pandas as pd
    >>> df = pd.DataFrame({"b": [2, 1], "a": [1, 2]})
    >>> repr_ = canonical_repr(df)
    >>> repr_["columns"]
    ['a', 'b']
    >>> repr_["row_count"]
    2
    """
    sorted_columns = sorted(str(c) for c in df.columns)

    # Extract values column-by-column (Series.tolist()) rather than via
    # DataFrame.iterrows(). iterrows() rebuilds each row as a fresh Series,
    # which silently upcasts nullable extension dtypes (e.g. "Int64" with a
    # pd.NA) to float64 -- turning 1 into 1.0. Column-wise extraction
    # preserves each column's native Python values faithfully.
    original_columns = list(df.columns)
    columns_as_str = [str(c) for c in original_columns]
    value_lists = {
        str(col): df[col].tolist() for col in original_columns
    }
    row_count = len(df)

    row_jsons: list[str] = []
    for i in range(row_count):
        normalised = [
            _normalise_scalar(value_lists[col][i], float_precision)
            for col in sorted_columns
        ]
        row_jsons.append(json.dumps(normalised, sort_keys=False, ensure_ascii=True))
    row_jsons.sort()

    return {
        "columns": sorted_columns,
        "row_count": len(df),
        "rows": row_jsons,
    }


def fingerprint_dataframe(
    df: pd.DataFrame, float_precision: int = DEFAULT_FLOAT_PRECISION
) -> str:
    """Return a stable SHA-256 hex digest identifying *df*'s data content.

    Two DataFrames that a human would call "the same answer" — same
    columns (regardless of order), same rows (regardless of order), same
    values (regardless of ``Decimal``/``float``/``numpy`` scalar type or
    trailing floating-point noise) — always produce the same fingerprint.
    Two DataFrames that differ in actual content practically never collide
    (SHA-256).

    Parameters
    ----------
    df:
        The result set to fingerprint. May be empty (0 rows); an empty
        frame with a given column set has a well-defined, stable
        fingerprint distinct from an empty frame with different columns.
    float_precision:
        Number of digits floats/Decimals are rounded to before hashing.
        Must match between the two fingerprints being compared — use the
        same value (or the default) consistently across a golden set.

    Returns
    -------
    str
        64-character lowercase hex SHA-256 digest.

    Raises
    ------
    TypeError
        If *df* is not a :class:`pandas.DataFrame`.

    Examples
    --------
    Column order does not matter:

    >>> import pandas as pd
    >>> a = pd.DataFrame({"name": ["x"], "count": [1]})
    >>> b = pd.DataFrame({"count": [1], "name": ["x"]})
    >>> fingerprint_dataframe(a) == fingerprint_dataframe(b)
    True

    Row order does not matter:

    >>> a = pd.DataFrame({"n": [1, 2, 3]})
    >>> b = pd.DataFrame({"n": [3, 1, 2]})
    >>> fingerprint_dataframe(a) == fingerprint_dataframe(b)
    True

    Different data does not match:

    >>> a = pd.DataFrame({"n": [1, 2, 3]})
    >>> b = pd.DataFrame({"n": [1, 2, 4]})
    >>> fingerprint_dataframe(a) == fingerprint_dataframe(b)
    False

    Empty frames with the same columns match each other:

    >>> a = pd.DataFrame({"n": pd.Series([], dtype="int64")})
    >>> b = pd.DataFrame({"n": pd.Series([], dtype="int64")})
    >>> fingerprint_dataframe(a) == fingerprint_dataframe(b)
    True
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"fingerprint_dataframe expects a DataFrame, got {type(df)!r}")

    repr_ = canonical_repr(df, float_precision=float_precision)
    payload = json.dumps(repr_, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
