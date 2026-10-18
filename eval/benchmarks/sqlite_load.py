# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Copy a SQLite database into one schema of a SQL Server database.

The benchmark importer (:mod:`eval.benchmarks.bird`) publishes each source
database as a schema of one target database, so the production pipeline reads
it through its normal read-only login and the 6.13 multi-schema and exclusion
features are exercised. This module is the loader, and is independent of any
benchmark:

* The source is reflected with SQLAlchemy (tables, columns, primary and foreign
  keys) and opened **read-only**; the original file is never modified.
* Types are mapped *conservatively*: what the data holds decides, not what the
  column was declared as (SQLite does not enforce declared types). A column
  becomes ``BIGINT`` only if every value is an integer, ``FLOAT`` (53 bits, an
  IEEE double, so values round-trip exactly) if numbers, ``NVARCHAR(n)`` as soon
  as one value is text (dates stay text, exactly as stored), ``VARBINARY(MAX)``
  for blobs. See :func:`column_kind`.
* Table and column names are kept and always quoted.
* Rows are inserted in chunks through a typed Core insert, which is what
  ``fast_executemany`` on pyodbc batches.
* Primary and foreign keys are added **after** the data, in their own
  statements, and a key the target refuses (SQL Server compares text without
  regard to case by default, SQLite does not) is reported as a warning, never
  fatal: the data are what matters.
* Afterwards every table's row count on the target is compared with the source,
  and then its **content** (:func:`compare_contents`): each row is reduced to a
  canonical form (integers as integers, floats rounded to 12 significant digits,
  text exactly as it is, trailing spaces and case included, blobs by their SHA-256)
  and hashed, and the sum of the row hashes is an order-independent digest of the
  table that is computed in Python on both sides, chunk by chunk.
* The load refuses a schema that already holds tables unless ``replace`` is
  given, so running it twice cannot silently merge two copies.

The target login needs DDL rights. That is deliberately not the application's
read-only login; see runbook 18.7.
"""

from __future__ import annotations

import hashlib
import sqlite3
import warnings
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy import types as sa_types
from sqlalchemy.engine import Engine

__all__ = [
    "ColumnKind",
    "ColumnObservation",
    "ContentReport",
    "LoadReport",
    "SchemaNotEmpty",
    "SourceColumn",
    "SourceForeignKey",
    "SourceTable",
    "TableContent",
    "TableLoad",
    "collation_problem",
    "column_kind",
    "compare_contents",
    "create_target_engine",
    "database_collation",
    "digest_rows",
    "load_sqlite_schema",
    "open_sqlite_readonly",
    "post_load_statements",
    "reflect_source",
    "schema_is_empty",
    "target_table",
    "text_length",
    "verify_loaded",
]

#: What a source column becomes on the target.
ColumnKind = Literal["integer", "float", "text", "blob"]

#: ``NVARCHAR`` lengths a text column is rounded up to. Longer than the last
#: bucket (``NVARCHAR`` holds 4000 characters short of ``MAX``) is ``MAX``.
_TEXT_BUCKETS: tuple[int, ...] = (16, 32, 64, 128, 255, 450, 1000, 4000)

#: A primary key on SQL Server may be at most 900 bytes: 450 ``NVARCHAR`` characters.
_PK_MAX_CHARS = 450

#: Rows per ``INSERT`` batch.
DEFAULT_CHUNK = 5000


class SchemaNotEmpty(ValueError):
    """A target schema already holds tables and ``replace`` was not given."""


@dataclass(frozen=True, slots=True)
class ColumnObservation:
    """What a source column actually holds, from one pass over the table.

    Parameters
    ----------
    rows:
        Rows in the table.
    has_text, has_real, has_int, has_blob:
        Whether at least one value is stored as that SQLite storage class.
    max_text_len:
        Longest text value, in characters.
    nulls:
        NULL values in the column.
    """

    rows: int = 0
    has_text: bool = False
    has_real: bool = False
    has_int: bool = False
    has_blob: bool = False
    max_text_len: int = 0
    nulls: int = 0


@dataclass(frozen=True, slots=True)
class SourceColumn:
    """One reflected column and the type it gets on the target."""

    name: str
    declared: str
    nullable: bool
    kind: ColumnKind
    observed: ColumnObservation
    pk_position: int = 0

    @property
    def length(self) -> int | None:
        """``NVARCHAR`` length for a text column, ``None`` for ``MAX`` (and non-text)."""
        return text_length(self.observed.max_text_len) if self.kind == "text" else None


@dataclass(frozen=True, slots=True)
class SourceForeignKey:
    """A declared foreign key of the source: columns of this table to columns of another."""

    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceTable:
    """One reflected table."""

    name: str
    columns: tuple[SourceColumn, ...]
    foreign_keys: tuple[SourceForeignKey, ...] = ()
    rows: int = 0

    @property
    def primary_key(self) -> tuple[SourceColumn, ...]:
        return tuple(sorted((c for c in self.columns if c.pk_position), key=lambda c: c.pk_position))


@dataclass(frozen=True, slots=True)
class TableLoad:
    """The outcome of loading one table."""

    name: str
    source_rows: int
    target_rows: int
    columns: int
    text_widened: tuple[str, ...] = ()

    @property
    def matches(self) -> bool:
        return self.source_rows == self.target_rows


@dataclass(slots=True)
class LoadReport:
    """The outcome of loading one schema."""

    schema: str
    tables: list[TableLoad] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def mismatched(self) -> list[TableLoad]:
        """Tables whose target count differs from the source."""
        return [t for t in self.tables if not t.matches]

    @property
    def ok(self) -> bool:
        """Every table loaded with equal row counts."""
        return not self.mismatched

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "ok": self.ok,
            "tables": {
                t.name: {
                    "source_rows": t.source_rows,
                    "target_rows": t.target_rows,
                    "columns": t.columns,
                    "match": t.matches,
                }
                for t in self.tables
            },
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


def open_sqlite_readonly(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite file read-only, decoding text leniently.

    A byte sequence that is not UTF-8 becomes the replacement character instead
    of aborting the load; the loader and the reference queries both read through
    this function, so they see the same text.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.

    Examples
    --------
    >>> import tempfile, os
    >>> d = tempfile.mkdtemp()
    >>> p = os.path.join(d, "x.sqlite")
    >>> c = sqlite3.connect(p); _ = c.execute("CREATE TABLE t (a)"); c.close()
    >>> conn = open_sqlite_readonly(p)
    >>> conn.execute("INSERT INTO t VALUES (1)")
    Traceback (most recent call last):
        ...
    sqlite3.OperationalError: attempt to write a readonly database
    >>> conn.close()
    """
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite file not found: {resolved}")
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    conn.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
    return conn


def _affinity(sa_type: Any) -> str:
    """The SQLite affinity of a reflected type: ``integer``, ``real``, ``text``, ``blob`` or ``numeric``.

    Examples
    --------
    >>> [_affinity(t) for t in (sa_types.INTEGER(), sa_types.TEXT(), sa_types.REAL(),
    ...                         sa_types.DATE(), sa_types.BLOB(), sa_types.NullType())]
    ['integer', 'text', 'real', 'numeric', 'blob', 'blob']
    """
    if isinstance(sa_type, (sa_types.Integer, sa_types.Boolean)):
        return "integer"
    if isinstance(sa_type, sa_types.String):
        return "text"
    if isinstance(sa_type, sa_types.LargeBinary) or isinstance(sa_type, sa_types.NullType):
        return "blob"
    if isinstance(sa_type, sa_types.Float):
        return "real"
    return "numeric"


def column_kind(declared_affinity: str, observed: ColumnObservation) -> ColumnKind:
    """The target type family of a column, decided by what it holds.

    * any text value -> ``text`` (a date, a code stored as a string, a column
      that mixes numbers and text);
    * otherwise any real -> ``float``;
    * otherwise integers only -> ``integer``;
    * only blobs -> ``blob``; a blob mixed with anything else is ``text``;
    * no value at all -> the declared affinity decides (``blob`` and
      ``text`` both give ``text``).

    Parameters
    ----------
    declared_affinity:
        ``integer``, ``real``, ``text``, ``blob`` or ``numeric`` (:func:`_affinity`).
    observed:
        From the table scan.

    Examples
    --------
    >>> column_kind("integer", ColumnObservation(rows=3, has_int=True))
    'integer'
    >>> column_kind("integer", ColumnObservation(rows=3, has_int=True, has_real=True))
    'float'
    >>> column_kind("numeric", ColumnObservation(rows=3, has_int=True, has_text=True))
    'text'
    >>> column_kind("numeric", ColumnObservation())
    'float'
    >>> column_kind("text", ColumnObservation())
    'text'
    >>> column_kind("blob", ColumnObservation(rows=1, has_blob=True))
    'blob'
    >>> column_kind("blob", ColumnObservation(rows=1, has_blob=True, has_int=True))
    'text'
    >>> column_kind("blob", ColumnObservation())
    'text'
    """
    if observed.has_text or (observed.has_blob and (observed.has_real or observed.has_int)):
        return "text"
    if observed.has_real:
        return "float"
    if observed.has_int:
        return "integer"
    if observed.has_blob:
        return "blob"
    return {"integer": "integer", "real": "float", "numeric": "float"}.get(declared_affinity, "text")


def text_length(max_len: int) -> int | None:
    """The ``NVARCHAR`` length for values up to *max_len* characters; ``None`` is ``MAX``.

    Examples
    --------
    >>> text_length(0), text_length(10), text_length(33), text_length(450), text_length(451)
    (16, 16, 64, 450, 1000)
    >>> text_length(4001) is None
    True
    """
    for bucket in _TEXT_BUCKETS:
        if max_len <= bucket:
            return bucket
    return None


def _observe(conn: sqlite3.Connection, table: str, columns: Sequence[str]) -> list[ColumnObservation]:
    """One pass over *table*: the storage classes, longest text and NULL count of each column."""
    quoted_table = _q(table)
    parts: list[str] = []
    for name in columns:
        c = _q(name)
        parts.append(
            f"COALESCE(MAX(typeof({c}) = 'text'), 0), COALESCE(MAX(typeof({c}) = 'real'), 0), "
            f"COALESCE(MAX(typeof({c}) = 'integer'), 0), COALESCE(MAX(typeof({c}) = 'blob'), 0), "
            f"COALESCE(MAX(CASE WHEN typeof({c}) = 'text' THEN length({c}) END), 0), "
            f"COALESCE(SUM({c} IS NULL), 0)"
        )
    row = conn.execute(f"SELECT COUNT(*), {', '.join(parts)} FROM {quoted_table}").fetchone()
    rows = int(row[0])
    result: list[ColumnObservation] = []
    for i in range(len(columns)):
        t, r, n, b, ln, nulls = row[1 + 6 * i: 7 + 6 * i]
        result.append(
            ColumnObservation(
                rows=rows, has_text=bool(t), has_real=bool(r), has_int=bool(n), has_blob=bool(b),
                max_text_len=int(ln), nulls=int(nulls),
            )
        )
    return result


def _q(identifier: str) -> str:
    """A SQLite identifier, quoted."""
    return '"' + identifier.replace('"', '""') + '"'


def reflect_source(path: str | Path) -> list[SourceTable]:
    """Reflect a SQLite file into :class:`SourceTable` records (read-only).

    Parameters
    ----------
    path:
        The ``.sqlite`` file.

    Returns
    -------
    list[SourceTable]
        Every user table, in name order, with the target type of each column
        decided from its data (:func:`column_kind`).

    Examples
    --------
    >>> import tempfile, os
    >>> p = os.path.join(tempfile.mkdtemp(), "x.sqlite")
    >>> c = sqlite3.connect(p)
    >>> _ = c.executescript('''
    ... CREATE TABLE Parent (ID INTEGER PRIMARY KEY, Name TEXT, Born DATE);
    ... CREATE TABLE Child (ID INTEGER PRIMARY KEY, ParentID INTEGER REFERENCES Parent(ID), Score REAL);
    ... INSERT INTO Parent VALUES (1, 'a', '2020-01-02');
    ... INSERT INTO Child VALUES (1, 1, 2.5);
    ... ''')
    >>> c.commit(); c.close()
    >>> tables = reflect_source(p)
    >>> [(t.name, [(col.name, col.kind) for col in t.columns]) for t in tables]
    [('Child', [('ID', 'integer'), ('ParentID', 'integer'), ('Score', 'float')]), ('Parent', [('ID', 'integer'), ('Name', 'text'), ('Born', 'text')])]
    >>> tables[0].foreign_keys
    (SourceForeignKey(columns=('ParentID',), ref_table='Parent', ref_columns=('ID',)),)
    """
    engine = sa.create_engine("sqlite://", creator=lambda: open_sqlite_readonly(path))
    raw = open_sqlite_readonly(path)
    try:
        inspector = sa.inspect(engine)
        tables: list[SourceTable] = []
        for name in sorted(n for n in inspector.get_table_names() if not n.startswith("sqlite_")):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", sa.exc.SAWarning)  # types SQLAlchemy does not know
                reflected = inspector.get_columns(name)
            pk = inspector.get_pk_constraint(name).get("constrained_columns") or []
            observations = _observe(raw, name, [c["name"] for c in reflected])
            columns = tuple(
                SourceColumn(
                    name=c["name"],
                    declared=str(c["type"]),
                    nullable=bool(c.get("nullable", True)) or obs.nulls > 0,
                    kind=column_kind(_affinity(c["type"]), obs),
                    observed=obs,
                    pk_position=pk.index(c["name"]) + 1 if c["name"] in pk else 0,
                )
                for c, obs in zip(reflected, observations)
            )
            foreign_keys = tuple(
                SourceForeignKey(
                    tuple(fk["constrained_columns"]), fk["referred_table"],
                    tuple(fk["referred_columns"] or ()),
                )
                for fk in inspector.get_foreign_keys(name)
                if fk["referred_table"] and fk["referred_columns"]
            )
            tables.append(
                SourceTable(
                    name=name, columns=columns, foreign_keys=foreign_keys,
                    rows=int(raw.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0]),
                )
            )
        return tables
    finally:
        raw.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------


def create_target_engine(url: str, *, fast_executemany: bool = True) -> Engine:
    """The engine a load writes through.

    ``fast_executemany`` is passed only for ``mssql+pyodbc``, where it makes
    pyodbc send each chunk as one batch.

    Examples
    --------
    >>> create_target_engine("sqlite://").dialect.name
    'sqlite'
    """
    kwargs: dict[str, Any] = {}
    if fast_executemany and url.startswith("mssql+pyodbc"):
        kwargs["fast_executemany"] = True
    return sa.create_engine(url, **kwargs)


def _sa_type(column: SourceColumn) -> sa_types.TypeEngine[Any]:
    if column.kind == "integer":
        return sa_types.BigInteger()
    if column.kind == "float":
        return sa_types.Float(53)
    if column.kind == "blob":
        return sa_types.LargeBinary()
    length = column.length
    return sa_types.Unicode(length) if length is not None else sa_types.UnicodeText()


def target_table(table: SourceTable, schema: str, metadata: sa.MetaData | None = None) -> sa.Table:
    """The SQLAlchemy ``Table`` a source table is created as on the target.

    Names are kept as they are and always quoted. Columns are keyed ``c0``,
    ``c1``, ... so an insert never depends on a name's spelling. No key is
    declared here; :func:`post_load_statements` adds them after the data.

    Examples
    --------
    >>> from sqlalchemy.dialects import mssql
    >>> from sqlalchemy.schema import CreateTable
    >>> obs = ColumnObservation(rows=2, has_int=True)
    >>> t = SourceTable("Order Item", (
    ...     SourceColumn("ID", "INTEGER", False, "integer", obs, 1),
    ...     SourceColumn("Free (K-12)", "REAL", True, "float", ColumnObservation(rows=2, has_real=True)),
    ...     SourceColumn("Note", "TEXT", True, "text", ColumnObservation(rows=2, has_text=True, max_text_len=40)),
    ... ), rows=2)
    >>> ddl = str(CreateTable(target_table(t, "shop")).compile(dialect=mssql.dialect()))
    >>> ddl.strip().startswith("CREATE TABLE [shop].[Order Item] (")
    True
    >>> for column in ("[ID] BIGINT NOT NULL", "[Free (K-12)] FLOAT(53) NULL", "[Note] NVARCHAR(64) NULL"):
    ...     print(column in ddl)
    True
    True
    True
    """
    md = metadata if metadata is not None else sa.MetaData()
    columns = [
        sa.Column(
            c.name, _sa_type(c), nullable=c.nullable, key=f"c{i}", quote=True,
        )
        for i, c in enumerate(table.columns)
    ]
    return sa.Table(table.name, md, *columns, schema=schema, quote=True, quote_schema=True)


def _primary_key_columns(table: SourceTable) -> tuple[SourceColumn, ...]:
    """The primary key as it can be created on SQL Server, or ``()``.

    A key is dropped when a column is not an integer or a short text, has NULLs,
    or the text columns together exceed the 900-byte limit.
    """
    key = table.primary_key
    if not key:
        return ()
    chars = 0
    for column in key:
        if column.observed.nulls > 0 or column.kind in ("float", "blob"):
            return ()
        if column.kind == "text":
            if column.length is None:
                return ()
            chars += column.length
    return key if chars <= _PK_MAX_CHARS else ()


def post_load_statements(
    tables: Sequence[SourceTable], schema: str, dialect: sa.engine.Dialect
) -> list[tuple[str, str]]:
    """``(description, DDL)`` for the primary and foreign keys, to run after the data.

    Primary keys come first (a foreign key needs its target to be one), then
    foreign keys ``WITH NOCHECK`` (the existing rows are not re-validated). Only
    for SQL Server: ``ALTER TABLE ... ADD CONSTRAINT`` does not exist in SQLite.
    A foreign key whose target table or columns are missing is not produced.

    Examples
    --------
    >>> from sqlalchemy.dialects import mssql
    >>> obs = ColumnObservation(rows=1, has_int=True)
    >>> parent = SourceTable("Parent", (SourceColumn("ID", "INTEGER", False, "integer", obs, 1),), rows=1)
    >>> child = SourceTable("Child", (
    ...     SourceColumn("ID", "INTEGER", False, "integer", obs, 1),
    ...     SourceColumn("ParentID", "INTEGER", True, "integer", obs),
    ... ), (SourceForeignKey(("ParentID",), "Parent", ("ID",)),), rows=1)
    >>> for what, ddl in post_load_statements([parent, child], "shop", mssql.dialect()):
    ...     print(ddl)
    ALTER TABLE [shop].[Parent] ADD CONSTRAINT [PK_Parent] PRIMARY KEY ([ID])
    ALTER TABLE [shop].[Child] ADD CONSTRAINT [PK_Child] PRIMARY KEY ([ID])
    ALTER TABLE [shop].[Child] WITH NOCHECK ADD CONSTRAINT [FK_Child_1] FOREIGN KEY ([ParentID]) REFERENCES [shop].[Parent] ([ID])
    >>> post_load_statements([parent], "shop", sa.create_engine("sqlite://").dialect)
    []
    """
    if dialect.name != "mssql":
        return []
    quote = dialect.identifier_preparer.quote_identifier

    def qualified(name: str) -> str:
        return f"{quote(schema)}.{quote(name)}"

    by_name = {t.name: t for t in tables}
    statements: list[tuple[str, str]] = []
    for table in tables:
        key = _primary_key_columns(table)
        if key:
            columns = ", ".join(quote(c.name) for c in key)
            statements.append((
                f"primary key of {table.name}",
                f"ALTER TABLE {qualified(table.name)} ADD CONSTRAINT {quote('PK_' + table.name)} "
                f"PRIMARY KEY ({columns})",
            ))
    for table in tables:
        for number, fk in enumerate(table.foreign_keys, start=1):
            target = by_name.get(fk.ref_table)
            known = {c.name for c in table.columns}
            if (
                target is None
                or not set(fk.columns) <= known
                or not set(fk.ref_columns) <= {c.name for c in target.columns}
            ):
                continue
            local = ", ".join(quote(c) for c in fk.columns)
            remote = ", ".join(quote(c) for c in fk.ref_columns)
            statements.append((
                f"foreign key {number} of {table.name}",
                f"ALTER TABLE {qualified(table.name)} WITH NOCHECK ADD CONSTRAINT "
                f"{quote(f'FK_{table.name}_{number}')} FOREIGN KEY ({local}) "
                f"REFERENCES {qualified(fk.ref_table)} ({remote})",
            ))
    return statements


def _existing_tables(engine: Engine, schema: str) -> list[str]:
    inspector = sa.inspect(engine)
    return sorted(inspector.get_table_names(schema=schema) + inspector.get_view_names(schema=schema))


def schema_is_empty(engine: Engine, schema: str) -> bool:
    """Whether *schema* of the target holds no table or view (a missing schema is empty)."""
    inspector = sa.inspect(engine)
    names = {s.lower() for s in inspector.get_schema_names()}
    if schema.lower() not in names:
        return True
    return not _existing_tables(engine, schema)


def _prepare_schema(engine: Engine, schema: str, replace: bool) -> None:
    """Make *schema* ready to receive tables: create it, or empty it when *replace*.

    Raises
    ------
    SchemaNotEmpty
        If the schema holds tables and *replace* is false.
    """
    if schema_is_empty(engine, schema):
        if engine.dialect.name == "mssql":
            names = {s.lower() for s in sa.inspect(engine).get_schema_names()}
            if schema.lower() not in names:
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        f"CREATE SCHEMA {engine.dialect.identifier_preparer.quote_identifier(schema)}"
                    )
        return
    if not replace:
        raise SchemaNotEmpty(
            f"schema {schema!r} of the target already holds tables ({', '.join(_existing_tables(engine, schema)[:5])}"
            f"{', ...' if len(_existing_tables(engine, schema)) > 5 else ''}); pass --replace to drop "
            "them and load again"
        )
    with engine.begin() as conn:
        metadata = sa.MetaData()
        metadata.reflect(bind=conn, schema=schema)
        metadata.drop_all(conn)


def load_sqlite_schema(
    source: str | Path,
    engine: Engine,
    schema: str,
    *,
    replace: bool = False,
    chunk_size: int = DEFAULT_CHUNK,
    progress: Callable[[str], None] | None = None,
) -> LoadReport:
    """Copy every table of the SQLite file *source* into *schema* of *engine*'s database.

    Parameters
    ----------
    source:
        The SQLite file (opened read-only).
    engine:
        The target, from :func:`create_target_engine`; its login needs DDL rights.
    schema:
        The target schema, created if absent (SQL Server). On SQLite, which
        cannot create a schema, it must be an attached database.
    replace:
        Drop the tables of a non-empty *schema* first. Without it a non-empty
        schema is refused (:class:`SchemaNotEmpty`) before anything is written.
    chunk_size:
        Rows per insert batch.
    progress:
        Called with a short message per table.

    Returns
    -------
    LoadReport
        Row counts per table (source and target) and the key warnings.

    Raises
    ------
    SchemaNotEmpty
        If the schema holds tables and *replace* is false.

    Examples
    --------
    >>> import tempfile, os
    >>> d = tempfile.mkdtemp()
    >>> src = os.path.join(d, "src.sqlite")
    >>> c = sqlite3.connect(src)
    >>> _ = c.executescript("CREATE TABLE t (ID INTEGER PRIMARY KEY, Name TEXT); INSERT INTO t VALUES (1, 'a'), (2, 'b');")
    >>> c.commit(); c.close()
    >>> main = os.path.join(d, "main.db")
    >>> sqlite3.connect(main).close()
    >>> tgt = sqlite3.connect(os.path.join(d, "shop.db")); tgt.close()
    >>> engine = sa.create_engine(f"sqlite:///{main}")
    >>> @sa.event.listens_for(engine, "connect")
    ... def _attach(dbapi_connection, _record):
    ...     dbapi_connection.execute("ATTACH DATABASE ? AS shop", (os.path.join(d, "shop.db"),))
    >>> report = load_sqlite_schema(src, engine, "shop")
    >>> [(t.name, t.source_rows, t.target_rows) for t in report.tables], report.ok
    ([('t', 2, 2)], True)
    >>> load_sqlite_schema(src, engine, "shop")
    Traceback (most recent call last):
        ...
    eval.benchmarks.sqlite_load.SchemaNotEmpty: schema 'shop' of the target already holds tables (t); pass --replace to drop them and load again
    >>> load_sqlite_schema(src, engine, "shop", replace=True).ok
    True
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    tables = reflect_source(source)
    _prepare_schema(engine, schema, replace)

    metadata = sa.MetaData()
    built = {t.name: target_table(t, schema, metadata) for t in tables}
    report = LoadReport(schema=schema)
    metadata.create_all(engine)

    raw = open_sqlite_readonly(source)
    try:
        for table in tables:
            sa_table = built[table.name]
            names = ", ".join(_q(c.name) for c in table.columns)
            cursor = raw.execute(f"SELECT {names} FROM {_q(table.name)}")
            converters = [_converter(c) for c in table.columns]
            keys = [f"c{i}" for i in range(len(table.columns))]
            with engine.begin() as conn:
                for chunk in _chunks(cursor, chunk_size):
                    conn.execute(
                        sa_table.insert(),
                        [dict(zip(keys, (f(v) for f, v in zip(converters, row)))) for row in chunk],
                    )
            with engine.connect() as conn:
                target_rows = int(conn.execute(sa.select(sa.func.count()).select_from(sa_table)).scalar_one())
            widened = tuple(
                c.name for c in table.columns
                if c.kind == "text" and (c.observed.has_int or c.observed.has_real)
            )
            report.tables.append(
                TableLoad(table.name, table.rows, target_rows, len(table.columns), widened)
            )
            if progress is not None:
                progress(f"{schema}.{table.name}: {table.rows} rows -> {target_rows}")
    finally:
        raw.close()

    for what, ddl in post_load_statements(tables, schema, engine.dialect):
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(ddl)
        except sa.exc.DBAPIError as exc:
            first = str(exc.orig).splitlines()[0] if exc.orig is not None else type(exc).__name__
            report.warnings.append(f"{schema}: {what} not created ({first})")
    for table in tables:
        if table.primary_key and not _primary_key_columns(table) and engine.dialect.name == "mssql":
            report.warnings.append(f"{schema}: primary key of {table.name} skipped (type, NULLs or length)")
    return report


# ---------------------------------------------------------------------------
# Content fidelity
# ---------------------------------------------------------------------------

#: Floats are compared after being written with this many significant digits
#: (a relative tolerance of about 5e-13): far below any difference a query could
#: see, and enough to absorb a driver that does not return the exact double.
FLOAT_SIGNIFICANT_DIGITS = 12

_MODULUS = 1 << 128


def _cell_bytes(kind: ColumnKind, value: Any) -> bytes:
    """One cell in the canonical form that is hashed.

    Examples
    --------
    >>> _cell_bytes("integer", 7), _cell_bytes("integer", None)
    (b'i7', b'N')
    >>> _cell_bytes("float", 0.1 + 0.2) == _cell_bytes("float", 0.3)
    True
    >>> _cell_bytes("float", -0.0)
    b'f0'
    >>> _cell_bytes("text", "Ab ") == _cell_bytes("text", "Ab")
    False
    >>> _cell_bytes("text", "Ab") == _cell_bytes("text", "AB")
    False
    >>> _cell_bytes("blob", b"\\x00\\x01")[:1]
    b'b'
    """
    if value is None:
        return b"N"
    if kind == "integer":
        return b"i" + str(int(value)).encode("ascii")
    if kind == "float":
        number = float(value)
        return b"f" + (b"0" if number == 0.0 else format(number, f".{FLOAT_SIGNIFICANT_DIGITS}g").encode("ascii"))
    if kind == "blob":
        return b"b" + hashlib.sha256(bytes(value)).digest()
    return b"s" + str(value).encode("utf-8", "surrogatepass")


def _hash_bytes(parts: Sequence[bytes]) -> int:
    digest = hashlib.blake2b(digest_size=16)
    for part in parts:
        digest.update(len(part).to_bytes(4, "big"))
        digest.update(part)
    return int.from_bytes(digest.digest(), "big")


@dataclass(frozen=True, slots=True)
class RowDigest:
    """An order-independent digest of a table: its row count and the sum of its row hashes.

    ``columns`` holds the same sum per column, when it was asked for, to say
    *which* column differs.
    """

    rows: int
    total: int
    columns: tuple[int, ...] | None = None


def digest_rows(
    rows: Iterable[Sequence[Any]], columns: Sequence[SourceColumn], *, per_column: bool = False
) -> RowDigest:
    """The :class:`RowDigest` of *rows* (tuples in the order of *columns*).

    Each value is first converted exactly as the loader converts it, then
    canonicalised (:func:`_cell_bytes`); a row hashes to 128 bits and the hashes
    are added modulo 2**128, so the order of the rows does not matter and a
    repeated row counts twice.

    Examples
    --------
    >>> cols = (SourceColumn("a", "INTEGER", True, "integer", ColumnObservation()),
    ...         SourceColumn("b", "TEXT", True, "text", ColumnObservation()))
    >>> one = digest_rows([(1, "x"), (2, "y")], cols)
    >>> one == digest_rows([(2, "y"), (1, "x")], cols)
    True
    >>> one == digest_rows([(1, "x"), (2, "y "), ], cols), one == digest_rows([(1, "x"), (2, "Y")], cols)
    (False, False)
    >>> digest_rows([(1, "x"), (1, "x")], cols) == digest_rows([(1, "x")], cols)
    False
    >>> digest_rows([(1, "x"), (2, "z")], cols, per_column=True).columns == \\
    ...     digest_rows([(1, "x"), (2, "y")], cols, per_column=True).columns
    False
    """
    converters = [_converter(c) for c in columns]
    kinds = [c.kind for c in columns]
    count = 0
    total = 0
    per = [0] * len(columns) if per_column else None
    for row in rows:
        cells = [_cell_bytes(k, f(v)) for k, f, v in zip(kinds, converters, row)]
        total = (total + _hash_bytes(cells)) % _MODULUS
        count += 1
        if per is not None:
            for i, cell in enumerate(cells):
                per[i] = (per[i] + _hash_bytes([cell])) % _MODULUS
    return RowDigest(count, total, tuple(per) if per is not None else None)


@dataclass(frozen=True, slots=True)
class TableContent:
    """Whether one table holds the same data on the target as in the source."""

    name: str
    source_rows: int
    target_rows: int
    same: bool
    differing_columns: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        """A short, value-free description of the difference (empty when the table is the same)."""
        if self.same:
            return ""
        if self.source_rows != self.target_rows:
            return f"{self.source_rows} rows in the source, {self.target_rows} on the target"
        if self.differing_columns:
            return "column(s) differ: " + ", ".join(self.differing_columns)
        return "the rows differ"


@dataclass(slots=True)
class ContentReport:
    """The content comparison of one schema."""

    schema: str
    tables: list[TableContent] = field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    @property
    def unfaithful(self) -> dict[str, str]:
        """``{table: what differs}`` for every table that is not the same."""
        return {t.name: t.detail for t in self.tables if not t.same}

    @property
    def faithful(self) -> bool:
        return not self.unfaithful

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "faithful": self.faithful,
            "tables_compared": len(self.tables),
            "unfaithful": self.unfaithful,
        }


def _digest_source(conn: sqlite3.Connection, table: SourceTable, *, per_column: bool, chunk: int) -> RowDigest:
    names = ", ".join(_q(c.name) for c in table.columns)
    cursor = conn.execute(f"SELECT {names} FROM {_q(table.name)}")
    return digest_rows(
        (row for rows in _chunks(cursor, chunk) for row in rows), table.columns, per_column=per_column
    )


def _digest_target(
    engine: Engine, schema: str, table: SourceTable, *, per_column: bool, chunk: int
) -> RowDigest:
    quote = engine.dialect.identifier_preparer.quote_identifier
    names = ", ".join(quote(c.name) for c in table.columns)
    sql = f"SELECT {names} FROM {quote(schema)}.{quote(table.name)}"
    with engine.connect() as conn:
        result = conn.exec_driver_sql(sql)

        def rows() -> Iterator[Sequence[Any]]:
            while True:
                batch = result.fetchmany(chunk)
                if not batch:
                    return
                yield from batch

        return digest_rows(rows(), table.columns, per_column=per_column)


def compare_contents(
    source: str | Path, engine: Engine, schema: str, *, chunk_size: int = DEFAULT_CHUNK
) -> ContentReport:
    """Compare every table of *schema* on the target with the SQLite file, by content.

    A count is not enough: a value changed on the way in, a text that lost its
    trailing spaces or its case, a number that lost precision all keep the count.
    Both sides are read in chunks and reduced to a :class:`RowDigest`; a table
    whose digests differ is read once more with a digest per column, to name the
    columns. A table missing on the target is reported as different.

    Parameters
    ----------
    source:
        The SQLite file.
    engine, schema:
        The target and its schema.
    chunk_size:
        Rows per fetch, on both sides.

    Examples
    --------
    >>> import tempfile, os
    >>> d = tempfile.mkdtemp()
    >>> src = os.path.join(d, "src.sqlite")
    >>> c = sqlite3.connect(src)
    >>> _ = c.executescript("CREATE TABLE t (ID INTEGER PRIMARY KEY, Name TEXT); INSERT INTO t VALUES (1, 'Ada '), (2, 'Bo');")
    >>> c.commit(); c.close()
    >>> main = os.path.join(d, "main.db")
    >>> sqlite3.connect(main).close()
    >>> sqlite3.connect(os.path.join(d, "shop.db")).close()
    >>> engine = sa.create_engine(f"sqlite:///{main}")
    >>> @sa.event.listens_for(engine, "connect")
    ... def _attach(dbapi_connection, _record):
    ...     dbapi_connection.execute("ATTACH DATABASE ? AS shop", (os.path.join(d, "shop.db"),))
    >>> _ = load_sqlite_schema(src, engine, "shop")
    >>> compare_contents(src, engine, "shop").faithful
    True
    >>> with engine.begin() as conn:
    ...     _ = conn.exec_driver_sql("UPDATE shop.t SET Name = 'Ada' WHERE ID = 1")
    >>> compare_contents(src, engine, "shop").unfaithful
    {'t': 'column(s) differ: Name'}
    """
    tables = reflect_source(source)
    present = set(sa.inspect(engine).get_table_names(schema=schema))
    report = ContentReport(schema=schema)
    raw = open_sqlite_readonly(source)
    try:
        for table in tables:
            if table.name not in present:
                report.tables.append(TableContent(table.name, table.rows, -1, False))
                continue
            a = _digest_source(raw, table, per_column=False, chunk=chunk_size)
            b = _digest_target(engine, schema, table, per_column=False, chunk=chunk_size)
            if a == b:
                report.tables.append(TableContent(table.name, a.rows, b.rows, True))
                continue
            differing: tuple[str, ...] = ()
            if a.rows == b.rows:
                a = _digest_source(raw, table, per_column=True, chunk=chunk_size)
                b = _digest_target(engine, schema, table, per_column=True, chunk=chunk_size)
                assert a.columns is not None and b.columns is not None
                differing = tuple(
                    c.name for c, x, y in zip(table.columns, a.columns, b.columns) if x != y
                )
            report.tables.append(TableContent(table.name, a.rows, b.rows, False, differing))
    finally:
        raw.close()
    return report


# ---------------------------------------------------------------------------
# Collation
# ---------------------------------------------------------------------------


def collation_problem(name: str) -> str | None:
    """Why the collation *name* cannot reproduce SQLite's text comparison, or ``None``.

    SQLite compares text case-sensitively and accent-sensitively. A SQL Server
    database must too (``CS`` and ``AS`` in the name, or a binary ``BIN`` /
    ``BIN2`` collation), or ``WHERE name = 'abc'`` matches ``'ABC'`` and a correct
    answer can differ from the reference through no fault of the model.

    Examples
    --------
    >>> collation_problem("Latin1_General_100_CS_AS_SC_UTF8") is None
    True
    >>> collation_problem("Latin1_General_BIN2") is None
    True
    >>> collation_problem("SQL_Latin1_General_CP1_CI_AS")
    'is case-insensitive (CI)'
    >>> collation_problem("Latin1_General_CS_AI")
    'is accent-insensitive (AI)'
    >>> collation_problem("Latin1_General_100")
    'is not recognisably case- and accent-sensitive'
    """
    tokens = set(name.upper().split("_"))
    if tokens & {"BIN", "BIN2"}:
        return None
    if "CI" in tokens:
        return "is case-insensitive (CI)"
    if "AI" in tokens:
        return "is accent-insensitive (AI)"
    if {"CS", "AS"} <= tokens:
        return None
    return "is not recognisably case- and accent-sensitive"


def database_collation(engine: Engine) -> str | None:
    """The default collation of the database *engine* is connected to; ``None`` off SQL Server.

    Examples
    --------
    >>> database_collation(sa.create_engine("sqlite://")) is None
    True
    """
    if engine.dialect.name != "mssql":
        return None
    with engine.connect() as conn:
        value = conn.exec_driver_sql(
            "SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS NVARCHAR(128))"
        ).scalar()
    return str(value) if value is not None else None


def verify_loaded(source: str | Path, engine: Engine, schema: str) -> LoadReport:
    """Compare the row counts of a schema already on the target with the source file.

    For a re-run that does not load again (``--skip-load``): nothing is written.
    A table missing on the target counts as ``-1`` rows there, so it shows as a
    mismatch.

    Examples
    --------
    >>> import tempfile, os
    >>> d = tempfile.mkdtemp()
    >>> src = os.path.join(d, "src.sqlite")
    >>> c = sqlite3.connect(src)
    >>> _ = c.executescript("CREATE TABLE t (a INTEGER); INSERT INTO t VALUES (1);")
    >>> c.commit(); c.close()
    >>> main = os.path.join(d, "main.db")
    >>> sqlite3.connect(main).close()
    >>> sqlite3.connect(os.path.join(d, "shop.db")).close()
    >>> engine = sa.create_engine(f"sqlite:///{main}")
    >>> @sa.event.listens_for(engine, "connect")
    ... def _attach(dbapi_connection, _record):
    ...     dbapi_connection.execute("ATTACH DATABASE ? AS shop", (os.path.join(d, "shop.db"),))
    >>> [(t.name, t.source_rows, t.target_rows) for t in verify_loaded(src, engine, "shop").tables]
    [('t', 1, -1)]
    >>> _ = load_sqlite_schema(src, engine, "shop")
    >>> verify_loaded(src, engine, "shop").ok
    True
    """
    tables = reflect_source(source)
    present = set(sa.inspect(engine).get_table_names(schema=schema))
    report = LoadReport(schema=schema)
    for table in tables:
        count = -1
        if table.name in present:
            sa_table = target_table(table, schema)
            with engine.connect() as conn:
                count = int(conn.execute(sa.select(sa.func.count()).select_from(sa_table)).scalar_one())
        report.tables.append(TableLoad(table.name, table.rows, count, len(table.columns)))
    return report


def _chunks(cursor: sqlite3.Cursor, size: int) -> Iterator[list[tuple[Any, ...]]]:
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        yield rows


def _converter(column: SourceColumn) -> Callable[[Any], Any]:
    """Convert one source value to the Python value the target column takes."""
    if column.kind == "integer":
        return lambda v: None if v is None else int(v)
    if column.kind == "float":
        return lambda v: None if v is None else float(v)
    if column.kind == "blob":
        return lambda v: None if v is None else bytes(v)

    def text(value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", errors="replace")
        return repr(value) if isinstance(value, float) else str(value)

    return text
