# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``validate_sql`` with scoped (join-only) ``denied_columns`` entries.

Tables come from the loaded ``schema.yaml`` (CI: the committed example):
``Order`` (``ID``, ``CustomerID``, ``BrokerID``, ``TotalAmount``, ...),
``Customer`` (``ID``, ``Name``, ``NationalID``, ``IsActive``), ``Date``, all
in schema ``sales``, and ``Broker`` (``ID``, ``PersianName``) in ``ref``.
The default deployment has one data source; the source-scoping tests patch
two in (:func:`tests._source_fixtures.routed_sources`).
"""

from __future__ import annotations

import pytest

from security.sql_guard import CorrectableRejection, PolicyRejection, validate_sql
from tests._source_fixtures import routed_sources

_ORDER_ID = ["sales.Order.ID"]
_JOIN = "JOIN sales.Customer c ON o.CustomerID = c.ID"


def _ok(sql: str, entries: list[str]) -> None:
    validate_sql(sql, denied_columns=entries)


def _rejected(sql: str, entries: list[str], subject: str | None = None) -> CorrectableRejection:
    with pytest.raises(CorrectableRejection) as excinfo:
        validate_sql(sql, denied_columns=entries)
    exc = excinfo.value
    assert exc.reason == "join_only_column"
    if subject is not None:
        assert exc.subject == subject
    return exc


class TestAllowedAsAJoinKey:
    def test_qualified_equality_in_on(self):
        _ok("SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c ON o.ID = c.ID", _ORDER_ID)

    def test_either_side_of_the_equality(self):
        _ok("SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c ON c.ID = o.ID", _ORDER_ID)

    def test_unaliased_tables(self):
        _ok(
            "SELECT Customer.Name FROM sales.[Order] JOIN sales.Customer "
            "ON [Order].ID = Customer.ID",
            _ORDER_ID,
        )

    def test_nested_under_and_and_parentheses(self):
        _ok(
            "SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c "
            "ON (o.ID = c.ID AND (c.IsActive = 1)) AND o.TotalAmount > 5",
            _ORDER_ID,
        )

    def test_a_second_join_in_the_same_query(self):
        _ok(
            "SELECT c.Name FROM sales.Customer c JOIN sales.[Order] o ON c.ID = o.CustomerID "
            "JOIN sales.Date d ON o.ID = d.ID",
            _ORDER_ID,
        )

    def test_left_join(self):
        _ok("SELECT c.Name FROM sales.[Order] o LEFT JOIN sales.Customer c ON o.ID = c.ID", _ORDER_ID)

    def test_the_same_name_on_an_unrestricted_table_is_not_affected(self):
        _ok("SELECT c.ID, c.Name FROM sales.[Order] o " + _JOIN + " WHERE c.ID > 3", _ORDER_ID)

    def test_the_restricted_table_without_the_column(self):
        _ok("SELECT o.TotalAmount FROM sales.[Order] o " + _JOIN, _ORDER_ID)

    def test_unqualified_name_when_no_table_in_scope_is_restricted(self):
        _ok("SELECT ID FROM sales.Customer", _ORDER_ID)

    def test_count_star_reads_no_column(self):
        _ok("SELECT COUNT(*) FROM sales.[Order]", _ORDER_ID)

    def test_a_star_over_a_table_that_is_not_restricted(self):
        _ok("SELECT c.* FROM sales.[Order] o " + _JOIN, _ORDER_ID)


class TestRefusedEverywhereElse:
    def test_select_list(self):
        exc = _rejected("SELECT o.ID FROM sales.[Order] o", _ORDER_ID, "sales.Order.ID")
        text = str(exc)
        assert "'ID'" in text and "'Order'" in text and "JOIN ... ON a.col = b.col" in text
        assert exc.is_refusal is True

    def test_where(self):
        _rejected("SELECT o.TotalAmount FROM sales.[Order] o WHERE o.ID = 5", _ORDER_ID)

    def test_where_comparing_two_columns(self):
        _rejected(
            "SELECT c.Name FROM sales.[Order] o, sales.Customer c WHERE o.ID = c.ID", _ORDER_ID,
        )

    def test_group_by(self):
        _rejected(
            "SELECT COUNT(*) FROM sales.[Order] o " + _JOIN + " GROUP BY o.ID", _ORDER_ID,
        )

    def test_order_by(self):
        _rejected("SELECT o.TotalAmount FROM sales.[Order] o ORDER BY o.ID", _ORDER_ID)

    def test_having(self):
        _rejected(
            "SELECT o.TotalAmount FROM sales.[Order] o GROUP BY o.TotalAmount HAVING MAX(o.ID) > 1",
            _ORDER_ID,
        )

    def test_function_argument(self):
        _rejected("SELECT COUNT(o.ID) FROM sales.[Order] o", _ORDER_ID)
        _rejected("SELECT CAST(o.ID AS VARCHAR(20)) FROM sales.[Order] o", _ORDER_ID)

    def test_on_against_a_literal(self):
        _rejected("SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c ON o.ID = 5", _ORDER_ID)

    def test_on_equality_wrapped_in_a_function(self):
        _rejected(
            "SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c "
            "ON CAST(o.ID AS INT) = c.ID",
            _ORDER_ID,
        )

    def test_on_inequality(self):
        _rejected("SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c ON o.ID < c.ID", _ORDER_ID)

    def test_on_equality_under_or(self):
        _rejected(
            "SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c "
            "ON o.ID = c.ID OR c.IsActive = 1",
            _ORDER_ID,
        )

    def test_using(self):
        _rejected(
            "SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c USING (ID)", _ORDER_ID,
            "sales.Order.ID",
        )

    def test_a_subquery_in_where(self):
        _rejected(
            "SELECT c.Name FROM sales.Customer c WHERE c.ID IN (SELECT o.ID FROM sales.[Order] o)",
            _ORDER_ID,
        )

    def test_window_partition_and_order(self):
        _rejected(
            "SELECT ROW_NUMBER() OVER (PARTITION BY o.ID ORDER BY o.TotalAmount) FROM sales.[Order] o",
            _ORDER_ID,
        )
        _rejected(
            "SELECT ROW_NUMBER() OVER (ORDER BY o.ID) FROM sales.[Order] o", _ORDER_ID,
        )

    def test_case_insensitive_names(self):
        _rejected("SELECT o.id FROM sales.[Order] o", ["SALES.order.Id"], "SALES.order.Id")


class TestUnqualifiedReferences:
    def test_refused_when_a_table_in_the_select_is_restricted_for_the_name(self):
        exc = _rejected("SELECT ID FROM sales.[Order] o " + _JOIN, _ORDER_ID)
        assert "qualify it" in str(exc)

    def test_conservative_even_if_another_table_also_has_the_name(self):
        # Customer.ID is not restricted, but which one is meant cannot be proved.
        _rejected("SELECT ID FROM sales.Customer c JOIN sales.[Order] o ON c.ID = o.CustomerID", _ORDER_ID)

    def test_allowed_as_an_unqualified_join_key_pair(self):
        _ok("SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c ON o.CustomerID = c.ID", _ORDER_ID)

    def test_a_correlated_reference_reaches_the_outer_table(self):
        _rejected(
            "SELECT o.TotalAmount FROM sales.[Order] o WHERE EXISTS "
            "(SELECT 1 FROM sales.Customer c WHERE c.Name = 'x' AND TotalAmount > 0 AND o.ID = c.ID)",
            _ORDER_ID,
        )


class TestCtesAndDerivedTables:
    def test_refused_inside_the_cte_body_where_it_is_written(self):
        _rejected(
            "WITH x AS (SELECT o.ID, o.TotalAmount FROM sales.[Order] o) SELECT x.TotalAmount FROM x",
            _ORDER_ID,
        )

    def test_outer_references_to_a_cte_output_are_allowed(self):
        _ok(
            "WITH x AS (SELECT o.CustomerID, o.TotalAmount FROM sales.[Order] o) "
            "SELECT x.TotalAmount FROM x JOIN sales.Customer c ON x.CustomerID = c.ID",
            _ORDER_ID,
        )

    def test_a_cte_column_that_shares_the_restricted_name_is_not_the_restricted_column(self):
        _ok(
            "WITH x AS (SELECT c.ID AS ID FROM sales.Customer c) SELECT x.ID FROM x",
            _ORDER_ID,
        )

    def test_derived_table_body_is_checked(self):
        _rejected("SELECT d.TotalAmount FROM (SELECT o.ID, o.TotalAmount FROM sales.[Order] o) d", _ORDER_ID)

    def test_outer_references_to_a_derived_table_are_allowed(self):
        _ok(
            "SELECT d.TotalAmount FROM (SELECT o.TotalAmount, o.CustomerID FROM sales.[Order] o) d "
            "JOIN sales.Customer c ON d.CustomerID = c.ID WHERE d.TotalAmount > 0",
            _ORDER_ID,
        )

    def test_a_key_selected_inside_a_derived_table_is_refused_at_its_source(self):
        # The body is where the restricted column is written, so it is the body that is refused.
        _rejected(
            "SELECT c.Name FROM sales.Customer c JOIN "
            "(SELECT o.ID FROM sales.[Order] o) d ON c.ID = d.ID",
            _ORDER_ID,
        )

    def test_star_over_a_cte_is_allowed(self):
        _ok(
            "WITH x AS (SELECT o.TotalAmount FROM sales.[Order] o) SELECT * FROM x", _ORDER_ID,
        )

    def test_star_inside_the_cte_body_is_refused(self):
        _rejected("WITH x AS (SELECT * FROM sales.[Order]) SELECT x.TotalAmount FROM x", _ORDER_ID)

    def test_star_over_a_derived_table_is_allowed(self):
        _ok("SELECT * FROM (SELECT o.TotalAmount FROM sales.[Order] o) d", _ORDER_ID)


class TestStars:
    def test_bare_star(self):
        exc = _rejected("SELECT * FROM sales.[Order]", _ORDER_ID, "sales.Order.ID")
        assert "name the columns" in str(exc)

    def test_alias_star(self):
        _rejected("SELECT o.* FROM sales.[Order] o", _ORDER_ID, "sales.Order.ID")

    def test_star_across_a_join_reaches_the_restricted_table(self):
        _rejected("SELECT * FROM sales.[Order] o " + _JOIN, _ORDER_ID)

    def test_two_restricted_columns_leave_no_single_subject(self):
        exc = _rejected(
            "SELECT * FROM sales.[Order]", ["sales.Order.ID", "sales.Order.CustomerID"],
        )
        assert exc.subject is None

    def test_a_star_over_something_unenumerable_is_refused(self):
        exc = _rejected(
            "SELECT * FROM sales.Customer c CROSS APPLY (VALUES (1)) v(x)", _ORDER_ID,
        )
        assert exc.subject is None and "name the columns" in str(exc)


class TestNaturalJoin:
    def test_refused_over_a_table_with_a_restricted_column(self):
        # T-SQL has no NATURAL JOIN, so exercise the guard in a dialect that does.
        with pytest.raises(CorrectableRejection) as excinfo:
            validate_sql(
                "SELECT c.Name FROM sales.Order o NATURAL JOIN sales.Customer c",
                denied_columns=_ORDER_ID, dialect="postgres",
            )
        assert excinfo.value.reason == "join_only_column"


class TestLegacyEntriesAreUnchanged:
    def test_a_legacy_entry_still_denies_every_reference(self):
        with pytest.raises(PolicyRejection) as excinfo:
            validate_sql(
                "SELECT c.Name FROM sales.[Order] o " + _JOIN + " WHERE c.NationalID = '1'",
                denied_columns=["NationalID", "sales.Order.ID"],
            )
        assert excinfo.value.reason == "denied_column"

    def test_a_legacy_entry_also_denies_the_join_use(self):
        with pytest.raises(PolicyRejection):
            validate_sql(
                "SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c ON o.ID = c.ID",
                denied_columns=["ID"],
            )

    def test_legacy_and_scoped_together(self):
        entries = ["NationalID", "sales.Order.ID"]
        _ok("SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c ON o.ID = c.ID", entries)
        _rejected("SELECT o.ID FROM sales.[Order] o", entries)
        with pytest.raises(PolicyRejection):
            validate_sql("SELECT c.NationalID FROM sales.Customer c", denied_columns=entries)

    def test_the_legacy_star_message_is_unchanged(self):
        with pytest.raises(PolicyRejection, match=r"would expose denied column"):
            validate_sql("SELECT * FROM sales.Customer", denied_columns=["Name", "sales.Order.ID"])

    def test_no_entries_at_all(self):
        validate_sql("SELECT o.ID FROM sales.[Order] o", denied_columns=None)
        validate_sql("SELECT o.ID FROM sales.[Order] o", denied_columns=[])


class TestMisconfiguredEntries:
    @pytest.mark.parametrize("entry", ["sales.NoSuchTable.ID", "nowhere:sales.Order.ID", "sales.Order.Nope"])
    def test_an_entry_that_names_nothing_real_is_refused_not_ignored(self, entry):
        with pytest.raises(PolicyRejection) as excinfo:
            validate_sql("SELECT o.TotalAmount FROM sales.[Order] o", denied_columns=[entry])
        assert "column policy" in str(excinfo.value)
        assert entry in str(excinfo.value)

    def test_a_malformed_entry_is_refused_not_ignored(self):
        with pytest.raises(PolicyRejection):
            validate_sql("SELECT o.TotalAmount FROM sales.[Order] o", denied_columns=["a:b:c"])

    def test_the_rejection_is_not_retryable(self):
        # PolicyRejection: the self-correction loops stop at once.
        with pytest.raises(PolicyRejection):
            validate_sql("SELECT 1 FROM sales.[Order] o", denied_columns=["x:y:z"])


class TestDataSourceScoping:
    """``Order`` is listed under both sources and runs on the default (``main``)
    when it is the only table; joined with a table only ``archive`` has it
    runs on ``archive``."""

    _SETS = {"Order": ("main", "archive"), "Broker": ("archive",), "Customer": ("main",)}
    _ORDER_ALONE = "SELECT o.ID FROM sales.[Order] o"
    _ORDER_WITH_BROKER = (
        "SELECT o.ID FROM sales.[Order] o JOIN ref.Broker b ON o.BrokerID = b.ID"
    )

    def test_active_on_its_own_source(self):
        with routed_sources(self._SETS):
            _rejected(self._ORDER_ALONE, ["main:sales.Order.ID"], "main:sales.Order.ID")

    def test_inactive_on_the_other_source(self):
        with routed_sources(self._SETS):
            _ok(self._ORDER_ALONE, ["archive:sales.Order.ID"])

    def test_the_source_follows_the_other_tables_of_the_query(self):
        with routed_sources(self._SETS):
            _rejected(self._ORDER_WITH_BROKER, ["archive:sales.Order.ID"])
            _ok(self._ORDER_WITH_BROKER, ["main:sales.Order.ID"])

    def test_without_a_source_it_applies_on_both(self):
        with routed_sources(self._SETS):
            _rejected(self._ORDER_ALONE, ["sales.Order.ID"])
            _rejected(self._ORDER_WITH_BROKER, ["sales.Order.ID"])

    def test_source_column_form_covers_every_table_of_that_source(self):
        with routed_sources(self._SETS):
            _rejected("SELECT b.ID FROM ref.Broker b", ["archive:ID"], "archive:ID")
            _rejected(self._ORDER_ALONE, ["main:ID"])
            # Customer lives in `main` only: `archive:ID` leaves it alone.
            _ok("SELECT c.ID FROM sales.Customer c", ["archive:ID"])

    def test_source_column_form_still_allows_join_keys(self):
        with routed_sources(self._SETS):
            _ok(self._ORDER_WITH_BROKER.replace("SELECT o.ID", "SELECT o.TotalAmount"), ["archive:ID"])

    def test_an_entry_for_a_source_the_query_does_not_use_is_still_checked(self):
        with routed_sources(self._SETS):
            with pytest.raises(PolicyRejection):
                validate_sql(self._ORDER_ALONE, denied_columns=["nowhere:ID"])

    def test_a_table_in_the_other_source_cannot_be_named_for_this_one(self):
        with routed_sources(self._SETS):
            with pytest.raises(PolicyRejection) as excinfo:
                validate_sql(self._ORDER_ALONE, denied_columns=["archive:sales.Customer.ID"])
        assert "not in data source" in str(excinfo.value)


class TestGuardErrorContract:
    def test_the_reason_is_part_of_the_closed_set(self):
        from security.sql_guard import _REASONS
        from session.models import GuardVerdict

        assert "join_only_column" in _REASONS
        assert GuardVerdict(verdict="rejected", reason="join_only_column").reason == "join_only_column"

    def test_it_is_a_retryable_class(self):
        exc = _rejected("SELECT o.ID FROM sales.[Order] o", _ORDER_ID)
        assert isinstance(exc, CorrectableRejection) and not isinstance(exc, PolicyRejection)
