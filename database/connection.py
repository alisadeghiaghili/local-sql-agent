"""SQLAlchemy engine factory for Auction_DM."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from config import DB_CONNECTION_URL


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a cached SQLAlchemy engine."""
    return create_engine(
        DB_CONNECTION_URL,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=10,
        max_overflow=20,
        fast_executemany=True,
        echo=False,
    )
