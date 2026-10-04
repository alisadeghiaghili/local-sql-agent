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

A table may live in several sources (``datasource: [A, B]`` in
``schema.yaml``: the same table, with the same shape, in each). The
sources that can run a statement are then the **intersection** of the
source sets of every table it reads. An empty intersection is the
refusal above; otherwise the statement runs on the default source when
it is a candidate, else on the first candidate in ``datasources.yaml``
order (:func:`database.datasources.pick_datasource`). A statement that
reads only a shared table therefore runs on the default source when the
table lives there. The rule lives in :func:`choose_datasource`, which the
SQL guard calls too, so guard and executor cannot disagree.

A statement that references no configured table (``SELECT 1``, a catalogue
probe written by an operator tool) runs on the default source. Callers
that must reach a specific source regardless of the SQL text -- ``/health``
and ``scripts/verify_deployment.py`` probing every source -- pass the
source name explicitly to :mod:`database.executor` instead.
"""

from __future__ import annotations

from typing import Iterable

from database.datasources import (
    default_datasource_name,
    pick_datasource,
    table_datasource_sets,
)

__all__ = [
    "CrossDatasourceError",
    "choose_datasource",
    "group_tables_by_datasource",
    "resolve_datasource",
    "target_datasource_or_none",
]


class CrossDatasourceError(ValueError):
    """No single data source has every table a statement reads.

    Attributes
    ----------
    groups:
        ``{source: sorted table names}`` for every source involved; a
        table that lives in several sources is listed under each of them.
    """

    def __init__(self, groups: dict[str, list[str]]) -> None:
        self.groups = groups
        described = "; ".join(
            f"{source}: {', '.join(tables)}" for source, tables in sorted(groups.items())
        )
        message = (
            "query reads tables from more than one data source "
            f"({described}); every table in one query must come from the "
            "same data source"
        )
        # Say where the shared tables are, so the reader can see why the
        # combination fails (a table in both sources does not help when
        # the other tables are each in only one).
        where: dict[str, list[str]] = {}
        for source, tables in sorted(groups.items()):
            for table in tables:
                where.setdefault(table, []).append(source)
        shared = {table: sources for table, sources in where.items() if len(sources) > 1}
        if shared:
            listed = "; ".join(
                f"{table} ({', '.join(sources)})" for table, sources in sorted(shared.items())
            )
            message += f". Available in several data sources: {listed}"
        super().__init__(message)


def group_tables_by_datasource(tables: Iterable[str]) -> dict[str, list[str]]:
    """Group canonical table names by the data sources they belong to.

    Parameters
    ----------
    tables:
        Canonical table names as they appear in ``schema.yaml``. A name
        ``schema.yaml`` does not list is assigned to the default source,
        the same place a table without ``datasource:`` goes.

    Returns
    -------
    dict[str, list[str]]
        ``{source: sorted table names}``; empty for no tables. A table
        that lives in several sources appears under each of them.

    Examples
    --------
    >>> group_tables_by_datasource([])
    {}
    """
    assignments = table_datasource_sets()
    default = default_datasource_name()
    groups: dict[str, set[str]] = {}
    for table in tables:
        for source in assignments.get(table, (default,)):
            groups.setdefault(source, set()).add(table)
    return {source: sorted(names) for source, names in groups.items()}


def choose_datasource(tables: Iterable[str]) -> str | None:
    """The one data source that can run a statement reading *tables*.

    The candidates are the sources every table lives in (the intersection
    of the tables' source sets). The default source wins when it is a
    candidate, otherwise the first candidate in ``datasources.yaml``
    order. Used by :func:`resolve_datasource` and, for the cross-source
    check, by :func:`security.sql_guard.validate_sql`.

    Parameters
    ----------
    tables:
        Canonical table names as they appear in ``schema.yaml``; a name
        ``schema.yaml`` does not list counts as living in the default
        source only.

    Returns
    -------
    str | None
        The source name, or ``None`` when *tables* is empty.

    Raises
    ------
    CrossDatasourceError
        If no source has every table.

    Examples
    --------
    >>> choose_datasource([]) is None
    True
    """
    names = set(tables)
    if not names:
        return None
    groups = group_tables_by_datasource(names)
    # A source can run the statement when every table is among its tables.
    candidates = [
        source for source, members in groups.items() if len(members) == len(names)
    ]
    if not candidates:
        raise CrossDatasourceError(groups)
    return pick_datasource(candidates)


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
        The source chosen by :func:`choose_datasource` for the referenced
        tables, or the default source when *sql* references no configured
        table.

    Raises
    ------
    CrossDatasourceError
        If no single source has every referenced table.

    Examples
    --------
    >>> resolve_datasource("SELECT 1") == default_datasource_name()
    True
    """
    import config as cfg
    from security.sql_guard import extract_touched_tables

    tables = extract_touched_tables(sql, dialect=dialect or cfg.settings.sql_dialect)
    return choose_datasource(tables) or default_datasource_name()


def target_datasource_or_none(sql: str | None, dialect: str | None = None) -> str | None:
    """The data source *sql* targets, or ``None`` when there is no single one.

    For the audit trail: the source a statement routes to, whether or not
    it went on to run (a guard rejection still names the source the
    statement was aimed at, which is what an investigation needs).
    ``None`` for empty *sql* (nothing was generated) and for a statement
    whose tables have no source in common, so a record never claims a
    single source that is not true. Never raises.

    Parameters
    ----------
    sql:
        The statement, or ``None``.
    dialect:
        As for :func:`resolve_datasource`.

    Returns
    -------
    str | None
        The source name, or ``None``.

    Examples
    --------
    >>> target_datasource_or_none(None) is None
    True
    >>> target_datasource_or_none("SELECT 1") == default_datasource_name()
    True
    """
    if not sql:
        return None
    try:
        return resolve_datasource(sql, dialect)
    except Exception:  # noqa: BLE001 - audit enrichment must never raise
        return None
