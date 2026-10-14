# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Resolve the table names the retrieval configuration writes to ``schema.yaml`` keys.

``entities.yaml``, ``retrieval_hints.yaml`` (fact tables, fact patterns,
``always_include``) name tables, and everything downstream compares those
names with the keys of ``schema.yaml``. The two files are written by different
hands and at different times: a warehouse that repeats table names across
schemas has keys such as ``sales.Customer`` while the entity file, written
before the schemas were qualified (``docs/design/TABLE-NAMES.md``), still says
``Customer``. A bare name that is not a key names no table: the aliased table
never reached the prompt (the schema block skips an unknown name), could not be
scored, and made every fact-table test (``name in FACT_TABLES``) false.

:func:`resolve_table_ref` maps a configured reference to the keys it can mean,
the way the SQL guard maps a reference in a query: an exact key (brackets and
case ignored) is itself; otherwise the keys with that bare name, narrowed by
the qualifier the reference writes. A reference that matches several keys is
narrowed, in this order, to

1. the keys in the data sources the question's own keywords point at
   (``datasources.yaml`` ``keywords:``), when there are any;
2. the keys in the schemas the configuration's unambiguous references live in
   (a file whose other tables are all ``sales.*`` means ``sales.Customer``
   when it says ``Customer``), when there are any;

and left as all of them otherwise. A deployment whose configuration already
writes the full key sees no change at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

__all__ = ["configured_fact_tables", "entity_for_table", "resolve_table_ref"]


@dataclass(frozen=True)
class _KeyIndex:
    """The ``schema.yaml`` keys, indexed for reference resolution."""

    keys: tuple[str, ...]
    exact: Mapping[str, str]
    by_bare: Mapping[str, tuple[str, ...]]
    qualifiers: Mapping[str, tuple[str, ...]]
    home: dict = field(default_factory=dict, compare=False)


_INDEX: dict[int, _KeyIndex] = {}


def _fold(text: str) -> str:
    return text.strip().lower()


def _parts(key: str) -> tuple[str, ...]:
    from schema_data.registry import split_table_key

    try:
        return tuple(_fold(p) for p in split_table_key(key).parts)
    except ValueError:
        return (_fold(key),)


def _key_index() -> _KeyIndex:
    """Index of the loaded ``schema.yaml`` keys (rebuilt when the schema object changes)."""
    from schema_data.registry import get_table_descriptions

    descriptions = get_table_descriptions()
    cached = _INDEX.get(id(descriptions))
    if cached is not None and cached.keys == tuple(descriptions):
        return cached
    exact: dict[str, str] = {}
    by_bare: dict[str, list[str]] = {}
    qualifiers: dict[str, tuple[str, ...]] = {}
    for key in descriptions:
        parts = _parts(key)
        exact.setdefault(".".join(parts), key)
        by_bare.setdefault(parts[-1], []).append(key)
        qualifiers[key] = parts[:-1]
    index = _KeyIndex(
        keys=tuple(descriptions),
        exact=exact,
        by_bare={b: tuple(k) for b, k in by_bare.items()},
        qualifiers=qualifiers,
    )
    _INDEX.clear()
    _INDEX[id(descriptions)] = index
    return index


def _candidates(ref: str, index: _KeyIndex) -> tuple[str, ...]:
    """Every key *ref* can mean, before any narrowing."""
    if ref in index.qualifiers:
        return (ref,)
    parts = _parts(ref)
    whole = index.exact.get(".".join(parts))
    if whole is not None:
        return (whole,)
    found = index.by_bare.get(parts[-1], ())
    wanted = parts[:-1]
    if wanted:
        # a qualified reference names its schema: ``Dim.Customer`` is the key
        # whose qualifier ends with ``Dim`` (``Warehouse.Dim.Customer``)
        found = tuple(k for k in found if index.qualifiers[k][-len(wanted):] == wanted)
    return found


def _sources_of(keys: Iterable[str]) -> dict[str, tuple[str, ...]]:
    from database.datasources import table_datasource_sets

    sets = table_datasource_sets()
    return {k: tuple(sets.get(k, ())) for k in keys}


def _home_qualifiers(index: _KeyIndex) -> frozenset[tuple[str, ...]]:
    """The schemas the configuration's own unambiguous references live in.

    Every table name in ``entities.yaml`` and ``retrieval_hints.yaml`` that
    matches exactly one key contributes that key's qualifier. A bare name that
    matches several keys is read in one of these schemas: the file was written
    for them.
    """
    from knowledge.entities import ENTITIES
    from knowledge.retrieval_hints import ALWAYS_INCLUDE, FACT_PATTERNS, FACT_TABLES

    marker = (
        tuple(ENTITIES), tuple(sorted(FACT_TABLES)), tuple(FACT_PATTERNS), tuple(ALWAYS_INCLUDE),
    )
    cached = index.home.get(marker)
    if cached is not None:
        return cached
    home: set[tuple[str, ...]] = set()
    for ref in (*ENTITIES, *FACT_TABLES, *FACT_PATTERNS, *ALWAYS_INCLUDE):
        found = _candidates(ref, index)
        if len(found) == 1:
            home.add(index.qualifiers[found[0]])
    index.home.clear()
    index.home[marker] = frozenset(home)
    return index.home[marker]


def _narrow(candidates: tuple[str, ...], text: str | None, index: _KeyIndex) -> tuple[str, ...]:
    """Reduce an ambiguous reference (see the module docstring)."""
    if len(candidates) < 2:
        return candidates
    from database.datasources import datasource_keywords, datasource_names

    if text and len(datasource_names()) > 1:
        from retrieval.source_selector import keyword_hits

        hits = keyword_hits(text, datasource_keywords())
        best = max(hits.values(), default=0)
        if best > 0:
            wanted = {s for s, n in hits.items() if n == best}
            owners = _sources_of(candidates)
            inside = tuple(k for k in candidates if wanted & set(owners[k]))
            if inside:
                return inside
    home = _home_qualifiers(index)
    inside = tuple(k for k in candidates if index.qualifiers[k] in home)
    return inside or candidates


def resolve_table_ref(ref: str, text: str | None = None) -> tuple[str, ...]:
    """The ``schema.yaml`` keys a configured table reference names.

    Parameters
    ----------
    ref:
        A table as the retrieval configuration writes it: a key, a bare name
        (``Customer``) or a partly qualified one (``Dim.Customer``). Case and
        brackets are ignored.
    text:
        The question, when the reference may be narrowed by the data source it
        points at; ``None`` skips that step.

    Returns
    -------
    tuple[str, ...]
        Keys in ``schema.yaml`` order; empty when *ref* names no table of the
        loaded schema.

    Examples
    --------
    >>> resolve_table_ref("no such table anywhere")
    ()
    """
    index = _key_index()
    return _narrow(_candidates(ref, index), text, index)


def entity_for_table(table: str) -> dict | None:
    """The ``ENTITIES`` record (``aliases``, ``table``, ``label``) that names *table*, if any.

    Looks *table* up the way the entity file may have written it: as the key
    itself, or as a bare name that resolves to it.
    """
    from knowledge.entities import ENTITIES

    if table in ENTITIES:
        return ENTITIES[table]
    for name, info in ENTITIES.items():
        if table in resolve_table_ref(name):
            return info
    return None


def configured_fact_tables() -> frozenset[str]:
    """``FACT_TABLES`` as schema keys (a reference that resolves to nothing is kept as written)."""
    from knowledge.retrieval_hints import FACT_TABLES

    out: set[str] = set()
    for ref in FACT_TABLES:
        out.update(resolve_table_ref(ref) or (ref,))
    return frozenset(out)
