# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""SQLAlchemy engine factory for the warehouse data sources.

Provides one cached :class:`~sqlalchemy.engine.Engine` per data source
(see :mod:`database.datasources`), shared across all database consumers in
the process. Engines do not pool connections (:class:`~sqlalchemy.pool.NullPool`):
every ``connect()`` opens a fresh DBAPI connection and closing it closes
the socket, so no idle session outlives the query that needed it. See
:func:`_build_engine` for why.

Each engine is created lazily on the **first** :func:`get_engine` call for
its source and cached (:func:`functools.lru_cache` keyed by source name).
Subsequent calls return the same object without re-reading ``cfg.settings``.
``get_engine()`` with no argument returns the default source's engine,
which in a deployment without ``datasources.yaml`` is the single
``DB_CONNECTION_URL`` engine every earlier release used.

Typical usage::

    from database.connection import get_engine

    with get_engine().connect() as conn:             # default source
        result = conn.execute(text("SELECT TOP 1 1"))

    with get_engine("archive").connect() as conn:    # a named source
        ...
"""

from __future__ import annotations

import threading

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

from database.connection_identity import with_application_name
from database.datasources import default_datasource_name, get_datasource


def get_engine(datasource: str | None = None) -> Engine:
    """Return the cached SQLAlchemy engine for *datasource*.

    Parameters
    ----------
    datasource:
        Data source name (see :mod:`database.datasources`). ``None`` means
        the default source. The name is resolved before the cache lookup,
        so ``get_engine()`` and ``get_engine(<default name>)`` share one
        engine and one pool.

    Raises
    ------
    database.datasources.UnknownDataSourceError
        If *datasource* is not configured.

    See :func:`_build_engine` for the pool configuration every engine gets.
    """
    name = datasource or default_datasource_name()
    engine = _engines.get(name)
    if engine is None:
        with _engines_lock:
            engine = _engines.get(name)
            if engine is None:
                engine = _build_engine(name)
                _engines[name] = engine
    return engine


#: Built engines by resolved source name. A plain dict rather than
#: ``functools.lru_cache`` because :func:`dispose_engine` has to enumerate
#: what has been built without building anything.
_engines: dict[str, Engine] = {}
_engines_lock = threading.Lock()


def _build_engine(datasource: str) -> Engine:
    """Build the engine for the resolved source name *datasource*.

    Called once per source by :func:`get_engine`; every later call for the
    same name returns the cached engine.

    Pool configuration
    ------------------
    ``poolclass=NullPool``
        No connection is kept between queries. ``engine.connect()`` opens a
        new DBAPI connection and closing it (the ``with`` block exit in
        :func:`database.executor.execute_sql`) closes the session on the
        server. The engine previously used SQLAlchemy's default
        ``QueuePool`` with ``pool_size=10, max_overflow=20``: up to ten
        connections per source *per worker process* stayed open and idle
        indefinitely, because ``QueuePool`` never closes a checked-in
        connection and ``pool_recycle`` only acts on checkout. DBAs saw
        those as sleeping sessions that never went away. For this workload
        (a handful of queries per question, each preceded by seconds of
        LLM time) the tens of milliseconds a fresh login costs are
        negligible, so the pool bought nothing worth that footprint.

        Without a pool, ``pool_recycle`` and the idle-aware pre-ping
        (:mod:`database.pool_ping`) have nothing to act on: a connection is
        never reused, so it can never be stale. Neither is installed here.
        :attr:`config.Settings.db_pool_pre_ping` still governs the
        application-database engine (:func:`appdb.engine.build_engine`).
        Concurrency against the warehouse is bounded upstream by
        :mod:`api.concurrency`, not by a pool limit.

    ODBC driver-manager pooling
        ``pyodbc`` enables ODBC connection pooling by default, which makes
        the driver manager keep a "closed" connection open for its own
        reuse (60 s by default on Windows). That would defeat ``NullPool``,
        so for an ``mssql+pyodbc`` source :func:`_disable_odbc_pooling`
        turns it off before the first connection is made.

    Application identification (Finding 5, 2026 warehouse-load audit)
    -------------------------------------------------------------------
    Before the engine is built, :func:`database.connection_identity.with_application_name`
    is applied to the source's connection string — if that URL
    is ``mssql+pyodbc`` and does not already set an application name, one
    is added (the source's ``application_name``, else
    :attr:`config.Settings.db_application_name`, default
    ``"local-sql-agent"``) so a DBA can attribute this application's
    sessions in their own traces. A no-op for any other backend/driver,
    and never overrides a name the URL already sets — see that module's
    own docstring.

    This workload is SELECT-only (every query passes through
    :func:`~security.sql_guard.validate_sql` first, and
    :func:`database.executor.execute_sql` additionally runs every query
    inside a transaction it always rolls back). ``fast_executemany`` — a
    pyodbc *batch-insert* optimisation for ``executemany()`` calls — was
    previously enabled here "for any future write operations"; it has no
    effect on this workload and is not set.

    Returns
    -------
    sqlalchemy.engine.Engine
        A ready-to-use SQLAlchemy engine connected to the source's
        database (``DB_CONNECTION_URL`` for the single-source fallback).

    Examples
    --------
    >>> from sqlalchemy import text
    >>> engine = get_engine()                             # doctest: +SKIP
    >>> with engine.connect() as conn:                    # doctest: +SKIP
    ...     conn.execute(text("SELECT 1"))                # doctest: +SKIP
    <sqlalchemy.engine.cursor.CursorResult ...>           # doctest: +SKIP
    """
    source = get_datasource(datasource)
    connection_url = with_application_name(source.url, source.application_name)
    if make_url(connection_url).drivername == "mssql+pyodbc":
        _disable_odbc_pooling()
    return create_engine(connection_url, poolclass=NullPool, echo=False)


def _disable_odbc_pooling() -> None:
    """Turn off pyodbc's ODBC driver-manager connection pooling.

    ``pyodbc.pooling`` is process-wide and is read when pyodbc allocates
    its ODBC environment, i.e. on the first connection; setting it after
    that has no effect. It is therefore set here, while building an
    ``mssql+pyodbc`` engine and before that engine can connect. Every
    ``mssql+pyodbc`` engine in this process then gets the same behaviour,
    which is intended: no layer below SQLAlchemy keeps idle sessions open.

    A missing ``pyodbc`` is not an error here; ``create_engine`` raises the
    real import error with its own message when the dialect loads.

    Returns
    -------
    None

    Examples
    --------
    >>> _disable_odbc_pooling()                  # doctest: +SKIP
    >>> import pyodbc; pyodbc.pooling            # doctest: +SKIP
    False
    """
    try:
        import pyodbc
    except ImportError:
        return
    pyodbc.pooling = False


def dispose_engine() -> None:
    """Dispose every cached engine and clear the cache.

    Engines hold no idle connections (see :func:`_build_engine`), so this
    mostly releases the engine objects; it still closes anything a dialect
    keeps per engine. It removes the cached engine object so the next call
    to :func:`get_engine` creates a completely fresh instance.

    Common use cases
    ----------------
    1. **Test teardown** — after tests that alter connection state, calling
       ``dispose_engine()`` guarantees the next test gets a clean pool.
    2. **Hot-reload** — if a data source's connection string changes at
       runtime (e.g. ``DB_CONNECTION_URL`` via
       :func:`~config.override_settings`, in the single-source case),
       ``dispose_engine()`` forces reconnection with the new URL on the
       next :func:`get_engine` call.
    3. **Graceful shutdown** — called from the FastAPI ``lifespan`` handler
       to release every source's DB connections before the process exits.

    This function is safe to call even if :func:`get_engine` has never been
    called (the cache is empty and nothing is disposed).

    Returns
    -------
    None

    Examples
    --------
    >>> dispose_engine()        # no-op when cache is empty
    >>> engine1 = get_engine()  # creates a fresh engine
    >>> dispose_engine()        # disposes engine1's pool
    >>> engine2 = get_engine()  # creates another fresh engine
    >>> engine1 is engine2      # doctest: +SKIP
    False
    """
    with _engines_lock:
        engines = list(_engines.values())
        _engines.clear()
    for engine in engines:
        engine.dispose()


def _clear_engine_cache() -> None:
    """Forget every cached engine without disposing it (for tests)."""
    with _engines_lock:
        _engines.clear()


#: ``get_engine`` used to be an ``lru_cache``-wrapped function, and the test
#: suite's root guard (``conftest._no_real_database``) and many tests call
#: ``get_engine.cache_clear()``. Kept as the same operation.
get_engine.cache_clear = _clear_engine_cache  # type: ignore[attr-defined]
