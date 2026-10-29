# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A ``ring_aliases`` key is the value of a ring, not the name of a table.

``aliases.yaml`` ``ring_aliases`` maps the exact value of the ring dimension's
name column to the forms analysts use for it.
:meth:`retrieval.value_retriever.ValueRetriever.extract_ring` finds a form in
the question and returns the *key*, which becomes the ring filter
(``WHERE r.[Name] = N'<key>'``). The setup wizard of 6.13 to 6.16.2 wrote one
entry per **table** instead, keyed by the table's ``schema.yaml`` key and
holding that table's aliases, so a question that named a table's alias (or a
word of it) filtered the ring by a table name and came back empty.

This module is the one place that tells the two apart, for the three that need
to know:

* the loader (:mod:`knowledge.aliases`) ignores an entry whose key is a table
  key of ``schema.yaml`` -- :func:`guard_ring_aliases`, which logs one WARNING;
* the preflight (``Aliases are curated``) fails on such entries
  -- :func:`table_keyed_ring_keys` and :func:`schema_table_keys`;
* the setup wizard drops them from an earlier ``aliases.yaml``
  -- :func:`ring_keys_naming_tables`.

Two rules, on purpose different. The loader and the preflight act only on an
*exact* match (text compared as a whole, letter case ignored): a ring called
``North Hall`` is not touched because a table is called ``Hall``, and a curated
file keeps behaving as it does. The wizard also drops a key that names a table
the way the application resolves a configured table name
(``Customer`` for ``sales.Customer``): it writes a file for the operator to
read, and what it removes is listed.

File reads only; nothing here touches the database. Nothing returned or logged
is a value of a row: keys of ``ring_aliases`` are configuration.

Public API
----------
listed(keys, limit=10) -> str
    ``a, b, c`` with at most *limit* keys, then ``and N more``: how a message names keys.
table_keyed_ring_keys(ring_keys, table_keys) -> list[str]
without_table_keys(ring_aliases, table_keys) -> tuple[Mapping, list[str]]
schema_table_keys(directory) -> frozenset[str]
guard_ring_aliases(ring_aliases, directory) -> Mapping
ring_keys_naming_tables(view, ring_keys) -> list[str]
table_view(tables) -> SchemaView
ring_keys_outside(ring_keys, cached) -> int
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TypeVar

from core import config_doctor as doctor
from core.persian import normalize_for_matching

__all__ = [
    "guard_ring_aliases",
    "listed",
    "ring_keys_naming_tables",
    "ring_keys_outside",
    "schema_table_keys",
    "table_keyed_ring_keys",
    "table_view",
    "without_table_keys",
]

logger = logging.getLogger(__name__)

_V = TypeVar("_V")


def _fold(text: str) -> str:
    return text.strip().casefold()


def listed(keys: Sequence[str], limit: int = 10) -> str:
    """``a, b, c`` with at most *limit* keys, then ``and N more``.

    Examples
    --------
    >>> listed(["a", "b", "c"], 2)
    'a, b, and 1 more'
    >>> listed(["a"])
    'a'
    """
    shown = list(keys[:limit])
    more = len(keys) - len(shown)
    return ", ".join(shown) + (f", and {more} more" if more > 0 else "")


def table_keyed_ring_keys(ring_keys: Iterable[str], table_keys: Iterable[str]) -> list[str]:
    """The *ring_keys* that are, as a whole and ignoring letter case, a key in *table_keys*.

    Parameters
    ----------
    ring_keys:
        The keys of ``ring_aliases``.
    table_keys:
        The keys of ``schema.yaml``'s ``tables``.

    Returns
    -------
    list[str]
        The ring keys as written, in their order.

    Examples
    --------
    >>> table_keyed_ring_keys(["Hall", "North Hall", "sales.trade"], ["Hall", "sales.Trade"])
    ['Hall', 'sales.trade']
    >>> table_keyed_ring_keys(["Region A"], [])
    []
    """
    tables = {_fold(key) for key in table_keys}
    return [key for key in ring_keys if _fold(key) in tables]


def without_table_keys(
    ring_aliases: Mapping[str, _V],
    table_keys: Iterable[str],
) -> tuple[Mapping[str, _V], list[str]]:
    """*ring_aliases* without the entries whose key is a table key, and those keys.

    When there is nothing to drop the very same mapping comes back, not a copy:
    a curated file is not touched in any way.

    Examples
    --------
    >>> rings = {"North Hall": ["north"], "Hall": ["hall"]}
    >>> kept, dropped = without_table_keys(rings, ["Hall"])
    >>> kept, dropped
    ({'North Hall': ['north']}, ['Hall'])
    >>> without_table_keys(rings, ["Customer"])[0] is rings
    True
    """
    dropped = table_keyed_ring_keys(ring_aliases, table_keys)
    if not dropped:
        return ring_aliases, []
    gone = set(dropped)
    return {key: value for key, value in ring_aliases.items() if key not in gone}, dropped


def schema_table_keys(directory: Path) -> frozenset[str]:
    """The keys of ``schema.yaml`` in *directory*; empty when it is absent or does not parse.

    Examples
    --------
    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     _ = Path(tmp, "schema.yaml").write_text("tables:\\n  Hall: {}\\n  sales.Trade: {}\\n", encoding="utf-8")
    ...     sorted(schema_table_keys(Path(tmp)))
    ['Hall', 'sales.Trade']
    >>> schema_table_keys(Path("no such directory"))
    frozenset()
    """
    return frozenset(table.key for table in doctor.read_schema(directory).tables)


def guard_ring_aliases(ring_aliases: Mapping[str, _V], directory: Path) -> Mapping[str, _V]:
    """*ring_aliases* without the entries named like a table of ``directory/schema.yaml``.

    The loader's guard (see the module docstring). One WARNING names how many
    entries were ignored and which (at most ten). Without
    a readable ``schema.yaml`` nothing is ignored: today's behaviour. The same
    mapping is returned when no key is a table key.

    Parameters
    ----------
    ring_aliases:
        ``aliases.yaml`` ``ring_aliases``.
    directory:
        The ``project_config`` directory ``schema.yaml`` is read from.

    Returns
    -------
    Mapping
        *ring_aliases* itself, or a copy without the table-keyed entries.

    Examples
    --------
    >>> import tempfile
    >>> rings = {"North Hall": ["north"], "Hall": ["hall"]}
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     guard_ring_aliases(rings, Path(tmp)) is rings
    True
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     _ = Path(tmp, "schema.yaml").write_text("tables:\\n  Hall: {}\\n", encoding="utf-8")
    ...     guard_ring_aliases(rings, Path(tmp))
    {'North Hall': ['north']}
    """
    kept, dropped = without_table_keys(ring_aliases, schema_table_keys(directory))
    if dropped:
        logger.warning(
            "aliases.yaml: %d ring_aliases key(s) are keys of schema.yaml tables and are ignored "
            "(a ring_aliases key is the exact value of the ring's name column, not a table): %s. "
            "The setup wizard of 6.13 to 6.16.2 wrote them; run it again to drop them, or restore the "
            "curated ring names.",
            len(dropped), listed(dropped),
        )
    return kept


def table_view(tables: Iterable[tuple[str, str, str]]) -> doctor.SchemaView:
    """A :class:`~core.config_doctor.SchemaView` of ``(key, schema, name)`` triples.

    For a caller that knows the tables but has no ``schema.yaml`` (the setup
    wizard, from the database it has just read).

    Examples
    --------
    >>> view = table_view([("sales.Trade", "sales", "Trade"), ("Hall", "ref", "Hall")])
    >>> [table.key for table in view.tables], view.parsed
    (['sales.Trade', 'Hall'], True)
    """
    rows = {key: doctor.SchemaTable(key, schema, name, "", (), ()) for key, schema, name in tables}
    return doctor.SchemaView(present=True, parsed=True, tables=tuple(rows.values()))


def ring_keys_naming_tables(view: doctor.SchemaView, ring_keys: Iterable[str]) -> list[str]:
    """The *ring_keys* that name a table of *view*: by its key, or the way the application resolves a table name.

    That is a key (letter case ignored), a bare name that finds a key
    (``Customer`` for ``sales.Customer``), or a ``Schema.Table`` spelling of a
    table the file keys differently -- the three ways the setup wizard has
    written a table into ``ring_aliases``.

    Examples
    --------
    >>> view = table_view([("sales.Trade", "sales", "Trade"), ("Hall", "ref", "Hall"), ("Date", "Ref_Dim", "Date")])
    >>> ring_keys_naming_tables(view, ["North Hall", "Hall", "Trade", "Ref_Dim.Date", "Region A"])
    ['Hall', 'Trade', 'Ref_Dim.Date']
    """
    keys = list(ring_keys)
    spelled = {key for key, _ in doctor.entity_key_findings(view, (), keys).unknown_rings}
    return [key for key in keys if doctor.resolve_table_keys(view, key) or key in spelled]


def ring_keys_outside(ring_keys: Iterable[str], cached: Iterable[str]) -> int:
    """How many *ring_keys* are not among the *cached* values of the ring dimension.

    Compared as the application compares names: Persian and Arabic letter
    forms, digits and spacing folded. A count, never a value.

    Examples
    --------
    >>> ring_keys_outside(["Region A", "Region C"], ["Region A", "Region B"])
    1
    >>> ring_keys_outside([], ["Region A"])
    0
    """
    known = {normalize_for_matching(value) for value in cached}
    return sum(1 for key in ring_keys if normalize_for_matching(key) not in known)
