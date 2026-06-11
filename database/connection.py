"""SQLAlchemy engine factory for Auction_DM.

Provides a single cached :class:`~sqlalchemy.engine.Engine` instance shared
across all database consumers in the process.  Connection pool settings are
tuned for a multi-threaded FastAPI server handling up to 20 concurrent queries.

The engine is created lazily on the **first** call to :func:`get_engine` and
cached via :func:`functools.lru_cache` (``maxsize=1``).  Subsequent calls
always return the same object without re-reading ``cfg.settings``.

Typical usage::

    from database.connection import get_engine

    with get_engine().connect() as conn:
        result = conn.execute(text("SELECT TOP 1 1"))
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

import config as cfg


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the singleton SQLAlchemy engine for Auction_DM.

    The engine is created on the first call and reused on every subsequent
    call (``lru_cache`` with ``maxsize=1``).

    Pool configuration
    ------------------
    ``pool_pre_ping=True``
        Verifies connections before use.  Provides transparent recovery
        after database restarts without manual pool recycling.
    ``pool_recycle=3600``
        Recycles connections older than 1 hour to prevent stale ODBC
        handles (common with SQL Server + pyodbc on Linux).
    ``pool_size=10``
        Persistent connections kept alive in the pool.  Sized for the
        expected number of concurrent FastAPI worker threads.
    ``max_overflow=20``
        Extra connections allowed under peak load above ``pool_size``.
        These connections are closed when the burst subsides.
    ``fast_executemany=True``
        pyodbc batch-insert optimisation.  Has no effect on SELECT queries
        but is included for any future write operations during migrations.

    Returns
    -------
    sqlalchemy.engine.Engine
        A ready-to-use, pre-pinged SQLAlchemy engine connected to the
        database specified by ``cfg.settings.db_connection_url``.

    Examples
    --------
    >>> from sqlalchemy import text
    >>> engine = get_engine()                             # doctest: +SKIP
    >>> with engine.connect() as conn:                    # doctest: +SKIP
    ...     conn.execute(text("SELECT 1"))                # doctest: +SKIP
    <sqlalchemy.engine.cursor.CursorResult ...>           # doctest: +SKIP
    """
    engine = create_engine(
        cfg.settings.db_connection_url,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=10,
        max_overflow=20,
        fast_executemany=True,
        echo=False,
    )
    return engine


def dispose_engine() -> None:
    """Dispose the cached engine and clear the LRU cache.

    Closes **all** connections in the pool (both idle and checked-out once
    they are returned) and removes the cached engine object so the next call
    to :func:`get_engine` creates a completely fresh instance.

    Common use cases
    ----------------
    1. **Test teardown** — after tests that alter connection state, calling
       ``dispose_engine()`` guarantees the next test gets a clean pool.
    2. **Hot-reload** — if ``DB_CONNECTION_URL`` changes at runtime (e.g.
       via :func:`~config.override_settings`), ``dispose_engine()`` forces
       reconnection with the new URL on the next :func:`get_engine` call.
    3. **Graceful shutdown** — called from the FastAPI ``lifespan`` handler
       to release all DB connections before the process exits.

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
    if get_engine.cache_info().currsize > 0:
        get_engine().dispose()
    get_engine.cache_clear()
