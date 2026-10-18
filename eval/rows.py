# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A result set as JSON-safe rows, for a golden case's ``expected_rows``.

Kept apart from :mod:`eval.verify` and :mod:`eval.runner` because those read
``project_config/`` when imported, and the benchmark importer
(:mod:`eval.benchmarks.bird`) records rows without one.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pandas as pd

__all__ = ["frame_to_rows", "jsonable"]


def jsonable(value: Any) -> Any:
    """One cell as a JSON value that replays to the same fingerprint.

    Mirrors :func:`eval.fingerprint._normalise_scalar` (so a frame rebuilt
    from the stored rows fingerprints like the live one) but keeps floats
    unrounded.

    Examples
    --------
    >>> jsonable(Decimal("2.50")), jsonable(None), jsonable(float("nan"))
    (2.5, None, None)
    >>> jsonable(datetime(2024, 1, 2, 3, 4)), jsonable(date(2024, 1, 2))
    ('2024-01-02T03:04:00', '2024-01-02')
    >>> import numpy as np
    >>> jsonable(np.int64(4)), type(jsonable(np.int64(4))).__name__
    (4, 'int')
    """
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and type(value).__module__ == "numpy":
        value = value.item()
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def frame_to_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    """The rows of *df* as ``[{column: value}]`` suitable for ``expected_rows``.

    Parameters
    ----------
    df:
        A result set.

    Returns
    -------
    list[dict[str, Any]]
        JSON-safe rows; replaying them with ``pd.DataFrame(rows)`` yields a
        frame with the same fingerprint as *df* (for results without
        duplicate column names).

    Examples
    --------
    >>> df = pd.DataFrame({"n": [1, 2], "d": [Decimal("1.5"), None]})
    >>> frame_to_rows(df)
    [{'n': 1, 'd': 1.5}, {'n': 2, 'd': None}]
    """
    columns = [str(c) for c in df.columns]
    return [
        {name: jsonable(value) for name, value in zip(columns, row)}
        for row in df.astype(object).itertuples(index=False, name=None)
    ]
