# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Keep ``schema.yaml``'s *structure* in step with the databases, and nothing else.

``schema.yaml`` mixes two kinds of content. The curated kind (descriptions,
business notes, resolvable and prefetchable flags, the comments around them)
is written by people and must never be touched by a tool. The structural kind
is a fact about the databases: which tables exist and in which data source,
which columns they have, and what type each column is. This module computes
and applies the structural part only, as line edits to the file's text, so
every comment, quote and blank line survives.

What it does, per table of ``schema.yaml``
------------------------------------------
* ``datasource:`` -- set or repaired with the rules of
  :mod:`schema_data.placement` (only with more than one data source).
* **Columns the database has and ``schema.yaml`` lacks** are appended to the
  table's ``columns:`` with the draft description ``"<type> column"`` and the
  comment :data:`DRAFT_COMMENT`, and their type is recorded.
* **Columns ``schema.yaml`` lists that no source's table has** get the
  comment line :data:`STALE_COLUMN_COMMENT` directly above them and stay
  where they are. With ``prune=True`` they are removed instead (together
  with their type), unless the column is still named in
  ``resolvable_columns`` or ``prefetchable_columns``.
* **Types** live in the table's ``column_types:`` map
  (:class:`schema_data.registry.TableDefinition`), never in a description: a
  column without an entry gets one, an entry that differs from the database
  is corrected, and each correction is reported.
* A table the databases do not have gets :data:`schema_data.placement.NOT_FOUND_COMMENT`
  under its key; with ``prune=True`` it is removed (unless
  ``relationships:`` still names it).
* A table with no ``columns:`` key is described-only on purpose and is left
  alone: its columns are not synced and it does not become queryable.

Tables the databases have and ``schema.yaml`` lacks are reported; those
matching an ``add_tables`` pattern (a glob on ``schema.table``) are appended.

Safety
------
:func:`sync_schema_text` validates its output with
:func:`~schema_data.registry.validate_schema_yaml_text` and then proves it
changed nothing but what the plan says: the output, parsed, must equal the
original, parsed, with the plan applied to it
(:func:`expected_tables`). A curated field that differs fails the run.
The functions here never connect to a database and never see a row; they
work from :class:`database.catalogue.TableInfo` objects.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from database.catalogue import ColumnInfo, TableInfo, table_location
from schema_data.placement import (
    NOT_FOUND_COMMENT,
    Placement,
    datasource_edits,
    locate_tables,
)
from schema_data.registry import (
    SchemaConfig,
    TableDefinition,
    bare_table_name,
    split_table_key,
    validate_schema_yaml_text,
)
from schema_data.yaml_text import (
    Edit,
    LayoutError,
    TableBlock,
    apply_edits,
    child_span,
    parse_entries,
    parse_tables,
    quote,
    trailing_comment,
    yaml_key,
)

__all__ = [
    "DRAFT_COMMENT",
    "STALE_COLUMN_COMMENT",
    "ColumnAdd",
    "NewTable",
    "RenderStats",
    "SourceTables",
    "SyncOptions",
    "SyncPlan",
    "SyncResult",
    "TablePlan",
    "TypeChange",
    "build_report",
    "compute_plan",
    "draft_description",
    "expected_tables",
    "render_synced_text",
    "sync_schema_text",
    "type_family",
    "verify_structural_only",
]

#: One source's catalogue: ``{(schema, table): info}``, keys lower-cased.
SourceTables = Mapping[tuple[str, str], TableInfo]

#: The comment on a column (or table) added from the database, to be curated.
DRAFT_COMMENT = "# TO BE FILLED (added by sync_schema.py)"

#: The comment line put directly above a column the database does not have.
STALE_COLUMN_COMMENT = "# not in database (sync_schema.py)"

_STALE_PREFIX = "# not in database"
_PLAIN_TABLE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_PLAIN_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: How many names a report section lists before summarising the rest.
REPORT_LIST_LIMIT = 200

_TYPE_FAMILIES = {
    "tinyint": "integer", "smallint": "integer", "int": "integer", "bigint": "integer",
    "integer": "integer", "decimal": "number", "numeric": "number", "money": "number",
    "smallmoney": "number", "float": "number", "real": "number",
    "char": "text", "varchar": "text", "nchar": "text", "nvarchar": "text",
    "text": "text", "ntext": "text",
    "date": "datetime", "datetime": "datetime", "datetime2": "datetime",
    "smalldatetime": "datetime", "datetimeoffset": "datetime", "time": "datetime",
}


@dataclass(frozen=True)
class SyncOptions:
    """What a sync may do beyond the safe defaults.

    Attributes
    ----------
    prune:
        Remove columns and tables ``schema.yaml`` lists that the databases
        do not have, instead of marking them.
    add_tables:
        Glob patterns on ``schema.table`` (case-insensitive); a table in a
        database, not in ``schema.yaml``, that matches one is added.
    """

    prune: bool = False
    add_tables: tuple[str, ...] = ()


@dataclass(frozen=True)
class ColumnAdd:
    """A database column ``schema.yaml`` lacks.

    Attributes
    ----------
    name:
        The column in the database's letter case.
    data_type:
        Its type (``int``, ``nvarchar(100)``).
    sources:
        The data sources that have it.
    """

    name: str
    data_type: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class TypeChange:
    """A recorded type that no longer matches the database.

    Attributes
    ----------
    column:
        The column as written in ``schema.yaml``.
    old, new:
        The recorded type and the database's.
    """

    column: str
    old: str
    new: str


@dataclass(frozen=True)
class TablePlan:
    """What a sync does to one existing ``schema.yaml`` table.

    Attributes
    ----------
    key:
        The table key.
    placement:
        Where the table was found (and its ``datasource:`` now).
    nowhere:
        Found in no data source.
    described_only:
        The table has no ``columns:`` key; its columns are not synced.
    unverifiable:
        Found, but the login sees none of its columns; columns are not synced.
    stale:
        Columns in ``schema.yaml`` that no source's table has.
    pruned:
        The part of *stale* to remove (``prune`` only).
    kept:
        ``{column: why it was not removed}`` for stale columns ``prune``
        left in place.
    live:
        Lower-cased names of the table's columns that the database has.
    adds:
        Database columns to append.
    recorded:
        ``(column, type)`` entries to add to ``column_types:`` for columns
        already in ``schema.yaml``.
    changed:
        Recorded types to correct.
    shape:
        Differences between the data sources a table is in, one line each.
    prune_table:
        The table is removed.
    kept_table:
        Why a table found nowhere was not removed under ``prune`` (``""``
        when it was, or when ``prune`` is off).
    """

    key: str
    placement: Placement
    nowhere: bool = False
    described_only: bool = False
    unverifiable: bool = False
    stale: tuple[str, ...] = ()
    pruned: tuple[str, ...] = ()
    kept: tuple[tuple[str, str], ...] = ()
    live: frozenset[str] = frozenset()
    adds: tuple[ColumnAdd, ...] = ()
    recorded: tuple[tuple[str, str], ...] = ()
    changed: tuple[TypeChange, ...] = ()
    shape: tuple[str, ...] = ()
    prune_table: bool = False
    kept_table: str = ""


@dataclass(frozen=True)
class NewTable:
    """A database table to append to ``schema.yaml``.

    Attributes
    ----------
    key:
        The table key it will be written under.
    schema, name:
        In the database's letter case.
    is_view:
        A view rather than a table.
    sources:
        The data sources that have it.
    columns:
        Its columns, in database order, with the first source's types.
    """

    key: str
    schema: str
    name: str
    is_view: bool
    sources: tuple[str, ...]
    columns: tuple[ColumnInfo, ...]


@dataclass(frozen=True)
class SyncPlan:
    """The structural difference between ``schema.yaml`` and the databases.

    Attributes
    ----------
    sources, default:
        The configured data sources (default first) and the default one.
    tables:
        One :class:`TablePlan` per existing table, in file order.
    new_tables:
        Tables to append (matched by ``add_tables``).
    unlisted:
        ``(schema.table, sources)`` of database tables ``schema.yaml`` lacks
        and the options did not add.
    skipped_new:
        ``(schema.table, reason)`` for a table matched by ``add_tables`` that
        could not be added.
    """

    sources: tuple[str, ...]
    default: str
    tables: tuple[TablePlan, ...]
    new_tables: tuple[NewTable, ...] = ()
    unlisted: tuple[tuple[str, tuple[str, ...]], ...] = ()
    skipped_new: tuple[tuple[str, str], ...] = ()

    @property
    def multi_source(self) -> bool:
        """Several data sources are configured, so ``datasource:`` lines apply."""
        return len(self.sources) > 1


@dataclass
class RenderStats:
    """What the text edit did that the plan alone does not say."""

    marked_columns: list[str] = field(default_factory=list)
    unmarked_columns: list[str] = field(default_factory=list)
    marked_tables: list[str] = field(default_factory=list)
    unmarked_tables: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SyncResult:
    """The outcome of :func:`sync_schema_text`.

    Attributes
    ----------
    plan:
        What was found.
    text:
        The synced ``schema.yaml`` text (equal to the input when nothing
        needs to change).
    changed:
        *text* differs from the input.
    stats:
        Marker changes made by the text edit.
    """

    plan: SyncPlan
    text: str
    changed: bool
    stats: RenderStats


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def draft_description(data_type: str) -> str:
    """The placeholder description of a column added from the database.

    Examples
    --------
    >>> draft_description("nvarchar(100)")
    'nvarchar(100) column'
    """
    return f"{data_type} column"


def _norm_type(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def type_family(data_type: str) -> str:
    """A coarse family for comparing two columns' types.

    Examples
    --------
    >>> type_family("int"), type_family("bigint"), type_family("nvarchar(40)")
    ('integer', 'integer', 'text')
    >>> type_family("uniqueidentifier")
    'uniqueidentifier'
    """
    base = data_type.split("(", 1)[0].strip().lower()
    return _TYPE_FAMILIES.get(base, base)


@dataclass(frozen=True)
class _Live:
    """One database column merged across the sources a table is in."""

    name: str
    data_type: str
    sources: tuple[str, ...]
    types: tuple[tuple[str, str], ...]


def _merge_columns(
    found: Sequence[tuple[str, TableInfo]],
) -> dict[str, _Live]:
    """Union of the columns of one table across the sources it is in.

    The name and type are the first source's (``datasources.yaml`` order,
    default first). Keys are lower-cased names, in first-seen order.
    """
    merged: dict[str, _Live] = {}
    for source, info in found:
        for column in info.columns:
            low = column.name.lower()
            seen = merged.get(low)
            if seen is None:
                merged[low] = _Live(
                    column.name, column.data_type, (source,), ((source, column.data_type),),
                )
            else:
                merged[low] = _Live(
                    seen.name, seen.data_type, (*seen.sources, source),
                    (*seen.types, (source, column.data_type)),
                )
    return merged


def _lower_catalogue(tables: SourceTables) -> dict[tuple[str, str], frozenset[str]]:
    return {
        location: frozenset(c.name.lower() for c in info.columns)
        for location, info in tables.items()
    }


def _display(info: TableInfo) -> str:
    return f"{info.schema}.{info.name}" if info.schema else info.name


def _table_key(schema: str, name: str, qualified: bool) -> str | None:
    """The key *schema.name* is written under, or ``None`` if it cannot be."""
    def ident(part: str) -> str:
        return part if _PLAIN_IDENT.match(part) else "[" + part.replace("]", "]]") + "]"

    key = f"{ident(schema)}.{ident(name)}" if qualified and schema else ident(name)
    try:
        ref = split_table_key(key)
    except ValueError:
        return None
    return key if ref.name == name else None


def _table_yaml_key(key: str) -> str:
    plain = _PLAIN_TABLE_KEY.match(key) and key.lower() not in {"y", "n", "yes", "no", "on", "off", "true", "false", "null"}
    return key if plain else quote(key)


def compute_plan(
    schema: SchemaConfig,
    sources: Sequence[str],
    default: str,
    catalogues: Mapping[str, SourceTables],
    options: SyncOptions = SyncOptions(),
) -> SyncPlan:
    """Compare ``schema.yaml`` with every source's catalogue.

    Parameters
    ----------
    schema:
        The validated ``schema.yaml``.
    sources:
        Source names in ``datasources.yaml`` order (default first).
    default:
        The default source.
    catalogues:
        ``{source: {(schema, table): TableInfo}}`` for every name in *sources*.
    options:
        What a sync may remove or add.

    Returns
    -------
    SyncPlan
        Nothing is written; see :func:`render_synced_text`.

    Examples
    --------
    >>> from database.catalogue import ColumnInfo
    >>> from schema_data.registry import validate_schema_yaml_text
    >>> schema = validate_schema_yaml_text(
    ...     "tables:\\n  T:\\n    db_schema: s\\n    columns: {ID: pk, Gone: g}\\n"
    ... )
    >>> info = TableInfo("s", "T", False, (ColumnInfo("ID", "int", False), ColumnInfo("New", "bit", True)))
    >>> plan = compute_plan(schema, ["a"], "a", {"a": {("s", "t"): info}})
    >>> [(a.name, a.data_type) for a in plan.tables[0].adds], plan.tables[0].stale
    ([('New', 'bit')], ('Gone',))
    """
    placements = locate_tables(
        schema, sources, default,
        {name: _lower_catalogue(catalogues[name]) for name in sources},
    )
    in_schema: set[tuple[str, str]] = set()
    table_plans: list[TablePlan] = []
    mentioned = {rel.from_table for rel in schema.relationships} | {
        rel.to_table for rel in schema.relationships
    }
    for placement in placements:
        table = schema.tables[placement.key]
        location = table_location(placement.key, table.db_schema, "dbo")
        in_schema.add(location)
        found = [(name, catalogues[name][location]) for name in placement.found_in]
        table_plans.append(_plan_table(table, placement, found, options, placement.key in mentioned))

    new_tables, unlisted, skipped = _plan_new_tables(
        schema, sources, catalogues, in_schema, options,
    )
    return SyncPlan(
        sources=tuple(sources), default=default, tables=tuple(table_plans),
        new_tables=tuple(new_tables), unlisted=tuple(unlisted), skipped_new=tuple(skipped),
    )


def _plan_table(
    table: TableDefinition,
    placement: Placement,
    found: Sequence[tuple[str, TableInfo]],
    options: SyncOptions,
    referenced: bool,
) -> TablePlan:
    key = placement.key
    if not found:
        reason = ""
        if options.prune and referenced:
            reason = "still named in relationships:"
        return TablePlan(
            key=key, placement=placement, nowhere=True,
            prune_table=options.prune and not referenced, kept_table=reason,
        )
    if table.columns is None:
        return TablePlan(key=key, placement=placement, described_only=True)
    live_columns = _merge_columns(found)
    if not live_columns:
        return TablePlan(key=key, placement=placement, unverifiable=True)

    schema_columns = list(table.columns)
    known = {c.lower() for c in schema_columns}
    live = frozenset(c.lower() for c in schema_columns if c.lower() in live_columns)
    stale = tuple(c for c in schema_columns if c.lower() not in live_columns)
    adds = tuple(
        ColumnAdd(item.name, item.data_type, item.sources)
        for low, item in live_columns.items() if low not in known
    )

    flagged = set(table.resolvable_columns) | set(table.prefetchable_columns)
    pruned: list[str] = []
    kept: list[tuple[str, str]] = []
    if options.prune:
        for column in stale:
            if column in flagged:
                kept.append((column, "still named in resolvable_columns or prefetchable_columns"))
            else:
                pruned.append(column)

    recorded: list[tuple[str, str]] = []
    changed: list[TypeChange] = []
    for column in schema_columns:
        item = live_columns.get(column.lower())
        if item is None:
            continue
        old = table.column_types.get(column)
        if old is None:
            recorded.append((column, item.data_type))
        elif _norm_type(old) != _norm_type(item.data_type):
            changed.append(TypeChange(column, old, item.data_type))

    shape: list[str] = []
    for item in live_columns.values():
        if len(item.sources) < len(found):
            shape.append(f"{item.name}: only in {', '.join(item.sources)}")
        elif len({_norm_type(t) for _, t in item.types}) > 1:
            shape.append(
                f"{item.name}: type differs ({', '.join(f'{s} {t}' for s, t in item.types)})"
            )
    return TablePlan(
        key=key, placement=placement, stale=stale, pruned=tuple(pruned), kept=tuple(kept),
        live=live, adds=adds, recorded=tuple(recorded), changed=tuple(changed),
        shape=tuple(shape),
    )


def _plan_new_tables(
    schema: SchemaConfig,
    sources: Sequence[str],
    catalogues: Mapping[str, SourceTables],
    in_schema: set[tuple[str, str]],
    options: SyncOptions,
) -> tuple[list[NewTable], list[tuple[str, tuple[str, ...]]], list[tuple[str, str]]]:
    """Database tables ``schema.yaml`` lacks: which to add, which to list."""
    seen: dict[tuple[str, str], list[tuple[str, TableInfo]]] = {}
    for source in sources:
        for location, info in catalogues[source].items():
            if location not in in_schema:
                seen.setdefault(location, []).append((source, info))

    bare_counts: dict[str, int] = {}
    for key in schema.tables:
        low = bare_table_name(key).lower()
        bare_counts[low] = bare_counts.get(low, 0) + 1

    chosen = [
        (location, found) for location, found in seen.items()
        if any(
            fnmatch.fnmatchcase(_display(found[0][1]).lower(), pattern.lower())
            for pattern in options.add_tables
        )
    ]
    for _, found in chosen:
        low = found[0][1].name.lower()
        bare_counts[low] = bare_counts.get(low, 0) + 1

    new_tables: list[NewTable] = []
    skipped: list[tuple[str, str]] = []
    taken = set(schema.tables)
    for location, found in chosen:
        info = found[0][1]
        merged = _merge_columns(found)
        if not merged:
            skipped.append((_display(info), "the login sees none of its columns"))
            continue
        qualified = bare_counts.get(info.name.lower(), 0) > 1
        key = _table_key(info.schema, info.name, qualified)
        if key is None or key in taken:
            skipped.append((_display(info), "its name cannot be written as a table key"))
            continue
        taken.add(key)
        new_tables.append(NewTable(
            key=key, schema=info.schema, name=info.name, is_view=info.is_view,
            sources=tuple(source for source, _ in found),
            columns=tuple(ColumnInfo(m.name, m.data_type, True) for m in merged.values()),
        ))
    chosen_locations = {location for location, _ in chosen}
    unlisted = [
        (_display(found[0][1]), tuple(source for source, _ in found))
        for location, found in seen.items() if location not in chosen_locations
    ]
    return new_tables, unlisted, skipped


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _new_table_definition(table: NewTable, multi_source: bool) -> TableDefinition:
    """The :class:`TableDefinition` a :class:`NewTable` is written as."""
    qualified = bool(split_table_key(table.key).qualifier)
    return TableDefinition(
        description=_new_table_description(table),
        columns={c.name: draft_description(c.data_type) for c in table.columns},
        column_types={c.name: c.data_type for c in table.columns},
        db_schema="" if qualified else table.schema,
        datasource=table.sources if multi_source else "",
    )


def _new_table_description(table: NewTable) -> str:
    base = f"{table.schema}.{table.name}" if table.schema else table.name
    return f"{base} (view)" if table.is_view else base


def _new_table_lines(
    table: NewTable, multi_source: bool, key_indent: int, unit: int, cr: str,
) -> list[str]:
    """The text of one appended table."""
    definition = _new_table_definition(table, multi_source)
    pad = " " * (key_indent + unit)
    deeper = " " * (key_indent + 2 * unit)
    lines = [
        f"{' ' * key_indent}{_table_yaml_key(table.key)}:",
        f"{pad}description: {quote(definition.description)}  {DRAFT_COMMENT}",
    ]
    if definition.db_schema:
        lines.append(f"{pad}db_schema: {quote(definition.db_schema)}")
    if multi_source:
        value = table.sources[0] if len(table.sources) == 1 else "[" + ", ".join(table.sources) + "]"
        lines.append(f"{pad}datasource: {value}")
    lines.append(f"{pad}columns:")
    for column in table.columns:
        lines.append(
            f"{deeper}{yaml_key(column.name)}: {quote(draft_description(column.data_type))}  {DRAFT_COMMENT}"
        )
    lines.append(f"{pad}column_types:")
    for column in table.columns:
        lines.append(f"{deeper}{yaml_key(column.name)}: {quote(column.data_type)}")
    return [line + cr for line in lines]


def render_synced_text(
    original: str, schema: SchemaConfig, plan: SyncPlan,
) -> tuple[str, RenderStats]:
    """*original* ``schema.yaml`` text with the structural edits of *plan* applied.

    Every original line is kept except the ones a rule replaces or removes
    (a ``datasource:`` line, a recorded type, a marker, a pruned column or
    table). Line endings, a missing final newline, a byte-order mark's
    absence and the file's own indentation are preserved.

    Parameters
    ----------
    original:
        The ``schema.yaml`` text, byte-order mark removed.
    schema:
        *original*, validated.
    plan:
        :func:`compute_plan`'s result for it.

    Returns
    -------
    tuple[str, RenderStats]
        The new text and the marker changes made.

    Raises
    ------
    LayoutError
        If a table or its ``columns:`` cannot be edited line by line
        (flow-style entries), or tables must be appended to a file with no
        block-style ``tables:`` mapping.

    Examples
    --------
    >>> from database.catalogue import ColumnInfo
    >>> from schema_data.registry import validate_schema_yaml_text
    >>> text = "tables:\\n  T:\\n    columns:\\n      ID: pk\\n"
    >>> schema = validate_schema_yaml_text(text)
    >>> info = TableInfo("", "T", False, (ColumnInfo("ID", "int", False), ColumnInfo("Note", "text", True)))
    >>> plan = compute_plan(schema, ["a"], "a", {"a": {("dbo", "t"): info}})
    >>> print(render_synced_text(text, schema, plan)[0], end="")
    tables:
      T:
        columns:
          ID: pk
          Note: "text column"  # TO BE FILLED (added by sync_schema.py)
        column_types:
          ID: "int"
          Note: "text"
    """
    lines = original.split("\n")
    cr = "\r" if "\r\n" in original else ""
    stats = RenderStats()
    layout = parse_tables(lines, list(schema.tables))
    edits: list[Edit] = []
    unit = 2
    if layout is not None and layout.tables:
        first = next(iter(layout.tables.values()))
        unit = (first.child_indent - layout.key_indent) or 2
    for table_plan in plan.tables:
        if layout is None:  # pragma: no cover - parse_tables raises first
            raise LayoutError("no block-style `tables:` mapping found in schema.yaml")
        block = layout.tables[table_plan.key]
        if table_plan.prune_table:
            # Its own lines, plus the blank lines after it when another
            # table follows; the comments around it stay.
            end = block.content_end
            if block.end != layout.stop:
                while end < block.end and not lines[end].strip():
                    end += 1
            edits.append((block.line, end, []))
            continue
        edits.extend(_datasource_and_marker_edits(lines, block, table_plan, plan, cr, stats))
        if schema.tables[table_plan.key].columns is not None and not (
            table_plan.nowhere or table_plan.unverifiable or table_plan.described_only
        ):
            edits.extend(_column_edits(lines, block, schema.tables[table_plan.key], table_plan, unit, cr, stats))
    if plan.new_tables:
        if layout is None:
            raise LayoutError("no block-style `tables:` mapping to append the new tables to")
        indent = layout.key_indent if layout.tables else 2
        appended: list[str] = []
        for table in plan.new_tables:
            appended.append(cr)
            appended.extend(_new_table_lines(table, plan.multi_source, indent, unit, cr))
        edits.append((layout.content_end, layout.content_end, appended))
    return "\n".join(apply_edits(lines, edits)), stats


def _datasource_and_marker_edits(
    lines: Sequence[str], block: TableBlock, table_plan: TablePlan, plan: SyncPlan, cr: str, stats: RenderStats,
) -> list[Edit]:
    placement = table_plan.placement
    if not plan.multi_source:
        # One source: no datasource: line to write, only the not-found marker.
        placement = Placement(
            placement.key, placement.found_in, placement.found_in, placement.effective, {},
        )
    first = block.line + 1
    had_marker = first < block.end and lines[first].strip() == NOT_FOUND_COMMENT
    edits = datasource_edits(lines, block, placement, cr)
    if edits and not had_marker and not placement.found_in:
        stats.marked_tables.append(table_plan.key)
    if had_marker and placement.found_in:
        stats.unmarked_tables.append(table_plan.key)
    return edits


def _column_edits(
    lines: Sequence[str], block: TableBlock, table: TableDefinition, table_plan: TablePlan,
    unit: int, cr: str, stats: RenderStats,
) -> list[Edit]:
    """Line edits for one table's columns and ``column_types:``."""
    span = child_span(lines, block, "columns")
    if span is None:
        raise LayoutError(f"table {table_plan.key!r}: cannot find its `columns:` block")
    first, stop = span
    entries = parse_entries(lines, first, stop)
    by_name = {entry.name: entry for entry in entries}
    if set(by_name) != set(table.columns or {}):
        raise LayoutError(
            f"table {table_plan.key!r}: cannot read its columns line by line "
            "(check for duplicate or oddly quoted column names)"
        )
    indent = entries[0].indent if entries else block.child_indent + unit
    pad = " " * indent
    edits: list[Edit] = []

    stale = set(table_plan.stale)
    pruned = set(table_plan.pruned)
    for entry in entries:
        marker_at = entry.line - 1
        has_marker = marker_at > first and lines[marker_at].strip().startswith(_STALE_PREFIX)
        label = f"{table_plan.key}.{entry.name}"
        if entry.name in pruned:
            edits.append((marker_at if has_marker else entry.line, entry.stop, []))
        elif entry.name in stale:
            if not has_marker:
                edits.append((entry.line, entry.line, [f"{pad}{STALE_COLUMN_COMMENT}{cr}"]))
                stats.marked_columns.append(label)
        elif has_marker:
            edits.append((marker_at, entry.line, []))
            stats.unmarked_columns.append(label)

    appended = [
        f"{pad}{yaml_key(add.name)}: {quote(draft_description(add.data_type))}  {DRAFT_COMMENT}{cr}"
        for add in table_plan.adds
    ]
    if appended:
        edits.append((stop, stop, appended))

    types_span = child_span(lines, block, "column_types")
    new_types = [*table_plan.recorded, *((a.name, a.data_type) for a in table_plan.adds)]
    if types_span is None:
        if new_types:
            body = [f"{' ' * block.child_indent}column_types:{cr}"]
            body += [f"{pad}{yaml_key(n)}: {quote(t)}{cr}" for n, t in new_types]
            edits.append((stop, stop, body))
        return edits
    types_first, types_stop = types_span
    type_entries = {e.name: e for e in parse_entries(lines, types_first, types_stop)}
    type_indent = next(iter(type_entries.values())).indent if type_entries else indent
    for change in table_plan.changed:
        entry = type_entries[change.column]
        raw = lines[entry.line].rstrip("\r")
        head = raw[: len(raw) - len(entry.rest)]
        comment = trailing_comment(entry.rest)
        tail = ""
        if comment:
            before = entry.rest[: entry.rest.rindex(comment)]
            tail = before[len(before.rstrip()):] + comment  # keep the original gap
        edits.append((entry.line, entry.stop, [f"{head} {quote(change.new)}{tail}{cr}"]))
    for name in table_plan.pruned:
        entry = type_entries.get(name)
        if entry is not None:
            edits.append((entry.line, entry.stop, []))
    if new_types:
        edits.append((
            types_stop, types_stop,
            [f"{' ' * type_indent}{yaml_key(n)}: {quote(t)}{cr}" for n, t in new_types],
        ))
    return edits


# ---------------------------------------------------------------------------
# Proving the result changed only what it should
# ---------------------------------------------------------------------------

def expected_tables(
    schema: SchemaConfig, plan: SyncPlan,
) -> dict[str, dict[str, object]]:
    """``schema`` with *plan* applied to the parsed model, without any text.

    This is the independent statement of what the rendered text must parse
    to: the original model, with each table's datasource set, columns added
    and removed, types recorded, corrected and removed, tables removed, and
    the new tables appended. :func:`verify_structural_only` compares it with
    the parsed output.

    Parameters
    ----------
    schema:
        The validated original.
    plan:
        :func:`compute_plan`'s result.

    Returns
    -------
    dict[str, dict[str, object]]
        ``{table key: TableDefinition.model_dump()}`` in the expected order.
    """
    out: dict[str, dict[str, object]] = {}
    for table_plan in plan.tables:
        if table_plan.prune_table:
            continue
        data = schema.tables[table_plan.key].model_dump()
        if plan.multi_source and table_plan.placement.found_in:
            data["datasource"] = table_plan.placement.wanted
        if data["columns"] is not None and not (
            table_plan.nowhere or table_plan.unverifiable or table_plan.described_only
        ):
            pruned = set(table_plan.pruned)
            columns = {c: d for c, d in data["columns"].items() if c not in pruned}
            types = {c: t for c, t in data["column_types"].items() if c not in pruned}
            for name, data_type in table_plan.recorded:
                types[name] = data_type
            for change in table_plan.changed:
                types[change.column] = change.new
            for add in table_plan.adds:
                columns[add.name] = draft_description(add.data_type)
                types[add.name] = add.data_type
            data["columns"], data["column_types"] = columns, types
        out[table_plan.key] = data
    for table in plan.new_tables:
        out[table.key] = _new_table_definition(table, plan.multi_source).model_dump()
    return out


def verify_structural_only(
    original: SchemaConfig, rendered: SchemaConfig, plan: SyncPlan,
) -> None:
    """Raise unless *rendered* is *original* plus exactly the planned changes.

    Parameters
    ----------
    original, rendered:
        The validated input and output.
    plan:
        What was meant to change.

    Raises
    ------
    ValueError
        Naming the table (and column) whose content differs from the
        expected one: a changed description, flag, note or ordering, an
        unplanned table or column, or a changed ``relationships:`` list.
    """
    expected = expected_tables(original, plan)
    if list(rendered.tables) != list(expected):
        raise ValueError("the synced file lists different tables than the plan says")
    for key, data in rendered.tables.items():
        want = expected[key]
        got = data.model_dump()
        for name in want:
            if got[name] != want[name]:
                raise ValueError(f"the synced file changed {name!r} of table {key!r} unexpectedly")
        if (got["columns"] is not None) and list(got["columns"]) != list(want["columns"]):
            raise ValueError(f"the synced file reordered the columns of table {key!r}")
    if rendered.relationships != original.relationships:
        raise ValueError("the synced file changed schema.yaml's relationships")


def sync_schema_text(
    original: str,
    sources: Sequence[str],
    default: str,
    catalogues: Mapping[str, SourceTables],
    options: SyncOptions = SyncOptions(),
) -> SyncResult:
    """Plan, render and verify a structural sync of one ``schema.yaml`` text.

    Parameters
    ----------
    original:
        The ``schema.yaml`` text, byte-order mark removed.
    sources, default:
        The configured data sources (default first) and the default one.
    catalogues:
        ``{source: {(schema, table): TableInfo}}``.
    options:
        What a sync may remove or add.

    Returns
    -------
    SyncResult
        With ``changed`` false when the file is already in sync.

    Raises
    ------
    ValueError
        If *original* is not a valid ``schema.yaml``, the output does not
        validate, or the output differs from the original in anything but
        the planned structural changes. :class:`LayoutError` (a
        ``ValueError``) when the file's layout cannot be edited.

    Examples
    --------
    >>> from database.catalogue import ColumnInfo
    >>> text = "tables:\\n  T:\\n    columns:\\n      ID: pk\\n    column_types:\\n      ID: \\"int\\"\\n"
    >>> info = TableInfo("", "T", False, (ColumnInfo("ID", "int", False),))
    >>> sync_schema_text(text, ["a"], "a", {"a": {("dbo", "t"): info}}).changed
    False
    """
    schema = validate_schema_yaml_text(original)
    plan = compute_plan(schema, sources, default, catalogues, options)
    text, stats = render_synced_text(original, schema, plan)
    verify_structural_only(schema, validate_schema_yaml_text(text), plan)
    return SyncResult(plan=plan, text=text, changed=text != original, stats=stats)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(result: SyncResult) -> str:
    """The human-readable report: names, types and counts only.

    Parameters
    ----------
    result:
        :func:`sync_schema_text`'s result.

    Returns
    -------
    str
        Multi-line text without a trailing newline. No section lists more
        than :data:`REPORT_LIST_LIMIT` names.

    Examples
    --------
    >>> plan = SyncPlan(("a",), "a", ())
    >>> print(build_report(SyncResult(plan, "", False, RenderStats())).splitlines()[0])
    == datasource: lines set or repaired: 0 ==
    """
    plan, stats = result.plan, result.stats
    out: list[str] = []

    def section(title: str, rows: Sequence[str]) -> None:
        if out:
            out.append("")
        out.append(f"== {title}: {len(rows)} ==")
        out.extend(f"  {row}" for row in rows[:REPORT_LIST_LIMIT])
        if len(rows) > REPORT_LIST_LIMIT:
            out.append(f"  ... and {len(rows) - REPORT_LIST_LIMIT} more")

    section(
        "datasource: lines set or repaired",
        [
            f"{t.key}: {', '.join(t.placement.current) or '(none)'} -> {', '.join(t.placement.wanted)}"
            for t in plan.tables
            if plan.multi_source and t.placement.found_in
            and t.placement.wanted != t.placement.current
        ],
    )
    section(
        "columns added (in the database, not in schema.yaml)",
        [f"{t.key}.{a.name} {a.data_type}" for t in plan.tables for a in t.adds],
    )
    section(
        "columns in schema.yaml that the database does not have",
        [
            f"{t.key}.{c} ({_stale_outcome(t, c)})"
            for t in plan.tables for c in t.stale
        ],
    )
    section(
        "column types recorded",
        [f"{t.key}: {len(t.recorded)} column(s)" for t in plan.tables if t.recorded],
    )
    section(
        "column types corrected",
        [f"{t.key}.{c.column}: {c.old} -> {c.new}" for t in plan.tables for c in t.changed],
    )
    section(
        "tables in the database that were not in schema.yaml",
        [f"{name} [{', '.join(srcs)}]" for name, srcs in plan.unlisted]
        + [f"{n.key}: added ({_added_from(n)})" for n in plan.new_tables]
        + [f"{name}: not added ({why})" for name, why in plan.skipped_new],
    )
    section(
        "tables in schema.yaml found in no data source",
        [
            f"{t.key} ({'removed' if t.prune_table else 'kept: ' + t.kept_table if t.kept_table else 'kept, marked'})"
            for t in plan.tables if t.nowhere
        ],
    )
    section(
        "columns that differ between data sources",
        [f"{t.key}.{line}" for t in plan.tables for line in t.shape],
    )
    skipped = [t.key for t in plan.tables if t.described_only]
    unverifiable = [t.key for t in plan.tables if t.unverifiable]
    section("tables with no columns: key (described only, not synced)", skipped)
    section("tables whose columns the login cannot see (not synced)", unverifiable)
    out.append("")
    out.append(
        "markers: "
        f"{len(stats.marked_columns)} column(s) and {len(stats.marked_tables)} table(s) newly marked, "
        f"{len(stats.unmarked_columns)} column(s) and {len(stats.unmarked_tables)} table(s) unmarked"
    )
    return "\n".join(out)


def _stale_outcome(table_plan: TablePlan, column: str) -> str:
    if column in table_plan.pruned:
        return "removed"
    for name, why in table_plan.kept:
        if name == column:
            return f"kept: {why}"
    return "kept, marked"


def _added_from(table: NewTable) -> str:
    return f"{table.schema}.{table.name}, {len(table.columns)} column(s)"
