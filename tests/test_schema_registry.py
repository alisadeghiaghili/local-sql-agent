"""Unit tests for schema/schema_registry.py."""

from __future__ import annotations

from schema.schema_registry import build_schema_context
from schema.table_schemas import TABLE_SCHEMAS
from schema.business_rules import BUSINESS_RULES
from schema.relationships import RELATIONSHIPS


class TestBuildSchemaContext:
    def test_returns_string(self):
        result = build_schema_context(("Contract",))
        assert isinstance(result, str)
        assert len(result) > 0

    def test_includes_selected_table(self):
        result = build_schema_context(("Customer",))
        assert "Customer" in result
        assert "[Auction_Dim].[Customer]" in result

    def test_excludes_unselected_table(self):
        result = build_schema_context(("Customer",))
        # Contract schema should not appear when not selected
        assert "[Auction_Fact].[Contract]" not in result

    def test_includes_business_rules(self):
        result = build_schema_context(("Contract",))
        # business rules are always prepended
        assert BUSINESS_RULES.strip()[:30] in result

    def test_includes_relationships(self):
        result = build_schema_context(("Contract",))
        assert "RELATIONSHIPS" in result

    def test_none_includes_all_tables(self):
        result = build_schema_context(None)
        for table in TABLE_SCHEMAS:
            assert table in result

    def test_empty_tuple_includes_all_tables(self):
        result = build_schema_context(())
        for table in TABLE_SCHEMAS:
            assert table in result

    def test_multiple_tables_included(self):
        result = build_schema_context(("Customer", "Contract", "Date"))
        assert "[Auction_Dim].[Customer]" in result
        assert "[Auction_Fact].[Contract]" in result
        assert "[General_Dim].[Date]" in result

    def test_cache_returns_same_object(self):
        # lru_cache should return identical object for same args
        r1 = build_schema_context(("Contract",))
        r2 = build_schema_context(("Contract",))
        assert r1 is r2

    def test_unknown_table_silently_skipped(self):
        # Should not raise; unknown table just produces no schema block
        result = build_schema_context(("NonExistentTable",))
        assert isinstance(result, str)
        assert "NonExistentTable" not in result
