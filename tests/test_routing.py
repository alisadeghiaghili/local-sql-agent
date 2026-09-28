# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for ``database.routing``.

``security.sql_guard.TABLE_COLUMNS`` is bound once, at that module's own
first import, from whichever ``schema.yaml`` was active at that moment
(see ``appdb/config_versions.py``'s module docstring, "What takes effect
immediately, and what needs a restart") -- it cannot be swapped out for a
synthetic one mid-suite the way ``schema_data.registry``'s cache can. So
every test here drives :func:`~database.routing.resolve_datasource`
against REAL table names from whichever schema is loaded for this test
run (``PROJECT_CONFIG_DIR=project_config.example`` in CI: ``Order``,
``Customer``, ``Ring``, all genuinely queryable) and reassigns only the
TABLE-TO-SOURCE mapping, by patching
:func:`database.routing.table_datasources` /
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


def _patched(assignments: dict[str, str], default: str = "main"):
    return patch.multiple(
        "database.routing",
        table_datasources=lambda: assignments,
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
