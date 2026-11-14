# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Which common table expression, if any, a table reference binds to.

A name in ``FROM``/``JOIN`` is either a stored table or a reference to a CTE.
The guard exempts CTE references from the table allowlist (the CTE's body is
checked where it is written), so getting this wrong in the permissive
direction lets a query read a table that is not allowlisted. This module is
the one place that decision is made; every caller that has to tell a CTE
reference from a stored table asks it.

The rule
--------
A table reference binds to a CTE only when all of these hold:

1. **It is unqualified.** ``hr.Secret`` and ``db.hr.Secret`` always name a
   stored object; a CTE cannot be addressed through a schema.
2. **A CTE of that name is visible where the reference is written.** Walking
   up from the reference:

   * a ``WITH`` clause attached to an enclosing query makes all of its CTEs
     visible in that query's body;
   * inside the ``WITH`` clause itself, a CTE body sees the CTEs defined
     *before* it, and itself only when the reference is recursive (rule 3).
     A reference to a CTE defined later is not a CTE reference;
   * the nearest definition wins, so an inner CTE shadows an outer one.

   A CTE defined in one subquery is therefore not visible in a sibling
   subquery or in the outer query.
3. **Self-reference is recursion.** A CTE that names itself in its own body
   refers to itself under ``WITH RECURSIVE``, and in a dialect that has no
   ``RECURSIVE`` keyword (:data:`IMPLICIT_RECURSION_DIALECTS`). Elsewhere it
   names the stored table.
4. **The names are the same identifier.** See :func:`_same_identifier`.

Where a database's behaviour is uncertain the rule answers "stored table":
the reference then has to pass the allowlist, which fails closed.

Why not ``sqlglot.optimizer.scope``
-----------------------------------
``build_scope`` answers a wider question (column sources) and its handling of
qualifiers, recursion and forward references has moved between sqlglot
releases; ``requirements.txt`` only sets a floor. An allowlist exemption must
not change meaning with a dependency upgrade, so the walk is written out here,
over nothing but ``parent`` links and the ``WITH`` node.

Public API
----------
cte_binding(table, dialect="tsql") -> exp.CTE | None
is_cte_reference(table, dialect="tsql") -> bool
"""

from __future__ import annotations

from sqlglot import exp

__all__ = ["IMPLICIT_RECURSION_DIALECTS", "cte_binding", "is_cte_reference"]

#: Dialects with no ``RECURSIVE`` keyword: a CTE naming itself is recursive.
IMPLICIT_RECURSION_DIALECTS: frozenset[str] = frozenset({"tsql", "oracle"})

#: Dialects where a quoted identifier still compares case-insensitively.
#: T-SQL does under its default (case-insensitive) collation; a database
#: created with a case-sensitive collation is narrower than this assumes.
_CASE_INSENSITIVE_DIALECTS: frozenset[str] = frozenset({"tsql", "sqlite"})


def _with_clause(node: exp.Expression) -> exp.With | None:
    """The ``WITH`` clause attached to *node* (the arg key differs by sqlglot release)."""
    clause = node.args.get("with_") or node.args.get("with")
    return clause if isinstance(clause, exp.With) else None


def _cte_identifier(cte: exp.CTE) -> exp.Identifier | None:
    """The identifier a CTE is defined under, or ``None`` when it has none."""
    alias = cte.args.get("alias")
    ident = alias.this if isinstance(alias, exp.TableAlias) else alias
    return ident if isinstance(ident, exp.Identifier) and ident.name else None


def _same_identifier(defined: exp.Identifier, used: exp.Identifier, dialect: str) -> bool:
    """Whether *used* names the CTE defined as *defined* in *dialect*.

    Equal text apart from case is the same name in a case-insensitive
    dialect. In the others, two unquoted identifiers fold to the same case
    and match; two quoted ones must match exactly; a quoted and an unquoted
    one match only when the text is identical and already lower-case (the
    fold PostgreSQL applies) -- ``WITH secret AS (...) ... FROM "Secret"``
    reads the stored table ``"Secret"``.
    """
    a, b = defined.name, used.name
    if a.lower() != b.lower():
        return False
    if dialect in _CASE_INSENSITIVE_DIALECTS:
        return True
    a_quoted, b_quoted = bool(defined.args.get("quoted")), bool(used.args.get("quoted"))
    if not a_quoted and not b_quoted:
        return True
    if a != b:
        return False
    return (a_quoted and b_quoted) or a == a.lower()


def _find(ctes: list[exp.CTE], used: exp.Identifier, dialect: str) -> exp.CTE | None:
    """The last CTE in *ctes* that *used* names, or ``None``."""
    for cte in reversed(ctes):
        defined = _cte_identifier(cte)
        if defined is not None and _same_identifier(defined, used, dialect):
            return cte
    return None


def _visible_inside(clause: exp.With, body: exp.Expression, dialect: str) -> list[exp.CTE]:
    """The CTEs of *clause* visible inside its own member *body*."""
    siblings = list(clause.expressions)
    index = next((i for i, cte in enumerate(siblings) if cte is body), None)
    if index is None:
        return []
    recursive = bool(clause.args.get("recursive")) or dialect in IMPLICIT_RECURSION_DIALECTS
    return siblings[: index + 1] if recursive else siblings[:index]


def cte_binding(table: exp.Table, dialect: str = "tsql") -> exp.CTE | None:
    """Return the CTE that *table* refers to, or ``None`` for a stored table.

    Args:
        table: A table reference still attached to its statement (the walk
            follows ``parent`` links).
        dialect: The sqlglot dialect the statement is validated in. It
            decides implicit recursion and identifier case rules.

    Returns:
        The defining :class:`sqlglot.expressions.CTE` node, or ``None`` when
        *table* is qualified, has no plain name (a table-valued function), or
        no CTE of that name is visible at that point.

    Examples:
        >>> import sqlglot
        >>> def bound(sql, dialect="tsql"):
        ...     tree = sqlglot.parse_one(sql, read=dialect)
        ...     return [cte_binding(t, dialect) is not None for t in tree.find_all(exp.Table)]
        >>> bound("WITH c AS (SELECT 1 AS x) SELECT x FROM c")
        [True]
        >>> bound("WITH c AS (SELECT 1 AS x) SELECT x FROM hr.c")
        [False]
        >>> bound("WITH c AS (SELECT x FROM c) SELECT x FROM c", "postgres")
        [True, False]
        >>> bound("WITH RECURSIVE c AS (SELECT x FROM c) SELECT x FROM c", "postgres")
        [True, True]
    """
    used = table.this
    if not isinstance(used, exp.Identifier) or not used.name:
        return None
    if table.args.get("db") or table.args.get("catalog"):
        return None

    child: exp.Expression = table
    node = table.parent
    while node is not None:
        if isinstance(node, exp.With):
            found = _find(_visible_inside(node, child, dialect), used, dialect)
            if found is not None:
                return found
            # The query owning this clause must not offer it a second time:
            # that would make every sibling visible, forward references too.
            owner = node.parent
            if owner is None:
                return None
            child, node = owner, owner.parent
            continue
        clause = _with_clause(node)
        if clause is not None:
            found = _find(list(clause.expressions), used, dialect)
            if found is not None:
                return found
        child, node = node, node.parent
    return None


def is_cte_reference(table: exp.Table, dialect: str = "tsql") -> bool:
    """Whether *table* refers to a CTE rather than a stored table.

    Args:
        table: A table reference still attached to its statement.
        dialect: The sqlglot dialect the statement is validated in.

    Returns:
        ``True`` when :func:`cte_binding` finds a visible CTE of that name.

    Examples:
        >>> import sqlglot
        >>> tree = sqlglot.parse_one(
        ...     "SELECT 1 FROM a WHERE EXISTS (SELECT 1 FROM (WITH s AS (SELECT 1 AS x) "
        ...     "SELECT x FROM s) q) AND 1 IN (SELECT 1 FROM s)", read="postgres")
        >>> [(t.name, is_cte_reference(t, "postgres")) for t in tree.find_all(exp.Table)]
        [('a', False), ('s', False), ('s', True)]
    """
    return cte_binding(table, dialect) is not None
