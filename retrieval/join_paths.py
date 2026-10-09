# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Add the tables that connect the retrieved ones: join-path expansion.

Retrieval finds the tables a question *names*. A question that asks for
revenue per product category names the fact table and the category, but the
SQL also needs the product table between them; one that asks for orders per
promotion needs the bridge table between the order and the promotion, which
no one types. Neither is in the retrieved set, and a table that is not in the
prompt cannot be joined. :func:`expand_join_paths` finds them on the
foreign-key graph and returns the smallest set of extra tables that joins the
retrieved ones together.

The graph
---------
Nodes are the queryable tables (those with ``columns`` in ``schema.yaml``).
An edge is a foreign key, from one of three places, strongest first:

1. ``schema.yaml``'s ``relationships`` (the joins the prompt already shows);
2. ``<PROJECT_CONFIG_DIR>/relationships.yaml``, when present -- its
   ``from_table``/``to_table`` are bare names, so ``from_schema``/``to_schema``
   pick the right table where two schemas share a name;
3. when ``retrieval_infer_relationships`` is on, the naming convention: a
   column ``X_ID`` / ``XID`` of table ``T`` is a key to table ``X``'s ``ID``
   (or to a column of the same name). Only an unambiguous, same-data-source
   target counts, a table is never matched to itself, and an edge already
   declared in (1) or (2) is not duplicated. Inferred edges steer retrieval
   only: nothing is written to configuration and no join hint is added to
   the prompt.

Paths
-----
Tables are attached one at a time to the component grown from the first
fact (or, without one, the first) retrieved table: each time, the retrieved
table closest to the component is joined to it along a shortest path of at
most ``retrieval_join_max_hops`` foreign keys, and the tables on that path are
the additions. Ties break on fewer inferred edges, then on the table names, so
the result never depends on dictionary or set order. A path

* never passes through a *hub* -- a table more than
  ``retrieval_join_max_hub_degree`` others reference (a calendar shared by
  every fact would otherwise "connect" two unrelated facts);
* stays inside one data source: every table on it must live in a source that
  both ends live in, so a dimension replicated into two sources never joins
  a sales fact to an inventory fact;
* is skipped when it would add more than ``retrieval_join_max_added_tables``
  tables in all, or make the schema block exceed ``prompt_retrieval_token_budget``.

A retrieved table that cannot be reached within the hop limit is left alone;
it was not wrong to retrieve it, it just is not joined to the rest.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["JoinEdge", "JoinGraph", "build_join_graph", "connected_within", "expand_join_paths"]

#: ``CustomerID`` / ``Customer_ID`` / ``customer_id`` -> stem ``Customer``.
#: A bare ``ID`` has no stem and does not match.
_KEY_COLUMN_RE = re.compile(r"^(?P<stem>.*?[^_])_?id$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class JoinEdge:
    """An undirected foreign key between two tables.

    ``origin`` is ``"schema"``, ``"relationships"`` or ``"inferred"``.
    """

    a: str
    b: str
    origin: str


@dataclass(frozen=True)
class JoinGraph:
    """Foreign-key graph over the queryable tables.

    Attributes
    ----------
    adjacency:
        ``table -> ((neighbour, inferred), ...)``, neighbours sorted by name;
        a table with no foreign key is absent.
    sources:
        ``table -> data sources it lives in``; empty when only one source is
        configured (every table then lives in the one source).
    """

    adjacency: dict[str, tuple[tuple[str, bool], ...]] = field(default_factory=dict)
    sources: dict[str, frozenset[str]] = field(default_factory=dict)

    def degree(self, table: str) -> int:
        """Number of tables joined to *table*."""
        return len(self.adjacency.get(table, ()))

    def edge_is_inferred(self, a: str, b: str) -> bool:
        """Whether the edge between *a* and *b* was inferred from column names."""
        return dict(self.adjacency.get(a, ())).get(b, False)


# ---------------------------------------------------------------------------
# Building the graph
# ---------------------------------------------------------------------------


def _relationships_yaml_entries() -> list[dict]:
    from core.yaml_loading import safe_load_strict
    from schema_data.registry import schema_yaml_path

    path: Path = schema_yaml_path().parent / "relationships.yaml"
    if not path.is_file():
        return []
    try:
        raw = safe_load_strict(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - an unreadable optional file adds no edges
        return []
    entries = raw.get("relationships", []) if isinstance(raw, dict) else []
    return [e for e in entries if isinstance(e, dict)]


def _resolve_bare(
    name: str, schema: object, by_bare: dict[str, list[str]], qualifiers: dict[str, str],
) -> str | None:
    """The one table key a bare name (and optional schema) names, else ``None``."""
    candidates = by_bare.get(name.strip().lower(), [])
    if schema:
        wanted = str(schema).strip().lower()
        candidates = [
            k for k in candidates
            if qualifiers.get(k, "").lower() == wanted
            or qualifiers.get(k, "").lower().rsplit(".", 1)[-1] == wanted
        ]
    return candidates[0] if len(candidates) == 1 else None


def _infer_edges(
    columns: dict[str, dict[str, str]],
    by_bare: dict[str, list[str]],
    qualifiers: dict[str, str],
    sources: dict[str, frozenset[str]],
    known: set[frozenset[str]],
) -> list[tuple[str, str]]:
    """``(table, target)`` for each column that follows the ``X_ID`` convention.

    *known* holds the pairs already joined; inferred pairs are added to it.
    """
    lowered = {t: {c.lower() for c in cols} for t, cols in columns.items()}
    found: list[tuple[str, str]] = []
    for table in sorted(columns):
        for column in sorted(columns[table]):
            match = _KEY_COLUMN_RE.match(column)
            if match is None:
                continue
            candidates = [
                t for t in by_bare.get(match.group("stem").lower(), [])
                if t != table and ("id" in lowered[t] or column.lower() in lowered[t])
            ]
            if sources:
                candidates = [t for t in candidates if sources[t] & sources[table]]
            if len(candidates) > 1:
                same_schema = [t for t in candidates if qualifiers.get(t) == qualifiers.get(table)]
                candidates = same_schema if len(same_schema) == 1 else []
            if len(candidates) != 1:
                continue
            pair = frozenset((table, candidates[0]))
            if pair in known:
                continue
            known.add(pair)
            found.append((table, candidates[0]))
    return found


_CACHE: dict[tuple, JoinGraph] = {}


def build_join_graph() -> JoinGraph:
    """Build (or fetch the cached) foreign-key graph of the loaded schema.

    The cache is keyed on everything the graph depends on -- the loaded
    schema objects, ``relationships.yaml``'s and ``datasources.yaml``'s
    modification times, and the inference switch -- so a changed
    configuration yields a new graph.

    Returns
    -------
    JoinGraph

    Examples
    --------
    >>> graph = build_join_graph()
    >>> all(isinstance(v, tuple) for v in graph.adjacency.values())
    True
    """
    import config as cfg
    from database.datasources import datasource_names, datasources_path, table_datasource_sets
    from schema_data.registry import (
        bare_table_name,
        get_relationships_map,
        get_table_columns,
        get_table_schema_qualifiers,
        schema_yaml_path,
    )

    columns = get_table_columns()
    relationships = get_relationships_map()
    rel_path = schema_yaml_path().parent / "relationships.yaml"
    ds_path = datasources_path()

    def mtime(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    infer = bool(cfg.settings.retrieval_infer_relationships)
    names = tuple(datasource_names())
    key = (str(rel_path), id(columns), id(relationships), mtime(rel_path), mtime(ds_path), infer, names)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    qualifiers = get_table_schema_qualifiers()
    by_bare: dict[str, list[str]] = {}
    for table in sorted(columns):
        by_bare.setdefault(bare_table_name(table).lower(), []).append(table)

    sources: dict[str, frozenset[str]] = {}
    if len(names) > 1:
        sources = {t: frozenset(s) for t, s in table_datasource_sets().items() if t in columns}

    edges: list[JoinEdge] = []
    known: set[frozenset[str]] = set()

    def add(a: str, b: str, origin: str) -> None:
        pair = frozenset((a, b))
        if a == b or a not in columns or b not in columns or pair in known:
            return
        known.add(pair)
        edges.append(JoinEdge(a, b, origin))

    for name in relationships:
        left, _, right = name.partition(" -> ")
        add(left, right, "schema")
    for entry in _relationships_yaml_entries():
        a = _resolve_bare(str(entry.get("from_table", "")), entry.get("from_schema"), by_bare, qualifiers)
        b = _resolve_bare(str(entry.get("to_table", "")), entry.get("to_schema"), by_bare, qualifiers)
        if a and b:
            add(a, b, "relationships")
    if infer:
        for a, b in _infer_edges(columns, by_bare, qualifiers, sources, known):
            edges.append(JoinEdge(a, b, "inferred"))

    neighbours: dict[str, dict[str, bool]] = {t: {} for t in columns}
    for edge in edges:
        inferred = edge.origin == "inferred"
        neighbours[edge.a][edge.b] = inferred
        neighbours[edge.b][edge.a] = inferred
    graph = JoinGraph(
        adjacency={t: tuple(sorted(n.items())) for t, n in neighbours.items() if n},
        sources=sources,
    )
    _CACHE.clear()
    _CACHE[key] = graph
    return graph


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _best_path(
    graph: JoinGraph,
    start: str,
    targets: set[str],
    *,
    max_hops: int,
    max_hub_degree: int,
) -> tuple[str, ...] | None:
    """Best path from *start* to any of *targets*; ``None`` if none within *max_hops*.

    Best = fewest foreign keys, then fewest inferred ones, then smallest path
    by table names. Intermediate tables must not be hubs and the whole path
    must lie in one data source that *start* and the target both live in.
    """
    own = graph.sources.get(start) if graph.sources else None
    candidates: list[str | None] = sorted(own) if own is not None else [None]
    best: tuple[int, int, tuple[str, ...]] | None = None
    for source in candidates:
        heap: list[tuple[int, int, tuple[str, ...]]] = [(0, 0, (start,))]
        settled: dict[str, tuple[int, int]] = {}
        while heap:
            hops, inferred, path = heapq.heappop(heap)
            node = path[-1]
            if best is not None and (hops, inferred) > best[:2]:
                break
            if node in targets and node != start:
                if best is None or (hops, inferred, path) < best:
                    best = (hops, inferred, path)
                continue
            if node in settled and settled[node] < (hops, inferred):
                continue
            settled[node] = (hops, inferred)
            if hops >= max_hops:
                continue
            if node != start and graph.degree(node) > max_hub_degree:
                continue
            for neighbour, is_inferred in graph.adjacency.get(node, ()):
                if neighbour in path:
                    continue
                if source is not None and source not in graph.sources.get(neighbour, frozenset()):
                    continue
                heapq.heappush(heap, (hops + 1, inferred + int(is_inferred), path + (neighbour,)))
    return best[2] if best is not None else None


def connected_within(
    graph: JoinGraph, table: str, anchors: Iterable[str], hops: int, *, max_hub_degree: int,
) -> bool:
    """Whether *table* reaches any of *anchors* in at most *hops* foreign keys.

    The same path rules as :func:`expand_join_paths`: no stepping through a hub,
    nothing across data sources. A table the graph does not know is never
    connected.

    Examples
    --------
    >>> graph = JoinGraph(adjacency={"A": (("B", False),), "B": (("A", False), ("C", False)),
    ...                              "C": (("B", False),)})
    >>> connected_within(graph, "A", {"C"}, 2, max_hub_degree=10)
    True
    >>> connected_within(graph, "A", {"C"}, 1, max_hub_degree=10)
    False
    """
    targets = set(anchors) - {table}
    if hops < 1 or not targets or table not in graph.adjacency:
        return False
    return _best_path(graph, table, targets, max_hops=hops, max_hub_degree=max_hub_degree) is not None


def _default_fits(tables: Sequence[str]) -> bool:
    import config as cfg
    from prompt_engine.static_prefix import estimate_tokens
    from schema_data.registry import SchemaRegistry

    text = SchemaRegistry.build_schema_context(list(tables))
    return estimate_tokens(text) <= cfg.settings.prompt_retrieval_token_budget


def expand_join_paths(
    selected: Sequence[str],
    *,
    fits: Callable[[Sequence[str]], bool] | None = None,
) -> list[str]:
    """Tables that join the *selected* ones to each other and are not among them.

    Parameters
    ----------
    selected:
        The retrieved tables, in retrieval order. Names the graph does not
        know are ignored.
    fits:
        ``tables -> bool``: whether the schema block of *tables* is still
        within the token budget. Defaults to comparing
        :func:`prompt_engine.static_prefix.estimate_tokens` of the rendered
        block with ``prompt_retrieval_token_budget``.

    Returns
    -------
    list[str]
        The additions in the order they were found, no duplicates, none of
        them in *selected*. Empty when ``retrieval_join_expansion`` is off,
        fewer than two tables are known, or nothing needs adding.

    Examples
    --------
    >>> expand_join_paths([])
    []
    >>> expand_join_paths(["no such table", "nor this one"])
    []
    """
    import config as cfg
    from knowledge.retrieval_hints import FACT_TABLES

    settings = cfg.settings
    if not settings.retrieval_join_expansion:
        return []
    graph = build_join_graph()
    ordered = [t for t in dict.fromkeys(selected) if t in graph.adjacency]
    if len(ordered) < 2:
        return []

    fit = fits or _default_fits
    start = next((t for t in ordered if t in FACT_TABLES), ordered[0])
    component = {start}
    pending = [t for t in ordered if t != start]
    chosen = set(selected)
    added: list[str] = []

    while pending:
        best: tuple[int, int, tuple[str, ...]] | None = None
        best_table = ""
        for table in pending:
            path = _best_path(
                graph, table, component,
                max_hops=settings.retrieval_join_max_hops,
                max_hub_degree=settings.retrieval_join_max_hub_degree,
            )
            if path is None:
                continue
            inferred = sum(1 for x, y in zip(path, path[1:]) if graph.edge_is_inferred(x, y))
            key = (len(path), inferred, path)
            if best is None or key < best:
                best, best_table = key, table
        if best is None:
            break
        path = best[2]
        new = [t for t in reversed(path[1:-1]) if t not in chosen and t not in added]
        pending.remove(best_table)
        if new:
            if len(added) + len(new) > settings.retrieval_join_max_added_tables:
                continue
            if not fit([*selected, *added, *new]):
                continue
            added.extend(new)
        component.update(path)
        pending = [t for t in pending if t not in component]
    return added
