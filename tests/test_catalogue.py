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
    COLUMN_DETAILS_SQL,
    COLUMNS_SQL,
    FOREIGN_KEYS_SQL,
    PRIMARY_KEYS_SQL,
    TABLES_SQL,
    ForeignKey,
    default_schema,
    format_sql_type,
    list_columns,
    list_tables,
    read_catalogue,
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


# ---------------------------------------------------------------------------
# read_catalogue -- types, nullability and declared keys, three queries
# ---------------------------------------------------------------------------

class TestFormatSqlType:
    @pytest.mark.parametrize("args, expected", [
        (("int", None, 10, 0), "int"),
        (("NVARCHAR", 100, None, None), "nvarchar(100)"),
        (("varchar", -1, None, None), "varchar(max)"),
        (("decimal", None, 18, 2), "decimal(18,2)"),
        (("numeric", None, 9, None), "numeric(9,0)"),
        (("float", None, 53, None), "float"),
        (("datetime2", None, None, None), "datetime2"),
        (("varbinary", -1, None, None), "varbinary(max)"),
        (("bit", None, None, None), "bit"),
    ])
    def test_length_and_precision_only_where_they_belong_to_the_type(self, args, expected):
        assert format_sql_type(*args) == expected


def _sql_server(details, keys, foreign):
    return _FakeSqlServerEngine({
        COLUMN_DETAILS_SQL: details, PRIMARY_KEYS_SQL: keys, FOREIGN_KEYS_SQL: foreign,
    })


class TestReadCatalogueSqlServer:
    DETAILS = [
        ("Sales", "Order", "BASE TABLE", "ID", "int", None, 10, 0, "NO"),
        ("Sales", "Order", "BASE TABLE", "CustomerID", "int", None, 10, 0, "YES"),
        ("Sales", "Order", "BASE TABLE", "Note", "nvarchar", 200, None, None, "YES"),
        ("Sales", "Customer", "BASE TABLE", "ID", "int", None, 10, 0, "NO"),
        ("Sales", "OrderView", "VIEW", "ID", "int", None, 10, 0, "YES"),
        ("Sales", "Hidden", "BASE TABLE", None, None, None, None, None, None),
    ]
    KEYS = [("Sales", "Order", "ID"), ("Sales", "Customer", "ID")]
    FOREIGN = [
        ("Sales", "Order", "CustomerID", "Sales", "Customer", "ID", "FK_Order_Customer"),
    ]

    def test_exactly_three_metadata_queries_run_and_nothing_else(self):
        engine = _sql_server(self.DETAILS, self.KEYS, self.FOREIGN)
        read_catalogue(engine)
        assert engine.executed == [COLUMN_DETAILS_SQL, PRIMARY_KEYS_SQL, FOREIGN_KEYS_SQL]

    def test_every_statement_reads_the_information_schema_only(self):
        for sql in (COLUMN_DETAILS_SQL, PRIMARY_KEYS_SQL, FOREIGN_KEYS_SQL):
            assert sql.startswith("SELECT ")
            assert "INFORMATION_SCHEMA." in sql
            assert ";" not in sql
            for word in ("INSERT", "UPDATE", "DELETE", "DROP", "EXEC"):
                assert word not in sql.upper().split()

    def test_tables_keep_their_case_and_are_keyed_lower_case(self):
        catalogue = read_catalogue(_sql_server(self.DETAILS, self.KEYS, self.FOREIGN))
        assert set(catalogue) == {
            ("sales", "order"), ("sales", "customer"), ("sales", "orderview"), ("sales", "hidden"),
        }
        order = catalogue[("sales", "order")]
        assert (order.schema, order.name, order.is_view) == ("Sales", "Order", False)
        assert order.location == ("sales", "order")
        assert catalogue[("sales", "orderview")].is_view is True

    def test_columns_have_types_nullability_and_keep_their_order(self):
        order = read_catalogue(_sql_server(self.DETAILS, self.KEYS, self.FOREIGN))[("sales", "order")]
        assert [(c.name, c.data_type, c.nullable) for c in order.columns] == [
            ("ID", "int", False), ("CustomerID", "int", True), ("Note", "nvarchar(200)", True),
        ]

    def test_primary_and_foreign_keys_are_attached_to_their_table(self):
        catalogue = read_catalogue(_sql_server(self.DETAILS, self.KEYS, self.FOREIGN))
        assert catalogue[("sales", "order")].primary_key == ("ID",)
        assert catalogue[("sales", "orderview")].primary_key == ()
        (fk,) = catalogue[("sales", "order")].foreign_keys
        assert fk == ForeignKey(
            name="FK_Order_Customer", columns=("CustomerID",),
            ref_location=("sales", "customer"), ref_columns=("ID",),
        )
        assert catalogue[("sales", "customer")].foreign_keys == ()

    def test_a_table_with_no_visible_column_is_listed_with_none(self):
        hidden = read_catalogue(_sql_server(self.DETAILS, self.KEYS, self.FOREIGN))[("sales", "hidden")]
        assert hidden.columns == ()

    def test_a_composite_key_keeps_its_column_order_and_pairing(self):
        foreign = [
            ("dbo", "Line", "OrderID", "dbo", "Order", "ID", "FK_Line"),
            ("dbo", "Line", "Seq", "dbo", "Order", "Seq", "FK_Line"),
        ]
        details = [("dbo", "Line", "BASE TABLE", "OrderID", "int", None, 10, 0, "NO"),
                   ("dbo", "Line", "BASE TABLE", "Seq", "int", None, 10, 0, "NO")]
        (fk,) = read_catalogue(_sql_server(details, [], foreign))[("dbo", "line")].foreign_keys
        assert fk.columns == ("OrderID", "Seq") and fk.ref_columns == ("ID", "Seq")


class TestReadCatalogueReflected:
    @pytest.fixture()
    def engine(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'k.db'}")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE Customer (ID INTEGER PRIMARY KEY, Name VARCHAR(40) NOT NULL)"))
            conn.execute(text(
                "CREATE TABLE Orders (ID INTEGER PRIMARY KEY, CustomerID INTEGER, "
                "FOREIGN KEY (CustomerID) REFERENCES Customer (ID))"
            ))
            conn.execute(text("CREATE VIEW OrderIds AS SELECT ID FROM Orders"))
        yield engine
        engine.dispose()

    def test_types_nullability_and_keys_come_from_reflection(self, engine):
        catalogue = read_catalogue(engine)
        customer = catalogue[("", "customer")]
        assert [(c.name, c.data_type) for c in customer.columns] == [
            ("ID", "integer"), ("Name", "varchar(40)"),
        ]
        assert [c.nullable for c in customer.columns][1] is False
        assert customer.primary_key == ("ID",)
        (fk,) = catalogue[("", "orders")].foreign_keys
        assert (fk.columns, fk.ref_location, fk.ref_columns) == (
            ("CustomerID",), ("", "customer"), ("ID",),
        )
        assert catalogue[("", "orderids")].is_view is True
