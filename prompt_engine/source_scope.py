# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Narrow the knowledge in a prompt to one data source.

With several data sources configured, a question is routed to one source
first (:mod:`retrieval.source_selector`) and the prompt then describes that
source alone: its tables (:meth:`schema_data.registry.SchemaRegistry.tables_for_source`),
the relationships between them, and the few-shot examples that can run
there. This module holds the two pieces the prompt builders share:
:func:`scoped_source`, which decides whether a source narrows anything at
all, and :func:`examples_for_source`.

Business rules and metrics are not table-scoped and stay as they are.

A deployment with one data source is never narrowed: :func:`scoped_source`
returns ``None`` for it whatever it is given, so every function that takes
a ``source`` renders exactly what it did before sources existed.
"""

from __future__ import annotations

from typing import Iterable, Mapping

__all__ = ["examples_for_source", "scoped_source"]


def scoped_source(source: str | None) -> str | None:
    """*source* if it narrows the prompt, else ``None``.

    Parameters
    ----------
    source:
        A data source name, or ``None`` for no restriction.

    Returns
    -------
    str | None
        *source* when several data sources are configured; ``None`` when
        *source* is ``None`` or only one source is configured (nothing to
        narrow).

    Raises
    ------
    ValueError
        If several sources are configured and *source* is not one of them
        (a mistyped name must not silently become "every table").

    Examples
    --------
    >>> scoped_source(None) is None
    True

    With the single-source fallback, any name is ignored:

    >>> import config as cfg
    >>> with cfg.override_settings(project_config_dir="/nonexistent"):
    ...     scoped_source("default") is None
    True

    >>> from unittest.mock import patch
    >>> with patch("database.datasources.datasource_names", return_value=("sales", "inventory")):
    ...     scoped_source("inventory")
    'inventory'
    >>> with patch("database.datasources.datasource_names", return_value=("sales", "inventory")):
    ...     scoped_source("archive")
    Traceback (most recent call last):
        ...
    ValueError: unknown data source 'archive'; configured: ['inventory', 'sales']
    """
    if source is None:
        return None
    # Module attribute lookup at call time, as the registry does.
    import database.datasources as datasources

    names = datasources.datasource_names()
    if len(names) < 2:
        return None
    if source not in names:
        raise ValueError(f"unknown data source {source!r}; configured: {sorted(names)}")
    return source


def examples_for_source(
    examples: Iterable[Mapping[str, object]], source: str | None,
) -> list[dict]:
    """The few-shot *examples* whose SQL can run on data source *source*.

    An example is kept when every table its SQL reads lives in *source*
    (:func:`security.sql_guard.extract_touched_tables` finds the tables,
    :func:`database.datasources.table_datasource_sets` says where they
    live; a table in several sources counts for each). An example whose
    tables cannot be determined (SQL that does not parse, or that reads no
    table the schema lists) is kept: dropping it would take away a
    demonstration for no reason anyone could check.

    Parameters
    ----------
    examples:
        ``{"question": ..., "sql": ..., ...}`` mappings, in the order to
        keep.
    source:
        The data source, or ``None`` to keep everything.

    Returns
    -------
    list[dict]
        The kept examples, in their original order. Everything, as a new
        list, when *source* is ``None`` or only one source is configured.

    Examples
    --------
    >>> from unittest.mock import patch
    >>> exs = [
    ...     {"question": "q1", "sql": "SELECT 1"},
    ...     {"question": "q2", "sql": "SELECT * FROM sales_fact"},
    ...     {"question": "q3", "sql": "SELECT * FROM stock_dim"},
    ... ]
    >>> sets = {"sales_fact": ("sales",), "stock_dim": ("inventory",)}
    >>> def fake_touched(sql, dialect="tsql"):
    ...     return sorted(t for t in sets if t in sql)
    >>> with patch("database.datasources.datasource_names", return_value=("sales", "inventory")), \\
    ...      patch("database.datasources.table_datasource_sets", return_value=sets), \\
    ...      patch("security.sql_guard.extract_touched_tables", fake_touched):
    ...     [e["question"] for e in examples_for_source(exs, "inventory")]
    ['q1', 'q3']
    """
    kept = [dict(example) for example in examples]
    source = scoped_source(source)
    if source is None:
        return kept

    import database.datasources as datasources
    from security.sql_guard import extract_touched_tables

    table_sets = datasources.table_datasource_sets()
    default = datasources.datasource_names()[0]

    def runs_on_source(example: Mapping[str, object]) -> bool:
        tables = extract_touched_tables(str(example.get("sql", "")))
        return all(source in table_sets.get(table, (default,)) for table in tables)

    return [example for example in kept if runs_on_source(example)]
