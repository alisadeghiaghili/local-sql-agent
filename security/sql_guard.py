"""SQL validation layer — blocks destructive and out-of-scope queries."""

from __future__ import annotations

import re

_FORBIDDEN: tuple[str, ...] = (
    "DELETE ", "UPDATE ", "INSERT ", "DROP ",
    "ALTER ", "TRUNCATE ", "MERGE ", "EXEC ",
)


def validate_sql(sql: str) -> None:
    """Raise ``ValueError`` if *sql* is not a safe, read-only query.

    Rules
    -----
    - Must start with SELECT or WITH (CTE).
    - Must not contain any forbidden DML/DDL keyword.
    - Must not reference system tables (INFORMATION_SCHEMA, SYS.).

    Raises
    ------
    ValueError
        With a human-readable message describing what was blocked.
    """
    if not sql or not sql.strip():
        raise ValueError("Empty SQL")

    upper = sql.upper().strip()

    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError("Only SELECT / CTE queries are allowed")

    for kw in _FORBIDDEN:
        if kw in upper:
            raise ValueError(f"Forbidden keyword: {kw.strip()}")

    if "INFORMATION_SCHEMA" in upper:
        raise ValueError("System tables are forbidden: INFORMATION_SCHEMA")

    if re.search(r"\bSYS\.", upper):
        raise ValueError("System tables are forbidden: SYS.")
