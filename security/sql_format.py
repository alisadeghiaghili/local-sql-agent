# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""House-style SQL layout for **display only**.

``sqlglot``'s pretty printer puts every clause on its own line and indents
everything by two spaces. This module lays a statement out the way the
maintainers write T-SQL by hand: ``SELECT`` alone on a line, one select item
per line with a leading comma, aliases and join conditions lined up in
columns, ``WHERE`` conditions joined by right-aligned ``AND`` / ``OR``.

The text of every *expression* still comes from sqlglot's own T-SQL
generator, so what an expression means is sqlglot's reading of it. Only the
clause structure around the expressions is laid out here. After laying a
statement out, the result is parsed again and compared with the parse of the
input; a layout that does not parse back to the same tree is thrown away. See
:func:`format_sql` for the fallback order.

The rules, exactly
------------------
Indentation is four spaces, never tabs.

* ``SELECT`` (with ``DISTINCT`` and ``TOP (n)`` on the same line) is alone on
  its line. Items follow one per line: the first indented five spaces, each
  later one four spaces then a leading comma, so the expressions line up.
* An alias is written ``expr<2 spaces>AS name`` with every ``AS`` in one
  column: the column sits two spaces after the longest aliased expression.
  An expression that is longer than 60 characters, or spans several lines,
  does not take part in that measurement and gets a single space before its
  ``AS``. An item without an alias is not padded.
* A ``CASE`` that is a whole select, ``GROUP BY`` or ``ORDER BY`` item is a
  block: ``CASE`` / one ``WHEN`` line per branch four spaces in / ``ELSE`` /
  ``END`` under ``CASE``. A ``CASE`` nested inside another expression is
  written inline unless it is longer than 60 characters.
* One empty line separates the select list from ``FROM``.
* ``FROM table AS alias`` is one line with single spaces. Each join is one
  line, ``<INNER|LEFT|RIGHT|FULL|CROSS> JOIN`` or ``<CROSS|OUTER> APPLY``,
  then the source, ``AS alias`` and ``ON  condition``. Across the join lines
  the ``AS`` column and the ``ON`` column are aligned, each two spaces after
  the longest cell before it, and ``ON`` is followed by two spaces. When a
  join's first condition is a plain ``left = right`` (the left side at most
  40 characters), the ``=`` is aligned too, two spaces after the longest such
  left side.
* A condition with ``AND`` / ``OR`` continues on the next lines with the
  operator right-aligned so that every condition starts in the same column
  (``WHERE a`` / ``  AND b`` / ``   OR c``, and under ``ON`` and ``HAVING``
  the same way). Only the top-level operator of a condition is split; a
  parenthesised group stays on one line when it is shorter than 80
  characters, and otherwise opens on its own line, with its conditions four
  columns in and its closing parenthesis under the opening one.
* ``GROUP BY`` and ``ORDER BY`` use the select-list layout (keyword alone,
  first item five spaces in, later items ``    ,item``). ``OFFSET ... ROWS``
  and ``FETCH ... ROWS ONLY`` are one line each.
* A derived table, an ``IN`` / ``EXISTS`` subquery and a scalar subquery are
  laid out with the same rules, their body four spaces inside the
  parentheses and the closing parenthesis back at the opening line's level.
  A scalar subquery of 60 characters or fewer stays inline. A CTE is
  ``name AS (`` / body / ``)``, CTEs separated by ``),``; ``UNION [ALL]``,
  ``EXCEPT`` and ``INTERSECT`` stand alone on a line between their branches.
* Identifier quoting, string and ``N'...'`` literals, numbers and ``?``
  placeholders are rendered as parsed, so what the input wrote is what the
  output shows.
* No line has trailing whitespace, lines end with ``\\n`` and there is no
  trailing newline.

What falls back
---------------
A statement with a comment, more than one statement, a statement that is not
a ``SELECT`` (``MERGE``, ``INSERT``, ...), and any clause or table form this
module does not lay out (``PIVOT``, ``USING``, ``OPTION``, ``INTO``,
``GROUP BY ROLLUP``, comma joins, table-valued ``FROM`` forms other than a
plain table, ``APPLY`` of a function, window clauses, ...) is handed to
sqlglot's pretty printer instead, and if that fails too the input is
returned unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

# Four-space indent; the first select / group / order item sits one column
# further in so that the expressions line up under the later items' text
# after their leading comma.
_INDENT = 4
_FIRST_ITEM_COL = 5

# Spaces between the widest aligned cell and the next one (the ``AS`` of a
# select item, the ``AS`` and ``ON`` of a join line, the ``=`` of a join
# condition).
_GAP = 2

# Spaces after ``ON``. Two, so that the first condition starts where the
# ``AND`` / ``OR`` continuation lines put theirs (``AND `` is four wide).
_ON_SPACES = 2

# An expression longer than this does not stretch the alias column; a nested
# CASE longer than this is broken over lines; a scalar subquery no longer
# than this stays inline.
_LONG_EXPR = 60

# A parenthesised AND / OR group shorter than this stays on one line.
_GROUP_INLINE = 80

# Left side of a join's ``a = b`` longer than this is not padded to; it would
# push every other ``=`` far to the right.
_EQ_ALIGN_MAX = 40

# A query as it sits inside an expression. ``exp.Query`` would also match
# ``Subquery``, the parenthesised wrapper whose parentheses sqlglot writes.
_QUERIES = (exp.Select, exp.SetOperation)

_SLOT = re.compile(r"__fmt_slot_(\d+)__")

# Join keyword by (side, kind) as sqlglot parses it. ``OUTER`` after a side
# and ``INNER`` / nothing are the same join.
_JOIN_KEYWORDS: dict[tuple[str, str], str] = {
    ("", ""): "INNER JOIN",
    ("", "INNER"): "INNER JOIN",
    ("LEFT", ""): "LEFT JOIN",
    ("LEFT", "OUTER"): "LEFT JOIN",
    ("RIGHT", ""): "RIGHT JOIN",
    ("RIGHT", "OUTER"): "RIGHT JOIN",
    ("FULL", ""): "FULL JOIN",
    ("FULL", "OUTER"): "FULL JOIN",
    ("", "CROSS"): "CROSS JOIN",
}

_SELECT_ARGS = frozenset({
    "with_", "with", "distinct", "expressions", "limit", "from_", "from",
    "joins", "where", "group", "having", "order", "offset",
})


class _HouseGenerator(Dialect.get_or_raise("tsql").generator_class):  # type: ignore[misc]
    """sqlglot's T-SQL generator, with negated predicates written naturally.

    The stock generator writes ``x NOT IN (...)`` as ``NOT x IN (...)`` and
    ``x IS NOT NULL`` as ``NOT x IS NULL``. Both mean the same and parse back
    to the same tree, but nobody writes them that way, so the reader sees
    something that looks like a different query. Everything else is the
    stock generator's output.
    """

    _NEGATED = {exp.Is: "IS NOT", exp.In: "NOT IN", exp.Between: "NOT BETWEEN", exp.Like: "NOT LIKE"}
    _POSITIVE = {exp.Is: "IS", exp.In: "IN", exp.Between: "BETWEEN", exp.Like: "LIKE"}

    def not_sql(self, expression: exp.Not) -> str:
        """Render ``NOT <predicate>`` as the predicate's negated spelling."""
        inner = expression.this
        kind = type(inner)
        if kind in self._NEGATED:
            left = self.sql(inner, "this")
            prefix = f"{left} {self._POSITIVE[kind]} "
            rendered = self.sql(inner)
            if rendered.startswith(prefix):
                return f"{left} {self._NEGATED[kind]} {rendered[len(prefix):]}"
        return super().not_sql(expression)


class _Unsupported(Exception):
    """Raised inside the layout for a construct it does not lay out."""


@dataclass
class _JoinRow:
    """One join line before alignment."""

    keyword: str
    body: str | list[str]
    alias_cell: str
    on: exp.Expression | None

    @property
    def head(self) -> str:
        """Keyword and one-line source, the first column of the line."""
        return f"{self.keyword} {self.body}"


def _present(node: exp.Expression) -> dict[str, object]:
    """Return *node*'s arguments that are actually set.

    sqlglot fills an expression's argument dict with ``None`` for every
    argument it knows about, so ``node.args`` alone cannot tell what the
    input contained.
    """
    return {
        key: value
        for key, value in node.args.items()
        if value is not None and value is not False and value != []
    }


def _require_only(node: exp.Expression, allowed: frozenset[str] | set[str]) -> None:
    """Raise :class:`_Unsupported` if *node* has an argument outside *allowed*."""
    extra = set(_present(node)) - set(allowed)
    if extra:
        raise _Unsupported(f"{type(node).__name__}: {sorted(extra)}")


def _get(node: exp.Expression, *names: str) -> object:
    """Return the first of *names* that is set on *node*, else ``None``."""
    for name in names:
        value = node.args.get(name)
        if value:
            return value
    return None


def _indent(lines: list[str], width: int) -> list[str]:
    """Indent *lines* by *width* spaces, leaving empty lines empty."""
    pad = " " * width
    return [pad + line if line else line for line in lines]


def _flatten(cond: exp.Expression) -> tuple[str | None, list[exp.Expression]]:
    """Split a condition at its top-level ``AND`` / ``OR``.

    Only the left spine of one connector type is flattened, which is the
    only shape the parser builds without parentheses, so joining the parts
    back with that connector parses to the same tree.

    Returns
    -------
    tuple[str | None, list[exp.Expression]]
        The connector (``"AND"``, ``"OR"`` or ``None`` for a single
        condition) and the operands in source order.
    """
    if isinstance(cond, exp.And):
        op = "AND"
    elif isinstance(cond, exp.Or):
        op = "OR"
    else:
        return None, [cond]
    cls = type(cond)
    operands: list[exp.Expression] = []
    node: exp.Expression = cond
    while isinstance(node, cls):
        operands.append(node.expression)
        node = node.this
    operands.append(node)
    operands.reverse()
    return op, operands


def _op_prefix(col: int, op: str) -> str:
    """Return the text before a continuation condition starting at *col*.

    The operator is right-aligned so that the condition begins exactly at
    *col*, in line with the first condition of the clause.
    """
    return " " * (col - 1 - len(op)) + op + " "


class _Layout:
    """Lays out one parsed statement; one instance per :func:`format_sql` call."""

    def __init__(self, dialect: str) -> None:
        self.dialect = dialect

    # -- plumbing ----------------------------------------------------------

    def _sql(self, node: exp.Expression) -> str:
        """Render *node* on one line with sqlglot's generator."""
        return _HouseGenerator(dialect=self.dialect).generate(node)

    def _ident(self, node: object) -> str:
        """Render an identifier (alias, CTE name), keeping its quoting."""
        if not isinstance(node, exp.Identifier):
            raise _Unsupported("alias is not a plain identifier")
        return self._sql(node)

    # -- statements --------------------------------------------------------

    def query(self, node: exp.Expression) -> list[str]:
        """Lay out a ``SELECT`` or a set operation as lines at indent 0."""
        if isinstance(node, exp.Select):
            lines = self._with(_get(node, "with_", "with"))
            return lines + self._select(node)
        if isinstance(node, exp.SetOperation):
            return self._set_operation(node)
        raise _Unsupported(type(node).__name__)

    def _with(self, with_: object) -> list[str]:
        """Lay out a ``WITH`` clause; ``[]`` when there is none."""
        if not with_:
            return []
        assert isinstance(with_, exp.With)
        _require_only(with_, {"expressions"})
        lines: list[str] = []
        ctes = with_.expressions
        for i, cte in enumerate(ctes):
            _require_only(cte, {"this", "alias"})
            alias = cte.args.get("alias")
            if not isinstance(alias, exp.TableAlias):
                raise _Unsupported("CTE without a name")
            body = self.query(cte.this)
            lines.append(("WITH " if i == 0 else "") + f"{self._sql(alias)} AS (")
            lines.extend(_indent(body, _INDENT))
            lines.append(")," if i < len(ctes) - 1 else ")")
        return lines

    def _set_operation(self, node: exp.SetOperation) -> list[str]:
        """Lay out ``UNION`` / ``EXCEPT`` / ``INTERSECT`` and its branches."""
        with_ = _get(node, "with_", "with")
        branches: list[exp.Expression] = []
        operators: list[str] = []

        def spine(n: exp.Expression) -> None:
            """Collect branches and operators of the left-nested chain, in order."""
            if not isinstance(n, exp.SetOperation):
                branches.append(n)
                return
            _require_only(n, {"this", "expression", "distinct", "with_", "with"})
            if isinstance(n.expression, exp.SetOperation):
                # A right-nested chain only comes from parentheses, which
                # parse as Subquery; anything else is not ours to flatten.
                raise _Unsupported("right-nested set operation")
            distinct = bool(n.args.get("distinct"))
            if isinstance(n, exp.Union):
                operator = "UNION" if distinct else "UNION ALL"
            elif distinct and isinstance(n, exp.Except):
                operator = "EXCEPT"
            elif distinct and isinstance(n, exp.Intersect):
                operator = "INTERSECT"
            else:
                raise _Unsupported(f"{type(n).__name__} ALL")
            spine(n.this)
            operators.append(operator)
            branches.append(n.expression)

        spine(node)
        lines = self._with(with_)
        for i, branch in enumerate(branches):
            if i:
                lines.append(operators[i - 1])
            lines.extend(self._branch(branch))
        return lines

    def _branch(self, node: exp.Expression) -> list[str]:
        """Lay out one branch of a set operation."""
        if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Query):
            _require_only(node, {"this"})
            return ["("] + _indent(self.query(node.this), _INDENT) + [")"]
        if isinstance(node, exp.Select):
            return self._select(node)
        raise _Unsupported(type(node).__name__)

    def _select(self, node: exp.Select) -> list[str]:
        """Lay out one ``SELECT`` without its ``WITH`` clause."""
        _require_only(node, _SELECT_ARGS)
        lines = [self._select_head(node)]
        lines.extend(self._item_list(node.expressions, alias_items=True))

        source = _get(node, "from_", "from")
        if source:
            lines.append("")
            lines.extend(self._from(source, node.args.get("joins") or []))
        elif node.args.get("joins"):
            raise _Unsupported("join without FROM")

        where = node.args.get("where")
        if where:
            lines.extend(self._condition("WHERE", where.this))

        group = node.args.get("group")
        if group:
            _require_only(group, {"expressions"})
            lines.append("GROUP BY")
            lines.extend(self._item_list(group.expressions, alias_items=False))

        having = node.args.get("having")
        if having:
            lines.extend(self._condition("HAVING", having.this))

        order = node.args.get("order")
        if order:
            _require_only(order, {"expressions"})
            lines.append("ORDER BY")
            lines.extend(self._item_list(order.expressions, alias_items=False))

        offset = node.args.get("offset")
        limit = node.args.get("limit")
        if offset:
            lines.append(self._sql(offset))
        if isinstance(limit, exp.Fetch):
            lines.append(self._sql(limit))
        elif limit and offset:
            raise _Unsupported("TOP together with OFFSET")
        return lines

    def _select_head(self, node: exp.Select) -> str:
        """Return ``SELECT [DISTINCT] [TOP (n)]``.

        The words come from sqlglot rendering a statement that has only the
        modifiers, so ``TOP n PERCENT`` / ``WITH TIES`` are whatever sqlglot
        writes; only a bare ``TOP 100`` gets its parentheses.
        """
        distinct = node.args.get("distinct")
        if distinct is not None and not isinstance(distinct, exp.Distinct):
            distinct = None
        if distinct is not None and _present(distinct):
            raise _Unsupported("DISTINCT ON")
        limit = node.args.get("limit")
        if not distinct and not isinstance(limit, exp.Limit):
            return "SELECT"
        probe = exp.Select(
            expressions=[exp.Var(this="__fmt_head__")],
            distinct=distinct.copy() if distinct else None,
            limit=limit.copy() if isinstance(limit, exp.Limit) else None,
        )
        text = self._sql(probe)
        head, sep, _ = text.partition(" __fmt_head__")
        if not sep:
            raise _Unsupported("SELECT modifiers")
        return re.sub(r"\bTOP (\d+)\b", r"TOP (\1)", head)

    # -- lists -------------------------------------------------------------

    def _item_list(self, items: list[exp.Expression], *, alias_items: bool) -> list[str]:
        """Lay out select / ``GROUP BY`` / ``ORDER BY`` items, one per line."""
        cells: list[tuple[str, str | None]] = []
        for item in items:
            alias: str | None = None
            if alias_items and isinstance(item, exp.Alias):
                alias = self._ident(item.args.get("alias"))
                item = item.this
            text = self._expr(item, _FIRST_ITEM_COL, _FIRST_ITEM_COL, case_block=True)
            cells.append((text, alias))

        def aligned(cell: tuple[str, str | None]) -> bool:
            text, alias = cell
            return alias is not None and "\n" not in text and len(text) <= _LONG_EXPR

        widths = [len(text) for text, alias in cells if aligned((text, alias))]
        width = max(widths) if widths else 0

        out: list[str] = []
        for i, (text, alias) in enumerate(cells):
            if alias is None:
                rendered = text
            elif aligned((text, alias)):
                rendered = f"{text.ljust(width)}{' ' * _GAP}AS {alias}"
            else:
                rendered = f"{text} AS {alias}"
            lead = " " * _FIRST_ITEM_COL if i == 0 else " " * _INDENT + ","
            first, *rest = rendered.split("\n")
            out.append(lead + first)
            out.extend(rest)
        return out

    # -- FROM and joins ----------------------------------------------------

    def _source(self, node: exp.Expression) -> tuple[str | list[str], str | None, str]:
        """Describe a table source.

        Returns
        -------
        tuple
            ``(body, alias, suffix)``. *body* is a one-line string for a
            table or function and a list of lines (opening ``(`` to the
            closing ``)``) for a derived table. *alias* is the rendered alias
            or ``None``. *suffix* is a table hint (`` WITH (NOLOCK)``) that
            follows the alias, or ``""``.
        """
        if isinstance(node, exp.Table):
            _require_only(node, {"this", "db", "catalog", "alias", "hints"})
            alias = self._table_alias(node.args.get("alias"))
            bare = node.copy()
            bare.set("alias", None)
            bare.set("hints", None)
            name = self._sql(bare)
            suffix = ""
            if node.args.get("hints"):
                hinted = node.copy()
                hinted.set("alias", None)
                full = self._sql(hinted)
                if not full.startswith(name):
                    raise _Unsupported("table hint")
                suffix = full[len(name):]
            return name, alias, suffix
        if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Query):
            _require_only(node, {"this", "alias"})
            alias = self._table_alias(node.args.get("alias"))
            body = ["("] + _indent(self.query(node.this), _INDENT) + [")"]
            return body, alias, ""
        raise _Unsupported(type(node).__name__)

    def _table_alias(self, alias: object) -> str | None:
        """Render a table alias (no column list) or ``None``."""
        if not alias:
            return None
        if not isinstance(alias, exp.TableAlias) or _present(alias).keys() - {"this"}:
            raise _Unsupported("table alias with columns")
        return self._ident(alias.this)

    def _from(self, source: exp.Expression, joins: list[exp.Join]) -> list[str]:
        """Lay out ``FROM`` and the join lines."""
        assert isinstance(source, exp.From)
        _require_only(source, {"this"})
        body, alias, suffix = self._source(source.this)
        if isinstance(body, str):
            line = "FROM " + body + (f" AS {alias}" if alias else "") + suffix
            lines = [line]
        else:
            lines = ["FROM " + body[0]] + body[1:-1]
            lines.append(body[-1] + (f" AS {alias}" if alias else "") + suffix)
        return lines + self._joins(joins)

    def _join_keyword(self, join: exp.Join) -> str:
        """Return the join keyword in house style."""
        _require_only(join, {"this", "on", "side", "kind"})
        if isinstance(join.this, exp.Lateral):
            cross = join.this.args.get("cross_apply")
            if cross is None or join.args.get("side") or join.args.get("kind"):
                raise _Unsupported("LATERAL")
            return "CROSS APPLY" if cross else "OUTER APPLY"
        side = str(join.args.get("side") or "").upper()
        kind = str(join.args.get("kind") or "").upper()
        keyword = _JOIN_KEYWORDS.get((side, kind))
        if keyword is None:
            raise _Unsupported(f"join {side} {kind}")
        has_on = bool(join.args.get("on"))
        if not has_on and keyword != "CROSS JOIN":
            # ``FROM a, b`` parses as a Join with nothing set.
            raise _Unsupported("comma join")
        return keyword

    def _joins(self, joins: list[exp.Join]) -> list[str]:
        """Lay out the join lines with aligned ``AS`` / ``ON`` / ``=`` columns."""
        rows: list[_JoinRow] = []
        for join in joins:
            keyword = self._join_keyword(join)
            target = join.this
            if isinstance(target, exp.Lateral):
                _require_only(target, {"this", "alias", "cross_apply"})
                inner = target.this
                alias = self._table_alias(target.args.get("alias"))
                if alias is None or not (
                    isinstance(inner, exp.Subquery) and isinstance(inner.this, exp.Query)
                ):
                    # ``APPLY`` of a function, or without the alias T-SQL requires.
                    raise _Unsupported("APPLY form")
                _require_only(inner, {"this"})
                body: str | list[str] = ["("] + _indent(self.query(inner.this), _INDENT) + [")"]
                suffix = ""
            else:
                body, alias, suffix = self._source(target)
            cell = f"AS {alias}{suffix}" if alias else suffix.strip()
            rows.append(_JoinRow(keyword, body, cell, join.args.get("on")))

        # Column widths come from the single-line sources only; a derived
        # table spans lines and is laid out on its own.
        one_line = [r for r in rows if isinstance(r.body, str)]
        w_source = max((len(r.head) for r in one_line), default=0)
        with_on = [r for r in one_line if r.on is not None]
        w_alias = max((len(r.alias_cell) for r in with_on), default=0)

        # ``=`` alignment: only joins whose first condition is a plain
        # ``left = right`` take part.
        eq_parts: dict[int, tuple[str, str]] = {}
        for r in one_line:
            if r.on is None:
                continue
            first = _flatten(r.on)[1][0]
            if isinstance(first, exp.EQ) and not first.find(exp.Query):
                left, right = self._sql(first.this), self._sql(first.expression)
                if len(left) <= _EQ_ALIGN_MAX:
                    eq_parts[id(r)] = (left, right)
        w_left = max((len(left) for left, _ in eq_parts.values()), default=0)

        lines: list[str] = []
        for r in rows:
            if isinstance(r.body, str):
                head = r.head.ljust(w_source)
                if r.on is None:
                    lines.append(head + " " * _GAP + r.alias_cell if r.alias_cell else r.head)
                    continue
                head += " " * _GAP
                if w_alias:
                    head += r.alias_cell.ljust(w_alias) + " " * _GAP
                lines.extend(self._on(head, r.on, eq_parts.get(id(r)), w_left))
            else:
                lines.append(f"{r.keyword} {r.body[0]}")
                lines.extend(r.body[1:-1])
                tail = r.body[-1] + (f" {r.alias_cell}" if r.alias_cell else "")
                if r.on is None:
                    lines.append(tail)
                else:
                    lines.extend(self._on(tail + " " * _GAP, r.on, None, 0))
        return lines

    def _on(
        self,
        head: str,
        cond: exp.Expression,
        eq: tuple[str, str] | None,
        w_left: int,
    ) -> list[str]:
        """Lay out ``ON  condition`` after *head*, which ends where ``ON`` starts."""
        col = len(head) + len("ON") + _ON_SPACES
        first = None
        if eq is not None:
            left, right = eq
            first = f"{left.ljust(w_left)}{' ' * _GAP}= {right}"
        lines = self._cond_lines(cond, col, 0, first)
        lines[0] = head + "ON" + " " * _ON_SPACES + lines[0]
        return lines

    # -- conditions --------------------------------------------------------

    def _condition(self, keyword: str, cond: exp.Expression) -> list[str]:
        """Lay out ``WHERE`` / ``HAVING`` and its conditions."""
        col = len(keyword) + 1
        lines = self._cond_lines(cond, col, 0)
        lines[0] = keyword + " " + lines[0]
        return lines

    def _cond_lines(
        self, cond: exp.Expression, col: int, base: int, first: str | None = None
    ) -> list[str]:
        """Lay out a condition whose first line starts at column *col*.

        The first returned line carries no indentation (the caller puts the
        clause keyword in front of it); continuation lines are absolute.
        *first* replaces the rendering of the first operand.
        """
        op, operands = _flatten(cond)
        lines: list[str] = []
        for i, operand in enumerate(operands):
            if i == 0 and first is not None:
                sub = [first]
            else:
                sub = self._operand(operand, col, base)
            if i:
                assert op is not None
                sub[0] = _op_prefix(col, op) + sub[0]
            lines.extend(sub)
        return lines

    def _operand(self, node: exp.Expression, col: int, base: int) -> list[str]:
        """Lay out one operand of an ``AND`` / ``OR`` chain."""
        if isinstance(node, exp.Paren) and isinstance(node.this, (exp.And, exp.Or)):
            flat = self._sql(node)
            if len(flat) >= _GROUP_INLINE or node.find(exp.Query):
                inner_col = col + _INDENT
                inner = self._cond_lines(node.this, inner_col, inner_col)
                inner[0] = " " * inner_col + inner[0]
                return ["("] + inner + [" " * col + ")"]
        return self._expr(node, col, base).split("\n")

    # -- expressions -------------------------------------------------------

    def _expr(
        self, node: exp.Expression, col: int, base: int, *, case_block: bool = False
    ) -> str:
        """Render an expression that starts at column *col*.

        sqlglot writes the expression; a nested query or a long ``CASE`` is
        swapped for a placeholder first and the laid-out block is put back in
        its place afterwards, so multi-line parts hang off *col* (a ``CASE``)
        or *base* (a subquery body is *base* + 4, its ``)`` back at *base*).
        Continuation lines of the result are absolute; the first line is not
        indented.
        """
        if isinstance(node, exp.Case) and (
            case_block or len(self._sql(node)) > _LONG_EXPR
        ):
            return self._case(node, col, base)

        work = node.copy()
        slots: dict[str, exp.Expression] = {}
        scalar: dict[str, bool] = {}

        if isinstance(work, _QUERIES):
            raise _Unsupported("query used as an expression")

        def stop(n: exp.Expression) -> bool:
            if isinstance(n, _QUERIES):
                return True
            if not isinstance(n, exp.Case) or n is work:
                return False
            # ``ORDER BY CASE ... END DESC``: the CASE is the whole item.
            whole_item = case_block and isinstance(work, exp.Ordered) and n.parent is work
            return whole_item or len(self._sql(n)) > _LONG_EXPR

        targets = [n for n in work.walk(prune=stop) if n is not work and stop(n)]
        for i, target in enumerate(targets):
            name = f"__fmt_slot_{i}__"
            slots[name] = target
            parent = target.parent
            scalar[name] = (
                isinstance(parent, exp.Subquery)
                and not isinstance(parent.parent, (exp.In, exp.Exists, exp.Any, exp.All))
            )
            target.replace(exp.Var(this=name))
        # sqlglot writes ``EXISTS(``; the house style has the space.
        text = self._sql(work).replace("EXISTS(__fmt_slot_", "EXISTS (__fmt_slot_")
        if not slots:
            return text

        out = ""
        pos = 0
        used = 0
        for match in _SLOT.finditer(text):
            name = match.group(0)
            if name not in slots:
                raise _Unsupported("unknown placeholder")
            out += text[pos:match.start()]
            anchor = (len(out) - out.rfind("\n") - 1) if "\n" in out else col + len(out)
            target = slots[name]
            if isinstance(target, exp.Case):
                out += self._case(target, anchor, base)
            else:
                flat = self._sql(target)
                if scalar[name] and len(flat) <= _LONG_EXPR:
                    out += flat
                else:
                    body = _indent(self.query(target), base + _INDENT)
                    out += "\n" + "\n".join(body) + "\n" + " " * base
            pos = match.end()
            used += 1
        if used != len(slots):
            raise _Unsupported("placeholder lost")
        return out + text[pos:]

    def _case(self, node: exp.Case, col: int, base: int) -> str:
        """Lay out a ``CASE`` as a block starting at column *col*."""
        _require_only(node, {"this", "ifs", "default"})
        head = "CASE"
        operand = node.args.get("this")
        if operand is not None:
            head += " " + self._expr(operand, col + len("CASE "), base)
        inner = col + _INDENT
        lines = [head]
        for branch in node.args.get("ifs") or []:
            cond = self._expr(branch.this, inner + len("WHEN "), base)
            then = self._expr(branch.args["true"], inner, base)
            lines.append(" " * inner + f"WHEN {cond} THEN {then}")
        default = node.args.get("default")
        if default is not None:
            lines.append(" " * inner + "ELSE " + self._expr(default, inner + len("ELSE "), base))
        lines.append(" " * col + "END")
        return "\n".join(lines)


def _has_comments(sql: str, dialect: str) -> bool:
    """Whether the tokenizer saw a comment anywhere in *sql*."""
    return any(token.comments for token in sqlglot.tokenize(sql, read=dialect))


def _normalise(tree: exp.Expression) -> exp.Expression:
    """Reduce spellings that mean the same join to one form, for comparing.

    ``JOIN`` and ``INNER JOIN`` are the same join, and so are ``LEFT JOIN``
    and ``LEFT OUTER JOIN``; the layout writes the short house spelling, and
    that must not count as a change.
    """
    tree = tree.copy()
    for join in tree.find_all(exp.Join):
        side = join.args.get("side")
        kind = str(join.args.get("kind") or "").upper()
        if kind == "INNER" or (kind == "OUTER" and side):
            join.set("kind", None)
    return tree


def _same_statement(original: exp.Expression, text: str, dialect: str) -> bool:
    """Whether *text* parses to a single statement equal to *original*."""
    parsed = sqlglot.parse(text, read=dialect)
    return (
        len(parsed) == 1
        and parsed[0] is not None
        and _normalise(parsed[0]) == _normalise(original)
    )


def _house_layout(sql: str, dialect: str) -> str | None:
    """Return *sql* in house style, or ``None`` when it cannot be done safely."""
    if "__fmt_" in sql or _has_comments(sql, dialect):
        return None
    parsed = sqlglot.parse(sql, read=dialect)
    if len(parsed) != 1 or parsed[0] is None:
        return None
    statement = parsed[0]
    try:
        lines = _Layout(dialect).query(statement)
    except _Unsupported:
        return None
    text = "\n".join(line.rstrip() for line in "\n".join(lines).split("\n"))
    if "__fmt_" in text or not _same_statement(statement, text, dialect):
        return None
    return text


def _sqlglot_pretty(sql: str, dialect: str) -> str | None:
    """Return sqlglot's pretty rendering of one statement, else ``None``."""
    rendered = sqlglot.transpile(sql, read=dialect, write=dialect, pretty=True)
    if len(rendered) != 1 or not rendered[0]:
        return None
    return rendered[0]


def format_sql(sql: str, dialect: str = "tsql") -> str:
    """Lay *sql* out in the house style, for **display only**.

    The text that is executed is never passed through here; this is what the
    UI shows and the copy button copies. The layout rules are listed in the
    module docstring.

    The result is checked before it is returned: it is parsed again and must
    equal the parse of *sql* (``JOIN`` / ``INNER JOIN`` and ``LEFT JOIN`` /
    ``LEFT OUTER JOIN`` count as equal). The fallbacks, in order:

    1. the house layout;
    2. sqlglot's pretty printer, when the house layout does not apply (a
       comment, a construct it does not lay out, a different *dialect*) or
       fails the check above;
    3. *sql* unchanged, when sqlglot cannot render it either.

    Never raises.

    Parameters
    ----------
    sql : str
        The statement to format.
    dialect : str, default "tsql"
        sqlglot dialect to parse in. The house layout is T-SQL's; any other
        dialect goes straight to sqlglot's pretty printer.

    Returns
    -------
    str
        The formatted statement with ``\\n`` line endings, no trailing
        whitespace and no trailing newline; or *sql* itself when it is empty
        or cannot be parsed.

    Examples
    --------
    >>> print(format_sql(
    ...     "select top 10 t.id, c.[Name] as customer, t.price "
    ...     "from sales_fact.Trade t "
    ...     "inner join sales_dim.Customer c on t.Customer_ID = c.ID "
    ...     "where t.price > 0 and c.[Name] is not null "
    ...     "order by t.price desc"
    ... ))
    SELECT TOP (10)
         t.id
        ,c.[Name]  AS customer
        ,t.price
    <BLANKLINE>
    FROM sales_fact.Trade AS t
    INNER JOIN sales_dim.Customer  AS c  ON  t.Customer_ID  = c.ID
    WHERE t.price > 0
      AND c.[Name] IS NOT NULL
    ORDER BY
         t.price DESC

    Unparseable input comes back untouched:

    >>> format_sql("this is not sql at all ((")
    'this is not sql at all (('
    """
    if not sql or not sql.strip():
        return sql
    if dialect.lower() == "tsql":
        dialect = "tsql"
        try:
            laid_out = _house_layout(sql, dialect)
        except Exception:  # noqa: BLE001 - cosmetic; the fallbacks follow
            laid_out = None
        if laid_out is not None:
            return laid_out
    try:
        pretty = _sqlglot_pretty(sql, dialect)
    except Exception:  # noqa: BLE001 - cosmetic; the input is the last resort
        pretty = None
    return sql if pretty is None else pretty
