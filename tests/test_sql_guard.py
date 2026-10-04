# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for security/sql_guard.py.

Covers clean_sql(), validate_sql(), and ensure_top().

Run::

    pytest tests/
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from schema_data.columns import TABLE_COLUMNS
from security.sql_guard import CorrectableRejection, clean_sql, ensure_top, validate_sql

#: A table (and one of its columns) picked dynamically from whatever
#: schema is loaded (real or project_config.example/), rather than a
#: hardcoded real name -- this case is about the GUARD's behaviour (does
#: it validate a CTE), not about any specific table or column, so it must
#: not incidentally depend on a real name that a generic example schema
#: does not define.
_ANY_TABLE = next(iter(TABLE_COLUMNS))
_ANY_COLUMN = next(iter(TABLE_COLUMNS[_ANY_TABLE]))

#: A SECOND known table, picked the same dynamic way, for the
#: cross-data-source rejection tests below (they need two distinct known
#: tables, not just one).
_ANOTHER_TABLE = next((t for t in TABLE_COLUMNS if t != _ANY_TABLE), _ANY_TABLE)


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

    def test_limit_on_cte_query_targets_outer_select_not_inner(self):
        """LIMIT->TOP conversion must land on the outer query, not the
        CTE body -- landing inside the CTE would materialise only n rows
        internally instead of capping the final result at n rows."""
        raw = "WITH c AS (SELECT x FROM t) SELECT * FROM c LIMIT 5"
        result = clean_sql(raw)
        assert result == "WITH c AS (SELECT x FROM t) SELECT TOP 5 * FROM c"

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
        validate_sql("SELECT TOP 10 Name FROM [sales].[Customer]")  # no raise

    def test_valid_cte_query(self):
        sql = (
            f"WITH cte AS (SELECT {_ANY_COLUMN} FROM [{_ANY_TABLE}]) "
            f"SELECT * FROM cte"
        )
        validate_sql(sql)

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

    def test_lands_on_outer_select_not_inside_cte(self):
        sql    = "WITH cte AS (SELECT 1) SELECT * FROM cte"
        result = ensure_top(sql, 20)
        assert result == "WITH cte AS (SELECT 1) SELECT TOP 20 * FROM cte"

    def test_distinct_precedes_top(self):
        sql    = "SELECT DISTINCT Name FROM Customer"
        result = ensure_top(sql, 20)
        assert result == "SELECT DISTINCT TOP 20 Name FROM Customer"

    def test_caps_outer_query_when_only_subquery_has_top(self):
        sql    = "SELECT * FROM (SELECT TOP 1 a FROM t) z"
        result = ensure_top(sql, 10)
        assert result == "SELECT TOP 10 * FROM (SELECT TOP 1 a FROM t) z"

    def test_wraps_top_level_union(self):
        sql    = "SELECT a FROM t1 UNION SELECT b FROM t2"
        result = ensure_top(sql, 5)
        assert result == (
            "SELECT TOP 5 * FROM (SELECT a FROM t1 UNION SELECT b FROM t2) "
            "AS _ensure_top_capped"
        )
        # The wrapped result is idempotent under a second call. (Not
        # counting "TOP" substrings here: the wrapper alias itself
        # contains "top", e.g. "_ensure_TOP_capped".)
        assert ensure_top(result, 5) == result

    def test_wraps_union_after_cte_without_touching_cte(self):
        sql = "WITH cte AS (SELECT 1 AS n) SELECT a FROM cte UNION SELECT b FROM t2"
        result = ensure_top(sql, 5)
        assert result == (
            "WITH cte AS (SELECT 1 AS n) SELECT TOP 5 * FROM "
            "(SELECT a FROM cte UNION SELECT b FROM t2) AS _ensure_top_capped"
        )

    def test_raises_when_no_select_found(self):
        with pytest.raises(ValueError):
            ensure_top("not sql at all", 10)

    def test_raises_for_union_with_top_level_order_by(self):
        """Wrapping a UNION that also has a trailing ORDER BY in a derived
        table would make that ORDER BY invalid T-SQL; correctly hoisting
        it out requires a real parser (Phase 1), so this must fail loudly
        instead of emitting broken SQL."""
        sql = "SELECT a FROM t1 UNION SELECT b FROM t2 ORDER BY a"
        with pytest.raises(ValueError):
            ensure_top(sql, 5)

    def test_order_by_inside_a_subquery_does_not_block_union_wrap(self):
        """Only a top-level (paren depth 0) ORDER BY must trigger the
        refusal above -- one that belongs to a branch's own subquery is
        unrelated to the wrapper. (ORDER BY inside a derived table is
        only valid T-SQL alongside TOP/OFFSET, hence the TOP 5 here.)"""
        sql = "SELECT a FROM (SELECT TOP 5 x FROM t1 ORDER BY x) s UNION SELECT b FROM t2"
        result = ensure_top(sql, 5)
        assert result.startswith("SELECT TOP 5 * FROM (")


# ---------------------------------------------------------------------------
# Multiple data sources -- reason="cross_datasource" (database.routing)
# ---------------------------------------------------------------------------

class TestCrossDatasourceRejection:
    """``_require_single_datasource`` -- see ``database.routing`` for the
    single-source-of-truth mapping this delegates to. With no
    ``datasources.yaml`` configured (every test elsewhere in this file),
    every table maps to the one default source and this rule never fires
    -- these tests patch ``database.routing.group_tables_by_datasource``
    directly to exercise the multi-source shape without needing a real
    ``datasources.yaml`` fixture."""

    def test_single_source_deployment_is_unaffected(self):
        """The default (single-source) shape: two known tables in one
        query, no ``datasources.yaml`` at all -- must not be refused."""
        validate_sql(
            f"SELECT a.{_ANY_COLUMN} FROM [{_ANY_TABLE}] a, [{_ANOTHER_TABLE}] b"
        )  # no raise

    def test_a_single_table_query_never_even_checks(self):
        """``len(tables) < 2`` short-circuits before
        ``group_tables_by_datasource`` is even imported -- a one-table
        query can never span two sources."""
        with patch("database.routing.group_tables_by_datasource") as mock_group:
            validate_sql(f"SELECT {_ANY_COLUMN} FROM [{_ANY_TABLE}]")
        mock_group.assert_not_called()

    def test_tables_in_two_sources_are_refused_as_cross_datasource(self):
        with patch(
            "database.routing.group_tables_by_datasource",
            return_value={"main": [_ANY_TABLE], "archive": [_ANOTHER_TABLE]},
        ):
            with pytest.raises(CorrectableRejection) as exc_info:
                validate_sql(
                    f"SELECT a.{_ANY_COLUMN} FROM [{_ANY_TABLE}] a, [{_ANOTHER_TABLE}] b"
                )
        assert exc_info.value.reason == "cross_datasource"
        assert exc_info.value.is_refusal is True
        assert _ANY_TABLE in str(exc_info.value)
        assert _ANOTHER_TABLE in str(exc_info.value)

    def test_two_known_tables_in_the_same_source_are_unaffected(self):
        with patch(
            "database.routing.group_tables_by_datasource",
            return_value={"main": [_ANY_TABLE, _ANOTHER_TABLE]},
        ):
            validate_sql(
                f"SELECT a.{_ANY_COLUMN} FROM [{_ANY_TABLE}] a, [{_ANOTHER_TABLE}] b"
            )  # no raise


class TestCrossDatasourceRejectionWithSharedTables:
    """The guard applies the executor's own rule (``choose_datasource``):
    a table listed under several sources counts for each of them, and the
    statement is refused only when no source has every table."""

    def _sources(self, assignments):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch.multiple(
            "database.routing",
            table_datasource_sets=lambda: assignments,
            default_datasource_name=lambda: "main",
        ))
        stack.enter_context(patch(
            "database.datasources.datasource_names", return_value=("main", "archive"),
        ))
        return stack

    _SQL = f"SELECT a.{_ANY_COLUMN} FROM [{_ANY_TABLE}] a, [{_ANOTHER_TABLE}] b"

    def test_a_shared_table_with_a_single_source_table_is_allowed(self):
        with self._sources({_ANY_TABLE: ("main", "archive"), _ANOTHER_TABLE: ("archive",)}):
            validate_sql(self._SQL)  # no raise

    def test_two_shared_tables_are_allowed(self):
        both = ("main", "archive")
        with self._sources({_ANY_TABLE: both, _ANOTHER_TABLE: both}):
            validate_sql(self._SQL)  # no raise

    def test_no_source_with_every_table_is_refused_and_says_where_each_is(self):
        with self._sources({_ANY_TABLE: ("main",), _ANOTHER_TABLE: ("archive",)}):
            with pytest.raises(CorrectableRejection) as exc_info:
                validate_sql(self._SQL)
        assert exc_info.value.reason == "cross_datasource"
        assert exc_info.value.is_refusal is True
        assert f"main: {_ANY_TABLE}" in str(exc_info.value)
        assert f"archive: {_ANOTHER_TABLE}" in str(exc_info.value)

    def test_guard_and_executor_agree(self):
        from database.routing import CrossDatasourceError, resolve_datasource

        cases = [
            {_ANY_TABLE: ("main", "archive"), _ANOTHER_TABLE: ("archive",)},
            {_ANY_TABLE: ("main",), _ANOTHER_TABLE: ("archive",)},
            {_ANY_TABLE: ("main", "archive"), _ANOTHER_TABLE: ("main", "archive")},
        ]
        for assignments in cases:
            with self._sources(assignments):
                try:
                    resolve_datasource(self._SQL)
                    executor_refuses = False
                except CrossDatasourceError:
                    executor_refuses = True
                try:
                    validate_sql(self._SQL)
                    guard_refuses = False
                except CorrectableRejection as exc:
                    assert exc.reason == "cross_datasource"
                    guard_refuses = True
            assert guard_refuses == executor_refuses, assignments


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
        with patch("database.connection.create_engine", return_value=mock_engine) as create, \
             patch("database.connection.install_idle_aware_ping"):
            get_engine.cache_clear()
            get_engine()                          # populate cache
            get_engine()                          # served from the cache
            assert create.call_count == 1
            dispose_engine()
            mock_engine.dispose.assert_called_once()
            get_engine()                          # cache was cleared: rebuilt
            assert create.call_count == 2
            get_engine.cache_clear()
