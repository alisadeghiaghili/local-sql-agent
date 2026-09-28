# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for schema_data/registry.py (SchemaRegistry)."""

from __future__ import annotations

from unittest.mock import patch

from schema_data.registry import SchemaRegistry
from schema_data.columns import TABLE_COLUMNS
from schema_data.tables import TABLE_DESCRIPTIONS
from schema_data.relationships import RELATIONSHIPS


class TestSchemaRegistry:

    def test_context_is_string(self):
        table = next(iter(TABLE_COLUMNS))
        ctx = SchemaRegistry.build_context((table,))
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_includes_selected_table(self):
        ctx = SchemaRegistry.build_context(("Customer",))
        assert "Customer" in ctx

    def test_excludes_unselected_table(self):
        ctx = SchemaRegistry.build_context(("Customer",))
        assert "Ring" not in ctx

    def test_none_includes_all_tables(self):
        ctx = SchemaRegistry.build_context(None)
        for table in TABLE_COLUMNS:
            assert table in ctx

    def test_empty_tuple_includes_all_tables(self):
        ctx = SchemaRegistry.build_context(())
        for table in TABLE_COLUMNS:
            assert table in ctx

    def test_multiple_tables_included(self):
        tables = sorted(TABLE_COLUMNS)[:3]
        ctx = SchemaRegistry.build_context(tuple(tables))
        for table in tables:
            assert table in ctx

    def test_unknown_table_silently_skipped(self):
        ctx = SchemaRegistry.build_context(("NonExistentTable",))
        assert isinstance(ctx, str)
        assert "NonExistentTable" not in ctx

    def test_table_descriptions_not_empty(self):
        assert len(TABLE_DESCRIPTIONS) > 0

    def test_relationships_not_empty(self):
        assert len(RELATIONSHIPS) > 0


class TestSchemaContextDataSources:
    """Multiple warehouse data sources: ``build_schema_context`` adds a
    ``Data source:`` line per table, and a closing rule, ONLY when more
    than one source is configured -- see
    ``schema_data.registry._table_sources_if_several``. With no
    ``datasources.yaml`` (the default, and every other test in this file),
    the rendered block must stay byte-identical to before this feature.
    """

    def test_single_source_deployment_renders_no_data_source_lines(self):
        """The default shape (no ``datasources.yaml``): the schema block
        must be UNCHANGED by this feature -- no ``Data source:`` line, no
        closing rule, for any table."""
        ctx = SchemaRegistry.build_context(("Customer", "Ring"))
        assert "Data source:" not in ctx
        assert "must come from the same data source" not in ctx

    def test_several_sources_adds_a_data_source_line_per_table(self):
        with patch(
            "database.datasources.datasource_names", return_value=("main", "archive"),
        ), patch(
            "database.datasources.table_datasources",
            return_value={"Customer": "main", "Ring": "archive"},
        ):
            ctx = SchemaRegistry.build_context(("Customer", "Ring"))
        assert "Data source: main" in ctx
        assert "Data source: archive" in ctx

    def test_several_sources_adds_the_closing_rule_once(self):
        with patch(
            "database.datasources.datasource_names", return_value=("main", "archive"),
        ), patch(
            "database.datasources.table_datasources",
            return_value={"Customer": "main", "Ring": "archive"},
        ):
            ctx = SchemaRegistry.build_context(("Customer", "Ring"))
        assert ctx.count("must come from the same data source") == 1

    def test_several_sources_but_every_selected_table_shares_one_still_omits_the_rule(self):
        """The rule is about what THIS rendered block actually shows, not
        about the deployment as a whole -- two configured sources with
        only one of them represented among the selected tables must not
        print a rule about a distinction the model can't even see here."""
        with patch(
            "database.datasources.datasource_names", return_value=("main", "archive"),
        ), patch(
            "database.datasources.table_datasources",
            return_value={"Customer": "main", "Ring": "main"},
        ):
            ctx = SchemaRegistry.build_context(("Customer", "Ring"))
        assert "Data source: main" in ctx
        assert "must come from the same data source" not in ctx
