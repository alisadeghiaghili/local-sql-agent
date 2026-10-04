# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for ``database.routing``.

``security.sql_guard``'s table lookup is built from whichever
``schema.yaml`` is loaded for the run; swapping it mid-suite needs
:func:`security.sql_guard.refresh_schema_lookup`, which these tests do
not need. So every test here drives
:func:`~database.routing.resolve_datasource` against REAL table names
from the loaded schema (``PROJECT_CONFIG_DIR=project_config.example`` in
CI: ``Order``, ``Customer``, ``Ring``, all genuinely queryable) and
reassigns only the TABLE-TO-SOURCE mapping, by patching
:func:`database.routing.table_datasource_sets` /
:func:`database.routing.default_datasource_name` directly -- the one part
of the picture that genuinely is just data, re-readable at any time.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from database.routing import (
    CrossDatasourceError,
    group_tables_by_datasource,
    resolve_datasource,
    target_datasource_or_none,
)


def _patched(assignments: dict[str, str | tuple[str, ...]], default: str = "main"):
    """Patch the table-to-sources mapping; a plain string is a table in one source."""
    sets = {
        table: (value,) if isinstance(value, str) else tuple(value)
        for table, value in assignments.items()
    }
    return patch.multiple(
        "database.routing",
        table_datasource_sets=lambda: sets,
        default_datasource_name=lambda: default,
    )


class TestGroupTablesByDatasource:
    def test_empty_input_returns_empty_dict(self):
        assert group_tables_by_datasource([]) == {}

    def test_single_source_groups_everything_together(self):
        with _patched({"Order": "main", "Customer": "main"}):
            groups = group_tables_by_datasource(["Order", "Customer"])
        assert groups == {"main": ["Customer", "Order"]}

    def test_several_sources_split_into_separate_groups(self):
        with _patched({"Order": "main", "Ring": "archive"}):
            groups = group_tables_by_datasource(["Order", "Ring"])
        assert groups == {"main": ["Order"], "archive": ["Ring"]}

    def test_an_unassigned_table_falls_back_to_the_default_source(self):
        with _patched({"Order": "archive"}, default="main"):
            groups = group_tables_by_datasource(["Order", "Customer"])
        assert groups == {"archive": ["Order"], "main": ["Customer"]}

    def test_duplicate_tables_are_deduplicated_within_a_group(self):
        with _patched({"Order": "main"}):
            groups = group_tables_by_datasource(["Order", "Order"])
        assert groups == {"main": ["Order"]}


class TestCrossDatasourceError:
    def test_message_names_every_source_and_its_tables(self):
        exc = CrossDatasourceError({"main": ["Order"], "archive": ["Ring"]})
        assert "main" in str(exc)
        assert "archive" in str(exc)
        assert "Order" in str(exc)
        assert "Ring" in str(exc)
        assert exc.groups == {"main": ["Order"], "archive": ["Ring"]}


class TestResolveDatasource:
    def test_no_table_reference_uses_the_default_source(self):
        with _patched({}, default="main"):
            assert resolve_datasource("SELECT 1") == "main"

    def test_single_table_resolves_to_its_source(self):
        with _patched({"Order": "archive"}, default="main"):
            assert resolve_datasource("SELECT ID FROM [sales].[Order]") == "archive"

    def test_two_tables_in_the_same_source_resolve_together(self):
        with _patched({"Order": "main", "Customer": "main"}):
            sql = (
                "SELECT o.ID FROM [sales].[Order] o "
                "JOIN [sales].[Customer] c ON c.ID = o.CustomerID"
            )
            assert resolve_datasource(sql) == "main"

    def test_tables_in_two_sources_are_refused(self):
        with _patched({"Order": "main", "Ring": "archive"}):
            sql = (
                "SELECT o.ID FROM [sales].[Order] o "
                "JOIN [ref].[Ring] r ON r.ID = o.RingID"
            )
            with pytest.raises(CrossDatasourceError) as exc_info:
                resolve_datasource(sql)
        assert exc_info.value.groups == {"main": ["Order"], "archive": ["Ring"]}

    def test_a_table_not_named_in_schema_yaml_at_all_is_ignored(self):
        """``extract_touched_tables`` only reports tables it recognises --
        an unknown one is silently absent from *tables*, so it can never
        by itself trigger a cross-datasource refusal."""
        with _patched({}, default="main"):
            assert resolve_datasource("SELECT * FROM NotARealTable") == "main"

    def test_dialect_argument_is_threaded_through(self):
        with _patched({"Order": "main"}):
            with patch(
                "security.sql_guard.extract_touched_tables", return_value=["Order"],
            ) as mock_extract:
                resolve_datasource("SELECT 1 FROM [sales].[Order]", dialect="postgres")
        assert mock_extract.call_args.kwargs["dialect"] == "postgres"


class TestTargetDatasourceOrNone:
    def test_no_sql_means_no_source(self):
        assert target_datasource_or_none(None) is None
        assert target_datasource_or_none("") is None

    def test_single_source_statement_names_its_source(self):
        with _patched({"Order": "archive"}, default="main"):
            assert target_datasource_or_none("SELECT * FROM [Order]") == "archive"

    def test_cross_source_statement_records_no_single_source(self):
        with _patched({"Order": "main", "Ring": "archive"}):
            sql = "SELECT * FROM [Order] o JOIN Ring r ON 1 = 1"
            assert target_datasource_or_none(sql) is None


class TestSharedTables:
    """A table listed under several sources (``datasource: [A, B]``): the
    candidates for a statement are the intersection of its tables' source
    sets; the default source if it is one, else the first in
    ``datasources.yaml`` order."""

    _ORDER = ("main", "archive", "cold")

    def _routed(self, assignments, default="main"):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(_patched(assignments, default=default))
        stack.enter_context(patch(
            "database.datasources.datasource_names", return_value=self._ORDER,
        ))
        return stack

    def test_a_statement_reading_only_a_shared_table_runs_on_the_default(self):
        with self._routed({"Order": ("main", "archive")}):
            assert resolve_datasource("SELECT ID FROM [sales].[Order]") == "main"

    def test_without_the_default_among_them_the_first_in_config_order_wins(self):
        # Written in the opposite order on purpose: the pick must not
        # depend on how schema.yaml happened to list them.
        with self._routed({"Order": ("cold", "archive")}):
            assert resolve_datasource("SELECT ID FROM [sales].[Order]") == "archive"

    def test_a_shared_table_joins_with_a_table_of_either_of_its_sources(self):
        sql = (
            "SELECT o.ID FROM [sales].[Order] o "
            "JOIN [sales].[Customer] c ON c.ID = o.CustomerID"
        )
        with self._routed({"Order": ("main", "archive"), "Customer": "archive"}):
            assert resolve_datasource(sql) == "archive"
        with self._routed({"Order": ("main", "archive"), "Customer": "main"}):
            assert resolve_datasource(sql) == "main"

    def test_the_intersection_of_several_shared_tables_is_used(self):
        sql = (
            "SELECT o.ID FROM [sales].[Order] o "
            "JOIN [sales].[Customer] c ON c.ID = o.CustomerID"
        )
        with self._routed({"Order": ("main", "archive"), "Customer": ("archive", "cold")}):
            assert resolve_datasource(sql) == "archive"

    def test_an_empty_intersection_is_refused_naming_sources_and_tables(self):
        sql = (
            "SELECT o.ID FROM [sales].[Order] o "
            "JOIN [sales].[Customer] c ON c.ID = o.CustomerID "
            "JOIN [ref].[Ring] r ON r.ID = o.RingID"
        )
        with self._routed({
            "Order": ("main", "archive"), "Customer": "main", "Ring": "archive",
        }):
            with pytest.raises(CrossDatasourceError) as exc_info:
                resolve_datasource(sql)
        # The shared table is listed under each of its sources.
        assert exc_info.value.groups == {
            "main": ["Customer", "Order"], "archive": ["Order", "Ring"],
        }
        text = str(exc_info.value)
        assert "more than one data source" in text
        assert "main: Customer, Order" in text
        assert "archive: Order, Ring" in text
        assert "Available in several data sources: Order (archive, main)" in text

    def test_a_refusal_without_shared_tables_reads_as_before(self):
        exc = CrossDatasourceError({"main": ["Order"], "archive": ["Ring"]})
        assert "several" not in str(exc)

    def test_group_tables_lists_a_shared_table_under_every_source(self):
        with _patched({"Order": ("main", "archive"), "Ring": "archive"}):
            groups = group_tables_by_datasource(["Order", "Ring"])
        assert groups == {"main": ["Order"], "archive": ["Order", "Ring"]}

    def test_the_audit_target_follows_the_same_rule(self):
        with self._routed({"Order": ("cold", "archive")}):
            assert target_datasource_or_none("SELECT ID FROM [sales].[Order]") == "archive"

    def test_choose_datasource_returns_none_for_no_tables(self):
        from database.routing import choose_datasource

        assert choose_datasource([]) is None

    def test_vocabulary_prefetch_and_value_resolver_sql_route_to_the_default(self):
        """Both build one single-table statement from the schema.yaml key;
        for a table in several sources that is the shared-table rule."""
        from retrieval.dimension_vocabulary import _prefetch_query
        from retrieval.value_resolver import _build_query

        statements = [
            _prefetch_query("Customer", "Name"),
            _build_query("Customer", "Name"),
        ]
        with self._routed({"Customer": ("cold", "archive", "main")}):
            assert [resolve_datasource(sql) for sql in statements] == ["main", "main"]
        with self._routed({"Customer": ("cold", "archive")}):
            assert [resolve_datasource(sql) for sql in statements] == ["archive", "archive"]
