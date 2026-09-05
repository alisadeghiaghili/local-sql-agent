# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
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
from typing import Sequence

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError

import config as cfg
from database.connection import get_engine
from security.dialects import get_dialect_profile

logger = logging.getLogger(__name__)

#: Dialects whose session-setup statement has already been confirmed by
#: this process to fail against the connected server (see the ``except``
#: branch in :func:`_apply_session_setup`) -- logged once per dialect, not
#: once per query, so a misconfigured/unsupported timeout statement does
#: not spam the log for every single request.
_session_setup_warned: set[str] = set()


def _apply_session_setup(conn, dialect: str, timeout_seconds: int) -> None:
    """Best-effort, dialect-aware session-level query-timeout setup.

    Replaces the previous hardcoded ``SET LOCK_TIMEOUT`` (T-SQL-only)
    call: the statement (if any) that bounds how long *this* dialect's
    session will wait/run before ``query_timeout_seconds`` kicks in comes
    from :attr:`~security.dialects.DialectProfile.session_timeout_statement`
    — config-keyed per dialect (T-SQL's ``SET LOCK_TIMEOUT``, PostgreSQL's
    ``SET statement_timeout``, MySQL's ``SET SESSION MAX_EXECUTION_TIME``),
    never a hardcoded per-dialect branch here.

    *dialect* is :data:`config.Settings.sql_dialect` — the deployment's own
    declared target, resolved once at start-up (see that setting's
    docstring) and validated by :func:`security.dialects.require_dialect_supported`
    — not re-derived from the live SQLAlchemy engine/connection, so this
    function (and every test exercising it) needs no real or mocked engine
    dialect metadata to behave correctly.

    SQLite has no session-level query-timeout mechanism at all
    (:attr:`~security.dialects.DialectProfile.session_timeout_statement`
    is ``None`` for it) — :func:`security.dialects.require_dialect_supported`
    already logs this loudly once at start-up (see its own docstring), so
    this function silently no-ops for that case rather than repeating the
    warning on every single query.
    """
    profile = get_dialect_profile(dialect)
    if profile.session_timeout_statement is None:
        return
    stmt = profile.session_timeout_statement.format(timeout_ms=int(timeout_seconds * 1_000))
    try:
        conn.exec_driver_sql(stmt)
    except SQLAlchemyError as exc:
        # A session-timeout statement that fails against the connected
        # server (unsupported server version, insufficient privilege, ...)
        # must not take the whole query down with it -- query_timeout_seconds
        # is a defence-in-depth backstop, not the only protection this
        # module applies (see the driver-level timeout below, and
        # max_rows_returned's fetchmany cap) -- but it must not fail
        # silently either, so it is logged once per dialect per process.
        if dialect not in _session_setup_warned:
            _session_setup_warned.add(dialect)
            logger.warning(
                "Session-level query-timeout statement failed for dialect "
                "%r (%r) -- query_timeout_seconds is not enforced at the "
                "session level for this connection: %s",
                dialect, stmt, exc,
            )


def _execute(sql: str, params: Sequence[object] | None) -> pd.DataFrame:
    """Shared implementation behind :func:`execute_sql` and
    :func:`execute_sql_params` — see :func:`execute_sql`'s docstring for the
    full behaviour contract (timeout, lock timeout, rollback-only
    transaction, streaming, row cap). The only difference between the two
    public entry points is whether *params* is ``None`` (plain
    :meth:`~sqlalchemy.engine.Connection.exec_driver_sql` call) or a bound
    parameter sequence passed straight through to the DBAPI driver.
    """
    timeout_seconds = cfg.settings.query_timeout_seconds
    dialect = cfg.settings.sql_dialect
    profile = get_dialect_profile(dialect)

    try:
        engine = get_engine()
        with engine.connect() as conn:
            # Connection.execution_options() mutates and returns the same
            # Connection, so this rebinding does not change what __exit__
            # closes -- see database.connection.get_engine's docstring for
            # why this workload is SELECT-only and streaming is safe here.
            conn = conn.execution_options(stream_results=True)

            raw_conn = conn.connection.dbapi_connection
            if raw_conn is not None and profile.driver_level_timeout_attr:
                # Driver-level timeout attribute, e.g. pyodbc's
                # Connection.timeout for tsql -- only set for a dialect
                # whose driver is confirmed to expose one (see
                # DialectProfile.driver_level_timeout_attr's docstring);
                # a dialect with none configured is left alone rather than
                # setting an attribute that would silently do nothing.
                setattr(raw_conn, profile.driver_level_timeout_attr, timeout_seconds)

            # begin() FIRST, then the session setup inside it.
            #
            # SQLAlchemy 2.0 autobegins a transaction on the connection's
            # first statement. Issuing the session-timeout statement
            # before begin() therefore left an implicit transaction
            # already open, and the explicit begin() below raised:
            #
            #   InvalidRequestError: This connection has already
            #   initialized a SQLAlchemy Transaction() object via begin()
            #   or autobegin; can't call begin() here unless rollback()
            #   or commit() is called first.
            #
            # -- on every query, against any dialect that has a session
            # timeout statement configured (every one except SQLite).
            #
            # Nothing in the suite caught it because every unit test here
            # substitutes the engine, and the integration tests that use a
            # real one are gated behind RUN_LIVE_DB_TESTS. The autobegin
            # is real SQLAlchemy behaviour, so only a real Connection
            # exhibits it -- see tests/test_executor.py's
            # TestRealConnectionTransaction, which drives a real SQLite
            # engine for exactly this reason.
            #
            # Running the setup inside the transaction is also the better
            # behaviour on its own terms: on PostgreSQL a plain SET is
            # transactional, so the rollback below returns the pooled
            # connection to its default timeout instead of leaking this
            # query's value onto whoever borrows it next.
            transaction = conn.begin()
            try:
                _apply_session_setup(conn, dialect, timeout_seconds)

                if params is None:
                    result = conn.exec_driver_sql(sql)
                else:
                    result = conn.exec_driver_sql(sql, params)
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
    return _execute(sql, None)


def execute_sql_params(sql: str, params: Sequence[object]) -> pd.DataFrame:
    """Execute a **parameterised** *sql* string against Auction_DM.

    The one, parameterised sibling of :func:`execute_sql` — added so a
    caller that must build a query from user-supplied text (the Phase 5b
    value resolver, ``retrieval.value_resolver.resolve_value``) never has
    to interpolate that text into the SQL string itself. Every protection
    :func:`execute_sql` provides (driver-level timeout, ``SET
    LOCK_TIMEOUT``, a transaction that is always rolled back and never
    committed, streamed/row-capped results) applies identically here — this
    is the *same* execution path, not a second, weaker one; see
    :func:`execute_sql`'s docstring for the full behaviour contract.

    Parameters
    ----------
    sql:
        A T-SQL string containing ``?`` placeholders (qmark style, what
        pyodbc expects) for every bound value. Must still be a query this
        codebase's own callers built from a **fixed template** — this
        function does no validation of *sql* itself (unlike
        :func:`execute_sql`'s callers, which are expected to have already
        run :func:`~security.sql_guard.validate_sql`); the caller's
        obligation here is instead to never format *sql* with untrusted
        text in the first place.
    params:
        Positional bind values, passed straight through to the DBAPI
        driver's ``execute(sql, parameters)`` — never interpolated into
        *sql* by this function or by anything it calls.

    Returns
    -------
    pandas.DataFrame
        Same shape/behaviour as :func:`execute_sql`'s return value.

    Raises
    ------
    RuntimeError
        Same wrapping behaviour as :func:`execute_sql`.

    Examples
    --------
    >>> df = execute_sql_params(
    ...     "SELECT DISTINCT TOP (?) [Name] FROM [Auction_Dim].[Customer] "
    ...     "WHERE [Name] LIKE ?",
    ...     (10, "%foo%"),
    ... )  # doctest: +SKIP
    >>> isinstance(df, pd.DataFrame)  # doctest: +SKIP
    True
    """
    return _execute(sql, params)


# Backward-compatible alias used by tests that monkeypatch
# ``database.executor.execute_query``.
execute_query = execute_sql
