# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for schema_data/registry.py (SchemaRegistry)."""

from __future__ import annotations

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
        assert "CustomerContract" not in ctx or "Customer" in ctx

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
