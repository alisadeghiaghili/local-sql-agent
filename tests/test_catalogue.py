# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for ``database.catalogue``: the two read-only metadata queries
behind the schema-drift placement hint and ``scripts/assign_datasources.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from database.catalogue import (
    COLUMNS_SQL,
    TABLES_SQL,
    default_schema,
    list_columns,
    list_tables,
    table_location,
)


class _FakeConnection:
    def __init__(self, rows_by_sql, executed):
        self._rows_by_sql = rows_by_sql
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement):
        sql = str(statement)
        self._executed.append(sql)
        return SimpleNamespace(fetchall=lambda: self._rows_by_sql[sql])


class _FakeSqlServerEngine:
    """Just enough engine to prove WHICH statements run on SQL Server."""

    dialect = SimpleNamespace(name="mssql")

    def __init__(self, rows_by_sql):
        self.executed: list[str] = []
        self._rows_by_sql = rows_by_sql

    def connect(self):
        return _FakeConnection(self._rows_by_sql, self.executed)


class TestTableLocation:
    @pytest.mark.parametrize("key, db_schema, default, expected", [
        ("Customer", "", "dbo", ("dbo", "customer")),
        ("Customer", "Sales", "dbo", ("sales", "customer")),
        ("sales.Customer", "", "dbo", ("sales", "customer")),
        ("[Sales].[Customer]", "", "dbo", ("sales", "customer")),
        ("Customer", "OtherDb.dbo", "dbo", ("dbo", "customer")),
        ("Linked.OtherDb.dbo.Customer", "", "dbo", ("dbo", "customer")),
        ("Customer", "", "", ("", "customer")),
    ])
    def test_schema_is_the_last_qualifier_part_and_case_is_ignored(
        self, key, db_schema, default, expected,
    ):
        assert table_location(key, db_schema, default) == expected


class TestSqlServerQueries:
    def test_list_tables_runs_exactly_the_tables_view_query(self):
        engine = _FakeSqlServerEngine({TABLES_SQL: [("Sales", "Order"), ("dbo", "Date")]})
        assert list_tables(engine) == {("sales", "order"), ("dbo", "date")}
        assert engine.executed == [TABLES_SQL]

    def test_list_columns_runs_exactly_the_columns_view_query(self):
        engine = _FakeSqlServerEngine({COLUMNS_SQL: [
            ("Sales", "Order", "ID"), ("Sales", "Order", "Total"), ("dbo", "Date", "ID"),
        ]})
        assert list_columns(engine) == {
            ("sales", "order"): {"id", "total"},
            ("dbo", "date"): {"id"},
        }
        assert engine.executed == [COLUMNS_SQL]

    def test_both_statements_only_read_the_information_schema(self):
        for sql in (TABLES_SQL, COLUMNS_SQL):
            assert sql.startswith("SELECT ")
            assert "INFORMATION_SCHEMA." in sql
            assert " WHERE " not in sql and ";" not in sql

    def test_default_schema_is_dbo_on_sql_server(self):
        assert default_schema(_FakeSqlServerEngine({})) == "dbo"


class TestOtherDialects:
    """SQLite fixtures have no INFORMATION_SCHEMA: SQLAlchemy reflection."""

    @pytest.fixture()
    def engine(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'w.db'}")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE Widget (ID INTEGER, Name TEXT)"))
            conn.execute(text("CREATE VIEW WidgetNames AS SELECT Name FROM Widget"))
        yield engine
        engine.dispose()

    def test_tables_and_views_are_listed_without_a_schema(self, engine):
        assert list_tables(engine) == {("", "widget"), ("", "widgetnames")}
        assert default_schema(engine) == ""

    def test_columns_are_listed_lower_cased(self, engine):
        assert list_columns(engine) == {
            ("", "widget"): {"id", "name"},
            ("", "widgetnames"): {"name"},
        }
