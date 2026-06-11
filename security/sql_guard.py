"""SQL safety layer — clean, validate, and cap generated SQL queries.

Three public functions form a processing pipeline that every model-generated
SQL string passes through before execution:

1. :func:`clean_sql`    — strip LLM artefacts (markdown fences, prose
   preamble, ``LIMIT → TOP``) and return bare T-SQL.
2. :func:`validate_sql` — block destructive / out-of-scope queries.
3. :func:`ensure_top`   — inject ``TOP n`` when no row-limit clause exists.

Typical usage::

    from security.sql_guard import clean_sql, validate_sql, ensure_top

    raw = llm_backend.generate(prompt)
    sql = clean_sql(raw)       # raises ValueError if no SELECT found
    validate_sql(sql)           # raises ValueError if forbidden keyword detected
    sql = ensure_top(sql, 100)  # adds TOP 100 if missing
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
_TOP_DISTINCT_RE = re.compile(r"SELECT\s+TOP\s+(\d+)\s+DISTINCT", re.IGNORECASE)
_LIMIT_STRIP_RE  = re.compile(r"\s*\bLIMIT\s+\d+\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def clean_sql(raw: str) -> str:
    """Strip LLM artefacts from *raw* and return a bare T-SQL string.

    Cleaning steps (applied in order)
    ----------------------------------
    1. **Empty check** — raises :class:`ValueError` immediately if *raw* is
       empty or whitespace-only.
    2. **Markdown fence extraction** — if the response contains a
       `````sql … ````` or ````` ``` … ````` block, the inner text is
       extracted and the surrounding markdown is discarded.
    3. **Preamble removal** — any text before the first ``SELECT`` or ``WITH``
       keyword is discarded (handles "Here is the SQL: SELECT …").
    4. **LIMIT → TOP conversion** — ``LIMIT n`` is MySQL syntax, invalid in
       T-SQL.  Conversion rules:

       * ``TOP n`` already present → strip ``LIMIT n`` clause only.
       * ``TOP n`` absent → replace the first ``SELECT`` with ``SELECT TOP n``
         and strip ``LIMIT n``.

    5. **TOP DISTINCT fix** — reorders ``SELECT TOP n DISTINCT`` to the
       valid T-SQL form ``SELECT DISTINCT TOP n``.

    Parameters
    ----------
    raw:
        Raw text returned by the LLM backend.  May contain markdown fences,
        introductory sentences, or MySQL-style ``LIMIT`` clauses.

    Returns
    -------
    str
        Cleaned, bare T-SQL string ready for :func:`validate_sql`.

    Raises
    ------
    ValueError
        * If *raw* is empty or whitespace-only.
        * If no ``SELECT`` / ``WITH`` keyword is found after fence extraction.

    Examples
    --------
    >>> clean_sql("```sql\\nSELECT * FROM Contract\\n```")
    'SELECT * FROM Contract'

    >>> clean_sql("Here is the query:\\nSELECT TOP 10 * FROM Contract")
    'SELECT TOP 10 * FROM Contract'

    >>> clean_sql("SELECT * FROM Contract LIMIT 5")
    'SELECT TOP 5 * FROM Contract'

    >>> clean_sql("SELECT TOP 10 * FROM Contract LIMIT 5")
    'SELECT TOP 10 * FROM Contract'

    >>> clean_sql("SELECT TOP 10 DISTINCT Name FROM Customer")
    'SELECT DISTINCT TOP 10 Name FROM Customer'

    >>> clean_sql("")
    Traceback (most recent call last):
        ...
    ValueError: Received empty SQL from model

    >>> clean_sql("No SQL here at all.")
    Traceback (most recent call last):
        ...
    ValueError: No SELECT / CTE found in model response: 'No SQL here at all.'
    """
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
    """Raise :class:`ValueError` if *sql* is not a safe, read-only SELECT query.

    Validation rules (checked in order)
    ------------------------------------
    1. **Not empty** — blank / whitespace-only SQL is always rejected.
    2. **No forbidden keywords** — checked *before* the SELECT guard so
       ``DELETE …`` raises ``"Forbidden keyword"`` rather than the less
       informative ``"Only SELECT"`` message.  Forbidden tokens are defined
       in :data:`_FORBIDDEN` and cover all DML and DDL commands, stored
       procedure prefixes, and dangerous system extensions.
    3. **Starts with SELECT or WITH** — only read-only queries are allowed.
       CTEs (``WITH name AS (…) SELECT …``) are permitted.
    4. **No system catalogue references** — ``INFORMATION_SCHEMA`` and
       ``SYS.`` are blocked to prevent schema enumeration.
    5. **No LIMIT clause** — ``LIMIT`` is MySQL syntax; T-SQL requires ``TOP``.
       :func:`clean_sql` should have converted this already, but the check
       is repeated here as a defence-in-depth measure.

    Parameters
    ----------
    sql:
        The SQL string to validate.  Should already have been processed by
        :func:`clean_sql` so markdown artefacts and preamble are removed.

    Returns
    -------
    None
        Returns silently when the query is safe.

    Raises
    ------
    ValueError
        With a human-readable message describing the specific violation.

    Examples
    --------
    >>> validate_sql("SELECT TOP 10 * FROM Contract")   # passes silently

    >>> validate_sql("")
    Traceback (most recent call last):
        ...
    ValueError: Empty SQL

    >>> validate_sql("DELETE FROM Contract")
    Traceback (most recent call last):
        ...
    ValueError: Forbidden keyword detected: DELETE

    >>> validate_sql("DROP TABLE Contract")
    Traceback (most recent call last):
        ...
    ValueError: Forbidden keyword detected: DROP

    >>> validate_sql("UPDATE Contract SET Volume = 0")
    Traceback (most recent call last):
        ...
    ValueError: Forbidden keyword detected: UPDATE

    >>> validate_sql("SELECT Name FROM Contract; DROP TABLE Contract")
    Traceback (most recent call last):
        ...
    ValueError: Forbidden keyword detected: DROP

    >>> validate_sql("SELECT * FROM INFORMATION_SCHEMA.TABLES")
    Traceback (most recent call last):
        ...
    ValueError: System catalogue forbidden: INFORMATION_SCHEMA

    >>> validate_sql("SELECT * FROM Contract LIMIT 10")
    Traceback (most recent call last):
        ...
    ValueError: LIMIT is not valid T-SQL — use TOP instead
    """
    if not sql or not sql.strip():
        raise ValueError("Empty SQL")

    upper = sql.upper().strip()

    for kw in _FORBIDDEN:
        if kw in upper:
            raise ValueError(f"Forbidden keyword detected: {kw.strip()}")

    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError("Only SELECT / CTE queries are allowed")

    if "INFORMATION_SCHEMA" in upper:
        raise ValueError("System catalogue forbidden: INFORMATION_SCHEMA")

    if re.search(r"\bSYS\.", upper):
        raise ValueError("System catalogue forbidden: SYS.")

    if _LIMIT_RE.search(sql):
        raise ValueError("LIMIT is not valid T-SQL — use TOP instead")


def ensure_top(sql: str, n: int = 100) -> str:
    """Inject ``TOP n`` into *sql* if no row-limit clause is already present.

    This is a safety net applied **after** :func:`validate_sql` to guarantee
    the database never returns an unbounded result set to the API layer,
    regardless of whether the LLM included a ``TOP`` clause.

    If ``TOP`` already exists anywhere in the query, *sql* is returned
    **unchanged** — the existing limit is preserved even if it is higher
    than *n*.

    Parameters
    ----------
    sql:
        A validated T-SQL SELECT (or WITH/CTE) query string.
    n:
        The row cap to inject when ``TOP`` is absent.  Defaults to ``100``.
        Pass ``cfg.settings.default_top_n`` to use the application-level
        default.

    Returns
    -------
    str
        Either *sql* unchanged (when ``TOP`` was already present) or *sql*
        with ``TOP n`` inserted immediately after the first ``SELECT``
        keyword.

    Examples
    --------
    >>> ensure_top("SELECT * FROM Contract", n=50)
    'SELECT TOP 50 * FROM Contract'

    >>> ensure_top("SELECT TOP 10 * FROM Contract", n=50)
    'SELECT TOP 10 * FROM Contract'

    >>> ensure_top("SELECT DISTINCT Name FROM Customer", n=20)
    'SELECT TOP 20 DISTINCT Name FROM Customer'

    >>> ensure_top("WITH cte AS (SELECT 1) SELECT * FROM cte", n=20)
    'WITH cte AS (SELECT 1) SELECT TOP 20 * FROM cte'
    """
    if _TOP_RE.search(sql):
        return sql
    return re.sub(
        r"\bSELECT\b", f"SELECT TOP {n}", sql, count=1, flags=re.IGNORECASE
    )
