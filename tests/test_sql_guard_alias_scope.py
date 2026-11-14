# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Alias-scoping regression tests for security/sql_guard.py.

``validate_sql`` checks column references against the schema and the
optional ``denied_columns`` policy. A qualified reference such as ``c.Name``
or ``c.*`` must be resolved through the select it is written in, not through
one statement-wide alias map in which the last use of an alias wins. A
qualifier ``q`` binds to a source as follows:

1. The nearest enclosing select that has a source named or aliased ``q``
   provides the binding. Correlated references walk outward, one enclosing
   select at a time, until a select with that source is found.
2. An inner alias shadows an outer alias of the same name.
3. A qualifier written outside every select (for example in the ORDER BY of
   a UNION) falls back to the statement-wide names.
4. A qualifier naming a CTE or a derived table is not a real table: its
   columns are unconstrained without a policy, and ``q.*`` cannot be
   expanded when a denied-columns policy is active.

Before scoping was fixed, a statement that used the same alias for two
different tables in two subqueries resolved the alias to the wrong table:
a denied column could slip through, and legitimate columns were refused.

Test data: ``Customer`` has the columns ID, IsActive, Name, NationalID and
``Order`` has BrokerID, CustomerID, ID, OrderDate_ID, RingID, SymbolID. The
denied-columns policy used throughout is ``NationalID``. Assertions target
the rejection ``reason`` and the ``table`` attribute, never the full
message wording.

Run::

    pytest tests/test_sql_guard_alias_scope.py -v
"""

from __future__ import annotations

import pytest

from schema_data.columns import TABLE_COLUMNS
from security.sql_guard import (
    CorrectableRejection,
    PolicyRejection,
    validate_sql,
)

#: The denied-columns policy used by the policy-dependent cases.
DENIED = ["NationalID"]


def _refusal(
    sql: str, denied: list[str] | None = None
) -> CorrectableRejection | PolicyRejection:
    """Validate *sql* and return the rejection it raises.

    Fails the test if the statement is accepted, so a "must be refused"
    case cannot pass by returning ``None``.
    """
    with pytest.raises((CorrectableRejection, PolicyRejection)) as exc_info:
        validate_sql(sql, dialect="tsql", denied_columns=denied)
    return exc_info.value


def _assert_refused_as_unknown_column(sql: str, table: str) -> None:
    """Assert *sql* is refused as an unknown column of *table*."""
    rejection = _refusal(sql)
    assert isinstance(rejection, CorrectableRejection)
    assert rejection.reason == "other"
    assert str(rejection).startswith("Unknown column")
    assert rejection.table == table


def _assert_allowed(sql: str, denied: list[str] | None = None) -> None:
    """Assert *sql* passes validation."""
    assert validate_sql(sql, dialect="tsql", denied_columns=denied) is None


class TestFixtureAssumptions:
    """The shapes below must be decided by alias scoping alone."""

    def test_customer_and_order_columns_match_the_example_config(self) -> None:
        assert {"ID", "Name", "NationalID"} <= set(TABLE_COLUMNS["Customer"])
        assert {"ID", "BrokerID", "CustomerID"} <= set(TABLE_COLUMNS["Order"])
        assert "NationalID" not in TABLE_COLUMNS["Order"]
        assert "Name" not in TABLE_COLUMNS["Order"]
        assert "BrokerID" not in TABLE_COLUMNS["Customer"]

    def test_plain_denied_column_is_refused(self) -> None:
        rejection = _refusal("SELECT TOP 5 c.NationalID FROM Customer c", DENIED)
        assert rejection.reason == "denied_column"

    def test_plain_allowed_query_with_policy_is_allowed(self) -> None:
        _assert_allowed("SELECT TOP 5 c.ID, c.Name FROM Customer c", DENIED)


class TestSameAliasInSiblingDerivedTables:
    """One alias, two tables, in two sibling subqueries: each reference
    resolves through its own select."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT TOP 5 x.ID FROM (SELECT c.* FROM Customer c) x "
            "JOIN (SELECT c.ID FROM [Order] c) y ON x.ID = y.ID",
            "SELECT TOP 5 x.ID FROM (SELECT c.ID FROM [Order] c) y "
            "JOIN (SELECT c.* FROM Customer c) x ON x.ID = y.ID",
        ],
        ids=["star-over-customer-first", "star-over-customer-last"],
    )
    def test_star_over_customer_is_refused_whichever_subquery_comes_last(
        self, sql: str
    ) -> None:
        assert _refusal(sql, DENIED).reason == "denied_column"

    def test_star_over_order_is_allowed_when_a_later_alias_is_customer(self) -> None:
        """The star is over Order, which has no denied column."""
        sql = (
            "SELECT TOP 5 x.ID FROM (SELECT c.* FROM [Order] c) y "
            "JOIN (SELECT c.ID FROM Customer c) x ON x.ID = y.ID"
        )
        _assert_allowed(sql, DENIED)

    def test_each_alias_use_is_checked_against_its_own_table(self) -> None:
        sql = (
            "SELECT TOP 5 x.ID FROM (SELECT c.BrokerID, c.ID FROM [Order] c) y "
            "JOIN (SELECT c.ID, c.Name FROM Customer c) x ON x.ID = y.ID"
        )
        _assert_allowed(sql)

    def test_column_missing_on_the_aliased_order_table_is_refused(self) -> None:
        """``c.Name`` exists on Customer but this ``c`` is Order."""
        sql = (
            "SELECT TOP 5 x.ID FROM (SELECT c.Name, c.ID FROM [Order] c) y "
            "JOIN (SELECT c.ID FROM Customer c) x ON x.ID = y.ID"
        )
        _assert_refused_as_unknown_column(sql, "Order")


class TestCorrelatedReferences:
    """A reference inside a subquery that is not a source of that subquery
    resolves through the enclosing selects."""

    def test_correlated_outer_alias_is_allowed(self) -> None:
        sql = (
            "SELECT TOP 5 c.ID FROM Customer c "
            "WHERE EXISTS (SELECT 1 FROM [Order] o WHERE o.CustomerID = c.ID)"
        )
        _assert_allowed(sql)

    def test_unknown_column_through_a_correlated_outer_alias_is_refused(self) -> None:
        sql = (
            "SELECT TOP 5 c.ID FROM Customer c "
            "WHERE EXISTS (SELECT 1 FROM [Order] o WHERE o.CustomerID = c.NoSuchColumn)"
        )
        _assert_refused_as_unknown_column(sql, "Customer")

    def test_denied_column_through_a_correlated_outer_alias_is_refused(self) -> None:
        sql = (
            "SELECT TOP 5 c.ID FROM Customer c "
            "WHERE EXISTS (SELECT 1 FROM [Order] o WHERE o.CustomerID = c.NationalID)"
        )
        assert _refusal(sql, DENIED).reason == "denied_column"


class TestInnerAliasShadowsOuterAlias:
    """Rule 2: the nearest enclosing select that has the alias wins."""

    def test_column_valid_for_the_inner_table_is_allowed(self) -> None:
        sql = (
            "SELECT TOP 5 c.ID FROM Customer c "
            "WHERE EXISTS (SELECT 1 FROM [Order] c WHERE c.BrokerID = 1)"
        )
        _assert_allowed(sql)

    def test_column_valid_only_for_the_outer_table_is_refused(self) -> None:
        sql = (
            "SELECT TOP 5 c.ID FROM Customer c "
            "WHERE EXISTS (SELECT 1 FROM [Order] c WHERE c.Name = 'x')"
        )
        _assert_refused_as_unknown_column(sql, "Order")


class TestQualifierOutsideEverySelect:
    """Rule 3: with no enclosing select, the statement-wide names apply."""

    def test_union_order_by_qualifier_is_allowed(self) -> None:
        sql = (
            "SELECT c.ID FROM Customer c UNION SELECT o.ID FROM [Order] o "
            "ORDER BY c.ID"
        )
        _assert_allowed(sql)


class TestDerivedTableAndCteQualifiers:
    """Rule 4: a derived table or CTE is not a real table."""

    def test_star_over_derived_table_is_refused_with_a_policy(self) -> None:
        sql = "SELECT TOP 5 d.* FROM (SELECT c.ID FROM Customer c) d"
        assert _refusal(sql, DENIED).reason == "denied_column"

    def test_star_over_derived_table_is_allowed_without_a_policy(self) -> None:
        _assert_allowed("SELECT TOP 5 d.* FROM (SELECT c.ID FROM Customer c) d")

    def test_any_column_name_on_a_derived_table_is_allowed_without_a_policy(self) -> None:
        _assert_allowed("SELECT TOP 5 d.Anything FROM (SELECT c.ID FROM Customer c) d")

    def test_any_column_name_on_a_cte_is_allowed_without_a_policy(self) -> None:
        sql = (
            "WITH k AS (SELECT c.ID FROM Customer c) "
            "SELECT TOP 5 k.Anything FROM k"
        )
        _assert_allowed(sql)
