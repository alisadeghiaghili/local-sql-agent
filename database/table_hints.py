# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Add ``WITH (NOLOCK)`` to every physical table a T-SQL query reads.

Some warehouses are shared with loaders and report jobs whose locks a DBA
does not want an analytical, read-only application to wait on, and the
DBA's rule is that every read of a table carries ``WITH (NOLOCK)``. A data
source opts in with ``nolock: true`` in ``datasources.yaml`` (see
:mod:`database.datasources`); :func:`database.executor.execute_sql` then
passes the statement through :func:`add_nolock_hints` just before it is
sent to the server. ``NOLOCK`` reads uncommitted data (dirty reads: rows
mid-update, rows that are later rolled back, occasionally a row twice or
not at all while pages split). Turning it on is the operator's decision,
made per data source.

Text is preserved, not regenerated
----------------------------------
The statement is **not** rendered back from a sqlglot tree. Re-generating
SQL can re-space operators, re-case keywords, rewrite ``TOP``/``OFFSET`` or
change a literal's form, and the guard has already approved the exact
text the model wrote (see ``security/sql_guard.py``). Instead the
statement is parsed only to *find* the table references, and the text
``" WITH (NOLOCK)"`` is inserted at one offset per table in the original
string; every other character, comment and literal is left as it was.

Where the offsets come from
---------------------------
sqlglot records ``start`` and ``end`` (character offsets, ``end`` inclusive)
in the ``meta`` of every ``Identifier`` it reads from the token stream,
including the parts of a multi-part name and a table alias. The hint
belongs after the alias when there is one, so the insertion point is one
past the last of the reference's own identifiers (``catalog``, ``db``,
``this`` and the alias). Each offset is checked against the text it
points at before it is used (a bracket or quote at the end of a quoted
name, the identifier's own spelling otherwise), so a sqlglot release that
changed what ``meta`` means turns into the fallback below, not into a hint
in the wrong place.

T-SQL grammar::

    table_or_view_name [ [AS] table_alias ] [ WITH ( <table_hint> [,...n] ) ]

so ``[A].[B] c JOIN [D].[E] AS d ON ...`` becomes
``[A].[B] c WITH (NOLOCK) JOIN [D].[E] AS d WITH (NOLOCK) ON ...``.

What is hinted, and what is not
-------------------------------
Hinted: a table in ``FROM`` or any kind of ``JOIN``, ``APPLY`` source,
subquery, CTE body, branch of ``UNION``/``EXCEPT``/``INTERSECT``, ``IN
(SELECT ...)`` or ``EXISTS``; any spelling of the name (``[A].[B]``,
``A.B``, ``[Other Db].[dbo].[T x]``).

Left alone, because a hint there is wrong or meaningless:

* a reference to a CTE name (``WITH x AS (...) SELECT * FROM x``);
* derived tables ``(SELECT ...) x``, ``VALUES`` lists, table-valued
  functions (``dbo.f(1)``, ``STRING_SPLIT``, ``OPENJSON``) and four-part
  linked-server names;
* table variables (``@t``) and temporary tables (``#t``, ``##t``);
* ``INFORMATION_SCHEMA.*`` and ``sys.*`` (the profile's
  :attr:`~security.dialects.DialectProfile.system_schemas`);
* a table that already has a ``WITH (...)`` hint list (kept exactly as
  written, never duplicated or merged);
* a ``SELECT ... INTO`` target, which is a write.

Fallback: the original text, unchanged
--------------------------------------
:func:`add_nolock_hints` never raises and never returns a statement it has
not checked. It returns *sql* unchanged, and logs one warning per distinct
reason for the life of the process, when

* the statement does not parse (``parse_error``) or is not a query
  (``not_a_query``);
* a table uses a clause whose position is not covered
  (``unsupported_table_clause``: ``TABLESAMPLE``, ``FOR SYSTEM_TIME``, a
  column list after the alias) or an offset fails its check
  (``bad_position``);
* the rewritten text, parsed again, does not have exactly the same table
  references, every one of them hinted (``verification_failed``).

Running without the hint is the safe direction here: the statement is the
one the guard approved, it simply takes the locks it would take without the
feature. The warning names the reason, never the statement.

The audit trail keeps the model's validated SQL (``generated_sql``); the
text sent to the server is the only thing that changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from security.dialects import get_dialect_profile

logger = logging.getLogger(__name__)

__all__ = ["NOLOCK_HINT", "add_nolock_hints"]

#: The text inserted after each hinted table reference.
NOLOCK_HINT = " WITH (NOLOCK)"

#: The dialect table hints are written in.
_DIALECT = "tsql"

#: Reasons already logged by this process (see :func:`_warn_once`).
_warned_reasons: set[str] = set()


def _warn_once(reason: str, message: str) -> None:
    """Log *message* at WARNING the first time *reason* is seen.

    Rewriting runs on every query, so a statement shape that cannot be
    rewritten would otherwise fill the log. The message never carries the
    SQL text.
    """
    if reason in _warned_reasons:
        return
    _warned_reasons.add(reason)
    logger.warning(
        "WITH (NOLOCK) was not added (reason: %s): %s. The statement runs "
        "unchanged; this is logged once per reason.",
        reason, message,
    )


class _Unsupported(Exception):
    """A statement this module declines to rewrite; carries the reason code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class _Ref:
    """One physical table reference found in a statement."""

    table: exp.Table
    #: Offset to insert :data:`NOLOCK_HINT` at, or ``None`` when the table
    #: already has a hint list.
    insert_at: int | None


def _system_schemas() -> frozenset[str]:
    return frozenset(s.lower() for s in get_dialect_profile(_DIALECT).system_schemas)


def _visible_cte_names(table: exp.Table) -> frozenset[str]:
    """Lower-cased names of the CTEs whose scope *table* sits in."""
    names: set[str] = set()
    node: exp.Expression | None = table.parent
    while node is not None:
        with_ = node.args.get("with_") or node.args.get("with")
        if with_ is not None:
            names.update(cte.alias.lower() for cte in with_.expressions if cte.alias)
        node = node.parent
    return frozenset(names)


def _is_physical_table(table: exp.Table, system_schemas: frozenset[str]) -> bool:
    """Whether *table* names a stored table or view that takes a hint."""
    name = table.this
    if not isinstance(name, exp.Identifier):
        return False  # function, OPENJSON, @variable, four-part name
    if name.args.get("temporary") or name.args.get("global_"):
        return False
    if name.name.startswith(("#", "@")):
        return False
    if isinstance(table.parent, exp.Into):
        return False  # SELECT ... INTO <target> writes
    # `db1..T` (default schema) leaves the schema part as an empty string.
    db, catalog = table.args.get("db"), table.args.get("catalog")
    if isinstance(db, exp.Identifier) and db.name.lower() in system_schemas:
        return False
    if not db and not catalog and name.name.lower() in _visible_cte_names(table):
        return False
    return True


def _identifier_end(sql: str, ident: object) -> int:
    """Offset one past the last character of *ident* in *sql*, checked.

    Raises
    ------
    _Unsupported
        If the identifier has no position, or the text at that position is
        not the identifier.
    """
    if not isinstance(ident, exp.Identifier):
        raise _Unsupported("bad_position", "a table part is not a plain identifier")
    start, end = ident.meta.get("start"), ident.meta.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end < len(sql):
        raise _Unsupported("bad_position", "an identifier has no source position")
    text = sql[start:end + 1]
    if ident.args.get("quoted"):
        ok = len(text) >= 2 and text[0] in '["' and text[-1] in ']"'
    else:
        ok = text == ident.name
    if not ok:
        raise _Unsupported("bad_position", "an identifier's position does not match the text")
    return end + 1


def _insertion_point(sql: str, table: exp.Table) -> int:
    """Where the hint goes for *table*: after the name, or after its alias."""
    if table.args.get("sample") is not None or table.args.get("version") is not None:
        raise _Unsupported(
            "unsupported_table_clause",
            "a table uses TABLESAMPLE or FOR SYSTEM_TIME, whose position "
            "relative to the hint is not handled",
        )
    ends = [
        _identifier_end(sql, table.args[part])
        for part in ("catalog", "db", "this")
        if table.args.get(part)  # absent, or '' in db1..T
    ]
    alias = table.args.get("alias")
    if alias is not None:
        if alias.args.get("columns"):
            raise _Unsupported(
                "unsupported_table_clause",
                "a table alias has a column list, which is not handled",
            )
        ends.append(_identifier_end(sql, alias.this))
    return max(ends)


def _references(sql: str, statements: list[exp.Expression]) -> list[_Ref]:
    system_schemas = _system_schemas()
    refs: list[_Ref] = []
    for statement in statements:
        for table in statement.find_all(exp.Table, bfs=False):
            if not _is_physical_table(table, system_schemas):
                continue
            if table.args.get("hints"):
                refs.append(_Ref(table, None))
            else:
                refs.append(_Ref(table, _insertion_point(sql, table)))
    return refs


def _parse(sql: str) -> list[exp.Expression]:
    """Parse *sql* as T-SQL into queries only.

    Raises
    ------
    _Unsupported
        If it does not parse, or any statement is not a query.
    """
    try:
        parsed = sqlglot.parse(sql, read=_DIALECT)
    except Exception as exc:  # noqa: BLE001 - sqlglot errors, recursion limits
        raise _Unsupported(
            "parse_error", f"the statement does not parse as T-SQL ({type(exc).__name__})"
        ) from None
    statements = [s for s in parsed if s is not None]
    if not statements:
        raise _Unsupported("not_a_query", "the statement is empty")
    if not all(isinstance(s, exp.Query) for s in statements):
        raise _Unsupported("not_a_query", "the statement is not a SELECT query")
    return statements


def _identity(table: exp.Table) -> tuple[str, ...]:
    """What names a reference: its parts and alias, lower-cased."""
    parts = (table.args.get("catalog"), table.args.get("db"), table.this)
    alias = table.args.get("alias")
    return (
        *(p.name.lower() if isinstance(p, exp.Identifier) else "" for p in parts),
        alias.name.lower() if alias is not None else "",
    )


def _carries_nolock(table: exp.Table) -> bool:
    return any(
        isinstance(hint, exp.WithTableHint)
        and any(str(v.name).upper() == "NOLOCK" for v in hint.expressions)
        for hint in table.args.get("hints") or ()
    )


def _verify(rewritten: str, before: list[_Ref]) -> None:
    """Check that *rewritten* parses to the same references, all hinted.

    Raises
    ------
    _Unsupported
        With reason ``verification_failed`` (or the parse reasons).
    """
    after = _references(rewritten, _parse(rewritten))
    if [_identity(r.table) for r in after] != [_identity(r.table) for r in before]:
        raise _Unsupported(
            "verification_failed", "the rewritten statement references different tables"
        )
    for old, new in zip(before, after):
        if not new.table.args.get("hints"):
            raise _Unsupported("verification_failed", "a table is still without a hint")
        if old.insert_at is not None and not _carries_nolock(new.table):
            raise _Unsupported("verification_failed", "an inserted hint is not NOLOCK")


def add_nolock_hints(sql: str) -> str:
    """Return *sql* with ``WITH (NOLOCK)`` after each physical table reference.

    Only the hint text is added; every other character of *sql* is
    returned as written (see the module docstring for how table references
    are located, which tables are left alone, and the fallback).

    Parameters
    ----------
    sql:
        One T-SQL query, with ``?`` placeholders if it is parameterised.

    Returns
    -------
    str
        *sql* with `` WITH (NOLOCK)`` inserted after each table reference
        (after its alias, when it has one); or *sql* itself, unchanged,
        when it cannot be rewritten and checked (logged once per reason).
        Never raises.

    Examples
    --------
    The hint goes after the alias, and nothing else changes:

    >>> add_nolock_hints(
    ...     "SELECT o.Id FROM [sales].[Order] o "
    ...     "JOIN [ref].[Location] AS l ON l.Id = o.LocationId"
    ... )
    'SELECT o.Id FROM [sales].[Order] o WITH (NOLOCK) JOIN [ref].[Location] AS l WITH (NOLOCK) ON l.Id = o.LocationId'

    A CTE name, a derived table, a temp table, a catalogue view and a
    string literal are not hinted:

    >>> add_nolock_hints(
    ...     "WITH x AS (SELECT Id FROM dbo.T) SELECT * FROM x "
    ...     "JOIN (SELECT 1 AS a) q ON 1 = 1 JOIN #tmp ON 1 = 1 "
    ...     "JOIN sys.objects o ON 1 = 1 WHERE n = N'from dbo.U'"
    ... )
    "WITH x AS (SELECT Id FROM dbo.T WITH (NOLOCK)) SELECT * FROM x JOIN (SELECT 1 AS a) q ON 1 = 1 JOIN #tmp ON 1 = 1 JOIN sys.objects o ON 1 = 1 WHERE n = N'from dbo.U'"

    An existing hint list is kept as it is:

    >>> add_nolock_hints("SELECT * FROM a WITH (READPAST) JOIN b ON 1 = 1")
    'SELECT * FROM a WITH (READPAST) JOIN b WITH (NOLOCK) ON 1 = 1'

    Text that cannot be parsed comes back unchanged:

    >>> add_nolock_hints("SELECT FROM WHERE")
    'SELECT FROM WHERE'
    """
    if not sql or not sql.strip():
        return sql
    try:
        refs = _references(sql, _parse(sql))
        insertions = sorted(
            {r.insert_at for r in refs if r.insert_at is not None}, reverse=True
        )
        if not insertions:
            return sql
        rewritten = sql
        for offset in insertions:  # from the end, so earlier offsets stay valid
            rewritten = rewritten[:offset] + NOLOCK_HINT + rewritten[offset:]
        _verify(rewritten, refs)
        return rewritten
    except _Unsupported as exc:
        _warn_once(exc.reason, str(exc))
    except Exception as exc:  # noqa: BLE001 - this function must never raise
        _warn_once("unexpected_error", f"unexpected {type(exc).__name__} while rewriting")
    return sql
