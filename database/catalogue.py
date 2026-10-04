# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Which tables and columns a data source has, read from its catalogue.

Two metadata queries, nothing else::

    SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
    SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS

Both run through the engine the caller passes (the application's own
read-only engine, :func:`database.connection.get_engine`) and return names
only, never row data. :func:`list_tables` backs the schema-drift hint that
says a table lives in another data source (:mod:`schema_data.drift`);
both functions back ``scripts/assign_datasources.py``.

Names are compared case-insensitively, the way SQL Server's default
collation compares identifiers, and a table is identified by its schema and
bare name. A multi-part qualifier (``OtherDb.dbo``) is matched on its last
part, the schema: the catalogue views describe the database the engine is
connected to, so a table that ``schema.yaml`` places in another database on
the same server is looked up under that schema in this one.

Any dialect other than SQL Server (the SQLite fixtures the tests use) is
read through SQLAlchemy's own reflection instead, because only SQL Server
is guaranteed to have ``INFORMATION_SCHEMA``.
"""

from __future__ import annotations

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.engine import Engine

from schema_data.registry import effective_qualifier, split_table_key

__all__ = [
    "COLUMNS_SQL",
    "TABLES_SQL",
    "default_schema",
    "list_columns",
    "list_tables",
    "table_location",
]

#: Every table and view of the connected database.
TABLES_SQL = "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"

#: Every column of every table and view of the connected database.
COLUMNS_SQL = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS"
)


def default_schema(engine: Engine) -> str:
    """The schema an unqualified table name resolves to on *engine*.

    Parameters
    ----------
    engine:
        A warehouse engine.

    Returns
    -------
    str
        ``"dbo"`` for SQL Server, ``""`` for any other dialect (no schema).
    """
    return "dbo" if engine.dialect.name == "mssql" else ""


def table_location(
    key: str, db_schema: str = "", default: str = "dbo",
) -> tuple[str, str]:
    """Where the catalogue lists the table that ``schema.yaml`` key *key* names.

    Parameters
    ----------
    key:
        A ``schema.yaml`` table key (``"Customer"``, ``"sales.Customer"``,
        ``"OtherDb.dbo.Customer"``).
    db_schema:
        The table's ``db_schema`` (or its dot-joined effective qualifier),
        used when *key* carries no qualifier of its own.
    default:
        The schema for a table with no qualifier at all; see
        :func:`default_schema`.

    Returns
    -------
    tuple[str, str]
        ``(schema, table)``, both lower-cased. The schema is the last
        part of the qualifier.

    Examples
    --------
    >>> table_location("Customer")
    ('dbo', 'customer')
    >>> table_location("sales.Customer")
    ('sales', 'customer')
    >>> table_location("Customer", "OtherDb.dbo")
    ('dbo', 'customer')
    >>> table_location("Customer", default="")
    ('', 'customer')
    """
    qualifier = effective_qualifier(key, db_schema)
    schema = qualifier[-1] if qualifier else default
    return (schema.lower(), split_table_key(key).name.lower())


def list_tables(engine: Engine) -> frozenset[tuple[str, str]]:
    """Every table and view *engine*'s database has.

    Parameters
    ----------
    engine:
        A warehouse engine; one ``INFORMATION_SCHEMA.TABLES`` query on SQL
        Server.

    Returns
    -------
    frozenset[tuple[str, str]]
        ``(schema, table)`` pairs, lower-cased, ``""`` for the schema on a
        dialect without schemas.
    """
    if engine.dialect.name == "mssql":
        with engine.connect() as conn:
            rows = conn.execute(text(TABLES_SQL)).fetchall()
        return frozenset((str(s).lower(), str(t).lower()) for s, t in rows)
    inspector = sa_inspect(engine)
    names = [*inspector.get_table_names(), *inspector.get_view_names()]
    return frozenset(("", name.lower()) for name in names)


def list_columns(engine: Engine) -> dict[tuple[str, str], frozenset[str]]:
    """Every column of every table and view *engine*'s database has.

    Parameters
    ----------
    engine:
        A warehouse engine; one ``INFORMATION_SCHEMA.COLUMNS`` query on
        SQL Server.

    Returns
    -------
    dict[tuple[str, str], frozenset[str]]
        ``{(schema, table): {column, ...}}``, all lower-cased.
    """
    columns: dict[tuple[str, str], set[str]] = {}
    if engine.dialect.name == "mssql":
        with engine.connect() as conn:
            rows = conn.execute(text(COLUMNS_SQL)).fetchall()
        for schema, table, column in rows:
            columns.setdefault((str(schema).lower(), str(table).lower()), set()).add(
                str(column).lower()
            )
    else:
        inspector = sa_inspect(engine)
        for name in [*inspector.get_table_names(), *inspector.get_view_names()]:
            columns[("", name.lower())] = {
                str(col["name"]).lower() for col in inspector.get_columns(name)
            }
    return {key: frozenset(names) for key, names in columns.items()}
