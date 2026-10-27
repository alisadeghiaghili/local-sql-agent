# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``security.sql_guard.column_origins``: which stored column does a reference read?

The function answers, for a caller that looks at an already validated
statement, the question the guard asks of a column when it checks a denied or
join-only entry, with the table named: through aliases, derived tables and
CTEs. It decides nothing the guard allows or refuses, and
``tests/test_filter_value_check_pins.py`` pins that the guard still decides as
before. Tables of ``project_config.example/schema.yaml``: ``Order`` and
``Customer`` (``sales``), ``Ring`` and ``Broker`` (``ref``).
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from security.sql_guard import column_origins, validate_sql


class TestTables:
    def test_alias_with_as_and_without(self):
        for source in ("[ref].[Ring] AS r", "[ref].[Ring] r"):
            tree = sqlglot.parse_one(f"SELECT 1 FROM {source} WHERE r.Name = 'x'", read="tsql")
            column = next(tree.find_all(exp.Column))
            assert column_origins(column, tree) == (("Ring", "name"),)

    def test_a_table_written_with_its_schema_and_no_alias(self):
        tree = sqlglot.parse_one("SELECT 1 FROM [ref].[Ring] WHERE [ref].[Ring].[Name] = 'x'", read="tsql")
        assert column_origins(next(tree.find_all(exp.Column)), tree) == (("Ring", "name"),)

    def test_the_other_side_of_a_join(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM [sales].[Order] o JOIN [ref].[Ring] r ON o.RingID = r.ID WHERE r.Name = 'x'",
            read="tsql",
        )
        column = [c for c in tree.find_all(exp.Column) if c.name == "Name"][0]
        assert column_origins(column, tree) == (("Ring", "name"),)

    def test_an_unqualified_name_is_the_table_that_has_it(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM [sales].[Order] o JOIN [ref].[Ring] r ON o.RingID = r.ID WHERE Name = 'x'",
            read="tsql",
        )
        column = [c for c in tree.find_all(exp.Column) if c.name == "Name"][0]
        assert column_origins(column, tree) == (("Ring", "name"),)

    def test_an_unqualified_name_two_tables_share_is_both(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM [sales].[Customer] c, [ref].[Ring] r WHERE Name = 'x'", read="tsql",
        )
        column = [c for c in tree.find_all(exp.Column) if c.name == "Name"][0]
        assert column_origins(column, tree) == (("Customer", "name"), ("Ring", "name"))

    def test_a_column_the_table_does_not_have_is_nothing(self):
        tree = sqlglot.parse_one("SELECT 1 FROM [ref].[Ring] r WHERE r.Nope = 'x'", read="tsql")
        assert column_origins(next(tree.find_all(exp.Column)), tree) == ()


class TestCtesAndDerivedTables:
    @pytest.mark.parametrize(
        "sql",
        [
            "WITH c AS (SELECT * FROM [ref].[Ring]) SELECT 1 FROM c WHERE c.Name = 'x'",
            "WITH c AS (SELECT r.Name FROM [ref].[Ring] r) SELECT 1 FROM c WHERE c.Name = 'x'",
            "WITH c AS (SELECT r.Name AS Label FROM [ref].[Ring] r) SELECT 1 FROM c WHERE c.Label = 'x'",
            "WITH c AS (SELECT r.* FROM [ref].[Ring] r) SELECT 1 FROM c WHERE Name = 'x'",
            "SELECT 1 FROM (SELECT r.Name AS Label FROM [ref].[Ring] r) d WHERE d.Label = 'x'",
            "SELECT 1 FROM (SELECT * FROM [ref].[Ring]) d WHERE d.Name = 'x'",
            "WITH a AS (SELECT * FROM [ref].[Ring]), b AS (SELECT a.Name AS Label FROM a) "
            "SELECT 1 FROM b WHERE b.Label = 'x'",
        ],
    )
    def test_the_column_is_followed_to_its_table(self, sql):
        tree = sqlglot.parse_one(sql, read="tsql")
        column = [c for c in tree.find_all(exp.Column) if c.name in ("Name", "Label")][-1]
        assert column_origins(column, tree) == (("Ring", "name"),)

    def test_a_literal_inside_the_body_is_checked_against_the_body_s_table(self):
        tree = sqlglot.parse_one(
            "WITH c AS (SELECT r.ID FROM [ref].[Ring] r WHERE r.Name = 'x') SELECT 1 FROM c", read="tsql",
        )
        column = [c for c in tree.find_all(exp.Column) if c.name == "Name"][0]
        assert column_origins(column, tree) == (("Ring", "name"),)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 FROM (SELECT UPPER(r.Name) AS Label FROM [ref].[Ring] r) d WHERE d.Label = 'x'",
            "SELECT 1 FROM (SELECT 'x' AS Label) d WHERE d.Label = 'x'",
            "SELECT 1 FROM (SELECT r.ID AS Label FROM [ref].[Ring] r) d WHERE d.Other = 'x'",
        ],
    )
    def test_a_computed_or_missing_column_is_not_a_stored_one(self, sql):
        tree = sqlglot.parse_one(sql, read="tsql")
        column = [c for c in tree.find_all(exp.Column) if c.name in ("Label", "Other")][-1]
        assert column_origins(column, tree) == ()

    def test_a_union_is_followed_branch_by_branch(self):
        tree = sqlglot.parse_one(
            "SELECT x.Label FROM (SELECT r.Name AS Label FROM [ref].[Ring] r "
            "UNION ALL SELECT b.PersianName FROM [ref].[Broker] b) x WHERE x.Label = 'v'",
            read="tsql",
        )
        column = [c for c in tree.find_all(exp.Column) if c.name == "Label"][-1]
        assert column_origins(column, tree) == (("Ring", "name"), ("Broker", "persianname"))

    def test_a_recursive_cte_ends_without_an_answer(self):
        tree = sqlglot.parse_one(
            "WITH c AS (SELECT r.ID AS Name FROM [ref].[Ring] r UNION ALL SELECT c.Name FROM c) "
            "SELECT 1 FROM c WHERE c.Name = 'x'",
            read="tsql",
        )
        column = [c for c in tree.find_all(exp.Column) if c.name == "Name"][-1]
        assert column_origins(column, tree) == ()


class TestScopes:
    def test_a_subquery_in_a_where(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM [sales].[Order] o WHERE o.RingID IN "
            "(SELECT r.ID FROM [ref].[Ring] r WHERE r.Name = 'x')",
            read="tsql",
        )
        column = [c for c in tree.find_all(exp.Column) if c.name == "Name"][0]
        assert column_origins(column, tree) == (("Ring", "name"),)

    def test_a_correlated_reference_reaches_the_outer_select(self):
        tree = sqlglot.parse_one(
            "SELECT 1 FROM [ref].[Ring] r WHERE EXISTS "
            "(SELECT 1 FROM [sales].[Order] o WHERE o.RingID = r.ID AND r.Name = 'x')",
            read="tsql",
        )
        column = [c for c in tree.find_all(exp.Column) if c.name == "Name"][0]
        assert column_origins(column, tree) == (("Ring", "name"),)

    def test_the_order_by_of_a_union_is_nothing(self):
        tree = sqlglot.parse_one(
            "SELECT r.Name FROM [ref].[Ring] r UNION SELECT b.PersianName FROM [ref].[Broker] b ORDER BY Name",
            read="tsql",
        )
        column = [c for c in tree.find_all(exp.Column) if c.name == "Name"][-1]
        assert column_origins(column, tree) == ()

    def test_a_table_valued_source_is_nothing(self):
        tree = sqlglot.parse_one("SELECT 1 FROM dbo.SomeFunction(1) f WHERE f.Name = 'x'", read="tsql")
        assert column_origins(next(tree.find_all(exp.Column)), tree) == ()


def test_the_guard_decides_as_before():
    """Calling it changes nothing: the same statement is accepted before and after."""
    sql = "SELECT TOP 5 r.Name FROM [ref].[Ring] r WHERE r.Name = N'x'"
    validate_sql(sql)
    tree = sqlglot.parse_one(sql, read="tsql")
    column_origins(next(tree.find_all(exp.Column)), tree)
    validate_sql(sql)
