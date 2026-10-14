# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Small SQLite databases whose schemas are ``ATTACH``ed files, for the inspector's tests.

SQLAlchemy reflects an attached SQLite database as a schema of its own name,
which is how a test gives one file the several schemas a warehouse has (the same
trick as ``tests/_warehouse_shape.py``, in miniature: a handful of tables, named
by the test, instead of a 300-table warehouse).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

#: Columns of a table the layout gives no definition for.
DEFAULT_COLUMNS = "ID INTEGER PRIMARY KEY, Name VARCHAR(40)"

#: ``{schema: {table: column definitions or None for the default}}``.
Layout = Mapping[str, Mapping[str, str | None]]


@dataclass(frozen=True)
class Database:
    """One SQLite file (``main``) and the files attached to it as schemas."""

    name: str
    main: Path
    schemas: Mapping[str, Path]

    @property
    def url(self) -> str:
        return f"sqlite:///{self.main}"


def build_database(root: Path, name: str, layout: Layout, rows: Mapping[str, Sequence[tuple]] | None = None) -> Database:
    """Create the files of *layout* under ``root/name`` and return the database.

    Parameters
    ----------
    root:
        A scratch directory.
    name:
        The database's name (a directory under *root*).
    layout:
        ``{schema: {table: columns}}``; a schema is one attached file.
    rows:
        ``{"schema.table": [row, ...]}`` inserted into the table's columns in
        order (a table with the default columns takes ``(id, name)``).
    """
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    main = folder / "main.db"
    sqlite3.connect(main).close()
    schemas: dict[str, Path] = {}
    for schema, tables in layout.items():
        path = folder / f"{schema}.db"
        connection = sqlite3.connect(path)
        try:
            for table, columns in tables.items():
                connection.execute(f'CREATE TABLE "{table}" ({columns or DEFAULT_COLUMNS})')
                for row in (rows or {}).get(f"{schema}.{table}", ()):
                    marks = ", ".join("?" for _ in row)
                    connection.execute(f'INSERT INTO "{table}" VALUES ({marks})', row)
            connection.commit()
        finally:
            connection.close()
        schemas[schema] = path
    return Database(name, main, schemas)


@contextmanager
def attached(*databases: Database) -> Iterator[None]:
    """Make every new SQLite connection to a database's main file see its schemas."""
    wanted = {os.path.normcase(os.path.realpath(db.main)): db for db in databases}

    def attach(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        for _, name, file in dbapi_connection.execute("PRAGMA database_list").fetchall():
            if name != "main":
                continue
            database = wanted.get(os.path.normcase(os.path.realpath(file))) if file else None
            if database is None:
                return
            for schema, path in database.schemas.items():
                quoted = '"' + schema.replace('"', '""') + '"'
                dbapi_connection.execute(f"ATTACH DATABASE ? AS {quoted}", (str(path),))
            return

    event.listen(Engine, "connect", attach)
    try:
        yield
    finally:
        event.remove(Engine, "connect", attach)


def engines_for(databases: Mapping[str, Database]) -> dict[str, Engine]:
    """``{source name: engine}`` for ``database.connection.get_engine`` to hand out (use inside :func:`attached`)."""
    return {name: create_engine(db.url) for name, db in databases.items()}
