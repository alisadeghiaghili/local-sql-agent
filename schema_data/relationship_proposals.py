# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Proposed JOIN relationships, read from the databases, for a person to review.

Two sources, one review file:

* **Declared foreign keys.** A foreign key the database enforces is a fact;
  each one between two tables of ``schema.yaml`` becomes a proposal with the
  constraint's name as its basis.
* **Naming convention.** Many warehouses declare no keys at all, and name the
  link instead: ``Order.CustomerID`` points at ``Customer.ID``;
  ``Order.OrderDate_ID`` points at ``Date.ID``. A column named ``<X>ID``,
  ``<X>_ID`` or ``<X>Id`` is matched to a table ``X`` of ``schema.yaml``, or
  to a table whose name ends the stem at a word boundary (``OrderDate`` ends
  in ``Date``), and joined to that table's primary key (or, with none
  declared, its ``ID`` column). The guess is dropped, and counted, when:

  - the column is a declared foreign key already (the fact wins);
  - the stem names no table, or names the table the column is in (``Order.OrderID``
    is that table's own key; ``Category.ParentCategoryID`` is a real link and is kept);
  - several tables of different schemas match and the source table's own
    schema does not pick one;
  - the two tables share no data source (a query cannot span sources);
  - the target has a composite primary key, or none and no ``ID`` column;
  - the target column is not listed in ``schema.yaml``;
  - the two columns' types are of different kinds (a number and a string).

Proposals already present in ``relationships.yaml`` or in ``schema.yaml``'s own
``relationships:`` are left out. The output is a separate file that nothing
reads; this module never edits ``relationships.yaml``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from database.catalogue import TableInfo, table_location
from schema_data.registry import (
    SchemaConfig,
    bare_table_name,
    effective_qualifier,
    table_reference_sql,
)
from schema_data.sync import SourceTables, type_family
from schema_data.yaml_text import quote

__all__ = [
    "PROPOSALS_FILENAME",
    "Proposal",
    "ProposalSet",
    "propose_relationships",
    "render_proposals_yaml",
]

#: Name of the review file written next to ``relationships.yaml``.
PROPOSALS_FILENAME = "relationships.proposed.yaml"

_ID_COLUMN = re.compile(r"^(?P<stem>.+?)(?:_(?:ID|Id|id)|ID|Id)$")


@dataclass(frozen=True)
class Proposal:
    """One proposed relationship, in the shape of a ``relationships.yaml`` entry.

    Attributes
    ----------
    from_table, from_schema, from_column:
        The referencing side: bare table name, its qualifier (``sales``,
        ``OtherDb.dbo`` or ``""``) and the column (several, comma-separated,
        for a composite key).
    to_table, to_schema, to_column:
        The referenced side, likewise.
    join_hint:
        The ready-to-use ``JOIN`` clause.
    basis:
        ``declared foreign key <name>`` or ``naming convention: <col> -> <table>.<col>``.
    confidence:
        ``declared``, ``high`` (the stem is the table's name and its primary
        key is declared) or ``medium`` (a word-boundary match, or no declared key).
    datasource:
        The data sources both tables are in, only when several are configured.
    """

    from_table: str
    from_schema: str
    from_column: str
    to_table: str
    to_schema: str
    to_column: str
    join_hint: str
    basis: str
    confidence: str
    datasource: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProposalSet:
    """The result of :func:`propose_relationships`.

    Attributes
    ----------
    proposals:
        New proposals, in schema order (declared before inferred, per table).
    declared, inferred:
        How many of them came from each source.
    already_known:
        Proposals left out because ``relationships.yaml`` or ``schema.yaml``
        already has the relationship.
    skipped:
        ``(what, why)`` for each foreign key or naming-convention guess that
        was dropped.
    """

    proposals: tuple[Proposal, ...]
    declared: int
    inferred: int
    already_known: int
    skipped: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Table:
    """A queryable ``schema.yaml`` table located in the databases."""

    key: str
    bare: str
    qualifier: str
    location: tuple[str, str]
    sources: tuple[str, ...]
    columns: frozenset[str]          # lower-cased names listed in schema.yaml
    info: Mapping[str, TableInfo]    # per source


def _boundary(stem: str, at: int) -> bool:
    """*stem* has a word boundary before index *at* (``_`` or a camel-case hump)."""
    if at <= 0:
        return False
    before, char = stem[at - 1], stem[at]
    return before == "_" or (char.isupper() and (before.islower() or before.isdigit()))


def _quote_ident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _locate(
    schema: SchemaConfig, sources: Sequence[str], catalogues: Mapping[str, SourceTables],
) -> dict[str, _Table]:
    tables: dict[str, _Table] = {}
    for key, table in schema.tables.items():
        if table.columns is None:
            continue
        location = table_location(key, table.db_schema, "dbo")
        found = tuple(s for s in sources if location in catalogues[s])
        if not found:
            continue
        tables[key] = _Table(
            key=key,
            bare=bare_table_name(key),
            qualifier=".".join(effective_qualifier(key, table.db_schema)),
            location=location,
            sources=found,
            columns=frozenset(c.lower() for c in table.columns),
            info={s: catalogues[s][location] for s in found},
        )
    return tables


def _known_pairs(
    schema: SchemaConfig, existing: Sequence[Mapping[str, object]],
) -> tuple[set[frozenset[tuple[str, str]]], list[tuple[frozenset[str], str]]]:
    """Relationships already written down: column pairs, and ``schema.yaml``'s joins."""
    pairs: set[frozenset[tuple[str, str]]] = set()
    for entry in existing:
        names = [str(entry.get(k) or "").strip().lower() for k in
                 ("from_table", "from_column", "to_table", "to_column")]
        if all(names):
            pairs.add(frozenset({(names[0], names[1]), (names[2], names[3])}))
    inline = [
        (frozenset({bare_table_name(r.from_table).lower(), bare_table_name(r.to_table).lower()}),
         r.join_sql.lower())
        for r in schema.relationships
    ]
    return pairs, inline


def _is_known(
    proposal: Proposal, pairs: set[frozenset[tuple[str, str]]],
    inline: list[tuple[frozenset[str], str]],
) -> bool:
    ft, tt = proposal.from_table.lower(), proposal.to_table.lower()
    fc, tc = proposal.from_column.lower(), proposal.to_column.lower()
    if frozenset({(ft, fc), (tt, tc)}) in pairs:
        return True
    names = frozenset({ft, tt})
    columns = [c.strip() for c in (*fc.split(","), *tc.split(","))]
    return any(
        tables == names and all(c in sql for c in columns) for tables, sql in inline
    )


def _join_hint(
    source: _Table, target: _Table, from_cols: Sequence[str], to_cols: Sequence[str],
) -> str:
    to_ref = table_reference_sql(target.key, target.qualifier)
    from_ref = table_reference_sql(source.key, source.qualifier)
    on = " AND ".join(
        f"{from_ref}.{_quote_ident(f)} = {to_ref}.{_quote_ident(t)}"
        for f, t in zip(from_cols, to_cols)
    )
    return f"JOIN {to_ref} ON {on}"


def _make(
    source: _Table, target: _Table, from_cols: Sequence[str], to_cols: Sequence[str],
    basis: str, confidence: str, shared: Sequence[str], multi_source: bool,
) -> Proposal:
    return Proposal(
        from_table=source.bare, from_schema=source.qualifier, from_column=", ".join(from_cols),
        to_table=target.bare, to_schema=target.qualifier, to_column=", ".join(to_cols),
        join_hint=_join_hint(source, target, from_cols, to_cols),
        basis=basis, confidence=confidence,
        datasource=tuple(shared) if multi_source else (),
    )


def propose_relationships(
    schema: SchemaConfig,
    sources: Sequence[str],
    catalogues: Mapping[str, SourceTables],
    existing: Sequence[Mapping[str, object]] = (),
) -> ProposalSet:
    """Relationships to review, from declared keys and column names.

    Parameters
    ----------
    schema:
        The ``schema.yaml`` the proposals are for (after a sync, so tables
        and columns just added are included). Only tables with a ``columns``
        map take part; a join to any other table would be refused by the guard.
    sources:
        Source names in ``datasources.yaml`` order.
    catalogues:
        ``{source: {(schema, table): TableInfo}}``.
    existing:
        The entries of ``relationships.yaml`` (dicts with ``from_table``,
        ``from_column``, ``to_table`` and ``to_column``); matching proposals
        are left out.

    Returns
    -------
    ProposalSet
        Deterministic: the same inputs give the same proposals in the same order.

    Examples
    --------
    >>> from database.catalogue import ColumnInfo
    >>> from schema_data.registry import validate_schema_yaml_text
    >>> schema = validate_schema_yaml_text(
    ...     "tables:\\n"
    ...     "  Order: {db_schema: s, columns: {ID: a, CustomerID: b}}\\n"
    ...     "  Customer: {db_schema: s, columns: {ID: a}}\\n"
    ... )
    >>> def info(name, *cols, pk=("ID",)):
    ...     return TableInfo("s", name, False, tuple(ColumnInfo(c, "int", False) for c in cols), pk)
    >>> cat = {"a": {("s", "order"): info("Order", "ID", "CustomerID"),
    ...              ("s", "customer"): info("Customer", "ID")}}
    >>> result = propose_relationships(schema, ["a"], cat)
    >>> [(p.from_table, p.from_column, p.to_table, p.to_column, p.confidence) for p in result.proposals]
    [('Order', 'CustomerID', 'Customer', 'ID', 'high')]
    """
    multi_source = len(sources) > 1
    tables = _locate(schema, sources, catalogues)
    by_location = {t.location: t for t in tables.values()}
    by_bare: dict[str, list[_Table]] = {}
    for t in tables.values():
        by_bare.setdefault(t.bare.lower(), []).append(t)
    collector = _Collector(*_known_pairs(schema, existing))
    for table in tables.values():
        covered = _add_declared(table, by_location, collector, multi_source)
        _add_by_convention(table, covered, by_bare, collector, multi_source)
    return ProposalSet(
        proposals=tuple(collector.proposals), declared=collector.declared,
        inferred=collector.inferred, already_known=collector.known,
        skipped=tuple(collector.skipped),
    )


class _Collector:
    """What :func:`propose_relationships` has found so far: proposals, skips and the counts."""

    def __init__(self, pairs: Any, inline: Any) -> None:
        self.pairs = pairs
        self.inline = inline
        self.proposals: list[Proposal] = []
        self.skipped: list[tuple[str, str]] = []
        self.declared = 0
        self.inferred = 0
        self.known = 0

    def add(self, proposal: Proposal, kind: str) -> None:
        """Keep *proposal* (counted as *kind*) unless the configuration already has it."""
        if _is_known(proposal, self.pairs, self.inline):
            self.known += 1
            return
        self.proposals.append(proposal)
        if kind == "declared":
            self.declared += 1
        else:
            self.inferred += 1


_ForeignKeyId = tuple[tuple[str, ...], str, tuple[str, ...]]


def _merged_foreign_keys(
    table: _Table, by_location: dict[Any, _Table], collector: _Collector,
) -> tuple[dict[_ForeignKeyId, tuple[str, list[str], _Table]], set[str]]:
    """*table*'s declared foreign keys merged across the sources that declare them, and the columns they cover.

    A key whose referenced table is not in ``schema.yaml`` with columns, or
    whose columns are not all listed there, is recorded as skipped.
    """
    covered: set[str] = set()
    merged: dict[_ForeignKeyId, tuple[str, list[str], _Table]] = {}
    for source in table.sources:
        for fk in table.info[source].foreign_keys:
            label = f"{table.key}.{', '.join(fk.columns)} -> {fk.ref_location[1]}"
            target = by_location.get(fk.ref_location)
            if target is None:
                collector.skipped.append((label, "the referenced table is not in schema.yaml with columns"))
                continue
            if not all(c.lower() in table.columns for c in fk.columns) or not all(
                c.lower() in target.columns for c in fk.ref_columns
            ):
                collector.skipped.append((label, "a key column is not listed in schema.yaml"))
                continue
            covered.update(c.lower() for c in fk.columns)
            ident = (tuple(c.lower() for c in fk.columns), target.key,
                     tuple(c.lower() for c in fk.ref_columns))
            if ident in merged:
                merged[ident][1].append(source)
            else:
                merged[ident] = (fk.name, [source], target)
    return merged, covered


def _add_declared(
    table: _Table, by_location: dict[Any, _Table], collector: _Collector, multi_source: bool,
) -> set[str]:
    """Propose *table*'s declared foreign keys; return the (lower-cased) columns they cover."""
    merged, covered = _merged_foreign_keys(table, by_location, collector)
    for (from_low, _, to_low), (name, found_in, target) in merged.items():
        first = table.info[found_in[0]]
        from_cols = [next(c.name for c in first.columns if c.name.lower() == low) for low in from_low]
        ref = target.info[found_in[0]] if found_in[0] in target.info else next(iter(target.info.values()))
        to_cols = [next(c.name for c in ref.columns if c.name.lower() == low) for low in to_low]
        collector.add(
            _make(table, target, from_cols, to_cols, f"declared foreign key {name}".strip(),
                  "declared", found_in, multi_source),
            "declared",
        )
    return covered


def _add_by_convention(
    table: _Table,
    covered: set[str],
    by_bare: dict[str, list[_Table]],
    collector: _Collector,
    multi_source: bool,
) -> None:
    """Propose links by the naming convention, for the columns no foreign key already explains."""
    first_source = table.sources[0]
    for column in table.info[first_source].columns:
        low = column.name.lower()
        if low not in table.columns or low in covered or low == "id":
            continue
        match = _ID_COLUMN.match(column.name)
        if match is None:
            continue
        guess = _infer(table, column.name, column.data_type, match.group("stem"), by_bare)
        if guess is None:
            continue
        if isinstance(guess, str):
            collector.skipped.append((f"{table.key}.{column.name}", guess))
            continue
        target, to_column, confidence = guess
        shared = [s for s in table.sources if s in target.sources]
        collector.add(
            _make(table, target, [column.name], [to_column],
                  f"naming convention: {column.name} -> {target.bare}.{to_column}",
                  confidence, shared, multi_source),
            "inferred",
        )


def _infer(
    table: _Table, column: str, data_type: str, stem: str, by_bare: Mapping[str, list[_Table]],
) -> tuple[_Table, str, str] | str | None:
    """The table and column a name-based guess points at.

    Returns ``None`` when the column is simply not a link (no table has that
    name, or it is the table's own key), and a sentence saying why when it
    looks like one but cannot be trusted.
    """
    pool, kind = _stem_matches(stem, by_bare)
    if not pool:
        return None
    if kind == "exact" and any(t.key == table.key for t in pool):
        return None  # `Order.OrderID` is Order's own key, not a link
    pool = [t for t in pool if set(t.sources) & set(table.sources)]
    if not pool:
        return "the matching table shares no data source with this one"
    if len(pool) > 1:
        same = [t for t in pool if t.location[0] == table.location[0]]
        if len(same) != 1:
            return "several tables match: " + ", ".join(sorted(t.key for t in pool))
        pool = same
    target = pool[0]
    source = next(s for s in table.sources if s in target.sources)
    info = target.info[source]
    key = _key_column(target, info)
    if isinstance(key, str):
        return key
    to_column, declared_key = key
    if to_column.lower() not in target.columns:
        return f"{target.key}.{to_column} is not listed in schema.yaml"
    target_type = next((c.data_type for c in info.columns if c.name == to_column), "")
    if type_family(data_type) != type_family(target_type):
        return f"types differ ({data_type} vs {target_type})"
    confidence = "high" if kind == "exact" and declared_key else "medium"
    return target, to_column, confidence


def _stem_matches(stem: str, by_bare: dict[str, list[_Table]]) -> tuple[list[_Table], str]:
    """The tables a column stem names, and how: ``"exact"`` (same bare name) or ``"suffix"``.

    A suffix match is a bare name of at least three letters that ends the
    stem at a word boundary; only the longest such names count.
    """
    stem_low = stem.lower()
    pool = list(by_bare.get(stem_low, ()))
    if pool:
        return pool, "exact"
    matches = [
        (bare, items) for bare, items in by_bare.items()
        if len(bare) >= 3 and len(stem) > len(bare) and stem_low.endswith(bare)
        and _boundary(stem, len(stem) - len(bare))
    ]
    if not matches:
        return [], "suffix"
    longest = max(len(bare) for bare, _ in matches)
    return [t for bare, items in matches if len(bare) == longest for t in items], "suffix"


def _key_column(target: _Table, info: TableInfo) -> tuple[str, bool] | str:
    """``(column, is_declared_primary_key)`` a link to *target* points at, or why there is none.

    The declared primary key when it is one column; else an ``ID``,
    ``<Table>ID`` or ``<Table>_ID`` column.
    """
    if len(info.primary_key) > 1:
        return f"{target.key} has a composite primary key"
    if info.primary_key:
        return info.primary_key[0], True
    names = {c.name.lower(): c.name for c in info.columns}
    wanted = next(
        (names[n] for n in ("id", f"{target.bare.lower()}id", f"{target.bare.lower()}_id") if n in names),
        None,
    )
    if wanted is None:
        return f"{target.key} has no primary key and no ID column"
    return wanted, False


def render_proposals_yaml(proposals: Sequence[Proposal]) -> str:
    """The text of the review file.

    Parameters
    ----------
    proposals:
        :attr:`ProposalSet.proposals`.

    Returns
    -------
    str
        A YAML document with a header that says nothing reads it, then one
        ``relationships:`` entry per proposal (``relationships: []`` when
        there are none). No timestamps, so the same proposals give the same
        bytes.

    Examples
    --------
    >>> text = render_proposals_yaml([])
    >>> text.splitlines()[-1]
    'relationships: []'
    """
    lines = [
        "# relationships.proposed.yaml -- PROPOSALS ONLY, written by scripts/sync_schema.py.",
        "# Nothing reads this file. Review every entry, delete the wrong ones, and copy",
        "# the rest into relationships.yaml by hand.",
        "#",
        "#   basis: declared foreign key ...   a constraint the database enforces.",
        "#   basis: naming convention: ...     a guess from column names (X_ID -> X.ID);",
        "#                                     check it before you trust it.",
        "#   confidence: declared | high | medium",
        "#   datasource: only with several data sources; both tables live there.",
        "",
    ]
    if not proposals:
        return "\n".join([*lines, "relationships: []", ""])
    lines.append("relationships:")
    for p in proposals:
        lines.append(f"  - from_table: {quote(p.from_table)}")
        if p.from_schema:
            lines.append(f"    from_schema: {quote(p.from_schema)}")
        lines.append(f"    from_column: {quote(p.from_column)}")
        lines.append(f"    to_table: {quote(p.to_table)}")
        if p.to_schema:
            lines.append(f"    to_schema: {quote(p.to_schema)}")
        lines.extend([
            f"    to_column: {quote(p.to_column)}",
            f"    join_hint: {quote(p.join_hint)}",
            f"    basis: {quote(p.basis)}",
            f"    confidence: {quote(p.confidence)}",
        ])
        if p.datasource:
            lines.append(f"    datasource: {quote(', '.join(p.datasource))}")
        lines.append("")
    return "\n".join(lines)
