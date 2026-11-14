# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""CTE-scoping regression tests for security/sql_guard.py.

``validate_sql`` exempts CTE names from the table allowlist, because a CTE
is not a real table. The exemption must be exactly as wide as the SQL
language makes it: a table reference is a CTE reference **only when a CTE
of that name is actually visible at that point of the statement**. A guard
that collects every CTE name in the whole statement, or ignores schema
qualifiers, lets a non-allowlisted table through whenever some unrelated
CTE happens to share its name. This module pins the scoped behaviour.

A table reference ``T`` binds to a CTE if and only if all of these hold:

1. ``T`` has no schema or database qualifier. ``hr.Secret`` and
   ``[db].[hr].[Secret]`` are never CTE references.
2. A CTE of that name is visible at ``T``. A WITH clause attached to a
   query makes all of its CTEs visible in that query's body; inside the
   WITH clause itself a CTE body sees only the CTEs defined before it (no
   forward references); CTEs of enclosing queries are visible further
   down; the nearest definition wins; a CTE defined inside one subquery is
   not visible in a sibling subquery or in the outer query.
3. A self-reference inside a CTE's own body binds to that CTE only for
   ``tsql`` (no RECURSIVE keyword, self-reference is recursion) or under
   ``WITH RECURSIVE``. Elsewhere it is the real table.
4. Names compare case-insensitively for ``tsql``. For other dialects a
   case-only difference binds only when neither identifier is quoted.

Test data: ``Broker`` is an allowlisted table of the example config;
``Secret`` is a made-up name that is NOT in the allowlist. Assertions
target the rejection ``reason`` and the table named in the message, never
the full message wording. The "must be refused" cases fail on a guard that
scopes CTE names globally and pass once binding is scoped.

Run::

    pytest tests/test_sql_guard_cte_scope.py -v
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from schema_data.columns import TABLE_COLUMNS
from security.sql_guard import (
    CorrectableRejection,
    PolicyRejection,
    extract_touched_tables,
    is_cte_reference,
    validate_sql,
)

#: An allowlisted table of the example config, and one of its real columns.
ALLOWED = "Broker"
_COL = "Code"

#: A name that is deliberately absent from the allowlist.
FORBIDDEN = "Secret"


def _refusal_reason(sql: str, dialect: str = "tsql") -> str:
    """Validate *sql* and return the ``reason`` of the rejection it raises.

    Fails the test if the statement is accepted, so a "must be refused"
    case cannot pass by returning ``None``.
    """
    with pytest.raises((CorrectableRejection, PolicyRejection)) as exc_info:
        validate_sql(sql, dialect=dialect)
    return exc_info.value.reason


def _assert_refused_as_unknown_table(sql: str, dialect: str = "tsql") -> None:
    """Assert *sql* is refused with reason ``unknown_table`` naming ``Secret``."""
    with pytest.raises((CorrectableRejection, PolicyRejection)) as exc_info:
        validate_sql(sql, dialect=dialect)
    assert exc_info.value.reason == "unknown_table"
    assert FORBIDDEN in str(exc_info.value)


def _tables(sql: str, dialect: str = "tsql") -> list[exp.Table]:
    """Every ``exp.Table`` node of *sql*, in ``find_all`` order."""
    return list(sqlglot.parse_one(sql, dialect=dialect).find_all(exp.Table))


def _table_named(
    sql: str, name: str, dialect: str = "tsql", *, in_cte_body: bool | None = None,
    in_alias: str | None = None,
) -> exp.Table:
    """The single ``exp.Table`` node of *sql* named *name* that matches the filters.

    ``find_all`` walks breadth-first, so list position says nothing about
    where a node sits in the text; nodes are selected by position in the
    tree instead. ``in_cte_body`` keeps only nodes that are (True) or are
    not (False) inside some CTE definition; ``in_alias`` keeps only nodes
    inside the derived table with that alias.
    """
    matches = []
    for table in _tables(sql, dialect):
        if table.name != name:
            continue
        if in_cte_body is not None and (table.find_ancestor(exp.CTE) is not None) != in_cte_body:
            continue
        if in_alias is not None and not any(
            sub.alias == in_alias for sub in _ancestors(table, exp.Subquery)
        ):
            continue
        matches.append(table)
    assert len(matches) == 1, f"expected exactly one match for {name!r}, got {len(matches)}"
    return matches[0]


def _ancestors(node: exp.Expression, kind: type[exp.Expression]) -> list[exp.Expression]:
    """Every ancestor of *node* that is an instance of *kind*."""
    found = []
    parent = node.parent
    while parent is not None:
        if isinstance(parent, kind):
            found.append(parent)
        parent = parent.parent
    return found


class TestFixtureAssumptions:
    """The shapes below must be decided by the CTE rule alone."""

    def test_allowed_table_is_in_the_example_allowlist(self) -> None:
        assert ALLOWED in TABLE_COLUMNS
        assert _COL in TABLE_COLUMNS[ALLOWED]
        assert FORBIDDEN not in TABLE_COLUMNS

    def test_non_cte_variant_of_the_join_shape_is_allowed(self) -> None:
        """The join shape used throughout (``ON 1=1``, explicit columns)
        trips no unrelated rule when both sides are allowlisted tables."""
        sql = f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b JOIN {ALLOWED} s ON 1=1"
        assert validate_sql(sql) is None

    def test_plain_allowlisted_query_is_allowed(self) -> None:
        assert validate_sql(f"SELECT TOP 5 {_COL} FROM {ALLOWED}") is None

    def test_plain_unknown_table_is_refused(self) -> None:
        _assert_refused_as_unknown_table(f"SELECT TOP 5 * FROM {FORBIDDEN}")

    def test_plain_unknown_join_without_any_cte_is_refused(self) -> None:
        _assert_refused_as_unknown_table(
            f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b JOIN {FORBIDDEN} s ON 1=1"
        )


class TestQualifiedNamesNeverBindToACte:
    """Rule 1: a schema or database qualifier rules out a CTE reference."""

    @pytest.mark.parametrize(
        "qualified",
        [
            f"hr.{FORBIDDEN}",
            f"[hr].[{FORBIDDEN}]",
            f"[db].[hr].[{FORBIDDEN}]",
            f"db.hr.{FORBIDDEN}",
        ],
    )
    def test_qualified_table_with_same_named_cte_is_refused(self, qualified: str) -> None:
        sql = (
            f"WITH {FORBIDDEN} AS (SELECT 1 AS a) "
            f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b JOIN {qualified} s ON 1=1"
        )
        _assert_refused_as_unknown_table(sql)

    def test_qualified_reference_inside_a_later_cte_is_refused(self) -> None:
        sql = (
            f"WITH {FORBIDDEN} AS (SELECT 1 AS a), "
            f"c AS (SELECT a FROM hr.{FORBIDDEN}) "
            f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b JOIN c ON 1=1"
        )
        _assert_refused_as_unknown_table(sql)


class TestCteVisibilityAcrossSubqueries:
    """Rule 2: a CTE is visible only in the query that owns its WITH clause."""

    def test_cte_inside_derived_table_does_not_cover_outer_from(self) -> None:
        sql = (
            f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b "
            f"JOIN (WITH {FORBIDDEN} AS (SELECT 1 AS a) SELECT a FROM {FORBIDDEN}) d ON 1=1 "
            f"JOIN {FORBIDDEN} s ON 1=1"
        )
        _assert_refused_as_unknown_table(sql)

    def test_cte_inside_derived_table_does_not_cover_sibling_derived_table(self) -> None:
        sql = (
            f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b "
            f"JOIN (WITH {FORBIDDEN} AS (SELECT 1 AS a) SELECT a FROM {FORBIDDEN}) d1 ON 1=1 "
            f"JOIN (SELECT a FROM {FORBIDDEN}) d2 ON 1=1"
        )
        _assert_refused_as_unknown_table(sql)

    def test_cte_inside_exists_subquery_does_not_cover_outer_from(self) -> None:
        sql = (
            f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b "
            f"JOIN {FORBIDDEN} s ON 1=1 "
            f"WHERE EXISTS (WITH {FORBIDDEN} AS (SELECT 1 AS a) SELECT a FROM {FORBIDDEN})"
        )
        _assert_refused_as_unknown_table(sql)

    def test_cte_inside_exists_subquery_does_not_cover_other_subquery(self) -> None:
        sql = (
            f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b "
            f"WHERE EXISTS (WITH {FORBIDDEN} AS (SELECT 1 AS a) SELECT a FROM {FORBIDDEN}) "
            f"AND EXISTS (SELECT 1 FROM {FORBIDDEN})"
        )
        _assert_refused_as_unknown_table(sql)

    def test_outer_cte_is_visible_in_where_subquery(self) -> None:
        sql = (
            f"WITH c AS (SELECT ID FROM {ALLOWED}) "
            f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b "
            f"WHERE b.ID IN (SELECT ID FROM c)"
        )
        assert validate_sql(sql) is None

    def test_inner_cte_shadows_outer_cte_of_the_same_name(self) -> None:
        sql = (
            f"WITH c AS (SELECT ID FROM {ALLOWED}) "
            f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b "
            f"WHERE b.ID IN (WITH c AS (SELECT ID FROM {ALLOWED}) SELECT ID FROM c)"
        )
        assert validate_sql(sql) is None


class TestForwardReferences:
    """Rule 2: a CTE body sees only the CTEs defined before it."""

    def test_forward_reference_to_later_cte_is_refused(self) -> None:
        sql = (
            f"WITH a AS (SELECT * FROM {FORBIDDEN}), {FORBIDDEN} AS (SELECT 1 AS x) "
            f"SELECT TOP 5 b.{_COL} FROM {ALLOWED} b JOIN a ON 1=1"
        )
        _assert_refused_as_unknown_table(sql)

    def test_backward_reference_to_earlier_cte_is_allowed(self) -> None:
        sql = (
            f"WITH first_cte AS (SELECT ID FROM {ALLOWED}), "
            f"second_cte AS (SELECT ID FROM first_cte) "
            f"SELECT TOP 5 ID FROM second_cte JOIN {ALLOWED} b ON 1=1"
        )
        assert validate_sql(sql) is None


class TestSelfReferenceDependsOnDialect:
    """Rule 3: self-reference is recursion only for tsql or WITH RECURSIVE."""

    @pytest.mark.parametrize("dialect", ["postgres", "sqlite"])
    def test_non_recursive_self_reference_is_the_real_table(self, dialect: str) -> None:
        sql = (
            f"WITH {FORBIDDEN} AS (SELECT * FROM {FORBIDDEN}) "
            f"SELECT b.{_COL} FROM {ALLOWED} b JOIN {FORBIDDEN} s ON 1=1 LIMIT 5"
        )
        _assert_refused_as_unknown_table(sql, dialect=dialect)

    def test_tsql_self_reference_is_recursion(self) -> None:
        sql = (
            f"WITH tree AS ("
            f"SELECT ID FROM {ALLOWED} "
            f"UNION ALL "
            f"SELECT ID FROM tree"
            f") SELECT TOP 5 t.ID FROM tree t JOIN {ALLOWED} b ON 1=1"
        )
        assert validate_sql(sql) is None

    def test_postgres_with_recursive_self_reference_is_allowed(self) -> None:
        sql = (
            f"WITH RECURSIVE tree AS ("
            f"SELECT ID FROM {ALLOWED} "
            f"UNION ALL "
            f"SELECT ID FROM tree"
            f") SELECT t.ID FROM tree t JOIN {ALLOWED} b ON 1=1 LIMIT 5"
        )
        assert validate_sql(sql, dialect="postgres") is None

    def test_postgres_recursive_does_not_exempt_a_forbidden_real_table(self) -> None:
        """RECURSIVE only makes a CTE's *own* name visible in its body; a
        different, non-allowlisted table in that body is still refused."""
        sql = (
            f"WITH RECURSIVE tree AS ("
            f"SELECT ID FROM {FORBIDDEN} "
            f"UNION ALL "
            f"SELECT ID FROM tree"
            f") SELECT t.ID FROM tree t JOIN {ALLOWED} b ON 1=1 LIMIT 5"
        )
        _assert_refused_as_unknown_table(sql, dialect="postgres")


class TestNameCaseRules:
    """Rule 4: case-insensitive for tsql, quote-aware elsewhere."""

    def test_tsql_cte_reference_matches_case_insensitively(self) -> None:
        sql = (
            f"WITH Cte AS (SELECT ID FROM {ALLOWED}) "
            f"SELECT TOP 5 ID FROM cte JOIN {ALLOWED} b ON 1=1"
        )
        assert validate_sql(sql) is None

    def test_postgres_quoted_case_different_name_is_the_real_table(self) -> None:
        sql = (
            f'WITH {FORBIDDEN.lower()} AS (SELECT 1 AS a) '
            f'SELECT b.{_COL} FROM {ALLOWED} b JOIN "{FORBIDDEN}" s ON 1=1 LIMIT 5'
        )
        _assert_refused_as_unknown_table(sql, dialect="postgres")

    def test_postgres_unquoted_case_difference_binds_to_the_cte(self) -> None:
        sql = (
            f"WITH {FORBIDDEN.lower()} AS (SELECT ID FROM {ALLOWED}) "
            f"SELECT b.{_COL} FROM {ALLOWED} b JOIN {FORBIDDEN} s ON 1=1 LIMIT 5"
        )
        assert validate_sql(sql, dialect="postgres") is None


class TestOrdinaryCteUsageStaysAllowed:
    """CTE usage that is legitimate must not be refused by the scoping fix."""

    def test_simple_cte_over_allowlisted_table(self) -> None:
        sql = (
            f"WITH c AS (SELECT ID, {_COL} FROM {ALLOWED}) "
            f"SELECT TOP 5 {_COL} FROM c"
        )
        assert validate_sql(sql) is None

    def test_chained_ctes(self) -> None:
        sql = (
            f"WITH c1 AS (SELECT ID, {_COL} FROM {ALLOWED}), "
            f"c2 AS (SELECT {_COL} FROM c1) "
            f"SELECT TOP 5 {_COL} FROM c2"
        )
        assert validate_sql(sql) is None

    def test_cte_referenced_inside_later_ctes_nested_subquery(self) -> None:
        sql = (
            f"WITH c1 AS (SELECT ID FROM {ALLOWED}), "
            f"c2 AS (SELECT {_COL} FROM {ALLOWED} WHERE ID IN (SELECT ID FROM c1)) "
            f"SELECT TOP 5 {_COL} FROM c2"
        )
        assert validate_sql(sql) is None


class TestCteOnlyStatementsStillHaveNoTableReference:
    """A statement reading only CTEs reads no warehouse table."""

    def test_only_cte_references_is_refused_as_no_table_reference(self) -> None:
        sql = "WITH c AS (SELECT 1 AS a) SELECT TOP 5 a FROM c"
        assert _refusal_reason(sql) == "no_table_reference"

    def test_chained_constant_ctes_are_refused_as_no_table_reference(self) -> None:
        sql = (
            "WITH c1 AS (SELECT 1 AS a), c2 AS (SELECT a FROM c1) "
            "SELECT TOP 5 a FROM c2"
        )
        assert _refusal_reason(sql) == "no_table_reference"


class TestExtractTouchedTablesHonoursCteScope:
    """``extract_touched_tables`` must use the same scoped binding."""

    def test_cte_named_like_allowlisted_table_shadows_it(self) -> None:
        sql = f"WITH {ALLOWED} AS (SELECT 1 AS a) SELECT * FROM {ALLOWED}"
        assert extract_touched_tables(sql) == []

    def test_unrelated_cte_does_not_hide_a_real_read(self) -> None:
        sql = f"WITH x AS (SELECT 1 AS a) SELECT * FROM {ALLOWED}"
        assert extract_touched_tables(sql) == [ALLOWED]

    def test_cte_inside_subquery_does_not_hide_outer_read(self) -> None:
        sql = (
            f"SELECT b.{_COL} FROM {ALLOWED} b "
            f"JOIN (WITH {ALLOWED} AS (SELECT 1 AS a) SELECT a FROM {ALLOWED}) d ON 1=1"
        )
        assert extract_touched_tables(sql) == [ALLOWED]

    def test_qualified_name_is_not_hidden_by_same_named_cte(self) -> None:
        sql = (
            f"WITH {ALLOWED} AS (SELECT 1 AS a) "
            f"SELECT * FROM ref.{ALLOWED}"
        )
        assert extract_touched_tables(sql) == [ALLOWED]


class TestIsCteReference:
    """``is_cte_reference(table, dialect)`` applies the binding rule to one node."""

    def test_reference_in_main_body_is_a_cte_reference(self) -> None:
        sql = f"WITH c AS (SELECT ID FROM {ALLOWED}) SELECT ID FROM c"
        assert is_cte_reference(_table_named(sql, "c")) is True

    def test_real_table_in_cte_body_is_not_a_cte_reference(self) -> None:
        sql = f"WITH c AS (SELECT ID FROM {ALLOWED}) SELECT ID FROM c"
        assert is_cte_reference(_table_named(sql, ALLOWED)) is False

    def test_table_without_any_cte_is_not_a_cte_reference(self) -> None:
        sql = f"SELECT ID FROM {ALLOWED}"
        assert is_cte_reference(_table_named(sql, ALLOWED)) is False

    @pytest.mark.parametrize("qualified", [f"hr.{FORBIDDEN}", f"[db].[hr].[{FORBIDDEN}]"])
    def test_qualified_table_is_not_a_cte_reference(self, qualified: str) -> None:
        sql = f"WITH {FORBIDDEN} AS (SELECT 1 AS a) SELECT a FROM {qualified}"
        assert is_cte_reference(_table_named(sql, FORBIDDEN)) is False

    def test_forward_reference_is_not_a_cte_reference(self) -> None:
        sql = f"WITH a AS (SELECT * FROM {FORBIDDEN}), {FORBIDDEN} AS (SELECT 1 AS x) SELECT * FROM a"
        assert is_cte_reference(_table_named(sql, FORBIDDEN)) is False

    def test_backward_reference_is_a_cte_reference(self) -> None:
        sql = "WITH a AS (SELECT 1 AS x), b AS (SELECT x FROM a) SELECT x FROM b"
        assert is_cte_reference(_table_named(sql, "a")) is True

    def test_cte_in_sibling_subquery_is_not_visible(self) -> None:
        sql = (
            f"SELECT 1 FROM (WITH {FORBIDDEN} AS (SELECT 1 AS a) SELECT a FROM {FORBIDDEN}) d1 "
            f"JOIN (SELECT a FROM {FORBIDDEN}) d2 ON 1=1"
        )
        inside = _table_named(sql, FORBIDDEN, in_alias="d1")
        sibling = _table_named(sql, FORBIDDEN, in_alias="d2")
        assert is_cte_reference(inside) is True
        assert is_cte_reference(sibling) is False

    def test_outer_cte_is_visible_in_nested_subquery(self) -> None:
        sql = f"WITH c AS (SELECT ID FROM {ALLOWED}) SELECT 1 FROM {ALLOWED} WHERE ID IN (SELECT ID FROM c)"
        assert is_cte_reference(_table_named(sql, "c")) is True

    def test_tsql_self_reference_is_a_cte_reference(self) -> None:
        sql = f"WITH t AS (SELECT ID FROM {ALLOWED} UNION ALL SELECT ID FROM t) SELECT ID FROM t"
        assert is_cte_reference(_table_named(sql, "t", in_cte_body=True), "tsql") is True

    def test_postgres_non_recursive_self_reference_is_not_a_cte_reference(self) -> None:
        sql = "WITH t AS (SELECT ID FROM t) SELECT ID FROM t"
        inner = _table_named(sql, "t", dialect="postgres", in_cte_body=True)
        outer = _table_named(sql, "t", dialect="postgres", in_cte_body=False)
        assert is_cte_reference(inner, "postgres") is False
        assert is_cte_reference(outer, "postgres") is True

    def test_postgres_with_recursive_self_reference_is_a_cte_reference(self) -> None:
        sql = f"WITH RECURSIVE t AS (SELECT ID FROM {ALLOWED} UNION ALL SELECT ID FROM t) SELECT ID FROM t"
        assert is_cte_reference(_table_named(sql, "t", dialect="postgres", in_cte_body=True), "postgres") is True

    def test_tsql_name_match_is_case_insensitive(self) -> None:
        sql = f"WITH Cte AS (SELECT ID FROM {ALLOWED}) SELECT ID FROM cte"
        assert is_cte_reference(_table_named(sql, "cte")) is True

    def test_postgres_quoted_case_difference_is_not_a_cte_reference(self) -> None:
        sql = 'WITH secret AS (SELECT 1 AS a) SELECT a FROM "Secret"'
        assert is_cte_reference(_table_named(sql, "Secret", dialect="postgres"), "postgres") is False

    def test_postgres_unquoted_case_difference_is_a_cte_reference(self) -> None:
        sql = "WITH secret AS (SELECT 1 AS a) SELECT a FROM Secret"
        assert is_cte_reference(_table_named(sql, "Secret", dialect="postgres"), "postgres") is True
