"""SQLAlchemy engine factory for Auction_DM.

Usage::

    from database.connection import get_engine
    engine = get_engine()
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from config import settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a cached, pre-pinged SQLAlchemy engine."""
    engine = create_engine(
        settings.db_connection_url,
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

    Safe to call even if ``get_engine()`` has never been called.
    The next call to ``get_engine()`` will create a fresh engine.

    Implementation note
    -------------------
    ``get_engine.__wrapped__`` returns the *original unwrapped function*,
    not the cached result, so calling it would create a **new** engine
    instead of disposing the cached one.  We use ``cache_info()`` to
    check whether the cache is populated before extracting the value.
    """
    if get_engine.cache_info().currsize > 0:
        # The only way to retrieve the cached object without bypassing
        # the cache is to call the cached function itself.
        engine = get_engine()
        engine.dispose()
    get_engine.cache_clear()
