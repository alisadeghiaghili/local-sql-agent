# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Decide which data source a SQL statement runs on.

The source is a property of the tables a statement reads, looked up in
``schema.yaml`` (each table's ``datasource:``, default source when
absent). It is never taken from the model's output or from the question,
so a prompt cannot steer a query onto a different server.

One statement runs on one source. SQL Server cannot join tables on two
different servers inside one query unless a DBA has set up a linked
server -- and a linked server is, from this application's side, one
source whose tables happen to be addressed with four-part names. So a
statement whose tables span two sources is refused here, and earlier by
:func:`security.sql_guard.validate_sql` with a message the model and the
analyst can act on (reason ``cross_datasource``).

A statement that references no configured table (``SELECT 1``, a catalogue
probe written by an operator tool) runs on the default source. Callers
that must reach a specific source regardless of the SQL text -- ``/health``
and ``scripts/verify_deployment.py`` probing every source -- pass the
source name explicitly to :mod:`database.executor` instead.
"""

from __future__ import annotations

from typing import Iterable

from database.datasources import default_datasource_name, table_datasources

__all__ = [
    "CrossDatasourceError",
    "group_tables_by_datasource",
    "resolve_datasource",
]


class CrossDatasourceError(ValueError):
    """A statement reads tables that live in more than one data source.

    Attributes
    ----------
    groups:
        ``{source: sorted table names}`` for every source involved.
    """

    def __init__(self, groups: dict[str, list[str]]) -> None:
        self.groups = groups
        described = "; ".join(
            f"{source}: {', '.join(tables)}" for source, tables in sorted(groups.items())
        )
        super().__init__(
            "query reads tables from more than one data source "
            f"({described}); every table in one query must come from the "
            "same data source"
        )


def group_tables_by_datasource(tables: Iterable[str]) -> dict[str, list[str]]:
    """Group canonical table names by the data source they belong to.

    Parameters
    ----------
    tables:
        Canonical table names as they appear in ``schema.yaml``. A name
        ``schema.yaml`` does not list is assigned to the default source,
        the same place a table without ``datasource:`` goes.

    Returns
    -------
    dict[str, list[str]]
        ``{source: sorted table names}``; empty for no tables.

    Examples
    --------
    >>> group_tables_by_datasource([])
    {}
    """
    assignments = table_datasources()
    default = default_datasource_name()
    groups: dict[str, set[str]] = {}
    for table in tables:
        groups.setdefault(assignments.get(table, default), set()).add(table)
    return {source: sorted(names) for source, names in groups.items()}


def resolve_datasource(sql: str, dialect: str | None = None) -> str:
    """Return the data source *sql* must run on.

    Parameters
    ----------
    sql:
        The statement, in *dialect*.
    dialect:
        sqlglot dialect key; :attr:`config.Settings.sql_dialect` when
        ``None``.

    Returns
    -------
    str
        The one source every referenced table belongs to, or the default
        source when *sql* references no configured table.

    Raises
    ------
    CrossDatasourceError
        If the referenced tables belong to more than one source.

    Examples
    --------
    >>> resolve_datasource("SELECT 1") == default_datasource_name()
    True
    """
    import config as cfg
    from security.sql_guard import extract_touched_tables

    tables = extract_touched_tables(sql, dialect=dialect or cfg.settings.sql_dialect)
    groups = group_tables_by_datasource(tables)
    if len(groups) > 1:
        raise CrossDatasourceError(groups)
    if groups:
        return next(iter(groups))
    return default_datasource_name()
