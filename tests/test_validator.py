"""Unit tests for sql_agent.validator.

Run with::

    pytest tests/
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from sql_agent.validator import clean_sql, validate_sql, ensure_top


# ---------------------------------------------------------------------------
# clean_sql
# ---------------------------------------------------------------------------

class TestCleanSql:
    def test_strips_fenced_block(self):
        raw = "```sql\nSELECT TOP 10 * FROM [dbo].[Users]\n```"
        assert clean_sql(raw) == "SELECT TOP 10 * FROM [dbo].[Users]"

    def test_keeps_cte(self):
        raw = "WITH cte AS (SELECT 1 AS n) SELECT * FROM cte"
        assert clean_sql(raw).startswith("WITH")

    def test_drops_preamble(self):
        raw = "Sure! Here is the SQL:\nSELECT TOP 5 * FROM [dbo].[T]"
        assert clean_sql(raw).startswith("SELECT")

    def test_converts_limit_to_top(self):
        raw = "SELECT * FROM [dbo].[T] LIMIT 50"
        result = clean_sql(raw)
        assert "LIMIT" not in result.upper()
        assert "TOP 50" in result.upper()

    def test_fixes_top_distinct_order(self):
        raw = "SELECT TOP 10 DISTINCT [Name] FROM [dbo].[T]"
        result = clean_sql(raw)
        assert result.upper().startswith("SELECT DISTINCT TOP")

    def test_raises_on_empty(self):
        with pytest.raises(ValueError, match="empty"):
            clean_sql("")

    def test_raises_on_no_select(self):
        with pytest.raises(ValueError, match="No SELECT"):
            clean_sql("This is just plain text with no SQL")


# ---------------------------------------------------------------------------
# validate_sql
# ---------------------------------------------------------------------------

class TestValidateSql:
    def test_valid_select(self):
        validate_sql("SELECT TOP 10 * FROM [dbo].[Users]")  # must not raise

    def test_valid_cte(self):
        validate_sql("WITH cte AS (SELECT 1 AS n) SELECT * FROM cte")

    def test_raises_on_delete(self):
        with pytest.raises(ValueError, match="DELETE"):
            validate_sql("DELETE FROM [dbo].[Users]")

    def test_raises_on_drop(self):
        with pytest.raises(ValueError, match="DROP"):
            validate_sql("DROP TABLE [dbo].[Users]")

    def test_raises_on_update(self):
        with pytest.raises(ValueError, match="UPDATE"):
            validate_sql("UPDATE [dbo].[Users] SET Name = 'x'")

    def test_raises_on_no_from(self):
        with pytest.raises(ValueError, match="FROM"):
            validate_sql("SELECT 1")

    def test_raises_on_limit(self):
        with pytest.raises(ValueError, match="LIMIT"):
            validate_sql("SELECT * FROM [dbo].[T] LIMIT 10")

    def test_raises_on_non_select(self):
        with pytest.raises(ValueError):
            validate_sql("INSERT INTO [dbo].[T] VALUES (1)")


# ---------------------------------------------------------------------------
# ensure_top
# ---------------------------------------------------------------------------

class TestEnsureTop:
    def test_injects_top_when_missing(self):
        sql    = "SELECT [Name] FROM [dbo].[Users]"
        result = ensure_top(sql, n=50)
        assert "TOP 50" in result.upper()

    def test_leaves_existing_top_untouched(self):
        sql    = "SELECT TOP 10 [Name] FROM [dbo].[Users]"
        result = ensure_top(sql, n=50)
        assert "TOP 10" in result.upper()
        assert "TOP 50" not in result.upper()

    def test_default_n_is_100(self):
        sql    = "SELECT [Name] FROM [dbo].[T]"
        result = ensure_top(sql)
        assert "TOP 100" in result.upper()
