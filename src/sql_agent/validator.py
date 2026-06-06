"""SQL validation and cleanup utilities.

Shared by both agents/ and runners/ to avoid code duplication.

Public API
----------
clean_sql(raw)            -> str   strip markdown, keep first SELECT/WITH block
validate_sql(sql)         -> None  raise ValueError on dangerous / malformed SQL
ensure_top(sql, n=100)    -> str   inject SELECT TOP N if missing
"""

from __future__ import annotations

import re

_FORBIDDEN = (
    "DELETE ", "UPDATE ", "DROP ", "ALTER ",
    "INSERT ", "TRUNCATE ", "EXEC ", "MERGE ",
)


# ---------------------------------------------------------------------------
# clean_sql
# ---------------------------------------------------------------------------

def clean_sql(raw: str) -> str:
    """Strip markdown fences and return only the first SELECT / WITH block.

    Raises ``ValueError`` if no SELECT or WITH statement is found.
    """
    if not raw:
        raise ValueError("Model returned an empty response")

    sql = re.sub(r"```sql", "", raw, flags=re.IGNORECASE)
    sql = re.sub(r"```",    "", sql)
    sql = sql.strip()

    upper = sql.upper()
    if upper.lstrip().startswith("WITH"):
        pass                              # CTE — keep as-is
    elif "SELECT" in upper:
        sql = sql[upper.find("SELECT"):]  # drop any preamble before SELECT
    else:
        raise ValueError("No SELECT statement found in model response")

    # drop trailing duplicate queries separated by blank line
    sql = sql.split("\n\n")[0].strip()

    # LIMIT n  →  SELECT TOP n  (MySQL / PostgreSQL leakage)
    m = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
    if m:
        n   = m.group(1)
        sql = re.sub(r"LIMIT\s+\d+", "", sql, flags=re.IGNORECASE).strip()
        sql = re.sub(r"(?i)^(SELECT)\s+", f"SELECT TOP {n} ", sql, count=1)

    # SELECT TOP N DISTINCT  →  SELECT DISTINCT TOP N  (SQL Server requirement)
    sql = re.sub(
        r"(?i)SELECT\s+TOP\s+(\d+)\s+DISTINCT",
        r"SELECT DISTINCT TOP \1",
        sql,
    )

    return sql.strip()


# ---------------------------------------------------------------------------
# validate_sql
# ---------------------------------------------------------------------------

def validate_sql(sql: str) -> None:
    """Raise ``ValueError`` if *sql* is empty, non-SELECT, or contains forbidden keywords."""
    if not sql:
        raise ValueError("SQL is empty")

    upper = sql.upper()

    if not (upper.lstrip().startswith("SELECT") or upper.lstrip().startswith("WITH")):
        raise ValueError(f"Only SELECT / CTE queries are allowed. Got: {sql[:80]}")

    if " FROM " not in upper:
        raise ValueError("SQL has no FROM clause")

    for kw in _FORBIDDEN:
        if kw in upper:
            raise ValueError(f"Forbidden keyword detected: {kw.strip()}")

    if " LIMIT " in upper:
        raise ValueError("SQL contains LIMIT (not valid in SQL Server; use TOP)")


# ---------------------------------------------------------------------------
# ensure_top
# ---------------------------------------------------------------------------

def ensure_top(sql: str, n: int = 100) -> str:
    """Inject ``SELECT TOP n`` if the query has no TOP clause."""
    if re.search(r"(?i)SELECT\s+TOP\s+\d+", sql):
        return sql
    if re.search(r"(?i)SELECT\s+DISTINCT\s+TOP\s+\d+", sql):
        return sql
    sql = re.sub(r"(?i)^SELECT\s+", f"SELECT TOP {n} ", sql, count=1)
    return sql
