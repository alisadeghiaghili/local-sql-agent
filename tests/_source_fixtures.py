# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Shared setup for the tests of per-question data-source routing.

The suite runs against one schema (``PROJECT_CONFIG_DIR``'s ``schema.yaml``)
and one ``datasources.yaml`` (none: a single source). To exercise several
sources without writing configuration files, :func:`configured_sources`
patches the four accessors of :mod:`database.datasources` that the routing
code reads -- the same ones ``tests/test_schema_registry.py`` patches -- so
a deployment with the sources ``sales``, ``inventory`` and ``archive``
exists for the length of the ``with`` block. :data:`TABLE_SETS` spreads the
tables of whichever schema is loaded over those sources, one table shared
by two of them, so no test needs to know the real tables' names.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Mapping, Sequence
from unittest.mock import patch

from prompt_engine.static_prefix import (
    _reset_logged_prompt_paths_for_testing,
    build_static_prefix,
)
from schema_data.registry import SchemaRegistry

SOURCES = ("sales", "inventory", "archive")


def spread_tables(tables: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Assign *tables* to :data:`SOURCES`: round-robin, the first also shared.

    The first table lives in ``sales`` and ``inventory`` (the replicated
    dimension of a real deployment); every other table lives in one source,
    cycling ``sales``, ``inventory``, ``archive``.

    Examples
    --------
    >>> spread_tables(["a", "b", "c", "d"])
    {'a': ('sales', 'inventory'), 'b': ('inventory',), 'c': ('archive',), 'd': ('sales',)}
    """
    sets: dict[str, tuple[str, ...]] = {}
    for index, table in enumerate(tables):
        if index == 0:
            sets[table] = ("sales", "inventory")
        else:
            sets[table] = (SOURCES[index % len(SOURCES)],)
    return sets


@contextmanager
def configured_sources(
    table_sets: Mapping[str, Sequence[str]] | None = None,
    *,
    names: Sequence[str] = SOURCES,
    descriptions: Mapping[str, str] | None = None,
    keywords: Mapping[str, Sequence[str]] | None = None,
) -> Iterator[dict[str, tuple[str, ...]]]:
    """Make the deployment look as if it had several data sources.

    Parameters
    ----------
    table_sets:
        ``{table: (source, ...)}``; defaults to :func:`spread_tables` over
        the loaded schema's tables.
    names:
        The configured sources, default first.
    descriptions, keywords:
        What ``datasources.yaml`` would say; both default to nothing.

    Yields
    ------
    dict
        The ``{table: (source, ...)}`` mapping in effect.
    """
    if table_sets is None:
        from schema_data.registry import get_table_columns

        table_sets = spread_tables(list(get_table_columns()))
    sets = {table: tuple(sources) for table, sources in table_sets.items()}
    texts = {name: "" for name in names} | dict(descriptions or {})
    phrases = {name: () for name in names} | {k: tuple(v) for k, v in (keywords or {}).items()}

    build_static_prefix.cache_clear()
    _reset_logged_prompt_paths_for_testing()
    with patch.multiple(
        "database.datasources",
        datasource_names=lambda *a, **k: tuple(names),
        table_datasource_sets=lambda: sets,
        datasource_descriptions=lambda *a, **k: texts,
        datasource_keywords=lambda *a, **k: phrases,
    ):
        try:
            yield sets
        finally:
            build_static_prefix.cache_clear()
            _reset_logged_prompt_paths_for_testing()


def tables_of(source: str, sets: Mapping[str, Sequence[str]]) -> list[str]:
    """The tables of *source* under *sets*, in schema order."""
    return [t for t in SchemaRegistry.tables_for_source(None) if source in sets.get(t, ())]
