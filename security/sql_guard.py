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
# Matches a trailing " LIMIT n" with optional surrounding whitespace /
# semicolons so it can be stripped without leaving a dangling space.
_LIMIT_STRIP_RE  = re.compile(r"\s*\bLIMIT\s+\d+\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def clean_sql(raw: str) -> str:
    """Strip LLM artefacts and return bare SQL.

    Steps applied in order:
    1. Strip markdown code fences.
    2. Extract the first SELECT / CTE block (drops preamble prose).
    3. Convert ``LIMIT n`` → ``TOP n``.

       - If the query has **no** TOP yet: inject ``TOP n`` right after
         the first SELECT keyword and strip the ``LIMIT n`` clause.
       - If the query **already** has TOP: the ``LIMIT n`` clause is
         redundant.  Strip it cleanly (including surrounding whitespace)
         so the SQL remains valid.

    4. Fix ``SELECT TOP n DISTINCT``  →  ``SELECT DISTINCT TOP n``.

    Raises
    ------
    ValueError
        If *raw* is empty or contains no SELECT statement.
    """
    if not raw or not raw.strip():
        raise ValueError("Received empty SQL from model")

    # 1. unwrap markdown fence
    fence_match = _FENCE_RE.search(raw)
    sql = fence_match.group(1) if fence_match else raw

    # 2. find first SELECT / WITH
    start = _SELECT_START_RE.search(sql)
    if not start:
        raise ValueError(f"No SELECT / CTE found in model response: {sql[:200]!r}")
    sql = sql[start.start():].strip()

    # 3. LIMIT n → TOP n
    #
    # Evaluate _TOP_RE *before* any substitution so the decision is based
    # on the original query, not on a partially-rewritten one.
    if _LIMIT_RE.search(sql):
        if _TOP_RE.search(sql):
            # Already has TOP — just strip LIMIT cleanly (no double row-cap).
            sql = _LIMIT_STRIP_RE.sub("", sql)
        else:
            # No TOP yet — inject it after SELECT and remove LIMIT.
            limit_n = _LIMIT_RE.search(sql).group(1)   # type: ignore[union-attr]
            sql = _LIMIT_STRIP_RE.sub("", sql)
            sql = re.sub(
                r"\bSELECT\b", f"SELECT TOP {limit_n}",
                sql, count=1, flags=re.IGNORECASE,
            )

    # 4. SELECT TOP n DISTINCT  →  SELECT DISTINCT TOP n
    sql = _TOP_DISTINCT_RE.sub(
        lambda m: f"SELECT DISTINCT TOP {m.group(1)}", sql
    )

    return sql.strip()


def validate_sql(sql: str) -> None:
    """Raise ``ValueError`` if *sql* is not a safe, read-only SELECT query.

    Rules
    -----
    - Must start with SELECT or WITH.
    - Must not contain forbidden DML/DDL keywords.
    - Must not reference system catalogues (INFORMATION_SCHEMA, SYS.).
    - Must not use LIMIT (SQL Server doesn't support it).
    """
    if not sql or not sql.strip():
        raise ValueError("Empty SQL")

    upper = sql.upper().strip()

    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError("Only SELECT / CTE queries are allowed")

    for kw in _FORBIDDEN:
        if kw in upper:
            raise ValueError(f"Forbidden keyword detected: {kw.strip()}")

    if "INFORMATION_SCHEMA" in upper:
        raise ValueError("System catalogue forbidden: INFORMATION_SCHEMA")

    if re.search(r"\bSYS\.", upper):
        raise ValueError("System catalogue forbidden: SYS.")

    if _LIMIT_RE.search(sql):
        raise ValueError("LIMIT is not valid T-SQL — use TOP instead")


def ensure_top(sql: str, n: int = 100) -> str:
    """Return *sql* with ``TOP n`` injected if no row-limit clause exists.

    Leaves queries that already have TOP untouched.
    """
    if _TOP_RE.search(sql):
        return sql
    return re.sub(
        r"\bSELECT\b", f"SELECT TOP {n}", sql, count=1, flags=re.IGNORECASE
    )
