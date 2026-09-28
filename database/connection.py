# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""SQLAlchemy engine factory for the warehouse data sources.

Provides one cached :class:`~sqlalchemy.engine.Engine` per data source
(see :mod:`database.datasources`), shared across all database consumers in
the process. Connection pool settings are tuned for a multi-threaded
FastAPI server handling up to 20 concurrent queries per source.

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
from sqlalchemy.engine import Engine

import config as cfg
from database.connection_identity import with_application_name
from database.datasources import default_datasource_name, get_datasource
from database.pool_ping import install_idle_aware_ping


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
    ``pool_pre_ping`` — idle-aware, via :func:`database.pool_ping.install_idle_aware_ping`
        When :attr:`config.Settings.db_pool_pre_ping` is ``True`` (the
        default), the engine is built with SQLAlchemy's own
        ``pool_pre_ping`` argument left off and
        :func:`~database.pool_ping.install_idle_aware_ping` is installed
        instead: a connection is only probed with ``SELECT 1`` on
        checkout once it has sat idle in the pool for at least
        :attr:`config.Settings.db_pool_ping_idle_seconds` (default 60;
        ``0`` pings literally every checkout, matching plain
        ``pool_pre_ping=True`` except for one narrow case documented on
        :func:`~database.pool_ping.install_idle_aware_ping`). A failed
        probe raises ``sqlalchemy.exc.InvalidatePoolError`` -- the same
        exception plain ``pool_pre_ping=True``'s own dialect-level ping
        raises on failure -- which makes the pool invalidate and
        transparently replace every pooled connection, not just the one
        that was probed, before the caller's own statement runs. When
        :attr:`~config.Settings.db_pool_pre_ping`
        is ``False``, ``install_idle_aware_ping`` is never called and no
        connection is ever probed on checkout. The steady per-checkout
        probe cost this trades for that safety — and when turning it off
        is a reasonable choice instead — is documented on
        :attr:`config.Settings.db_pool_pre_ping` (Finding 1, 2026
        warehouse-load audit, revised by the idle-aware-ping follow-up).
    ``pool_recycle=cfg.settings.db_pool_recycle_seconds`` (default 3600)
        Recycles connections older than 1 hour to prevent stale ODBC
        handles (common with SQL Server + pyodbc on Linux).
    ``pool_size=10``
        Persistent connections kept alive in the pool.  Sized for the
        expected number of concurrent FastAPI worker threads.
    ``max_overflow=20``
        Extra connections allowed under peak load above ``pool_size``.
        These connections are closed when the burst subsides.

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
    engine = create_engine(
        connection_url,
        pool_recycle=cfg.settings.db_pool_recycle_seconds,
        pool_size=10,
        max_overflow=20,
        echo=False,
    )
    if cfg.settings.db_pool_pre_ping:
        install_idle_aware_ping(engine, cfg.settings.db_pool_ping_idle_seconds)
    return engine


def dispose_engine() -> None:
    """Dispose every cached engine and clear the cache.

    Closes **all** connections in every source's pool (both idle and checked-out once
    they are returned) and removes the cached engine object so the next call
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
