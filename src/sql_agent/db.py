"""Shared SQLAlchemy engine factory.

Agents and runners import `get_engine()` instead of each building their own.

Example::

    from sql_agent.db import get_engine, execute_sql

    columns, rows = execute_sql("SELECT TOP 5 * FROM [dbo].[Users]")
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .config import Settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def get_engine(uri: str) -> Engine:
    """Return a cached SQLAlchemy engine for *uri*.

    The result is cached per URI so repeated calls from the same process
    reuse the connection pool rather than creating a new one.
    """
    logger.debug("Creating engine for URI: %s", uri.split("@")[0] + "@...")
    return create_engine(
        uri,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=3600,
        fast_executemany=True,
        echo=False,
    )


def get_engine_from_settings(settings: Settings | None = None) -> Engine:
    """Convenience wrapper: build Settings if not provided, then get engine."""
    if settings is None:
        settings = Settings()
    return get_engine(settings.sqlserver_uri())


def execute_sql(sql: str, settings: Settings | None = None) -> tuple[list[str], list[Any]]:
    """Execute *sql* and return (columns, rows).

    Sets a 30-second lock timeout to avoid long blocking queries.
    """
    engine = get_engine_from_settings(settings)
    with engine.connect() as conn:
        conn.execute(text("SET LOCK_TIMEOUT 30000"))
        result  = conn.execute(text(sql))
        rows    = result.fetchall()
        columns = list(result.keys())
    logger.info("Query returned %d rows.", len(rows))
    return columns, rows
