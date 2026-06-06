"""Unit tests for security/sql_guard.py.

Covers clean_sql(), validate_sql(), and ensure_top().

Run::

    pytest tests/
"""

from __future__ import annotations

import pytest

from security.sql_guard import clean_sql, ensure_top, validate_sql


# ---------------------------------------------------------------------------
# clean_sql
# ---------------------------------------------------------------------------

class TestCleanSql:
    def test_strips_markdown_fence(self):
        raw = "```sql\nSELECT TOP 10 * FROM [dbo].[Users]\n```"
        assert clean_sql(raw) == "SELECT TOP 10 * FROM [dbo].[Users]"

    def test_strips_plain_fence(self):
        raw = "```\nSELECT TOP 5 Name FROM [dbo].[T]\n```"
        assert "SELECT" in clean_sql(raw)

    def test_preserves_cte(self):
        raw = "WITH cte AS (SELECT 1 AS n) SELECT * FROM cte"
        assert clean_sql(raw).startswith("WITH")

    def test_drops_prose_preamble(self):
        raw = "Sure! Here is the SQL query:\nSELECT TOP 5 Name FROM [dbo].[T]"
        result = clean_sql(raw)
        assert result.startswith("SELECT")

    def test_converts_limit_to_top(self):
        raw = "SELECT Name FROM [dbo].[T] LIMIT 50"
        result = clean_sql(raw)
        assert "LIMIT" not in result.upper()
        assert "TOP 50" in result.upper()

    def test_limit_to_top_produces_valid_sql(self):
        """Converted SQL must end cleanly — no trailing space or empty token."""
        raw = "SELECT Name FROM [dbo].[T] LIMIT 50"
        result = clean_sql(raw)
        assert result == result.strip()
        assert not result.endswith(" ")

    def test_limit_dropped_when_top_already_present(self):
        """Bug fix: when TOP exists, LIMIT must be stripped cleanly."""
        raw = "SELECT TOP 10 Name FROM [dbo].[T] LIMIT 10"
        result = clean_sql(raw)
        assert "LIMIT" not in result.upper()
        assert "TOP 10" in result.upper()
        # The column list must still be intact
        assert "Name" in result
        assert "[dbo].[T]" in result

    def test_limit_dropped_preserves_where_clause(self):
        raw = "SELECT Name FROM [dbo].[T] WHERE Active=1 LIMIT 20"
        result = clean_sql(raw)
        assert "LIMIT" not in result.upper()
        assert "WHERE Active=1" in result
        assert "TOP 20" in result.upper()

    def test_fixes_top_distinct_order(self):
        raw = "SELECT TOP 10 DISTINCT [Name] FROM [dbo].[T]"
        result = clean_sql(raw)
        assert result.upper().startswith("SELECT DISTINCT TOP")

    def test_raises_on_empty_input(self):
        with pytest.raises(ValueError, match="empty"):
            clean_sql("")

    def test_raises_when_no_select_found(self):
        with pytest.raises(ValueError, match="No SELECT"):
            clean_sql("This text contains no SQL at all")


# ---------------------------------------------------------------------------
# validate_sql
# ---------------------------------------------------------------------------

class TestValidateSql:
    def test_valid_simple_select(self):
        validate_sql("SELECT TOP 10 Name FROM [dbo].[Users]")  # no raise

    def test_valid_cte_query(self):
        validate_sql("WITH cte AS (SELECT 1 AS n) SELECT * FROM cte")

    def test_blocks_delete(self):
        with pytest.raises(ValueError):
            validate_sql("DELETE FROM [dbo].[Users]")

    def test_blocks_drop(self):
        with pytest.raises(ValueError):
            validate_sql("DROP TABLE [dbo].[Users]")

    def test_blocks_update(self):
        with pytest.raises(ValueError):
            validate_sql("UPDATE [dbo].[Users] SET Name = 'x'")

    def test_blocks_insert(self):
        with pytest.raises(ValueError):
            validate_sql("INSERT INTO [dbo].[T] VALUES (1)")

    def test_blocks_non_select(self):
        with pytest.raises(ValueError):
            validate_sql("EXEC sp_helptext 'myProc'")

    def test_blocks_delete_embedded_in_select(self):
        with pytest.raises(ValueError, match="DELETE"):
            validate_sql("SELECT * FROM t WHERE DELETE FROM t")

    def test_blocks_drop_embedded_in_select(self):
        with pytest.raises(ValueError, match="DROP"):
            validate_sql("SELECT * FROM t; DROP TABLE t")

    def test_blocks_update_embedded_in_select(self):
        with pytest.raises(ValueError, match="UPDATE"):
            validate_sql("SELECT * FROM t; UPDATE t SET x=1")

    def test_blocks_information_schema(self):
        with pytest.raises(ValueError, match="INFORMATION_SCHEMA"):
            validate_sql("SELECT * FROM INFORMATION_SCHEMA.TABLES")

    def test_blocks_sys_catalogue(self):
        with pytest.raises(ValueError, match="SYS"):
            validate_sql("SELECT * FROM SYS.TABLES")

    def test_blocks_limit(self):
        with pytest.raises(ValueError, match="LIMIT"):
            validate_sql("SELECT Name FROM [dbo].[T] LIMIT 10")


# ---------------------------------------------------------------------------
# ensure_top
# ---------------------------------------------------------------------------

class TestEnsureTop:
    def test_injects_top_when_missing(self):
        sql    = "SELECT Name FROM [dbo].[Users]"
        result = ensure_top(sql, n=50)
        assert "TOP 50" in result.upper()

    def test_leaves_existing_top_untouched(self):
        sql    = "SELECT TOP 10 Name FROM [dbo].[Users]"
        result = ensure_top(sql, n=50)
        assert "TOP 10" in result.upper()
        assert "TOP 50" not in result.upper()

    def test_default_n_is_100(self):
        sql    = "SELECT Name FROM [dbo].[T]"
        result = ensure_top(sql)
        assert "TOP 100" in result.upper()

    def test_does_not_double_inject(self):
        sql    = "SELECT TOP 5 Name FROM [dbo].[T]"
        result = ensure_top(ensure_top(sql, 20), 20)
        assert result.upper().count("TOP") == 1


# ---------------------------------------------------------------------------
# dispose_engine
# ---------------------------------------------------------------------------

class TestDisposeEngine:
    def test_dispose_when_cache_empty_does_not_raise(self):
        """dispose_engine() must be safe to call before get_engine()."""
        from database.connection import dispose_engine, get_engine
        get_engine.cache_clear()   # ensure clean state
        dispose_engine()           # should not raise

    def test_dispose_clears_cache(self):
        from unittest.mock import patch, MagicMock
        from database.connection import dispose_engine, get_engine

        mock_engine = MagicMock()
        with patch("database.connection.create_engine", return_value=mock_engine):
            get_engine.cache_clear()
            get_engine()                          # populate cache
            assert get_engine.cache_info().currsize == 1
            dispose_engine()
            assert get_engine.cache_info().currsize == 0
            mock_engine.dispose.assert_called_once()
