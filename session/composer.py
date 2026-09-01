# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""§2 refinement composer — "among those" implemented as Reading B, not A.

Two hard requirements from ``docs/api-contract-v2.md`` §2 govern every
function here:

1. **The composed statement is what the guard validates and what
   executes.** Nothing in this module calls ``validate_sql`` itself —
   composition only ever *builds text*; the caller (``session.engine``)
   is responsible for running :func:`security.sql_guard.validate_sql`
   against the fully composed string, never against ``outer_sql`` alone.
   This is deliberate: an outer query that only mentions ``_prev`` (a CTE
   name) would sail through the table-allowlist check trivially — real
   validation only means something once the previous turn's actual
   ``FROM``/``JOIN``/``WHERE`` is back in the tree being walked.
2. **Dropping the previous turn's display ``TOP`` uncaps the inner scan**,
   so :func:`build_capped_predicate` re-caps it at ``refinement_scan_cap``
   rather than leaving it unbounded.

Reading A vs. Reading B
-----------------------
Reading A (wrap the previous turn's SQL *including* its ``TOP``) is wrong
because that ``TOP`` was a *display* cap, not a statement of "these are
all the rows that matter" — the true top-10-by-volume can easily live
outside the rows that happened to be shown. :func:`strip_display_cap`
removes exactly that ``TOP``/``ORDER BY``/``GROUP BY``/select-list
scaffolding and reduces the previous query to its raw, unaggregated
predicate (the ``FROM``/``JOIN``/``WHERE``), which is Reading B's "all
rows matching the previous filter". Re-aggregating happens in
``outer_sql``, supplied by the caller.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from schema_data.columns import TABLE_COLUMNS
from security.sql_guard import clean_sql, ensure_top

_DIALECT = "tsql"

#: Case-insensitive table-name lookup, mirroring ``security.sql_guard``'s
#: own (private) table allowlist lookup -- duplicated here rather than
#: imported so this module does not reach into that module's private
#: helpers; both are built from the same ``TABLE_COLUMNS`` source of truth.
_TABLE_LOOKUP: dict[str, str] = {name.lower(): name for name in TABLE_COLUMNS}


def _qualified_projection(tree: exp.Select) -> list[exp.Expression]:
    """Explicit ``alias_column`` projections for every FROM/JOIN source.

    A bare ``SELECT *`` over a multi-table join is exactly the kind of
    query that produces duplicate output column names the moment two
    joined tables share a column name (``Customer.Name`` and
    ``Ring.Name`` are a real example in this project's own schema) --
    and SQL Server rejects an *unqualified* reference to an ambiguous
    column name the moment anything downstream (here: the caller's outer
    query, operating on the CTE this becomes) tries to select it. Rather
    than let that surface as an opaque database error at execution time,
    every column of every resolvable source table is projected under a
    guaranteed-unique ``{alias}_{column}`` name instead of relying on the
    source schema happening not to collide.

    A source this function cannot resolve to a known table (a derived
    table, a CTE reference, a table-valued function) falls back to a
    bare ``alias.*`` -- still possibly ambiguous in that one case, which
    is a known, documented limitation of composing a refinement over a
    previous turn shaped that way (see the module docstring).
    """
    sources: list[exp.Expression] = []
    from_clause = tree.args.get("from_") or tree.args.get("from")
    if from_clause is not None and from_clause.this is not None:
        sources.append(from_clause.this)
    for join in tree.args.get("joins") or ():
        if join.this is not None:
            sources.append(join.this)

    projections: list[exp.Expression] = []
    for source in sources:
        if not isinstance(source, exp.Table):
            projections.append(exp.Star())
            continue
        alias = source.alias_or_name
        canonical = _TABLE_LOOKUP.get((source.name or "").lower())
        if canonical is None:
            projections.append(exp.Column(this=exp.Star(), table=exp.to_identifier(alias)))
            continue
        for column in TABLE_COLUMNS[canonical]:
            projections.append(
                exp.column(column, table=alias).as_(f"{alias}_{column}")
            )
    return projections or [exp.Star()]


class CompositionError(ValueError):
    """Raised when a previous turn's SQL cannot be reduced to a predicate.

    Distinct from a *security* rejection (that is ``validate_sql``'s job,
    run by the caller against the final composed text) — this is "the text
    literally does not parse as a single SELECT/WITH query", e.g. a
    previous turn whose stored SQL is empty, a stacked-statement string, or
    a non-query statement.
    """


def strip_display_cap(sql: str) -> str:
    """Reduce *sql* to its raw, unaggregated predicate (§2 Reading B).

    Parses *sql* (T-SQL dialect) and, on its outermost ``Select`` node:

    * clears ``limit`` (sqlglot parses T-SQL's ``TOP n`` into this same
      slot — see ``security.sql_guard``'s module docstring) and ``order``
      — a display cap and the ordering that only existed to support it;
    * clears ``group`` — the previous turn's own aggregation (by
      customer, by month, ...) is not necessarily the grouping the *new*
      question needs;
    * replaces the ``SELECT`` list with an explicit, collision-free
      projection of every column of every joined source table (see
      :func:`_qualified_projection`), rather than keeping the previous
      turn's own (possibly aggregated, possibly narrower) column list.

    This is what makes "reuse Q1's *predicate*" (§2's Reading B) work even
    when the new question needs a column Q1 never selected at all — e.g.
    Q1 aggregated ``SUM(TotalPrice)`` per customer, and Q2 asks to rank by
    a completely different measure (``Quantity``) that only exists at the
    per-contract grain, before Q1's own ``GROUP BY`` collapsed it away.

    A CTE-prefixed query (``WITH x AS (...) SELECT ...``) parses as a
    single ``exp.Select`` with its CTEs attached, so this only ever
    rewrites the outer statement, never anything inside an
    already-defined CTE body.

    Parameters
    ----------
    sql:
        A previous turn's executed SQL — expected to already have passed
        :func:`~security.sql_guard.validate_sql` when that turn was
        created.

    Returns
    -------
    str
        The filtered, ungrouped, uncapped predicate — every joined
        table's columns, each under a unique ``{alias}_{column}`` name.

    Raises
    ------
    CompositionError
        If *sql* does not parse as exactly one query, or its root is not
        a ``SELECT``/``WITH`` (set operations — ``UNION``/``INTERSECT``/
        ``EXCEPT`` — are also out of scope for this phase's composer; a
        previous turn shaped that way cannot currently be refined via the
        CTE path).

    Examples
    --------
    >>> strip_display_cap("SELECT TOP 100 Name FROM Customer c ORDER BY Name")
    'SELECT c.ID AS c_ID, c.Name AS c_Name, c.NationalID AS c_NationalID, c.IsActive AS c_IsActive FROM Customer AS c'

    A previous turn's own aggregation is dropped along with its display
    cap -- the new question may need a column that never survived it:

    >>> "GROUP BY" in strip_display_cap(
    ...     "SELECT TOP 5 c.Name, COUNT(*) AS N FROM Customer c GROUP BY c.Name ORDER BY N DESC"
    ... )
    False
    """
    if not sql or not sql.strip():
        raise CompositionError("Cannot compose a refinement: previous turn has no SQL")
    try:
        statements = [s for s in sqlglot.parse(sql, read=_DIALECT) if s is not None]
    except SqlglotError as exc:
        raise CompositionError(f"Previous turn's SQL does not parse: {exc}") from exc
    if len(statements) != 1:
        raise CompositionError(
            f"Previous turn's SQL is not a single statement ({len(statements)} found) "
            "-- cannot compose a refinement over it"
        )
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise CompositionError(
            f"Previous turn's SQL root is {type(tree).__name__}, not a SELECT/WITH query "
            "-- cannot compose a refinement over it"
        )
    tree = tree.copy()
    tree.set("limit", None)
    tree.set("order", None)
    tree.set("group", None)
    tree.set("distinct", None)
    tree.set("expressions", _qualified_projection(tree))
    return tree.sql(dialect=_DIALECT)


def predicate_columns(previous_sql: str) -> list[str]:
    """The output column names :func:`strip_display_cap` will project.

    Lets a caller (``session.engine``) tell the model what ``_prev``
    actually exposes -- ``{alias}_{column}`` for every joined table's
    every column -- without re-deriving the projection logic itself.

    Examples
    --------
    >>> predicate_columns("SELECT TOP 5 Name FROM Customer c")
    ['c_ID', 'c_Name', 'c_NationalID', 'c_IsActive']
    """
    try:
        statements = [s for s in sqlglot.parse(previous_sql, read=_DIALECT) if s is not None]
    except SqlglotError:
        return []
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return []
    projection = _qualified_projection(statements[0].copy())
    names: list[str] = []
    for expr in projection:
        if isinstance(expr, exp.Alias):
            names.append(expr.alias)
        elif isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
            names.append(f"{expr.table}.*")
        else:
            names.append("*")
    return names


def build_capped_predicate(previous_sql: str, cap: int) -> str:
    """:func:`strip_display_cap` then re-cap at *cap* rows (§2 requirement 2).

    Parameters
    ----------
    previous_sql:
        The previous turn's executed SQL.
    cap:
        ``cfg.settings.refinement_scan_cap`` (or an explicit override in
        tests).

    Returns
    -------
    str
        A query equivalent to the previous turn's filter, capped at
        *cap* rows -- safe to embed as the ``_prev`` CTE body.

    Examples
    --------
    >>> build_capped_predicate("SELECT TOP 100 Name FROM Customer", cap=10000)
    'SELECT TOP 10000 Customer.ID AS Customer_ID, Customer.Name AS Customer_Name, Customer.NationalID AS Customer_NationalID, Customer.IsActive AS Customer_IsActive FROM Customer'
    """
    uncapped = strip_display_cap(previous_sql)
    return ensure_top(uncapped, cap)


def compose_refinement_sql(previous_sql: str, outer_sql: str, cap: int) -> str:
    """Build the full ``WITH _prev AS (...) <outer>`` statement (§2).

    ``outer_sql`` is passed through :func:`~security.sql_guard.clean_sql`
    (markdown-fence/preamble stripping only -- **not** validation; see the
    module docstring's hard requirement 1) before being appended. Callers
    MUST run :func:`~security.sql_guard.validate_sql` on this function's
    *return value*, never on ``outer_sql`` alone and never on
    ``previous_sql`` alone.

    Parameters
    ----------
    previous_sql:
        The previous turn's executed SQL (its filter is what "among
        those" refers to).
    outer_sql:
        Raw model output for the new aggregation/ranking, written against
        a CTE named ``_prev`` whose columns are the ``{alias}_{column}``
        projections :func:`strip_display_cap` produced (e.g. ``SELECT
        TOP 10 c_Name, SUM(ct_Quantity) AS TotalVolume FROM _prev GROUP
        BY c_Name ORDER BY TotalVolume DESC``).
    cap:
        ``cfg.settings.refinement_scan_cap``.

    Returns
    -------
    str
        The composed, not-yet-validated SQL text.

    Raises
    ------
    CompositionError
        If *outer_sql* itself defines a ``WITH`` clause (it must not --
        ``_prev`` is defined exactly once, by this function), or if
        *previous_sql* cannot be reduced to a predicate (see
        :func:`strip_display_cap`).

    Examples
    --------
    >>> prev = "SELECT TOP 100 c.Name AS CustomerName FROM Customer c WHERE c.IsActive = 1"
    >>> outer = "SELECT TOP 10 c_Name FROM _prev ORDER BY c_Name"
    >>> composed = compose_refinement_sql(prev, outer, cap=10000)
    >>> composed.startswith("WITH _prev AS (")
    True
    >>> "c.IsActive = 1" in composed
    True
    >>> composed.endswith("SELECT TOP 10 c_Name FROM _prev ORDER BY c_Name")
    True
    """
    cleaned_outer = clean_sql(outer_sql)
    if cleaned_outer.strip().upper().startswith("WITH"):
        raise CompositionError(
            "Model-generated outer query must not define its own WITH clause "
            "-- '_prev' is defined exactly once by the composer"
        )
    capped_predicate = build_capped_predicate(previous_sql, cap)
    return f"WITH _prev AS (\n    {capped_predicate}\n)\n{cleaned_outer}"


def check_scan_truncated(
    execute_fn: Callable[[str], pd.DataFrame], previous_sql: str, cap: int,
) -> bool:
    """Cheaply detect whether the capped inner scan actually hit *cap*.

    Runs one bounded query -- ``SELECT COUNT(*) FROM (SELECT TOP cap+1 1
    AS _x FROM (<uncapped predicate>) AS _base) AS _check`` -- so the cost
    of *checking* for truncation is itself capped at ``cap + 1`` rows,
    never a full unbounded scan of the predicate. If the count comes back
    as ``cap + 1`` (the query could not even fill that one extra probe
    row without being capped), there were strictly more than *cap*
    matching rows and the refinement's ``_prev`` was truncated -- the
    caller must add the §2 warning.

    Parameters
    ----------
    execute_fn:
        Same ``(sql: str) -> pandas.DataFrame`` shape
        ``llm.sql_agent.SQLAgent`` uses -- typically
        ``database.executor.execute_query``, or a stub in tests.
    previous_sql:
        The previous turn's executed SQL.
    cap:
        The cap that was applied to the ``_prev`` CTE.

    Returns
    -------
    bool
        ``True`` if the predicate matches more than *cap* rows (the
        refinement was computed over a truncated base).
    """
    uncapped = strip_display_cap(previous_sql)
    probe = f"SELECT COUNT(*) AS Cnt FROM (SELECT TOP {cap + 1} 1 AS _x FROM ({uncapped}) AS _base) AS _check"
    df = execute_fn(probe)
    if df is None or df.empty:
        return False
    count = int(df.iloc[0, 0])
    return count > cap
