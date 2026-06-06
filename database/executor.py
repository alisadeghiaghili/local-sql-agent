"""Execute a validated SQL query and return a pandas DataFrame."""

from __future__ import annotations

import pandas as pd
from sqlalchemy import text

from config import MAX_ROWS_RETURNED, QUERY_TIMEOUT_SECONDS
from database.connection import get_engine


def execute_sql(sql: str) -> pd.DataFrame:
    """Run *sql* against Auction_DM and return results as a DataFrame.

    - Sets ``LOCK_TIMEOUT`` to avoid long blocking.
    - Caps results at ``MAX_ROWS_RETURNED``.
    """
    engine = get_engine()
    timeout_ms = QUERY_TIMEOUT_SECONDS * 1000

    with engine.connect() as conn:
        conn.execute(text(f"SET LOCK_TIMEOUT {timeout_ms}"))
        result  = conn.execute(text(sql))
        rows    = result.fetchmany(MAX_ROWS_RETURNED)
        columns = list(result.keys())

    return pd.DataFrame(rows, columns=columns)
