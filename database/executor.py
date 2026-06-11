"""Execute a validated SQL query and return a pandas DataFrame.

All database errors are wrapped in a single ``RuntimeError`` so callers
only need to handle one exception type for DB failures.
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
    """Run *sql* against Auction_DM and return results as a ``DataFrame``.

    - Sets ``LOCK_TIMEOUT`` to avoid long waits on locked rows.
    - Caps result set at ``cfg.settings.max_rows_returned`` (read lazily
      so that ``override_settings()`` patches are visible).

    Raises
    ------
    RuntimeError
        Wraps any ``SQLAlchemyError`` with a clean message.
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
# ``database.executor.execute_query``
execute_query = execute_sql
