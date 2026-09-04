# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
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

Every rejection raised by any of the three functions above is one of two
:class:`ValueError` subclasses, not a bare ``ValueError`` — every existing
``except ValueError`` call site across the codebase still catches them
unchanged, but a caller that cares (the self-correction loops in
``llm/sql_agent.py`` and ``session/engine.py``) can tell them apart:

* :class:`CorrectableRejection` — a malformed-but-fixable candidate (bad
  syntax, a literal ``LIMIT``, an empty response, a hallucinated table or
  column name, a disallowed comment, ...). A corrected re-prompt can
  plausibly produce different, valid SQL, so this is retried exactly as
  before.
* :class:`PolicyRejection` — the query is categorically out of bounds
  regardless of phrasing (a forbidden statement type or keyword, a
  system-catalogue reference, a denied column, ...). No re-prompt can
  change that outcome, since the policy that caused it is not part of the
  prompt — retrying would only spend another LLM round trip to reach the
  exact same rejection, so both correction loops stop immediately instead.

Each raise site below is annotated with which of the two it uses and why.

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

``INFORMATION_SCHEMA`` / ``sys.*`` access remains blocked (against the
*target* dialect's own catalogue names — see "Multi-dialect" below, not
always literally ``INFORMATION_SCHEMA``/``sys``), and a literal ``LIMIT n``
is rejected **only when validating tsql** — MySQL/Postgres/SQLite syntax,
invalid T-SQL. sqlglot's tsql dialect parses ``LIMIT`` and ``TOP`` into the
*same* AST node, so this one check is necessarily a raw-text scan rather
than an AST check: by the time the query is parsed, the syntactic
distinction the check cares about (did the *model* write ``LIMIT``, which
is invalid T-SQL, as opposed to ``TOP``) has already been normalised away.
This is the one narrowly-scoped ``if dialect == "tsql":`` in this module —
not a dialect-branching ladder, but a guard that must not fire for a
dialect where ``LIMIT`` is the *correct* syntax (every other dialect this
module supports): rejecting it there would refuse the majority of valid
queries, not close a bypass.

``clean_sql`` — always tsql, deliberately still string/structure based
--------------------------------------------------------------------------
:func:`clean_sql` has no ``dialect`` parameter and is not part of the
multi-dialect surface described below: the model always generates ``tsql``
regardless of the deployment's target dialect (see
:data:`config.Settings.sql_dialect`'s docstring for why), so the raw text
this function cleans is always tsql, unconditionally. It still locates the
query's outermost ``SELECT`` (or top-level set operation) using a
paren-depth / string-literal-aware scan rather than a naive "first
occurrence of the word SELECT" search — the Phase 0 fix (commit
``ad44b93``) for the CTE / ``DISTINCT`` / subquery / ``UNION`` bugs
documented in ``tests/test_sql_guard_bypass.py`` — because its LIMIT-to-TOP
rewrite must land on the correct SELECT without disturbing the rest of the
model's original text (:func:`ensure_top`, below, is a different function
covering the already-has-a-cap detection; ``clean_sql`` only ever calls it
to have that placement logic run once, not to regenerate its own output).

``ensure_top`` — AST row cap for every dialect except tsql
------------------------------------------------------------------
This phase set out to replace this function's hand-written paren-depth /
string-literal-aware scanner with a genuine sqlglot AST cap
(``exp.Select.limit()``, rendered with ``Expression.sql(dialect=...)``) —
a scanner that would otherwise need to grow a second, per-dialect opinion
about row-limiting syntax is exactly the "second, hand-written dialect
layer beside sqlglot" this project's own review history warns against.

That rewrite was verified by running it against every existing assertion
in ``tests/test_sql_guard.py`` and ``tests/test_sql_guard_bypass.py``, not
by inspection — and running it surfaced a real cost the earlier,
text-splicing implementation's own docstring had already predicted: a
full AST render does not reproduce the exact input text whenever a query
contains an unaliased CTE/derived-table column (``SELECT 1`` inside a CTE
becomes ``SELECT 1 AS [1]``) or an operator sqlglot re-spaces
(``Active=1`` becomes ``Active = 1``). For **tsql**, that turned out not
to be a narrow edge case confined to this function's own tests:
:func:`clean_sql`'s LIMIT-to-TOP conversion also calls this function, so
the same reformatting leaked into ordinary ``clean_sql`` output for the
common production case (any generated query needing a cap injected,
i.e. lacking its own ``TOP``) — a real behavioural change to this
deployment's default and, before this phase, *only* dialect, with
consequences this module's own tests do not fully capture (cache keys,
audit-log SQL text, golden-set fixtures).

So the AST rewrite is used for every dialect **except** tsql, which keeps
its original text-splicing implementation completely unchanged — see
:func:`ensure_top`'s own docstring for the full reasoning and
:func:`_ensure_top_ast` for the AST implementation used for
postgres/mysql/sqlite. This is reported, not quietly decided: the
multi-dialect phase report asked for this function's *implementation* to
be replaced outright, and this is the one place that request could not be
honoured literally without a regression risk to tsql's existing,
already-verified production behaviour that this project's own review
history would equally object to.

Multi-dialect: ``dialect`` parameters and :mod:`security.dialects`
------------------------------------------------------------------------
:func:`validate_sql`, :func:`ensure_top`, and :func:`extract_touched_tables`
all take an optional ``dialect`` keyword (default ``"tsql"``, this
module's original and only target) naming a sqlglot dialect key. Passing a
different value re-points every sqlglot parse/render call in the function
at that dialect, and swaps the system-catalogue blocklist and
dangerous-function-name set for the ones configured for that dialect in
:data:`security.dialects.DIALECT_PROFILES` — never a hardcoded
``if dialect == ...: ... elif ...`` chain re-implementing what sqlglot (SQL
shape) or :mod:`security.dialects` (per-dialect *data*: catalogue names,
session-timeout statements, schema-qualification style) already own. The
table/column allowlist itself (:data:`schema_data.columns.TABLE_COLUMNS`)
is dialect-agnostic — it is warehouse domain data, not SQL syntax — so it
needs no per-dialect variant.

The *default* dialect (``"tsql"``) is unaffected by any of this: every
call site that does not pass ``dialect=`` explicitly behaves exactly as it
did before this module supported other dialects, which is how
``tests/test_sql_guard.py`` and ``tests/test_sql_guard_bypass.py`` keep
passing for tsql without themselves being rewritten. See
:func:`transpile_and_revalidate` for how a *different* target dialect is
actually reached in production: this module never generates SQL in another
dialect, it only validates/caps whatever
``llm.sql_agent.SQLAgent`` hands it after transpiling the model's tsql
output with sqlglot.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from schema_data.columns import TABLE_COLUMNS
from security.dialects import get_dialect_profile

# ---------------------------------------------------------------------------
# Exception taxonomy
# ---------------------------------------------------------------------------
#
# Every rejection this module raises is a ``ValueError`` subclass, split
# into two flavours a caller that cares can switch on without inspecting
# message text -- specifically the self-correction loops in
# ``llm/sql_agent.py`` and ``session/engine.py``, which previously
# re-prompted the model up to ``max_corrections`` times for EVERY
# rejection alike, including ones no re-prompt could ever fix (a denied
# column, a forbidden statement type -- the policy that caused the
# rejection is not in the prompt, so the model has no way to produce a
# different outcome). Each blocked request used to cost
# ``max_corrections + 1`` LLM round trips and return the same rejection it
# would have returned immediately.


class SqlGuardRejection(ValueError):
    """Base class for every rejection this module raises.

    Subclasses :class:`ValueError` so every existing ``except ValueError``
    call site across the codebase (``api/``, ``llm/``, ``session/``,
    ``app.py``, and the test suite) keeps working unchanged -- this is a
    backward-compatible refinement of the exception type, not a new one
    replacing it.

    Two INDEPENDENT axes are modelled on this hierarchy, and a caller must
    not conflate them:

    * **Can a retry help?** -- the class itself
      (:class:`CorrectableRejection` vs. :class:`PolicyRejection`), which
      the two self-correction loops (``llm/sql_agent.py``,
      ``session/engine.py``) switch on to decide whether to keep
      re-prompting.
    * **Is this a refusal, or unusable output?** -- :attr:`is_refusal`,
      which a caller translating to an HTTP status (``api/runner.py``)
      switches on instead: ``True`` means "the query was syntactically
      fine but we refuse to run it" (400 ``FORBIDDEN_SQL``); ``False``
      means "nothing usable came out of the pipeline" (502
      ``INVALID_SQL_RESPONSE``).

    These two axes do not line up one-to-one. Every :class:`PolicyRejection`
    is necessarily a refusal (the class sets :attr:`is_refusal` to
    ``True`` for the whole class), but an unknown-table rejection is a
    counter-example on the :class:`CorrectableRejection` side: a retry
    can plausibly fix it (name a real table), yet it is *also* a refusal
    -- the table-allowlist violation, not the SQL's shape, is why it was
    rejected -- so that one raise site flips :attr:`is_refusal` to
    ``True`` on the specific instance despite its class default. Encoding
    the HTTP-status axis as a substring test on the message
    (``"Forbidden keyword" in str(exc)``) was the bug this attribute
    replaces: it happened to match most refusals only because their
    messages were written to start with that phrase, and silently
    mis-mapped the ones that weren't (``"System catalogue forbidden: ..."``
    had no such guarantee).
    """

    #: ``True`` if this rejection should be reported as an outright
    #: refusal (e.g. HTTP 400) rather than unusable model output (e.g.
    #: HTTP 502). Default ``False`` here; see the class docstring above
    #: for how :class:`PolicyRejection` and the unknown-table raise site
    #: override it.
    is_refusal: bool = False


class CorrectableRejection(SqlGuardRejection):
    """A rejection a corrected re-prompt can plausibly resolve.

    Raised for a malformed-but-fixable candidate: a syntax error, a
    MySQL-style ``LIMIT``, an empty or non-SQL response, a hallucinated
    table or column name, or this module's own inability to safely inject
    a row cap. A model told what went wrong and asked to try again can
    plausibly produce different, valid SQL on the next attempt -- this is
    exactly today's self-correction behaviour, preserved by this class.

    ``is_refusal`` stays ``False`` (this class's inherited default) for
    every raise site except one: an unknown table is *also* a refusal (a
    table-allowlist violation, not merely unusable SQL), so that specific
    instance has ``is_refusal`` set to ``True`` -- see its raise site in
    :func:`validate_sql` for the reasoning.
    """


class PolicyRejection(SqlGuardRejection):
    """A rejection no re-prompt can resolve, because the answer is "no".

    Raised when the *query itself* is categorically out of bounds
    regardless of how it is phrased: a forbidden statement type or
    keyword, a system-catalogue reference, a denied column, or any other
    construct this module refuses on principle rather than for being
    malformed. A caller's correction loop should treat this as terminal
    and stop re-prompting immediately: the policy that caused the
    rejection is not in the prompt, so a retry cannot change the outcome
    -- it would only spend an extra LLM round trip to reach the exact
    same rejection.

    Every instance is also a refusal by definition -- there is no
    PolicyRejection that is merely "unusable output" -- so ``is_refusal``
    is fixed ``True`` for the whole class.
    """

    is_refusal = True


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: SQL Server is this module's default and original target — every parse
#: and validation call defaults to this dialect rather than sqlglot's
#: generic default, so T-SQL-specific syntax (``TOP``, bracketed
#: identifiers, ``N''`` national string literals, ``+`` string
#: concatenation, ...) is understood instead of rejected as invalid. Every
#: public function that takes an explicit ``dialect`` keyword (see the
#: module docstring's "Multi-dialect" section) still defaults to this
#: constant, so a call site that never mentions ``dialect`` at all behaves
#: exactly as it did before this module supported any other target.
_DIALECT = "tsql"

_LIMIT_RE        = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)
_FENCE_RE        = re.compile(r"```(?:sql)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_SELECT_START_RE = re.compile(r"(SELECT|WITH)\b", re.IGNORECASE)
_TOP_DISTINCT_RE = re.compile(r"SELECT\s+TOP\s+(\d+)\s+DISTINCT", re.IGNORECASE)
_LIMIT_STRIP_RE  = re.compile(r"\s*\bLIMIT\s+\d+\b", re.IGNORECASE)

# ensure_top() tsql helpers — locate the query's structurally OUTERMOST
# SELECT instead of naively matching the first literal "SELECT" in the
# string (see ensure_top's docstring for why that matters for CTEs,
# subqueries, and DISTINCT ordering, and for why the tsql path keeps this
# text-based approach rather than the AST-based one used for every other
# dialect).
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
#: an ``exp.Anonymous`` function call). Dialect-agnostic: these are T-SQL
#: spellings, but refusing them by name costs nothing even when validating
#: another dialect's SQL, and a per-dialect *equivalent* remote/file-access
#: name is layered on top via
#: :attr:`security.dialects.DialectProfile.extra_dangerous_functions` (see
#: :func:`_is_dangerous_identifier`) rather than replacing this set.
_DANGEROUS_FUNCTION_NAMES = frozenset({"OPENROWSET", "OPENQUERY", "OPENDATASOURCE"})


def _is_dangerous_identifier(name: str, dialect: str = _DIALECT) -> bool:
    """True if *name* is a stored-procedure/extension prefix or remote-access function.

    ``xp_*``/``sp_*`` and :data:`_DANGEROUS_FUNCTION_NAMES` are checked for
    every dialect unconditionally (see that constant's docstring); *dialect*
    additionally pulls in that dialect's own
    :attr:`~security.dialects.DialectProfile.extra_dangerous_functions` —
    e.g. PostgreSQL's ``dblink*`` family, MySQL's ``LOAD_FILE`` — instead of
    a hardcoded ``if dialect == ...`` branch naming them inline here.
    """
    upper = name.upper()
    profile = get_dialect_profile(dialect)
    return (
        upper.startswith("XP_")
        or upper.startswith("SP_")
        or upper in _DANGEROUS_FUNCTION_NAMES
        or upper in profile.extra_dangerous_functions
    )


def _is_string_literal_operand(node: exp.Expression | None) -> bool:
    """True if *node* is unambiguously a string value: a string
    :class:`exp.Literal`, or a T-SQL national-character literal
    (``N'...'``, parsed by sqlglot as :class:`exp.National` wrapping an
    inner string ``Literal`` — a *separate* node type, not a
    ``Literal(is_string=True)`` itself, so it needs its own check here; see
    :func:`validate_sql`'s ``+``-concatenation rule, the one call site that
    needs this distinction).
    """
    if isinstance(node, exp.National):
        return True
    return isinstance(node, exp.Literal) and node.is_string


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
    CorrectableRejection
        A :class:`ValueError` subclass (see the module docstring's
        exception taxonomy):

        * If *raw* is empty or whitespace-only.
        * If no ``SELECT`` / ``WITH`` keyword is found after fence extraction.

        Both are re-promptable — an empty or non-SQL response is exactly
        the kind of mistake a corrected retry can fix — so neither is a
        :class:`PolicyRejection`.

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
    security.sql_guard.CorrectableRejection: Received empty SQL from model

    >>> clean_sql("No SQL here at all.")
    Traceback (most recent call last):
        ...
    security.sql_guard.CorrectableRejection: No SELECT / CTE found in model response: 'No SQL here at all.'
    """
    if not raw or not raw.strip():
        raise CorrectableRejection("Received empty SQL from model")

    fence_match = _FENCE_RE.search(raw)
    sql = fence_match.group(1) if fence_match else raw

    start = _SELECT_START_RE.search(sql)
    if not start:
        raise CorrectableRejection(f"No SELECT / CTE found in model response: {sql[:200]!r}")
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


def validate_sql(
    sql: str,
    *,
    denied_columns: Iterable[str] | None = None,
    dialect: str = _DIALECT,
) -> None:
    """Raise :class:`ValueError` if *sql* is not a safe, read-only SELECT query.

    Validation rules (checked in order)
    ------------------------------------
    1. **Not empty** — blank / whitespace-only SQL is always rejected.
    2. **No literal ``LIMIT`` — tsql only.** ``LIMIT`` is invalid T-SQL;
       T-SQL requires ``TOP``. This check runs **only when** ``dialect ==
       "tsql"`` — every other dialect this module supports treats ``LIMIT``
       as its own correct row-limiting syntax, so rejecting it there would
       refuse the majority of valid queries rather than close a bypass; see
       the module docstring's "Multi-dialect" section for why this single
       condition is not the dialect-branching ladder this module otherwise
       avoids. It is a raw-text check rather than an AST check even for
       tsql: sqlglot's ``tsql`` dialect parses ``LIMIT n`` and ``TOP n``
       into the *same* AST node, so the syntactic distinction this check
       cares about no longer exists once the query is parsed.
    3. **Parses as exactly one statement in the target dialect** — the
       query is parsed with `sqlglot <https://sqlglot.com/>`_ pinned to
       *dialect* (``"tsql"`` unless the caller passes another supported
       dialect — see :data:`security.dialects.DIALECT_PROFILES`). A syntax
       error, or more than one statement (sqlglot splits on ``;`` the same
       way the database driver would), is rejected outright — this alone
       rejects every stacked-statement bypass as a class, rather than
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
    7. **No system catalogue references** — *dialect*'s own system
       catalogues (per
       :attr:`security.dialects.DialectProfile.system_schemas` /
       :attr:`~security.dialects.DialectProfile.system_name_prefixes` —
       ``INFORMATION_SCHEMA``/``SYS`` for tsql, ``pg_catalog``/``pg_*`` for
       postgres, ``mysql``/``performance_schema`` for mysql,
       ``sqlite_master``/``sqlite_*`` for sqlite) are blocked (by
       schema/table AST node, not substring) to prevent schema enumeration.
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
    dialect:
        A sqlglot dialect key (see
        :data:`security.dialects.DIALECT_PROFILES`). Defaults to
        ``"tsql"`` — every existing call site that never passes this is
        completely unaffected by multi-dialect support. Pass
        :data:`config.Settings.sql_dialect` to validate SQL already
        transpiled to this deployment's target dialect (see
        :func:`transpile_and_revalidate`).

    Returns
    -------
    None
        Returns silently when the query is safe.

    Raises
    ------
    ValueError
        Always one of the two typed subclasses below (never a bare
        ``ValueError``), with a human-readable message describing the
        specific violation. Messages for a security-relevant rejection
        (forbidden statement kind, dangerous function, denied column, ...)
        contain the substring ``"Forbidden keyword"`` — callers (see
        ``api/runner.py``) rely on that substring to distinguish a
        security block from an ordinary malformed-SQL error.
    CorrectableRejection
        A malformed-but-fixable candidate a corrected re-prompt can
        plausibly resolve: bad syntax, a literal ``LIMIT``, an unresolvable
        table or column name, or a disallowed comment.
    PolicyRejection
        A candidate no re-prompt can resolve because the query itself is
        categorically out of bounds: a forbidden statement type or nested
        construct, a system-catalogue reference, or a denied column. The
        two self-correction loops (``llm/sql_agent.py``,
        ``session/engine.py``) stop retrying immediately on this one
        rather than spending the rest of their ``max_corrections`` budget
        re-asking a model that has no way to change the outcome.

    Examples
    --------
    >>> validate_sql("SELECT TOP 10 * FROM Customer")   # passes silently

    >>> validate_sql("")
    Traceback (most recent call last):
        ...
    security.sql_guard.CorrectableRejection: Empty SQL

    >>> validate_sql("DELETE FROM Contract")
    Traceback (most recent call last):
        ...
    security.sql_guard.PolicyRejection: Forbidden keyword detected: DELETE

    >>> validate_sql("DROP TABLE Contract")
    Traceback (most recent call last):
        ...
    security.sql_guard.PolicyRejection: Forbidden keyword detected: DROP

    >>> validate_sql("UPDATE Contract SET Price = 0")
    Traceback (most recent call last):
        ...
    security.sql_guard.PolicyRejection: Forbidden keyword detected: UPDATE

    >>> validate_sql("SELECT Price FROM Contract; DROP TABLE Contract")
    Traceback (most recent call last):
        ...
    security.sql_guard.PolicyRejection: Forbidden keyword detected: DROP

    >>> validate_sql("SELECT * FROM INFORMATION_SCHEMA.TABLES")
    Traceback (most recent call last):
        ...
    security.sql_guard.PolicyRejection: System catalogue forbidden: INFORMATION_SCHEMA

    >>> validate_sql("SELECT * FROM Contract LIMIT 10")
    Traceback (most recent call last):
        ...
    security.sql_guard.CorrectableRejection: LIMIT is not valid T-SQL — use TOP instead

    A column name that merely *contains* a legacy blocklist substring is no
    longer a false positive (contrast ``tests/test_sql_guard_bypass.py``,
    which documents this as a bug at the pre-Phase-1 baseline):

    >>> validate_sql("SELECT EXP_DATE FROM Customer")   # passes silently

    ``SELECT ... INTO`` is a write disguised as a read:

    >>> validate_sql("SELECT * INTO NewTbl FROM Contract")
    Traceback (most recent call last):
        ...
    security.sql_guard.PolicyRejection: Forbidden keyword detected: INTO

    A table this module does not recognise is refused, even though it
    starts with an otherwise-harmless ``SELECT`` — this does not depend on
    the database login also being scoped to just the known tables (see
    ``docs/db-hardening.md``):

    >>> validate_sql("SELECT * FROM HR_Payroll")
    Traceback (most recent call last):
        ...
    security.sql_guard.CorrectableRejection: Forbidden keyword detected: unknown table 'HR_Payroll' is not in the schema allowlist

    A comment is refused outright, regardless of what it says:

    >>> validate_sql("SELECT Price FROM Contract -- looks harmless")
    Traceback (most recent call last):
        ...
    security.sql_guard.CorrectableRejection: SQL comments are not allowed: a comment is not executable SQL syntax, so its content is never inspected for keywords -- the comment itself is refused outright.

    The optional column-level ACL seam:

    >>> validate_sql("SELECT Name FROM Customer", denied_columns={"Name"})
    Traceback (most recent call last):
        ...
    security.sql_guard.PolicyRejection: Forbidden keyword detected: denied column 'Name'

    ``*`` cannot be used to read around an active column policy:

    >>> validate_sql("SELECT * FROM Customer", denied_columns={"Name"})
    Traceback (most recent call last):
        ...
    security.sql_guard.PolicyRejection: Forbidden keyword detected: '*' would expose denied column(s): ['name']
    """
    if not sql or not sql.strip():
        raise CorrectableRejection("Empty SQL")

    if dialect == "tsql" and _LIMIT_RE.search(sql):
        raise CorrectableRejection("LIMIT is not valid T-SQL — use TOP instead")

    profile = get_dialect_profile(dialect)

    try:
        raw_statements = sqlglot.parse(sql, read=dialect)
    except SqlglotError as exc:
        raise CorrectableRejection(f"SQL syntax error: {exc}") from exc

    statements = [stmt for stmt in raw_statements if stmt is not None]
    if not statements:
        raise CorrectableRejection("Empty SQL")

    if len(statements) > 1:
        # Stacking is refused as a class regardless of content -- a known
        # SQL-injection bypass vector, not a formatting mistake a retry
        # could plausibly avoid by chance -- so both branches below are a
        # PolicyRejection. Prefer naming the specific offending statement
        # kind when one of the extra statements is recognisably dangerous
        # (matches the pre-Phase-1 message shape for these cases); fall
        # back to a generic message when every statement individually
        # looks benign (e.g. "SELECT 1; SELECT 2").
        for stmt in statements:
            label = _forbidden_label(stmt)
            if label is not None:
                raise PolicyRejection(f"Forbidden keyword detected: {label}")
        raise PolicyRejection(
            "Forbidden keyword detected: multiple SQL statements are not "
            f"allowed ({len(statements)} statements found)"
        )

    tree = statements[0]

    root_label = _forbidden_label(tree)
    if root_label is not None:
        raise PolicyRejection(f"Forbidden keyword detected: {root_label}")
    if not isinstance(tree, _ALLOWED_ROOT_TYPES):
        raise PolicyRejection(
            "Forbidden keyword detected: only SELECT / WITH / set-operation "
            f"queries are allowed, got {type(tree).__name__}"
        )

    cte_names = _cte_names(tree)

    for node in tree.walk():
        label = _forbidden_label(node)
        if label is not None:
            raise PolicyRejection(f"Forbidden keyword detected: {label}")

        # A comment is refused because it is present, not for what it
        # says — scanning its text for keywords would just relocate the
        # substring-matching mistake this module exists to get away from
        # (see the module docstring). This intentionally does not reuse
        # the "Forbidden keyword" phrasing: no keyword was found, the
        # comment itself is the thing being refused. Classified as
        # CorrectableRejection, not PolicyRejection: unlike a denied
        # column or a forbidden statement, nothing about the *question*
        # is out of bounds here -- the model can drop the comment and
        # answer the exact same question on the very next attempt.
        if node.comments:
            raise CorrectableRejection(
                "SQL comments are not allowed: a comment is not "
                "executable SQL syntax, so its content is never inspected "
                "for keywords -- the comment itself is refused outright."
            )

        # T-SQL national-character literal (N'...') on a dialect that
        # rejects it -- the finding that shapes this entire phase, found
        # by direct execution against a real SQLite engine, not by
        # reading: sqlglot's transpile leaves N'...' COMPLETELY UNCHANGED
        # for every target dialect (it is not converted to a plain string
        # literal), and its own parser silently ACCEPTS N'...' as valid
        # input regardless of the `read=` dialect given -- so a naive
        # "does the transpiled text still parse" check (this module's
        # original re-validation plan) does not actually catch this at
        # all; only an explicit rule does. Essentially every query this
        # deployment generates filters on a Persian name, so essentially
        # every query carries one of these -- this is not a rare edge
        # case. See security.dialects.DialectProfile.supports_national_literal
        # for exactly which dialects accept it (tsql, MySQL) and which
        # reject it as a syntax error (PostgreSQL, SQLite).
        if isinstance(node, exp.National) and not profile.supports_national_literal:
            raise CorrectableRejection(
                f"Forbidden keyword detected: T-SQL national-character "
                f"literal ({node.sql(dialect=dialect)!r}) is not valid "
                f"{dialect} syntax -- sqlglot leaves N'...' unchanged "
                "under transpilation and its parser accepts it regardless "
                "of dialect, so this is refused explicitly rather than "
                "left to fail at the database"
            )

        # T-SQL string-concatenation-via-`+`, transpiled unchanged into a
        # dialect where `+` is exclusively numeric addition -- found by
        # direct execution against SQLite, not by reading: sqlglot parses
        # `Name + ' (' + NationalID + ')'` as a generic arithmetic
        # ``exp.Add`` expression in every dialect (T-SQL overloads `+` for
        # both addition and concatenation with no distinct AST node), so
        # transpiling it produces *identical*, syntactically valid-looking
        # `+` text on the target dialect -- which then either raises a
        # runtime type error (PostgreSQL) or, worse, silently coerces a
        # non-numeric string operand to ``0`` and returns a plausible
        # number instead of failing (confirmed for SQLite:
        # ``'foo' + 'bar'`` evaluates to ``0``, not an error) -- exactly
        # the "looks right, means something different" failure class this
        # phase's verification exists to catch. Refusing every ``+`` is
        # not viable (ordinary numeric addition, e.g. ``TotalPrice + Fee``,
        # is common and legitimate), so this refuses only the *unambiguous*
        # case: a ``+`` with a string-literal operand, which is never a
        # legitimate numeric expression regardless of dialect. This does
        # NOT catch the harder case of concatenating two *columns* typed
        # as text with no literal present (``Name + NationalID``) --
        # schema.yaml carries no column-type metadata this module could
        # use to distinguish that from real numeric addition; that residual
        # gap is reported, not silently left undocumented, in this phase's
        # report. Scoped to non-tsql dialects only: this is exactly valid,
        # correct T-SQL and must not be refused when *dialect* is "tsql".
        if (
            dialect != "tsql"
            and isinstance(node, exp.Add)
            and (_is_string_literal_operand(node.this) or _is_string_literal_operand(node.expression))
        ):
            raise CorrectableRejection(
                "Forbidden keyword detected: '+' with a string-literal "
                f"operand ({node.sql(dialect=dialect)!r}) is T-SQL string "
                f"concatenation, which does not transpile to {dialect} -- "
                "this dialect's '+' is exclusively numeric addition and "
                "would silently coerce a string operand instead of "
                "concatenating it"
            )

        if isinstance(node, exp.Anonymous) and _is_dangerous_identifier(node.name or "", dialect):
            raise PolicyRejection(f"Forbidden keyword detected: {(node.name or '').upper()}")

        if isinstance(node, exp.Table):
            raw_name = node.name
            if _is_dangerous_identifier(raw_name or "", dialect):
                raise PolicyRejection(f"Forbidden keyword detected: {raw_name.upper()}")
            db = (node.db or "").upper()
            name_upper = (raw_name or "").upper()
            # System-catalogue check for *this* dialect: an exact-match
            # schema/db qualifier (INFORMATION_SCHEMA, pg_catalog, mysql,
            # sqlite_master, ...) or a by-convention name PREFIX
            # (postgres' pg_*, sqlite's sqlite_*) -- see
            # security.dialects.DialectProfile's docstring for why these
            # are two separate fields rather than one.
            if db in profile.system_schemas:
                raise PolicyRejection(f"System catalogue forbidden: {db}")
            if not db and name_upper in profile.system_schemas:
                raise PolicyRejection(f"System catalogue forbidden: {name_upper}")
            if any(db.startswith(prefix) for prefix in profile.system_name_prefixes):
                raise PolicyRejection(f"System catalogue forbidden: {db}")
            if any(name_upper.startswith(prefix) for prefix in profile.system_name_prefixes):
                raise PolicyRejection(f"System catalogue forbidden: {name_upper}")

            # Table allowlist: unlike the column check below, this is not
            # lenient. A raw_name of "" (an exp.Anonymous table-valued
            # function, e.g. OPENROWSET(...) or sp_who()) is skipped here
            # -- it is already refused above, either by the dangerous
            # function-name checks or because it can never match
            # TABLE_COLUMNS anyway once it does something real.
            if raw_name and raw_name.lower() not in cte_names:
                if _TABLE_LOOKUP.get(raw_name.lower()) is None:
                    # CorrectableRejection: a hallucinated or misspelled
                    # table name is exactly the kind of small-model mistake
                    # a retry can plausibly fix by naming a real table --
                    # unlike a denied column, nothing here says the
                    # question itself may not be asked. It is, however,
                    # STILL a refusal on the other axis (is_refusal): the
                    # allowlist violation, not the SQL's shape, is why this
                    # was rejected, so this specific instance overrides the
                    # class default -- see SqlGuardRejection's docstring.
                    exc = CorrectableRejection(
                        f"Forbidden keyword detected: unknown table "
                        f"'{raw_name}' is not in the schema allowlist"
                    )
                    exc.is_refusal = True
                    raise exc

    denied = frozenset(c.upper() for c in denied_columns) if denied_columns else frozenset()
    alias_map = _collect_table_alias_map(tree, cte_names)

    for col in tree.find_all(exp.Column):
        name = col.name
        if not name or name == "*":
            continue
        if name.upper() in denied:
            raise PolicyRejection(f"Forbidden keyword detected: denied column '{name}'")

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
            # CorrectableRejection: same reasoning as the unknown-table
            # case above -- a hallucinated column name, plausibly fixed
            # by a retry that names a real one.
            raise CorrectableRejection(f"Unknown column '{name}' on table '{canonical}'")

    if denied:
        # "*" must not be usable to silently read around an active
        # column-denial policy -- expand it against whatever it resolves
        # to and check the expansion, or refuse it outright if it can't be
        # resolved with confidence (see _resolve_star_tables). Both
        # branches enforce the same denied-column policy as the named-
        # column check above, so both are PolicyRejection: no rewrite of
        # the query can make the underlying question answerable without
        # touching the denied column.
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
                raise PolicyRejection(
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
                raise PolicyRejection(
                    f"Forbidden keyword detected: '{ref_label}' would "
                    f"expose denied column(s): {exposed_denied}"
                )


def extract_touched_tables(sql: str, dialect: str = _DIALECT) -> list[str]:
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
    dialect:
        A sqlglot dialect key. Defaults to ``"tsql"``. Pass the target
        dialect when calling this on already-transpiled SQL (see
        :func:`transpile_and_revalidate`, which uses this function on
        *both* the source and transpiled text to confirm transpilation
        did not silently change which tables a query touches).

    Returns
    -------
    list[str]
        Canonical table names (as they appear in ``TABLE_COLUMNS``),
        de-duplicated and sorted. Empty if *sql* fails to parse, is
        empty, or references no table this module recognises.

    Examples
    --------
    >>> extract_touched_tables("SELECT * FROM Customer")
    ['Customer']

    >>> extract_touched_tables(
    ...     "SELECT c.Name FROM Customer c JOIN [Order] o ON o.CustomerId = c.Id"
    ... )
    ['Customer', 'Order']

    A CTE reference is not a table:

    >>> extract_touched_tables("WITH cte AS (SELECT * FROM Customer) SELECT * FROM cte")
    ['Customer']

    Unparsable or table-free SQL degrades to an empty list, never raises:

    >>> extract_touched_tables("not valid sql at all")
    []
    >>> extract_touched_tables("")
    []
    """
    try:
        raw_statements = sqlglot.parse(sql, read=dialect)
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


def _setop_branches(node: exp.Expression) -> list[exp.Expression]:
    """Flatten a (possibly nested) set-operation tree into its immediate branches.

    ``a UNION b UNION c`` parses as a left-leaning tree of nested
    :class:`exp.Union` nodes (``Union(this=Union(this=a, expression=b),
    expression=c)``); this returns ``[a, b, c]`` — the query's own
    top-level ``SELECT`` branches — without descending into a branch's
    *own* ``FROM`` source, so a derived table's inner query (which sits at
    a real nesting level, inside a :class:`exp.Subquery`) is never
    mistaken for one of the set operation's own branches. Used by
    :func:`ensure_top` to correctly distinguish a top-level trailing
    ``ORDER BY`` (attaches to the last branch reached this way) from one
    that belongs to a branch's own subquery (invisible to this walk,
    exactly as it should be).
    """
    if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
        return _setop_branches(node.this) + _setop_branches(node.expression)
    return [node]


def _ensure_top_ast(sql: str, n: int, dialect: str) -> str:
    """AST-based row cap for every dialect except tsql — see :func:`ensure_top`.

    Parses *sql* with sqlglot pinned to *dialect*, reads whether a
    row-limiting clause already exists directly off the tree's own
    ``limit`` argument (covers ``TOP``/``LIMIT``/etc. uniformly — no
    per-dialect clause name to hardcode), and — when one is missing — adds
    it via ``exp.Select.limit()`` and renders the **whole** tree with
    ``Expression.sql(dialect=dialect)``. This is unsafe to use for tsql
    (see :func:`ensure_top`'s docstring for why) but is exactly right for
    every other dialect: there is no pre-existing byte-exact production
    text to preserve for a dialect this deployment never targeted before,
    and a full, correct AST render is strictly better than a second
    hand-written paren-depth scanner per dialect.
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError as exc:
        # CorrectableRejection: a parse failure here is a mismatch between
        # this function and what validate_sql's own parser already
        # accepted as well-formed *dialect* SQL -- not a policy call about
        # the question itself, so a differently-structured (but still
        # valid) retry can plausibly get past it.
        raise CorrectableRejection(
            f"ensure_top: {sql[:200]!r} does not parse as {dialect} SQL "
            f"({exc}) — refusing to guess where to inject a row cap"
        ) from exc

    if isinstance(tree, exp.Select):
        if tree.args.get("limit") is not None:
            return sql  # outermost query already capped -- untouched, byte-identical
        capped = tree.copy()
        capped.limit(n, copy=False)
        return capped.sql(dialect=dialect)

    if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
        if tree.args.get("limit") is not None:
            return sql  # outermost query already capped -- untouched, byte-identical

        # A trailing top-level ORDER BY attaches to different nodes
        # depending on dialect -- confirmed by direct parsing, not
        # assumed: tsql attaches it to the rightmost branch's own Select
        # (see the tsql-only text-splicing path's _has_top_level_order_by
        # helper), while postgres/mysql/sqlite attach it directly to the
        # Union/Intersect/Except node itself (`tree.args["order"]`). Both
        # locations must be checked -- checking only one silently missed
        # the other for exactly the dialects this function is used for.
        branches = _setop_branches(tree)
        has_order_by = tree.args.get("order") is not None or any(
            isinstance(branch, exp.Select) and branch.args.get("order")
            for branch in branches
        )
        if has_order_by:
            # CorrectableRejection: same reasoning as the parse-failure
            # branch above -- a query shaped without a top-level ORDER BY
            # on the set operation would sail through unaffected, so this
            # is a fixable structural limitation of this function, not a
            # refusal of the question.
            raise CorrectableRejection(
                "ensure_top: cannot safely cap a UNION/INTERSECT/EXCEPT "
                "query that also has a top-level ORDER BY (wrapping it in "
                "a derived table would make that ORDER BY invalid) — "
                "refusing to emit unsafe SQL"
            )

        # Detach any CTEs before wrapping (they belong on the new OUTER
        # query, not re-attached to the inner derived table -- sqlglot
        # would happily render either, but only the former is valid SQL:
        # a CTE must be visible to the query that references its alias).
        with_ = tree.args.get("with")
        core = tree.copy()
        core.set("with", None)
        wrapped = exp.select("*").from_(core.subquery("_ensure_top_capped")).limit(n)
        if with_ is not None:
            wrapped.set("with", with_.copy())
        return wrapped.sql(dialect=dialect)

    # CorrectableRejection: validate_sql's own root-type check already
    # refuses anything that isn't SELECT/set-operation before this
    # function is ever reached in the real pipeline -- reaching this
    # branch means a caller invoked ensure_top() directly on something
    # validate_sql never approved, which is a caller mistake a retry with
    # correctly-shaped SQL can plausibly fix.
    raise CorrectableRejection(
        f"ensure_top: {sql[:200]!r} is not a SELECT/WITH/set-operation "
        f"query in {dialect} (parsed as {type(tree).__name__ if tree else 'nothing'}) "
        "— refusing to guess where to inject a row cap"
    )


def ensure_top(sql: str, n: int = 100, dialect: str = _DIALECT) -> str:
    """Inject a row cap into *sql* if no row-limiting clause is already present.

    This is a safety net applied **after** :func:`validate_sql` to guarantee
    the database never returns an unbounded result set to the API layer,
    regardless of whether the model's own SQL included one.

    Two implementations, chosen by *dialect* — and why
    -------------------------------------------------------
    This function was rewritten for multi-dialect support to use a genuine
    sqlglot AST cap (``exp.Select.limit()``, rendered with
    ``Expression.sql(dialect=...)``) instead of the original hand-written
    paren-depth / string-literal-aware scanner — that scanner is real,
    tested, and correct, but it is also "a parser with extra steps" (see
    the module docstring), and it would need to grow a second, per-dialect
    opinion about row-limiting syntax to serve any target besides tsql.

    That AST rewrite was verified — by running it, not by reading it —
    against every existing test in ``tests/test_sql_guard.py`` and
    ``tests/test_sql_guard_bypass.py``, and it changes the **rendered
    text** of any query that (a) actually needs a cap injected (not
    already capped) **and** (b) contains an unaliased CTE/derived-table
    column (``SELECT 1`` inside a CTE becomes ``SELECT 1 AS [1]``) **or**
    any operator sqlglot re-spaces on render (``Active=1`` becomes
    ``Active = 1``). For *tsql specifically*, that is not a narrow edge
    case confined to this function's own tests: ``clean_sql``'s
    LIMIT-to-TOP conversion also calls this function, so the same
    reformatting leaked into ordinary ``clean_sql`` output too (e.g.
    ``WHERE Active=1`` silently became ``WHERE Active = 1``) — semantically
    identical SQL, but a **different string**, for what is the common case
    in production (any generated query that doesn't already carry its own
    ``TOP``). That is a real behavioural change for this deployment's
    default and only dialect before this phase, with consequences beyond
    this module's own tests (cache keys, audit-log SQL text, golden-set
    fixtures) — not something "keep the existing tests passing unchanged
    for tsql" can be read to permit.

    So: **tsql keeps the original text-splicing implementation, completely
    unchanged** (delegated to the module-private helpers this function
    used before this phase — see their docstrings), and the new AST-based
    cap (:func:`_ensure_top_ast`) is used for every *other* dialect, where
    there is no pre-existing byte-exact production text to protect and a
    full AST render is unambiguously the right tool. This is the same
    shape of exception as the tsql-only ``LIMIT`` rejection in
    :func:`validate_sql`: one condition on *dialect*, not a per-dialect
    branching ladder, kept deliberately narrow and explained at the one
    place it exists.

    Behaviour (both implementations)
    ------------------------------------
    * **Plain query** — a row cap is added to the outermost query.
    * **CTE** (``WITH x AS (...) SELECT ...``) — the cap lands on the
      outer query following the CTE definition(s), not the CTE body's own
      ``SELECT``.
    * **Subquery** (``SELECT ... FROM (SELECT TOP n ...) z``) — the outer
      query is still capped even though a subquery already has its own
      row-limiting clause.
    * **Already capped** — if the outermost query already has a
      row-limiting clause, *sql* is returned **completely unchanged**,
      preserving both an existing limit even if higher than *n* and the
      input's exact original text.
    * **Top-level** ``UNION``/``INTERSECT``/``EXCEPT`` — a cap on only the
      first branch would cap that branch, not the combined result
      actually returned, so the whole (post-CTE) query is wrapped in a
      capped outer ``SELECT * FROM (...) AS _ensure_top_capped`` instead.

    Parameters
    ----------
    sql:
        A validated SELECT (or WITH/CTE, or set-operation) query string in
        *dialect*.
    n:
        The row cap to inject when no row-limiting clause is present.
        Defaults to ``100``. Pass ``cfg.settings.default_top_n`` to use the
        application-level default.
    dialect:
        A sqlglot dialect key (see
        :data:`security.dialects.DIALECT_PROFILES`). Defaults to
        ``"tsql"`` — every call site that never passes this argument is
        completely unaffected by multi-dialect support, byte-for-byte.

    Returns
    -------
    str
        *sql*, either unchanged (the outermost query already has a
        row-limiting clause) or with a cap injected — or, for a top-level
        set operation, wrapped in a new capped outer query.

    Raises
    ------
    CorrectableRejection
        A :class:`ValueError` subclass (see the module docstring's
        exception taxonomy) — neither case below is a policy refusal of
        the question, just a structural limit of this function, so a
        differently-shaped retry can plausibly get past it:

        * If no top-level query can be found/parsed at all.
        * If *sql* is a top-level ``UNION``/``INTERSECT``/``EXCEPT`` that
          also has a top-level ``ORDER BY`` — wrapping it in a derived
          table would make that ``ORDER BY`` invalid, and correctly
          hoisting it out of the wrapper is out of scope for a row-cap
          injector.

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

    >>> ensure_top("SELECT a FROM t1 LIMIT 5", n=50, dialect="postgres")
    'SELECT a FROM t1 LIMIT 5'

    >>> ensure_top("SELECT a FROM t1", n=5, dialect="postgres")
    'SELECT a FROM t1 LIMIT 5'
    """
    if dialect != "tsql":
        return _ensure_top_ast(sql, n, dialect)

    matches = _top_level_select_matches(sql)
    if not matches:
        # CorrectableRejection: this is a mismatch between the regex-based
        # scanner here and what validate_sql's parser already accepted as
        # a well-formed query -- not a policy call about the question
        # itself, so a differently-structured (but still valid) retry can
        # plausibly get past it.
        raise CorrectableRejection(
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
            # CorrectableRejection: same reasoning as above -- a query
            # shaped without a top-level ORDER BY on the set operation
            # would sail through, so this is a fixable structural
            # limitation of this function, not a refusal of the question.
            raise CorrectableRejection(
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


# ---------------------------------------------------------------------------
# Multi-dialect pipeline: transpile the model's tsql output, then re-verify
# it in the dialect it will actually run in.
# ---------------------------------------------------------------------------

def _strip_unsupported_national_literals(node: exp.Expression, dialect: str) -> exp.Expression:
    """Rewrite ``exp.National`` (T-SQL's ``N'...'``) to a plain string literal.

    Used by :func:`transpile_sql` as a targeted fix for this phase's
    headline finding: sqlglot's transpile leaves ``N'...'`` **completely
    unchanged** for every target dialect — it is not converted to a plain
    string literal — even though a national-character literal is
    semantically *just a string* on a natively-Unicode database that has
    no separate national character set to distinguish it from (PostgreSQL,
    SQLite both store ``TEXT``/``VARCHAR`` as UTF-8 natively; there is
    nothing for the ``N`` prefix to opt into). Dropping the prefix is
    therefore not a lossy approximation, it is the *correct* rendering —
    unlike the ``+``-concatenation gap (also found in this phase; see
    :func:`validate_sql`), there is no ambiguity here to preserve by
    refusing instead of rewriting.

    Only applied when :attr:`~security.dialects.DialectProfile.supports_national_literal`
    is ``False`` for *dialect* (PostgreSQL, SQLite) — leaves the tree
    completely alone for a dialect that accepts ``N'...'`` natively (tsql,
    MySQL), so this function is a no-op there by construction, not by a
    special-cased early return.
    """
    if get_dialect_profile(dialect).supports_national_literal:
        return node
    if isinstance(node, exp.National):
        inner = node.this
        value = inner.this if isinstance(inner, exp.Literal) else str(inner)
        return exp.Literal.string(value)
    return node


def transpile_sql(sql: str, *, target_dialect: str, source_dialect: str = _DIALECT) -> str:
    """Transpile *sql* from *source_dialect* to *target_dialect* with sqlglot.

    Parses *sql* with sqlglot pinned to *source_dialect*, applies exactly
    one targeted rewrite — :func:`_strip_unsupported_national_literals`,
    converting a T-SQL national-character literal to a plain string
    literal when *target_dialect* cannot parse the former (see that
    function's docstring for why this is a correct rewrite, not a lossy
    one) — and renders the result with *target_dialect*'s own syntax.
    Every other construct is handled entirely by sqlglot's own dialect
    support (see the module docstring's "Do not build a dialect layer"
    principle): this function does not implement a second, hand-written
    opinion about SQL shape, it adds one narrow, config-keyed correction
    for a gap sqlglot itself does not close.

    Every rejection is a typed :class:`CorrectableRejection`, not a bare
    :class:`SqlglotError`, matching every other guard function in this
    module, so a caller (see :func:`transpile_and_revalidate`) does not
    need a second exception type to catch.

    Parameters
    ----------
    sql:
        SQL text in *source_dialect*. Should already have passed
        :func:`validate_sql` (pinned to *source_dialect*) and
        :func:`ensure_top` — this function has no opinion about whether
        *sql* is safe, only about whether it transpiles.
    target_dialect:
        The sqlglot dialect key to render *sql* as.
    source_dialect:
        The sqlglot dialect key *sql* is written in. Defaults to
        ``"tsql"`` — the one dialect the model ever generates (see
        :data:`config.Settings.sql_dialect`'s docstring).

    Returns
    -------
    str
        *sql* re-rendered in *target_dialect*'s own syntax.

    Raises
    ------
    CorrectableRejection
        If *sql* fails to parse as *source_dialect*. This is a
        :class:`CorrectableRejection`, not a :class:`PolicyRejection`: a
        transpilation failure is a property of this *specific* SQL shape,
        not of the question being categorically out of bounds — a
        differently-phrased retry that happens to generate an equivalent
        but more portable query can plausibly succeed where this one
        failed.

    Examples
    --------
    >>> transpile_sql("SELECT TOP 10 * FROM t", target_dialect="postgres")
    'SELECT * FROM t LIMIT 10'

    >>> transpile_sql("SELECT [Name] FROM [Customer]", target_dialect="sqlite")
    'SELECT "Name" FROM "Customer"'

    A national-character literal is rewritten to a plain string literal
    for a dialect that would otherwise reject it as a syntax error:

    >>> transpile_sql("SELECT * FROM t WHERE x = N'abc'", target_dialect="sqlite")
    "SELECT * FROM t WHERE x = 'abc'"

    ...but left completely untouched for a dialect that accepts it
    natively:

    >>> transpile_sql("SELECT * FROM t WHERE x = N'abc'", target_dialect="mysql")
    "SELECT * FROM t WHERE x = N'abc'"
    """
    try:
        tree = sqlglot.parse_one(sql, read=source_dialect)
    except SqlglotError as exc:
        raise CorrectableRejection(
            f"transpile_sql: failed to parse {sql[:200]!r} as "
            f"{source_dialect}: {exc}"
        ) from exc

    if tree is None:
        raise CorrectableRejection(
            f"transpile_sql: parsing {sql[:200]!r} as {source_dialect} "
            "produced no statement"
        )

    rewritten = tree.transform(_strip_unsupported_national_literals, target_dialect)

    try:
        return rewritten.sql(dialect=target_dialect)
    except SqlglotError as exc:
        raise CorrectableRejection(
            f"transpile_sql: failed to render {sql[:200]!r} as "
            f"{target_dialect}: {exc}"
        ) from exc


def transpile_and_revalidate(
    sql: str,
    *,
    target_dialect: str,
    source_dialect: str = _DIALECT,
    denied_columns: Iterable[str] | None = None,
) -> str:
    """Transpile *sql* to *target_dialect* and refuse it unless re-verified.

    This is the multi-dialect phase's central safety rule, stated in the
    phase report as: *never execute anything the guard has not approved in
    the dialect it will actually run in*. Looking "transpiled and correct"
    is not enough — sqlglot transpiles the vast majority of this
    project's generated SQL flawlessly (``TOP`` → ``LIMIT``/``FETCH FIRST``,
    bracketed identifiers → the target's own quoting, ``ISNULL`` →
    ``COALESCE``, ...) but was found, by direct execution against a real
    SQLite engine, to leave a T-SQL national-character literal
    (``N'...'``) completely untouched in every dialect — syntactically
    invalid on PostgreSQL/SQLite, silently accepted (but not necessarily
    meaning the same thing) on MySQL/Oracle. A transpiled string that
    merely *parses* is not evidence it is safe or correct; only a second
    pass through the guard, pinned to the dialect that will actually
    execute it, is.

    Three steps, each of which can refuse
    -------------------------------------------
    1. **Transpile** — :func:`transpile_sql` from *source_dialect* to
       *target_dialect*. A parse/transpile failure raises
       :class:`CorrectableRejection` (see that function's docstring).
    2. **Re-validate in the target dialect** — :func:`validate_sql` runs
       again, this time pinned to *target_dialect* (its own system
       catalogues, its own dangerous-function names, and — critically —
       *not* rejecting a literal ``LIMIT``, which is *target_dialect*'s
       own correct syntax whenever it is not ``"tsql"``). Anything the
       guard would refuse in the source dialect that somehow survived
       transpilation unrecognisable, or any construct the transpile step
       introduced that the target dialect's own rules forbid, is caught
       here exactly as it would be for freshly-generated SQL.
    3. **Touched-table set must be identical** — :func:`extract_touched_tables`
       is compared between *sql* (in *source_dialect*) and the transpiled
       text (in *target_dialect*). A mismatch means transpilation silently
       changed which tables the query reads — the single scenario this
       module considers worse than an outright error (a plausible-looking
       result computed over the wrong data) — and is refused.

    When *target_dialect* equals *source_dialect* (this deployment's
    default, ``"tsql"``), this function is a **no-op passthrough**: it
    returns *sql* completely unchanged, without calling
    :func:`transpile_sql` or re-running :func:`validate_sql` at all. This
    is deliberate, not an optimisation shortcut — see the module
    docstring's "Multi-dialect" section and
    :data:`config.Settings.sql_dialect`'s docstring: a tsql-only
    deployment (this project's only target before this phase, and still
    its default) must see *zero* behavioural change from this function
    existing. ``tests/test_sql_guard.py``/``tests/test_sql_guard_bypass.py``
    passing unchanged, and the tsql guard allowlist diffing empty
    before/after this phase, both depend on that no-op path being taken
    unconditionally for ``target_dialect == "tsql"`` — never merely
    "usually produces the same result".

    Parameters
    ----------
    sql:
        Guard-approved, capped SQL in *source_dialect* — the output of
        :func:`validate_sql` + :func:`ensure_top` on the model's own
        response.
    target_dialect:
        The sqlglot dialect key this deployment's database actually
        speaks — :data:`config.Settings.sql_dialect`.
    source_dialect:
        The sqlglot dialect key *sql* is written in. Defaults to
        ``"tsql"`` — the one dialect the model ever generates.
    denied_columns:
        Forwarded to the target-dialect :func:`validate_sql` call
        unchanged — see that function's own docstring.

    Returns
    -------
    str
        *sql* unchanged (``target_dialect == source_dialect``), or the
        transpiled, re-validated, table-set-checked equivalent in
        *target_dialect*.

    Raises
    ------
    CorrectableRejection
        If transpilation fails, if the transpiled SQL fails re-validation
        with a rejection class that is itself a ``CorrectableRejection``,
        or if the touched-table set changed under transpilation.
    PolicyRejection
        If the transpiled SQL fails re-validation with a rejection class
        that is itself a ``PolicyRejection`` (propagated as-is — a
        forbidden construct is exactly as terminal post-transpile as it
        was pre-transpile).
    """
    if target_dialect == source_dialect:
        return sql

    transpiled = transpile_sql(
        sql, source_dialect=source_dialect, target_dialect=target_dialect
    )

    # Re-validate in the dialect this will actually execute in -- never
    # trust that "parses" means "safe" (see this function's own docstring
    # for the N'...' finding that makes this step non-negotiable).
    validate_sql(transpiled, denied_columns=denied_columns, dialect=target_dialect)

    source_tables = set(extract_touched_tables(sql, dialect=source_dialect))
    transpiled_tables = set(extract_touched_tables(transpiled, dialect=target_dialect))
    if source_tables != transpiled_tables:
        # CorrectableRejection, not PolicyRejection: this is a property of
        # this specific SQL shape's transpilation, not a categorical
        # policy violation -- a differently-phrased retry that generates
        # an equally-valid but more portable query can plausibly transpile
        # cleanly where this one did not.
        raise CorrectableRejection(
            f"transpile_and_revalidate: transpiling from {source_dialect} "
            f"to {target_dialect} changed the set of tables this query "
            f"touches ({sorted(source_tables)} -> {sorted(transpiled_tables)}) "
            "— refusing to execute SQL whose meaning may have changed under "
            "transpilation"
        )

    return transpiled
