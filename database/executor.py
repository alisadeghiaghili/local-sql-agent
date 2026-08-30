"""Execute a validated SQL query and return a pandas DataFrame.

All errors raised while acquiring the engine, opening a connection, or
executing SQL against the database are wrapped in a single
:class:`RuntimeError` so callers need to handle only one exception type for
DB failures. (Scope note: this covers *database* errors specifically — a
bug in, say, the pandas DataFrame construction after a successful query
would still propagate as whatever pandas raises; the guarantee is about the
database round trip, not about this function never raising anything else.)

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
from sqlalchemy.exc import SQLAlchemyError

import config as cfg
from database.connection import get_engine

logger = logging.getLogger(__name__)


def execute_sql(sql: str) -> pd.DataFrame:
    """Execute *sql* against Auction_DM and return the result as a DataFrame.

    Behaviour
    ---------
    * **Raw driver execution** — the query is passed to
      :meth:`~sqlalchemy.engine.Connection.exec_driver_sql`, not
      ``conn.execute(text(sql))``. SQLAlchemy's :func:`~sqlalchemy.text`
      construct parses ``:name`` in the SQL string as a bind parameter
      placeholder; a generated query containing a literal like
      ``N' :label'`` would be misinterpreted as an unbound parameter and
      fail (or, worse, be silently reinterpreted) instead of executing as
      written. ``exec_driver_sql`` sends the string to the driver
      unmodified.
    * **Driver-level query timeout** — the underlying pyodbc connection's
      ``timeout`` attribute is set to
      ``cfg.settings.query_timeout_seconds`` before the query runs. This
      bounds the *entire* execution time of the query, including a heavy
      table scan that never waits on a lock — unlike ``SET LOCK_TIMEOUT``
      below, which only bounds time spent waiting to *acquire* a lock and
      does nothing for a query that is simply slow to run.
    * **Lock timeout** — ``SET LOCK_TIMEOUT`` is additionally issued before
      the query, set to ``cfg.settings.query_timeout_seconds * 1000``
      milliseconds, so a query blocked on someone else's lock fails fast
      rather than waiting for the driver-level timeout above.
    * **Read-only transaction** — the query runs inside an explicit
      transaction that is **always rolled back**, on both success and
      failure, and never committed. T-SQL has no ``SET TRANSACTION READ
      ONLY`` mode (unlike, e.g., PostgreSQL); this is the practical
      equivalent for a connection that should only ever run ``SELECT``:
      even a write that somehow slipped past
      :func:`~security.sql_guard.validate_sql` is never persisted.
    * **Streamed results** — the connection is put in
      ``stream_results=True`` execution mode so the driver does not
      buffer the entire result set before the row cap below is applied;
      only ``cfg.settings.max_rows_returned`` rows are ever materialised
      client-side.
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
        Wraps any :class:`~sqlalchemy.exc.SQLAlchemyError` raised while
        acquiring the engine, connecting, or executing *sql*, with a
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
    timeout_seconds = cfg.settings.query_timeout_seconds
    timeout_ms = timeout_seconds * 1_000

    try:
        engine = get_engine()
        with engine.connect() as conn:
            # Connection.execution_options() mutates and returns the same
            # Connection, so this rebinding does not change what __exit__
            # closes -- see database.connection.get_engine's docstring for
            # why this workload is SELECT-only and streaming is safe here.
            conn = conn.execution_options(stream_results=True)

            raw_conn = conn.connection.dbapi_connection
            if raw_conn is not None:
                raw_conn.timeout = timeout_seconds

            conn.exec_driver_sql(f"SET LOCK_TIMEOUT {int(timeout_ms)}")

            transaction = conn.begin()
            try:
                result = conn.exec_driver_sql(sql)
                rows = result.fetchmany(cfg.settings.max_rows_returned)
                columns = list(result.keys())
            finally:
                # Always roll back, never commit -- see the "Read-only
                # transaction" behaviour note above.
                transaction.rollback()
    except SQLAlchemyError as exc:
        logger.error("SQL execution failed: %s", exc)
        raise RuntimeError(f"Database error: {exc}") from exc

    df = pd.DataFrame(rows, columns=columns)
    logger.debug("Query returned %d rows, %d columns", len(df), len(df.columns))
    return df


# Backward-compatible alias used by tests that monkeypatch
# ``database.executor.execute_query``.
execute_query = execute_sql
