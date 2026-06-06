"""SQLAlchemy engine factory for Auction_DM.

Usage::

    from database.connection import get_engine
    engine = get_engine()
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine, event
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
    """Dispose the cached engine and clear the cache (useful for tests / reload)."""
    engine = get_engine.__wrapped__()   # type: ignore[attr-defined]
    if engine:
        engine.dispose()
    get_engine.cache_clear()
