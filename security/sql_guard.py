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
    validate_sql(sql)           # raises ValueError if the query is unsafe
    sql = ensure_top(sql, 100)  # adds TOP 100 if missing

``validate_sql`` — parser-based, not a string blocklist
---------------------------------------------------------
Earlier versions of this module checked forbidden keywords with a substring
scan (``"DROP " in sql.upper()``), which is defeated by *any* separator other
than a literal trailing space (``DROP\\n``, ``DROP\\t``, or the keyword being
the last token in the string) and produces false positives for ordinary
identifiers that merely contain a blocked substring (``EXP_DATE`` contains
``XP_``). See ``tests/test_sql_guard_bypass.py`` for the full catalogue of
bypasses this was replaced to fix.

``validate_sql`` now parses the query with `sqlglot <https://sqlglot.com/>`_,
pinned to the ``tsql`` dialect (SQL Server is the only execution target), and
checks the resulting **AST**, not the source text:

* Exactly one statement is required — sqlglot splits on ``;`` the same way
  the database driver would, so stacked statements are rejected as a class
  rather than by recognising each one's keyword individually.
* The single statement's root node must be a read-only query
  (``SELECT``/``WITH``, or a top-level ``UNION``/``INTERSECT``/``EXCEPT``).
  Anything else — ``INSERT``, ``UPDATE``, ``DELETE``, ``DROP``, ``CREATE``,
  ``ALTER``, ``MERGE``, ``TRUNCATE``, ``GRANT``, ``REVOKE``, ``EXEC`` — is
  refused by its node type, so a bypass now requires defeating the parser
  itself, not guessing at a new spelling of a keyword.
* The whole tree is walked for dangerous constructs that can hide inside an
  otherwise-innocuous ``SELECT``: ``SELECT ... INTO`` (creates a table),
  ``OPENROWSET``/``OPENQUERY``/``OPENDATASOURCE`` (remote/file access),
  and any ``xp_*``/``sp_*``-prefixed function or table reference.
* Because the check operates on typed AST nodes, a keyword that merely
  *contains* a forbidden substring (``EXP_DATE``) or that appears inside a
  string literal's *value* (``N'please DROP the box'``) is never confused
  with the keyword appearing as actual SQL syntax — the parser already knows
  the difference between an ``Identifier`` / ``Literal`` and a ``Drop``
  statement.
* **Any SQL comment is refused outright**, regardless of its content. An
  earlier version of this check scanned comment *text* for forbidden
  keywords — but that is exactly the substring-scanning mistake this
  module was rewritten to get away from, just relocated to a new part of
  the string (see the string-literal bullet above: the fix there was to
  stop treating a string literal's *value* as syntax, not to scan it more
  cleverly). A model generating SQL for this application has no legitimate
  reason to emit a comment, and a comment is a known vector for hiding an
  instruction from a keyword scanner (or a human reviewer) without
  affecting execution, so the presence of *any* comment — not its
  content — is the refusal condition.
* Every table reference is checked against
  ``schema_data.columns.TABLE_COLUMNS`` (case-insensitively, schema/db
  qualifiers ignored) and **rejected if it does not resolve to a known
  table** — except a reference to a CTE defined earlier in the same query,
  which is not a reference to a real table at all. This is enforced even
  though the application's DB login is not yet scoped to just these
  tables (see ``docs/db-hardening.md``): the guard should not depend on
  that hardening having been applied to do its job, and a hallucinated or
  out-of-domain table name (``HR_Payroll``, ``[Evil].[Secrets]``, ...) is
  exactly the kind of reference this allowlist exists to catch.
* Every *column* reference that can be unambiguously resolved to a known
  table is checked against that table's known columns; this part remains
  deliberately conservative — an unqualified column, a column qualified by
  a CTE name or a derived-table alias, or ``*`` (when no ``denied_columns``
  policy is active — see below) is **allowed** rather than rejected, since
  guessing wrong here would reject a working production query. See the
  accompanying phase report for the specific references this module
  chose not to resolve, and why. Table names are not given this same
  leniency: an unresolvable *table* is refused (previous bullet), while an
  unresolvable *column qualifier* is allowed — these are different
  failure modes with different false-positive risk, not an inconsistency.
* ``denied_columns`` is an optional seam for column-level access control: any
  column reference whose name is in that set is refused regardless of which
  table it resolves to. No policy data ships with this module yet — this is
  the foundation multi-tenant column policies (a later phase) will build on.
  When a policy is active, ``*`` cannot be used to silently read around it:
  ``alias.*`` is expanded against the table ``alias`` resolves to and
  checked column-by-column, and a bare ``*`` is expanded against the
  tables directly in its own ``SELECT``'s ``FROM``/``JOIN``; either form is
  refused outright if it cannot be resolved that way (a derived table, a
  CTE, or an unrecognised source), since "cannot prove it's safe" and
  "unsafe" get the same answer when a column policy is actually in force.

``INFORMATION_SCHEMA`` / ``sys.*`` access remains blocked, and a literal
``LIMIT n`` (MySQL syntax, invalid T-SQL) is still rejected — sqlglot's tsql
dialect parses ``LIMIT`` and ``TOP`` into the *same* AST node, so this one
check is necessarily a raw-text scan rather than an AST check: by the time
the query is parsed, the syntactic distinction the check cares about (did
the *model* write ``LIMIT``, which is invalid T-SQL, as opposed to ``TOP``)
has already been normalised away.

``clean_sql`` and ``ensure_top`` — deliberately still string/structure based
------------------------------------------------------------------------------
``ensure_top`` locates the query's outermost ``SELECT`` (or top-level set
operation) using a paren-depth / string-literal-aware scan rather than a
naive "first occurrence of the word SELECT" search — this was the Phase 0
fix (commit ``ad44b93``) for the CTE / ``DISTINCT`` / subquery / ``UNION``
bugs documented in ``tests/test_sql_guard_bypass.py``. Regenerating the SQL
from a sqlglot AST instead was evaluated for this phase and rejected: round
tripping a parsed query back through ``Expression.sql(dialect="tsql")`` does
not reproduce the *exact* input text — it adds explicit column aliases
(``SELECT 1`` becomes ``SELECT 1 AS [1]`` inside a CTE), adds explicit
derived-table aliases, and re-spaces operators (``Active=1`` becomes
``Active = 1``) — which would silently change the SQL sent to the database
and would break several byte-exact assertions in ``tests/test_sql_guard.py``
that predate this phase and are the specification for this function's
behaviour. ``ensure_top`` therefore still performs surgical text insertion at
a structurally-determined position rather than full AST regeneration.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from schema_data.columns import TABLE_COLUMNS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: SQL Server is the only execution target — every parse and validation call
#: in this module is pinned to this dialect rather than sqlglot's generic
#: default, so T-SQL-specific syntax (``TOP``, bracketed identifiers, ``N''``
#: national string literals, ``+`` string concatenation, ...) is understood
#: instead of rejected as invalid.
_DIALECT = "tsql"

_LIMIT_RE        = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)
_FENCE_RE        = re.compile(r"```(?:sql)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_SELECT_START_RE = re.compile(r"(SELECT|WITH)\b", re.IGNORECASE)
_TOP_DISTINCT_RE = re.compile(r"SELECT\s+TOP\s+(\d+)\s+DISTINCT", re.IGNORECASE)
_LIMIT_STRIP_RE  = re.compile(r"\s*\bLIMIT\s+\d+\b", re.IGNORECASE)

# ensure_top() helpers — locate the query's structurally OUTERMOST SELECT
# instead of naively matching the first literal "SELECT" in the string
# (see ensure_top's docstring for why that matters for CTEs, subqueries,
# and DISTINCT ordering).
_SELECT_TOKEN_RE           = re.compile(r"\bSELECT\b", re.IGNORECASE)
_ORDER_BY_RE               = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
_DISTINCT_AFTER_SELECT_RE  = re.compile(r"\s+DISTINCT\b", re.IGNORECASE)
_TOP_AFTER_RE              = re.compile(r"\s+TOP\s+\d+\b", re.IGNORECASE)


def _scan_structure(sql: str) -> tuple[list[int], list[bool]]:
    """Return parallel per-character ``(paren_depth, in_string)`` arrays.

    ``'...'`` string literals (T-SQL escapes an embedded quote by
    doubling it, e.g. ``N'it''s'``) are treated as opaque: a ``(``/``)``
    inside one does not affect ``paren_depth``, and a keyword match
    starting inside one is reported via ``in_string`` so callers can
    exclude it — a forbidden or structural keyword appearing as quoted
    *data* is not the same as it appearing as SQL syntax.
    """
    depth = 0
    in_string = False
    depths: list[int] = []
    in_str: list[bool] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if in_string:
            depths.append(depth)
            in_str.append(True)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    depths.append(depth)
                    in_str.append(True)
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            depths.append(depth)
            in_str.append(True)
            i += 1
            continue
        depths.append(depth)
        in_str.append(False)
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        i += 1
    return depths, in_str


def _top_level_select_matches(sql: str) -> list[re.Match[str]]:
    """Return ``SELECT`` keyword matches at paren depth 0, outside strings.

    These are the query's outermost SELECT(s) — a CTE body's inner
    SELECT (inside the CTE's own parentheses) and any subquery's SELECT
    sit at depth >= 1 and are excluded. More than one match means a
    top-level ``UNION``/``INTERSECT``/``EXCEPT``.
    """
    depths, in_str = _scan_structure(sql)
    matches = []
    for m in _SELECT_TOKEN_RE.finditer(sql):
        pos = m.start()
        if pos < len(depths) and depths[pos] == 0 and not in_str[pos]:
            matches.append(m)
    return matches


def _has_top_level_order_by(sql: str) -> bool:
    """True if *sql* has an ``ORDER BY`` at paren depth 0, outside strings."""
    depths, in_str = _scan_structure(sql)
    for m in _ORDER_BY_RE.finditer(sql):
        pos = m.start()
        if pos < len(depths) and depths[pos] == 0 and not in_str[pos]:
            return True
    return False


# ---------------------------------------------------------------------------
# validate_sql() — AST-based constants and helpers
# ---------------------------------------------------------------------------

#: Root/node types that are always refused, mapped to the human-readable
#: label used in the raised message. Checked both as the parsed statement's
#: root (a bare ``DELETE FROM t`` — the whole query *is* the dangerous
#: statement) and while walking every statement's full tree (a
#: ``SELECT ... INTO`` — the dangerous part is nested inside an otherwise
#: allowed ``Select``). ``exp.TruncateTable`` is relabelled to the more
#: familiar ``TRUNCATE`` keyword.
_NAMED_FORBIDDEN_TYPES: dict[type[exp.Expression], str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Drop: "DROP",
    exp.Alter: "ALTER",
    exp.Create: "CREATE",
    exp.Merge: "MERGE",
    exp.TruncateTable: "TRUNCATE",
    exp.Revoke: "REVOKE",
    exp.Execute: "EXECUTE",
    exp.Into: "INTO",
}

#: Root node types that represent a read-only query. A ``WITH ... SELECT``
#: parses as an ``exp.Select`` with its CTEs attached, not a separate root
#: type, so no explicit ``With`` entry is needed here.
_ALLOWED_ROOT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Select, exp.Union, exp.Intersect, exp.Except,
)

#: Function/table names that grant remote, file, or cross-server access
#: regardless of the columns/rows they touch — these are refused by name
#: even though sqlglot has no dedicated node type for them (they parse as
#: an ``exp.Anonymous`` function call).
_DANGEROUS_FUNCTION_NAMES = frozenset({"OPENROWSET", "OPENQUERY", "OPENDATASOURCE"})

#: System catalogues that would let a query enumerate the whole schema.
_SYSTEM_SCHEMAS = frozenset({"INFORMATION_SCHEMA", "SYS"})


def _is_dangerous_identifier(name: str) -> bool:
    """True if *name* is a stored-procedure/extension prefix or remote-access function."""
    upper = name.upper()
    return (
        upper.startswith("XP_")
        or upper.startswith("SP_")
        or upper in _DANGEROUS_FUNCTION_NAMES
    )


def _forbidden_label(node: exp.Expression) -> str | None:
    """Return a human-readable forbidden-keyword label for *node*, or ``None``.

    ``exp.Command`` is sqlglot's fallback node for T-SQL statements it has
    no dedicated class for (``GRANT``, ``SHUTDOWN WITH NOWAIT``, ...); its
    ``this`` attribute carries the leading keyword sqlglot recognised
    (``GRANT``), which is a better label than the generic type name.
    """
    if isinstance(node, exp.Command):
        cmd = node.this
        return str(cmd).upper() if cmd else "COMMAND"
    for node_type, label in _NAMED_FORBIDDEN_TYPES.items():
        if isinstance(node, node_type):
            return label
    return None


def _build_schema_lookup() -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """Build case-insensitive lookup tables from ``TABLE_COLUMNS``.

    Returns
    -------
    tuple[dict[str, str], dict[str, frozenset[str]]]
        ``(table_lookup, columns_by_table)`` where *table_lookup* maps a
        lower-cased table name to its canonical (correctly-cased) name, and
        *columns_by_table* maps that canonical name to the frozenset of its
        lower-cased column names.
    """
    table_lookup = {name.lower(): name for name in TABLE_COLUMNS}
    columns_by_table = {
        name: frozenset(col.lower() for col in cols)
        for name, cols in TABLE_COLUMNS.items()
    }
    return table_lookup, columns_by_table


_TABLE_LOOKUP, _COLUMNS_BY_TABLE = _build_schema_lookup()


def _cte_names(tree: exp.Expression) -> frozenset[str]:
    """Lower-cased names of every CTE defined in *tree*.

    A reference to one of these is a reference to a CTE, not to a real
    table — it must be excluded from both the table allowlist check and
    table-name resolution, since the CTE's own body (not
    ``TABLE_COLUMNS``) is what actually defines its shape.
    """
    return frozenset(cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias)


def _resolve_table_name(raw_name: str | None, cte_names: frozenset[str]) -> str | None:
    """Return the canonical ``TABLE_COLUMNS`` name for *raw_name*, or ``None``.

    ``None`` covers three distinct cases callers deliberately do not
    distinguish here: *raw_name* is empty (e.g. a table-valued function
    call, whose ``exp.Table.this`` is an ``exp.Anonymous`` rather than an
    ``exp.Identifier``), it names a CTE, or it simply is not in
    ``TABLE_COLUMNS``.
    """
    if not raw_name or raw_name.lower() in cte_names:
        return None
    return _TABLE_LOOKUP.get(raw_name.lower())


def _collect_table_alias_map(
    tree: exp.Expression, cte_names: frozenset[str]
) -> dict[str, str]:
    """Map every resolvable alias/name (lower-cased) to its canonical table name.

    By the time this runs, every non-CTE ``exp.Table`` in *tree* has
    already been checked against the schema allowlist (see
    ``validate_sql``'s main walk) and either resolved or caused a
    rejection — so in practice every table this function sees does
    resolve. It re-resolves defensively via :func:`_resolve_table_name`
    rather than assuming that, so it stays correct on its own if ever
    called before that check.
    """
    alias_map: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        canonical = _resolve_table_name(table.name, cte_names)
        if canonical is None:
            continue
        alias_map[table.name.lower()] = canonical
        if table.alias:
            alias_map[table.alias.lower()] = canonical
    return alias_map


def _enclosing_select(node: exp.Expression) -> exp.Select | None:
    """Walk up *node*'s parents to the nearest enclosing ``exp.Select``."""
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        parent = parent.parent
    return parent


def _direct_from_sources(select: exp.Select) -> list[exp.Expression]:
    """The expressions directly in *select*'s own ``FROM``/``JOIN`` clauses.

    Deliberately not recursive: a bare ``*`` in *select*'s own SELECT list
    expands to columns from exactly these sources, not from a subquery
    nested inside one of them.
    """
    sources: list[exp.Expression] = []
    # sqlglot has spelled this arg key both "from" and "from_" across
    # versions (Python cannot use the bare word "from" as an attribute
    # name, which is likely why); check both so this does not silently
    # stop finding FROM clauses on a sqlglot upgrade/downgrade.
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None and from_clause.this is not None:
        sources.append(from_clause.this)
    for join in select.args.get("joins") or ():
        if join.this is not None:
            sources.append(join.this)
    return sources


def _resolve_star_tables(
    select: exp.Select | None, cte_names: frozenset[str]
) -> list[str] | None:
    """Canonical table names a bare ``*`` in *select* expands to, or ``None``.

    ``None`` means "cannot resolve with confidence" — *select* is missing,
    or one of its direct ``FROM``/``JOIN`` sources is not a plain
    known-table reference (a derived table, a table-valued function, or a
    CTE). Callers must treat ``None`` as "refuse", not "allow": this
    function is only consulted when a ``denied_columns`` policy is active,
    where an unprovable ``*`` must not be allowed to read around it.
    """
    if select is None:
        return None
    resolved: list[str] = []
    for source in _direct_from_sources(select):
        if not isinstance(source, exp.Table):
            return None
        canonical = _resolve_table_name(source.name, cte_names)
        if canonical is None:
            return None
        resolved.append(canonical)
    return resolved


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
       T-SQL.  Conversion rules (delegated to :func:`ensure_top`, which
       locates the query's OUTERMOST ``SELECT`` — skipping CTE bodies and
       subqueries — rather than blindly the first literal ``SELECT`` in
       the string; see its docstring for the full CTE / DISTINCT /
       subquery / ``UNION`` handling):

       * ``TOP n`` already present on the outermost ``SELECT`` → strip
         ``LIMIT n`` clause only.
       * ``TOP n`` absent → inject ``SELECT TOP n`` on the outermost
         ``SELECT`` and strip ``LIMIT n``.

    5. **TOP DISTINCT fix** — reorders ``SELECT TOP n DISTINCT`` to the
       valid T-SQL form ``SELECT DISTINCT TOP n``, for cases where the
       LLM emitted that ordering directly (step 4's ``ensure_top`` call
       already produces the correct order when *it* is the one adding
       ``TOP``).

    This is deliberately still text surgery rather than parse-and-regenerate
    — see the module docstring for why a sqlglot round trip is unsafe here
    (it does not reproduce the input text exactly). :func:`validate_sql`,
    which runs immediately after this function in the pipeline, is the
    parser-based half of the guard.

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

    >>> clean_sql("WITH c AS (SELECT x FROM t) SELECT * FROM c LIMIT 5")
    'WITH c AS (SELECT x FROM t) SELECT TOP 5 * FROM c'

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
        limit_n = int(_LIMIT_RE.search(sql).group(1))   # type: ignore[union-attr]
        sql = _LIMIT_STRIP_RE.sub("", sql)
        # Delegate the actual placement of TOP to ensure_top(), which
        # locates the OUTERMOST SELECT (skipping CTE bodies and
        # subqueries) and is a no-op if that SELECT already has a TOP —
        # exactly "TOP n absent -> inject" / "TOP n present -> strip
        # LIMIT only" from the docstring above, without duplicating the
        # CTE/DISTINCT/subquery-aware placement logic here too.
        sql = ensure_top(sql, limit_n)

    sql = _TOP_DISTINCT_RE.sub(
        lambda m: f"SELECT DISTINCT TOP {m.group(1)}", sql
    )

    return sql.strip()


def validate_sql(sql: str, *, denied_columns: Iterable[str] | None = None) -> None:
    """Raise :class:`ValueError` if *sql* is not a safe, read-only SELECT query.

    Validation rules (checked in order)
    ------------------------------------
    1. **Not empty** — blank / whitespace-only SQL is always rejected.
    2. **No literal ``LIMIT``** — ``LIMIT`` is MySQL syntax; T-SQL requires
       ``TOP``. This is a raw-text check rather than an AST check: sqlglot's
       ``tsql`` dialect parses ``LIMIT n`` and ``TOP n`` into the *same* AST
       node, so the syntactic distinction this check cares about no longer
       exists once the query is parsed.
    3. **Parses as exactly one T-SQL statement** — the query is parsed with
       `sqlglot <https://sqlglot.com/>`_ pinned to the ``tsql`` dialect. A
       syntax error, or more than one statement (sqlglot splits on ``;`` the
       same way the database driver would), is rejected outright — this
       alone rejects every stacked-statement bypass as a class, rather than
       relying on recognising each statement's individual keyword.
    4. **Root node is a read-only query** — only ``SELECT``/``WITH`` (a
       ``WITH`` attaches to the ``Select`` root, so no separate check is
       needed) and top-level ``UNION``/``INTERSECT``/``EXCEPT`` are allowed.
       Every other statement kind (``INSERT``, ``UPDATE``, ``DELETE``,
       ``DROP``, ``CREATE``, ``ALTER``, ``MERGE``, ``TRUNCATE``, ``GRANT``,
       ``REVOKE``, ``EXEC``/``EXECUTE``, ...) is refused by its node type.
    5. **No dangerous construct anywhere in the tree** — the whole AST is
       walked (not just the root) so a dangerous node nested inside an
       otherwise-allowed ``SELECT`` is still caught: ``SELECT ... INTO``
       (creates a table), ``OPENROWSET``/``OPENQUERY``/``OPENDATASOURCE``
       (remote/file access), and any ``xp_*``/``sp_*``-prefixed function or
       table reference.
    6. **No SQL comments, of any content** — a comment is refused outright
       because it is present, not because its text was scanned for a
       keyword (that would just relocate the substring-matching mistake
       rule 5 exists to avoid — see the module docstring). A model
       generating SQL for this application has no legitimate reason to
       emit one.
    7. **No system catalogue references** — ``INFORMATION_SCHEMA`` and
       ``SYS`` are blocked (by schema/table AST node, not substring) to
       prevent schema enumeration.
    8. **Table allowlist** — every table reference must resolve to a known
       table (case-insensitively, per
       :data:`schema_data.columns.TABLE_COLUMNS`, schema/db qualifier
       ignored) or be a reference to a CTE defined earlier in the same
       query; anything else — a hallucinated, out-of-domain, or malicious
       table name — is refused. Unlike the column check below, this is
       *not* lenient: an unresolvable table name is a rejection, not a
       pass.
    9. **Column allowlist** — every *qualified* column reference against a
       table resolved in rule 8 is checked against that table's known
       columns; an unqualified column, or one qualified by a CTE name or a
       derived-table alias, is allowed rather than rejected — resolving
       those correctly in the general case (joins, computed aliases in
       ORDER BY/GROUP BY, ...) risks a false positive that breaks a
       working query, which this module treats as the worse outcome.
    10. **Denied-column policy** — if *denied_columns* is given: any column
        reference by that name (qualified or not, resolvable or not) is
        refused regardless of which table it belongs to, and ``*``/``alias.*``
        cannot be used to read around the policy — each is expanded
        against the table(s) it resolves to and checked column-by-column,
        or refused outright if it cannot be resolved that way. This check
        is intentionally *not* pragmatic like rule 9: a denied column
        should never slip through just because this module couldn't prove
        which table a reference came from.

    Parameters
    ----------
    sql:
        The SQL string to validate.  Should already have been processed by
        :func:`clean_sql` so markdown artefacts and preamble are removed.
    denied_columns:
        Optional iterable of column names (case-insensitive) to refuse
        outright wherever they are referenced. This is a seam for
        column-level access control (e.g. multi-tenant row/column
        policies) — no default policy is applied; pass ``None`` (the
        default) to skip this check entirely.

    Returns
    -------
    None
        Returns silently when the query is safe.

    Raises
    ------
    ValueError
        With a human-readable message describing the specific violation.
        Messages for a security-relevant rejection (forbidden statement
        kind, dangerous function, denied column, ...) contain the substring
        ``"Forbidden keyword"`` — callers (see ``api/runner.py``) rely on
        that substring to distinguish a security block from an ordinary
        malformed-SQL error.

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

    >>> validate_sql("UPDATE Contract SET Price = 0")
    Traceback (most recent call last):
        ...
    ValueError: Forbidden keyword detected: UPDATE

    >>> validate_sql("SELECT Price FROM Contract; DROP TABLE Contract")
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

    A column name that merely *contains* a legacy blocklist substring is no
    longer a false positive (contrast ``tests/test_sql_guard_bypass.py``,
    which documents this as a bug at the pre-Phase-1 baseline):

    >>> validate_sql("SELECT EXP_DATE FROM Contract")   # passes silently

    ``SELECT ... INTO`` is a write disguised as a read:

    >>> validate_sql("SELECT * INTO NewTbl FROM Contract")
    Traceback (most recent call last):
        ...
    ValueError: Forbidden keyword detected: INTO

    A table this module does not recognise is refused, even though it
    starts with an otherwise-harmless ``SELECT`` — this does not depend on
    the database login also being scoped to just the known tables (see
    ``docs/db-hardening.md``):

    >>> validate_sql("SELECT * FROM HR_Payroll")
    Traceback (most recent call last):
        ...
    ValueError: Forbidden keyword detected: unknown table 'HR_Payroll' is not in the schema allowlist

    A comment is refused outright, regardless of what it says:

    >>> validate_sql("SELECT Price FROM Contract -- looks harmless")
    Traceback (most recent call last):
        ...
    ValueError: SQL comments are not allowed: a comment is not executable SQL syntax, so its content is never inspected for keywords -- the comment itself is refused outright.

    The optional column-level ACL seam:

    >>> validate_sql("SELECT NationalID FROM Customer", denied_columns={"NationalID"})
    Traceback (most recent call last):
        ...
    ValueError: Forbidden keyword detected: denied column 'NationalID'

    ``*`` cannot be used to read around an active column policy:

    >>> validate_sql("SELECT * FROM Customer", denied_columns={"NationalID"})
    Traceback (most recent call last):
        ...
    ValueError: Forbidden keyword detected: '*' would expose denied column(s): ['nationalid']
    """
    if not sql or not sql.strip():
        raise ValueError("Empty SQL")

    if _LIMIT_RE.search(sql):
        raise ValueError("LIMIT is not valid T-SQL — use TOP instead")

    try:
        raw_statements = sqlglot.parse(sql, read=_DIALECT)
    except SqlglotError as exc:
        raise ValueError(f"SQL syntax error: {exc}") from exc

    statements = [stmt for stmt in raw_statements if stmt is not None]
    if not statements:
        raise ValueError("Empty SQL")

    if len(statements) > 1:
        # Prefer naming the specific offending statement kind when one of
        # the extra statements is recognisably dangerous (matches the
        # pre-Phase-1 message shape for these cases); fall back to a
        # generic message when every statement individually looks benign
        # (e.g. "SELECT 1; SELECT 2") — stacking is refused as a class
        # regardless.
        for stmt in statements:
            label = _forbidden_label(stmt)
            if label is not None:
                raise ValueError(f"Forbidden keyword detected: {label}")
        raise ValueError(
            "Forbidden keyword detected: multiple SQL statements are not "
            f"allowed ({len(statements)} statements found)"
        )

    tree = statements[0]

    root_label = _forbidden_label(tree)
    if root_label is not None:
        raise ValueError(f"Forbidden keyword detected: {root_label}")
    if not isinstance(tree, _ALLOWED_ROOT_TYPES):
        raise ValueError(
            "Forbidden keyword detected: only SELECT / WITH / set-operation "
            f"queries are allowed, got {type(tree).__name__}"
        )

    cte_names = _cte_names(tree)

    for node in tree.walk():
        label = _forbidden_label(node)
        if label is not None:
            raise ValueError(f"Forbidden keyword detected: {label}")

        # A comment is refused because it is present, not for what it
        # says — scanning its text for keywords would just relocate the
        # substring-matching mistake this module exists to get away from
        # (see the module docstring). This intentionally does not reuse
        # the "Forbidden keyword" phrasing: no keyword was found, the
        # comment itself is the thing being refused.
        if node.comments:
            raise ValueError(
                "SQL comments are not allowed: a comment is not "
                "executable SQL syntax, so its content is never inspected "
                "for keywords -- the comment itself is refused outright."
            )

        if isinstance(node, exp.Anonymous) and _is_dangerous_identifier(node.name or ""):
            raise ValueError(f"Forbidden keyword detected: {(node.name or '').upper()}")

        if isinstance(node, exp.Table):
            raw_name = node.name
            if _is_dangerous_identifier(raw_name or ""):
                raise ValueError(f"Forbidden keyword detected: {raw_name.upper()}")
            db = (node.db or "").upper()
            if db in _SYSTEM_SCHEMAS:
                raise ValueError(f"System catalogue forbidden: {db}")
            if not db and raw_name.upper() in _SYSTEM_SCHEMAS:
                raise ValueError(f"System catalogue forbidden: {raw_name.upper()}")

            # Table allowlist: unlike the column check below, this is not
            # lenient. A raw_name of "" (an exp.Anonymous table-valued
            # function, e.g. OPENROWSET(...) or sp_who()) is skipped here
            # -- it is already refused above, either by the dangerous
            # function-name checks or because it can never match
            # TABLE_COLUMNS anyway once it does something real.
            if raw_name and raw_name.lower() not in cte_names:
                if _TABLE_LOOKUP.get(raw_name.lower()) is None:
                    raise ValueError(
                        f"Forbidden keyword detected: unknown table "
                        f"'{raw_name}' is not in the schema allowlist"
                    )

    denied = frozenset(c.upper() for c in denied_columns) if denied_columns else frozenset()
    alias_map = _collect_table_alias_map(tree, cte_names)

    for col in tree.find_all(exp.Column):
        name = col.name
        if not name or name == "*":
            continue
        if name.upper() in denied:
            raise ValueError(f"Forbidden keyword detected: denied column '{name}'")

        qualifier = (col.table or "").lower()
        if not qualifier:
            # An unqualified column can't be safely resolved to a single
            # table in the general case (joins, CTEs, computed aliases in
            # ORDER BY/GROUP BY) — allow rather than risk a false positive.
            continue
        canonical = alias_map.get(qualifier)
        if canonical is None:
            # Qualifier is a CTE name or a derived-table alias --
            # unresolvable, so allow. (It cannot be an unrecognised table
            # name: every non-CTE exp.Table was already checked against
            # the allowlist above.)
            continue
        if name.lower() not in _COLUMNS_BY_TABLE[canonical]:
            raise ValueError(f"Unknown column '{name}' on table '{canonical}'")

    if denied:
        # "*" must not be usable to silently read around an active
        # column-denial policy -- expand it against whatever it resolves
        # to and check the expansion, or refuse it outright if it can't be
        # resolved with confidence (see _resolve_star_tables).
        for star in tree.find_all(exp.Star):
            parent = star.parent
            if isinstance(parent, exp.Column) and parent.table:
                canonical = alias_map.get(parent.table.lower())
                table_names = [canonical] if canonical else None
                ref_label = f"{parent.table}.*"
            else:
                table_names = _resolve_star_tables(_enclosing_select(star), cte_names)
                ref_label = "*"

            if table_names is None:
                raise ValueError(
                    f"Forbidden keyword detected: cannot verify whether "
                    f"'{ref_label}' exposes a denied column -- name the "
                    "columns explicitly instead of using '*'"
                )

            exposed_denied = sorted(
                col
                for table_name in table_names
                for col in _COLUMNS_BY_TABLE[table_name]
                if col.upper() in denied
            )
            if exposed_denied:
                raise ValueError(
                    f"Forbidden keyword detected: '{ref_label}' would "
                    f"expose denied column(s): {exposed_denied}"
                )


def extract_touched_tables(sql: str) -> list[str]:
    """Return the canonical, known table names *sql* references, sorted.

    This is the read-only observability counterpart to
    :func:`validate_sql`'s rule 8 table allowlist: that check already
    walks the whole AST resolving every ``exp.Table`` node against
    :data:`schema_data.columns.TABLE_COLUMNS` (via :func:`_resolve_table_name`)
    in order to reject an unknown one. This function reuses the exact
    same resolution to *report* what it found instead of gating on it —
    the intended caller (``api/runner.py``'s audit trail, per
    ``docs/api-contract-v2.md`` §4's ``guard.tables_touched``) needs "which
    known tables did this query read", not a second validation pass.

    Deliberately lenient rather than a guard: a CTE reference is excluded
    (it is not a real table), and anything this function cannot resolve —
    including *sql* failing to parse at all — is silently skipped rather
    than raised. Calling this on SQL that has already passed
    :func:`validate_sql` is the common case and never hits that leniency;
    it is also safe to call on a *rejected* candidate query (e.g. to
    report which known tables a forbidden query still touched), where
    some references may legitimately be unresolvable.

    Parameters
    ----------
    sql:
        Any SQL string — typically SQL that already passed or was
        rejected by :func:`validate_sql`.

    Returns
    -------
    list[str]
        Canonical table names (as they appear in ``TABLE_COLUMNS``),
        de-duplicated and sorted. Empty if *sql* fails to parse, is
        empty, or references no table this module recognises.

    Examples
    --------
    >>> extract_touched_tables("SELECT * FROM Contract")
    ['Contract']

    >>> extract_touched_tables(
    ...     "SELECT c.Name FROM Customer c JOIN Contract t ON t.CustomerId = c.Id"
    ... )
    ['Contract', 'Customer']

    A CTE reference is not a table:

    >>> extract_touched_tables("WITH cte AS (SELECT * FROM Contract) SELECT * FROM cte")
    ['Contract']

    Unparsable or table-free SQL degrades to an empty list, never raises:

    >>> extract_touched_tables("not valid sql at all")
    []
    >>> extract_touched_tables("")
    []
    """
    try:
        raw_statements = sqlglot.parse(sql, read=_DIALECT)
    except SqlglotError:
        return []

    statements = [stmt for stmt in raw_statements if stmt is not None]
    if not statements:
        return []

    touched: set[str] = set()
    for tree in statements:
        cte_names = _cte_names(tree)
        for node in tree.find_all(exp.Table):
            canonical = _resolve_table_name(node.name, cte_names)
            if canonical:
                touched.add(canonical)
    return sorted(touched)


def ensure_top(sql: str, n: int = 100) -> str:
    """Inject ``TOP n`` into *sql* if no row-limit clause is already present.

    This is a safety net applied **after** :func:`validate_sql` to guarantee
    the database never returns an unbounded result set to the API layer,
    regardless of whether the LLM included a ``TOP`` clause.

    Unlike a naive "does the string contain TOP anywhere" check, this
    function locates the query's **outermost** ``SELECT`` — the one whose
    rows are what the caller actually receives — by tracking parenthesis
    depth (and skipping over ``'...'`` string literals, so a stray ``(``
    or the word ``SELECT`` inside quoted data never confuses the scan).
    A CTE body's inner ``SELECT`` and any subquery's ``SELECT`` sit at
    depth ``>= 1`` and are correctly ignored:

    * **Plain query** — ``TOP n`` is inserted right after ``SELECT``
      (after ``DISTINCT``, if present, so the result is valid T-SQL —
      ``DISTINCT`` must precede ``TOP``).
    * **CTE** (``WITH x AS (...) SELECT ...``) — the cap lands on the
      outer ``SELECT`` following the CTE definition(s), not the CTE
      body's own ``SELECT``.
    * **Subquery** (``SELECT ... FROM (SELECT TOP n ... ) z``) — the
      outer query is still capped even though the string already
      contains ``TOP`` (belonging to the inner subquery).
    * **Already capped** — if the outermost ``SELECT`` already has
      ``TOP`` (optionally after ``DISTINCT``), *sql* is returned
      **unchanged**, preserving an existing limit even if higher than
      *n*.
    * **Top-level** ``UNION`` / ``INTERSECT`` / ``EXCEPT`` — a single
      ``TOP n`` after the first branch would only cap that branch, not
      the combined result actually returned, so the whole (post-CTE)
      query is wrapped in a capped outer ``SELECT ... FROM (...) AS
      _ensure_top_capped`` instead.

    This performs surgical text insertion at a structurally-determined
    position rather than a full sqlglot parse-and-regenerate — see the
    module docstring for why a round trip through
    ``Expression.sql(dialect="tsql")`` is unsafe here (it does not
    reproduce the input text exactly, e.g. it adds explicit aliases and
    re-spaces operators). Where this function cannot construct correct SQL
    (an arbitrarily-nested case, or hoisting a ``UNION``'s trailing
    ``ORDER BY`` outside the wrapper), it raises :class:`ValueError` rather
    than silently return an unsafe or invalid query.

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
        *sql*, either unchanged (outermost ``SELECT`` already capped) or
        with ``TOP n`` inserted at the outermost ``SELECT`` — or, for a
        top-level set operation, *sql* wrapped in a capped outer query.

    Raises
    ------
    ValueError
        * If no top-level ``SELECT`` can be found at all.
        * If *sql* is a top-level ``UNION``/``INTERSECT``/``EXCEPT`` that
          also has a top-level ``ORDER BY`` — wrapping it in a derived
          table would make that ``ORDER BY`` invalid T-SQL, and correctly
          hoisting it out is parser-rewrite territory.

    Examples
    --------
    >>> ensure_top("SELECT * FROM Contract", n=50)
    'SELECT TOP 50 * FROM Contract'

    >>> ensure_top("SELECT TOP 10 * FROM Contract", n=50)
    'SELECT TOP 10 * FROM Contract'

    >>> ensure_top("SELECT DISTINCT Name FROM Customer", n=20)
    'SELECT DISTINCT TOP 20 Name FROM Customer'

    >>> ensure_top("WITH cte AS (SELECT 1) SELECT * FROM cte", n=20)
    'WITH cte AS (SELECT 1) SELECT TOP 20 * FROM cte'

    >>> ensure_top("SELECT * FROM (SELECT TOP 1 a FROM t) z", n=10)
    'SELECT TOP 10 * FROM (SELECT TOP 1 a FROM t) z'

    >>> ensure_top("SELECT a FROM t1 UNION SELECT b FROM t2", n=5)
    'SELECT TOP 5 * FROM (SELECT a FROM t1 UNION SELECT b FROM t2) AS _ensure_top_capped'
    """
    matches = _top_level_select_matches(sql)
    if not matches:
        raise ValueError(
            f"ensure_top: no top-level SELECT found in {sql[:200]!r} — "
            "refusing to guess where to inject TOP"
        )

    if len(matches) > 1:
        # Top-level UNION / INTERSECT / EXCEPT: two or more SELECTs at
        # paren depth 0. TOP after only the first branch would cap that
        # branch, not the combined result actually returned — wrap the
        # whole (post-CTE) query in a capped outer SELECT instead.
        prefix = sql[: matches[0].start()]
        body = sql[matches[0].start():]
        if _has_top_level_order_by(body):
            raise ValueError(
                "ensure_top: cannot safely cap a UNION/INTERSECT/EXCEPT "
                "query that also has a top-level ORDER BY without a full "
                "SQL parser (wrapping it in a derived table would make "
                "that ORDER BY invalid T-SQL) — refusing to emit unsafe SQL"
            )
        return f"{prefix}SELECT TOP {n} * FROM ({body}) AS _ensure_top_capped"

    match = matches[0]
    pos = match.end()
    rest = sql[pos:]

    distinct_match = _DISTINCT_AFTER_SELECT_RE.match(rest)
    if distinct_match:
        after = distinct_match.end()
        if _TOP_AFTER_RE.match(rest[after:]):
            return sql  # already 'SELECT DISTINCT TOP n ...'
        return sql[:pos] + rest[:after] + f" TOP {n}" + rest[after:]

    if _TOP_AFTER_RE.match(rest):
        return sql  # outermost SELECT already capped

    return sql[:pos] + f" TOP {n}" + rest
