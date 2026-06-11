"""Execute a validated SQL query and return a pandas DataFrame.

All database errors are wrapped in a single :class:`RuntimeError` so callers
need to handle only one exception type for DB failures.

The public API intentionally exposes **two names** for the same function:

* :func:`execute_sql`   — canonical name used throughout production code.
* :func:`execute_query` — backward-compatible alias preserved for tests that
  monkeypatch ``database.executor.execute_query``.

Typical usage::

    from database.executor import execute_sql

    df = execute_sql("SELECT TOP 10 * FROM [Auction_Fact].[Contract]")
    print(df.shape)   # (10, <n_columns>)
"""

from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import config as cfg
from database.connection import get_engine

logger = logging.getLogger(__name__)


def execute_sql(sql: str) -> pd.DataFrame:
    """Execute *sql* against Auction_DM and return the result as a DataFrame.

    Behaviour
    ---------
    * **Lock timeout** — ``SET LOCK_TIMEOUT`` is issued before the query,
      set to ``cfg.settings.query_timeout_seconds * 1000`` milliseconds.
      This prevents the query from blocking indefinitely on locked rows.
    * **Row cap** — results are fetched with
      ``fetchmany(cfg.settings.max_rows_returned)`` so the cap applies even
      when the SQL already contains a ``TOP n`` clause.  Both config values
      are read at **call time** (not at import time), so
      :func:`~config.override_settings` patches in tests are always visible.
    * **Alias** — the module-level name ``execute_query`` is an alias for
      this function (see end of module).

    Parameters
    ----------
    sql:
        A validated, sanitised T-SQL SELECT query string.  The caller is
        responsible for running
        :func:`~security.sql_guard.validate_sql` (and optionally
        :func:`~security.sql_guard.ensure_top`) before passing SQL here.

    Returns
    -------
    pandas.DataFrame
        One row per result row, one column per selected column.  Column
        names are taken from ``result.keys()`` (the cursor description).
        Returns an **empty** DataFrame with 0 rows when the query matches
        no data.

    Raises
    ------
    RuntimeError
        Wraps any :class:`~sqlalchemy.exc.SQLAlchemyError` with a
        human-readable message.  The original exception is attached as
        ``__cause__`` so tracebacks still show the root cause.

    Examples
    --------
    >>> df = execute_sql("SELECT TOP 5 * FROM [Auction_Fact].[Contract]")  # doctest: +SKIP
    >>> isinstance(df, pd.DataFrame)                                        # doctest: +SKIP
    True
    >>> len(df) <= 5                                                         # doctest: +SKIP
    True

    >>> # Empty result → empty DataFrame, no exception
    >>> df = execute_sql("SELECT * FROM [Dim].[Ring] WHERE 1=0")  # doctest: +SKIP
    >>> df.empty                                                   # doctest: +SKIP
    True

    >>> # Bad SQL → RuntimeError
    >>> execute_sql("NOT VALID SQL")  # doctest: +SKIP
    Traceback (most recent call last):
        ...
    RuntimeError: Database error: ...
    """
    engine     = get_engine()
    timeout_ms = cfg.settings.query_timeout_seconds * 1_000

    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET LOCK_TIMEOUT {int(timeout_ms)}"))
            result  = conn.execute(text(sql))
            rows    = result.fetchmany(cfg.settings.max_rows_returned)
            columns = list(result.keys())
    except SQLAlchemyError as exc:
        logger.error("SQL execution failed: %s", exc)
        raise RuntimeError(f"Database error: {exc}") from exc

    df = pd.DataFrame(rows, columns=columns)
    logger.debug("Query returned %d rows, %d columns", len(df), len(df.columns))
    return df


# Backward-compatible alias used by tests that monkeypatch
# ``database.executor.execute_query``.
execute_query = execute_sql
