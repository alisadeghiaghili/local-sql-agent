"""SQL safety layer.

Three public functions:

``clean_sql(raw)``
    Strip LLM artefacts (markdown fences, preamble text, LIMIT → TOP)
    and return bare SQL.  Raises ``ValueError`` if no SELECT is found.

``validate_sql(sql)``
    Block destructive / out-of-scope queries.  Raises ``ValueError``
    with a human-readable reason.

``ensure_top(sql, n)``
    Inject ``TOP n`` when the query has no row-limit clause.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FORBIDDEN: tuple[str, ...] = (
    "DELETE ", "UPDATE ", "INSERT ", "DROP ",
    "ALTER ", "TRUNCATE ", "MERGE ", "EXEC ",
    "EXECUTE ", "XP_", "SP_",
)

_LIMIT_RE        = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)
_TOP_RE          = re.compile(r"\bTOP\s+\d+", re.IGNORECASE)
_FENCE_RE        = re.compile(r"```(?:sql)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_SELECT_START_RE = re.compile(r"(SELECT|WITH)\b", re.IGNORECASE)
_TOP_DISTINCT_RE = re.compile(
    r"SELECT\s+TOP\s+(\d+)\s+DISTINCT", re.IGNORECASE
)
_LIMIT_STRIP_RE  = re.compile(r"\s*\bLIMIT\s+\d+\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def clean_sql(raw: str) -> str:
    """Strip LLM artefacts and return bare SQL."""
    if not raw or not raw.strip():
        raise ValueError("Received empty SQL from model")

    fence_match = _FENCE_RE.search(raw)
    sql = fence_match.group(1) if fence_match else raw

    start = _SELECT_START_RE.search(sql)
    if not start:
        raise ValueError(f"No SELECT / CTE found in model response: {sql[:200]!r}")
    sql = sql[start.start():].strip()

    if _LIMIT_RE.search(sql):
        if _TOP_RE.search(sql):
            sql = _LIMIT_STRIP_RE.sub("", sql)
        else:
            limit_n = _LIMIT_RE.search(sql).group(1)   # type: ignore[union-attr]
            sql = _LIMIT_STRIP_RE.sub("", sql)
            sql = re.sub(
                r"\bSELECT\b", f"SELECT TOP {limit_n}",
                sql, count=1, flags=re.IGNORECASE,
            )

    sql = _TOP_DISTINCT_RE.sub(
        lambda m: f"SELECT DISTINCT TOP {m.group(1)}", sql
    )

    return sql.strip()


def validate_sql(sql: str) -> None:
    """Raise ``ValueError`` if *sql* is not a safe, read-only SELECT query.

    Rules (checked in this order)
    ------------------------------
    1. Must not be empty.
    2. Must not contain forbidden DML/DDL keywords  ← checked BEFORE SELECT guard
       so that ``DELETE ...`` raises "Forbidden keyword" not "Only SELECT".
    3. Must start with SELECT or WITH.
    4. Must not reference system catalogues.
    5. Must not use LIMIT.
    """
    if not sql or not sql.strip():
        raise ValueError("Empty SQL")

    upper = sql.upper().strip()

    # 2. Forbidden keywords — checked first so DELETE/UPDATE etc. get the
    #    correct error message even though they don't start with SELECT.
    for kw in _FORBIDDEN:
        if kw in upper:
            raise ValueError(f"Forbidden keyword detected: {kw.strip()}")

    # 3. Must start with SELECT / CTE
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError("Only SELECT / CTE queries are allowed")

    if "INFORMATION_SCHEMA" in upper:
        raise ValueError("System catalogue forbidden: INFORMATION_SCHEMA")

    if re.search(r"\bSYS\.", upper):
        raise ValueError("System catalogue forbidden: SYS.")

    if _LIMIT_RE.search(sql):
        raise ValueError("LIMIT is not valid T-SQL — use TOP instead")


def ensure_top(sql: str, n: int = 100) -> str:
    """Return *sql* with ``TOP n`` injected if no row-limit clause exists."""
    if _TOP_RE.search(sql):
        return sql
    return re.sub(
        r"\bSELECT\b", f"SELECT TOP {n}", sql, count=1, flags=re.IGNORECASE
    )
