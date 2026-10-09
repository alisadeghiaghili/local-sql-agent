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

:func:`read_catalogue` is the fuller read behind ``scripts/sync_schema.py``:
exactly three metadata queries (columns with their data types and
nullability, primary keys, foreign keys) and nothing else. It keeps the
original letter case of every name and the declared keys, which the two
functions above do not.

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

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.engine import Engine

from schema_data.registry import effective_qualifier, split_table_key

__all__ = [
    "COLUMNS_SQL",
    "COLUMN_DETAILS_SQL",
    "FOREIGN_KEYS_SQL",
    "PRIMARY_KEYS_SQL",
    "TABLES_SQL",
    "ColumnInfo",
    "ForeignKey",
    "TableInfo",
    "default_schema",
    "format_sql_type",
    "list_columns",
    "list_tables",
    "read_catalogue",
    "table_location",
]

#: Every table and view of the connected database.
TABLES_SQL = "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"

#: Every column of every table and view of the connected database.
COLUMNS_SQL = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS"
)

#: Every table and view with its columns, data types and nullability, in
#: column order. ``LEFT JOIN`` keeps a table the login can see no column of
#: (one row, ``COLUMN_NAME`` NULL).
COLUMN_DETAILS_SQL = (
    "SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE, c.COLUMN_NAME, c.DATA_TYPE, "
    "c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION, c.NUMERIC_SCALE, c.IS_NULLABLE "
    "FROM INFORMATION_SCHEMA.TABLES t "
    "LEFT JOIN INFORMATION_SCHEMA.COLUMNS c "
    "ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME "
    "ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME, c.ORDINAL_POSITION"
)

#: The columns of every declared primary key, in key order.
PRIMARY_KEYS_SQL = (
    "SELECT k.TABLE_SCHEMA, k.TABLE_NAME, k.COLUMN_NAME "
    "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
    "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k "
    "ON k.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA AND k.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
    "WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' "
    "ORDER BY k.TABLE_SCHEMA, k.TABLE_NAME, k.ORDINAL_POSITION"
)

#: Every declared foreign key as one row per column pair: the referencing
#: column, then the column it references.
FOREIGN_KEYS_SQL = (
    "SELECT fk.TABLE_SCHEMA, fk.TABLE_NAME, fk.COLUMN_NAME, "
    "pk.TABLE_SCHEMA, pk.TABLE_NAME, pk.COLUMN_NAME, rc.CONSTRAINT_NAME "
    "FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc "
    "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE fk "
    "ON fk.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA AND fk.CONSTRAINT_NAME = rc.CONSTRAINT_NAME "
    "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE pk "
    "ON pk.CONSTRAINT_SCHEMA = rc.UNIQUE_CONSTRAINT_SCHEMA "
    "AND pk.CONSTRAINT_NAME = rc.UNIQUE_CONSTRAINT_NAME "
    "AND pk.ORDINAL_POSITION = fk.ORDINAL_POSITION "
    "ORDER BY fk.TABLE_SCHEMA, fk.TABLE_NAME, rc.CONSTRAINT_NAME, fk.ORDINAL_POSITION"
)


@dataclass(frozen=True)
class ColumnInfo:
    """One column as the catalogue describes it.

    Attributes
    ----------
    name:
        The column name in the database's own letter case.
    data_type:
        A short lower-case type such as ``int``, ``nvarchar(100)``,
        ``nvarchar(max)`` or ``decimal(18,2)`` (see :func:`format_sql_type`).
    nullable:
        Whether the column accepts NULL.
    """

    name: str
    data_type: str
    nullable: bool


@dataclass(frozen=True)
class ForeignKey:
    """A declared foreign key.

    Attributes
    ----------
    name:
        The constraint name (``""`` when the dialect gives none).
    columns:
        The referencing columns, in key order.
    ref_location:
        ``(schema, table)`` of the referenced table, lower-cased.
    ref_columns:
        The referenced columns, in key order, pairing with *columns*.
    """

    name: str
    columns: tuple[str, ...]
    ref_location: tuple[str, str]
    ref_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableInfo:
    """One table or view with its columns and declared keys.

    Attributes
    ----------
    schema, name:
        In the database's own letter case (``""`` for a dialect without
        schemas).
    is_view:
        True for a view.
    columns:
        In column order. Empty when the login can see no column of it.
    primary_key:
        The primary key's columns, in key order; ``()`` for none.
    foreign_keys:
        The declared foreign keys that start at this table.
    """

    schema: str
    name: str
    is_view: bool
    columns: tuple[ColumnInfo, ...]
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()

    @property
    def location(self) -> tuple[str, str]:
        """``(schema, table)``, lower-cased: the key of :func:`read_catalogue`'s result."""
        return (self.schema.lower(), self.name.lower())


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


_LENGTH_TYPES = frozenset({"char", "varchar", "nchar", "nvarchar", "binary", "varbinary"})
_EXACT_NUMERIC_TYPES = frozenset({"decimal", "numeric"})


def format_sql_type(
    data_type: str,
    length: int | None = None,
    precision: int | None = None,
    scale: int | None = None,
) -> str:
    """One short, comparable type string from ``INFORMATION_SCHEMA.COLUMNS`` fields.

    Parameters
    ----------
    data_type:
        ``DATA_TYPE`` (``int``, ``nvarchar``, ...).
    length:
        ``CHARACTER_MAXIMUM_LENGTH``; ``-1`` means ``max``.
    precision, scale:
        ``NUMERIC_PRECISION`` and ``NUMERIC_SCALE``, used for
        ``decimal``/``numeric`` only.

    Returns
    -------
    str
        Lower-case, no spaces: ``int``, ``nvarchar(100)``, ``varchar(max)``,
        ``decimal(18,2)``. The precision of ``int``, ``float`` and the date
        types is not part of the string.

    Examples
    --------
    >>> format_sql_type("int", None, 10, 0)
    'int'
    >>> format_sql_type("NVarChar", 100)
    'nvarchar(100)'
    >>> format_sql_type("varchar", -1)
    'varchar(max)'
    >>> format_sql_type("decimal", None, 18, 2)
    'decimal(18,2)'
    """
    base = str(data_type).strip().lower()
    if base in _LENGTH_TYPES and length is not None:
        return f"{base}(max)" if int(length) < 0 else f"{base}({int(length)})"
    if base in _EXACT_NUMERIC_TYPES and precision is not None:
        return f"{base}({int(precision)},{int(scale or 0)})"
    return base


def read_catalogue(engine: Engine) -> dict[tuple[str, str], TableInfo]:
    """Every table and view *engine*'s database has, with columns and keys.

    On SQL Server exactly three metadata queries run
    (:data:`COLUMN_DETAILS_SQL`, :data:`PRIMARY_KEYS_SQL`,
    :data:`FOREIGN_KEYS_SQL`) over one connection; no table is read. Any
    other dialect goes through SQLAlchemy's reflection.

    Parameters
    ----------
    engine:
        A warehouse engine (the application's own read-only one).

    Returns
    -------
    dict[tuple[str, str], TableInfo]
        ``{(schema, table): info}`` with the key lower-cased and the info
        keeping the database's own letter case.

    Raises
    ------
    sqlalchemy.exc.SQLAlchemyError
        If the database cannot be reached or a query is refused.

    Examples
    --------
    >>> from sqlalchemy import create_engine, text
    >>> engine = create_engine("sqlite://")
    >>> with engine.begin() as conn:
    ...     _ = conn.execute(text("CREATE TABLE Widget (ID INTEGER PRIMARY KEY, Name TEXT)"))
    >>> info = read_catalogue(engine)[("", "widget")]
    >>> [(c.name, c.data_type) for c in info.columns], info.primary_key
    ([('ID', 'integer'), ('Name', 'text')], ('ID',))
    """
    if engine.dialect.name == "mssql":
        with engine.connect() as conn:
            detail_rows = conn.execute(text(COLUMN_DETAILS_SQL)).fetchall()
            pk_rows = conn.execute(text(PRIMARY_KEYS_SQL)).fetchall()
            fk_rows = conn.execute(text(FOREIGN_KEYS_SQL)).fetchall()
        return _assemble(detail_rows, pk_rows, fk_rows)
    return _read_catalogue_reflected(engine)


def _assemble(
    detail_rows: Sequence[Sequence[object]],
    pk_rows: Sequence[Sequence[object]],
    fk_rows: Sequence[Sequence[object]],
) -> dict[tuple[str, str], TableInfo]:
    """Build the catalogue from the rows of the three SQL Server queries."""
    heads: dict[tuple[str, str], tuple[str, str, bool]] = {}
    columns: dict[tuple[str, str], list[ColumnInfo]] = {}
    for schema, table, table_type, column, data_type, length, precision, scale, nullable in detail_rows:
        location = (str(schema).lower(), str(table).lower())
        heads.setdefault(
            location, (str(schema), str(table), str(table_type).upper() == "VIEW"),
        )
        bucket = columns.setdefault(location, [])
        if column is not None:
            bucket.append(ColumnInfo(
                name=str(column),
                data_type=format_sql_type(str(data_type), length, precision, scale),
                nullable=str(nullable).upper() != "NO",
            ))
    keys: dict[tuple[str, str], list[str]] = {}
    for schema, table, column in pk_rows:
        keys.setdefault((str(schema).lower(), str(table).lower()), []).append(str(column))
    # One constraint is several rows (one per column pair), already in key order.
    pending: dict[tuple[tuple[str, str], str], list[tuple[str, tuple[str, str], str]]] = {}
    for schema, table, column, ref_schema, ref_table, ref_column, name in fk_rows:
        owner = (str(schema).lower(), str(table).lower())
        pending.setdefault((owner, str(name)), []).append((
            str(column), (str(ref_schema).lower(), str(ref_table).lower()), str(ref_column),
        ))
    foreign: dict[tuple[str, str], list[ForeignKey]] = {}
    for (owner, name), pairs in pending.items():
        foreign.setdefault(owner, []).append(ForeignKey(
            name=name,
            columns=tuple(p[0] for p in pairs),
            ref_location=pairs[0][1],
            ref_columns=tuple(p[2] for p in pairs),
        ))
    return {
        location: TableInfo(
            schema=schema, name=name, is_view=is_view,
            columns=tuple(columns.get(location, ())),
            primary_key=tuple(keys.get(location, ())),
            foreign_keys=tuple(foreign.get(location, ())),
        )
        for location, (schema, name, is_view) in heads.items()
    }


def _read_catalogue_reflected(engine: Engine) -> dict[tuple[str, str], TableInfo]:
    """The same catalogue through SQLAlchemy reflection (non-SQL Server)."""
    inspector = sa_inspect(engine)
    views = set(inspector.get_view_names())
    result: dict[tuple[str, str], TableInfo] = {}
    for name in [*inspector.get_table_names(), *sorted(views)]:
        pk = inspector.get_pk_constraint(name) or {}
        foreign = tuple(
            ForeignKey(
                name=str(fk.get("name") or ""),
                columns=tuple(str(c) for c in fk["constrained_columns"]),
                ref_location=(
                    str(fk.get("referred_schema") or "").lower(),
                    str(fk["referred_table"]).lower(),
                ),
                ref_columns=tuple(str(c) for c in fk["referred_columns"]),
            )
            for fk in inspector.get_foreign_keys(name)
        )
        result[("", name.lower())] = TableInfo(
            schema="", name=name, is_view=name in views,
            columns=tuple(
                ColumnInfo(
                    name=str(col["name"]),
                    data_type=str(col["type"]).lower().replace(" ", ""),
                    nullable=bool(col.get("nullable", True)),
                )
                for col in inspector.get_columns(name)
            ),
            primary_key=tuple(str(c) for c in pk.get("constrained_columns") or ()),
            foreign_keys=foreign,
        )
    return result
