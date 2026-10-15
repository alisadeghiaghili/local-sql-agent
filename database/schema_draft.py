# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""First-install drafts of ``project_config/``, from the application's own data sources.

This is the engine behind :mod:`database.schema_inspector_cli`. It differs from
the older single-database :class:`database.schema_inspector.SchemaInspector`
in three ways:

* **It reads what the application can query.** Each data source is read through
  the engine the server uses for it (:func:`database.connection.get_engine`),
  so a draft lists the tables the application's login sees and no more. A
  wide Windows login used to add about 150 change-capture, temporary and
  staging tables the application cannot even query; they ended up in the live
  ``schema.yaml``.
* **``schema.yaml`` is built by the sync engine.** The catalogues are planned
  against an empty file with :func:`schema_data.sync.draft_schema_text`, so a
  draft and a later ``scripts/sync_schema.py --add-tables`` produce the same
  keys (bare unless a name is shared across schemas, applied across every
  selected source), ``db_schema``, ``datasource:`` lines (omitted with a single
  source; a table in several sources written once with a list) and column
  layout. Nothing in this module renders a table.
* **No real data in ``schema.yaml``.** Sample values are read only on request
  and are written to ``sample_values.review.yaml``, a file nothing loads.

Which tables are drafted
------------------------
Per source, every catalogue table is kept unless a rule skips it, tried in this
order, and counted under the first that applies (:class:`Skipped`):

``system``
    :data:`database.schema_filter.SYSTEM_SCHEMAS`.
``configured``
    ``EXCLUDE_SCHEMAS`` and the source's ``exclude_schemas:``.
``cli``
    ``--exclude-schemas`` of this run.
``chosen``
    Only the setup wizard uses it: schemas an operator left out when it asked.
``not-included``
    The schema is not in ``--include-schemas``.
``exclude-tables``
    The table is named by ``--exclude-tables``.
``staging``
    ``STAGING_PATTERNS`` matches (unless ``--include-staging``).

Relationships
-------------
Declared foreign keys and relationships inferred from column names
(``Order.CustomerID`` -> ``Customer.ID``) both come from
:func:`schema_data.relationship_proposals.propose_relationships`, so a warehouse
that declares no key still gets a draft. A declared key is written as is; an
inferred one carries the comment :data:`INFERRED_COMMENT` and must be reviewed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from core import config_doctor as doctor
from database.catalogue import TableInfo as CatalogueTable
from database.catalogue import read_catalogue
from database.schema_filter import (
    RULE_CLI,
    RULE_CONFIGURED,
    RULE_SYSTEM,
    SchemaExclusion,
)
from database.schema_inspector import (
    ColumnInfo,
    ForeignKeyInfo,
    SchemaInspector,
    SchemaSnapshot,
    TableInfo,
    _classify_table,
    _fetch_row_count,
    _fetch_samples,
    _is_string_type,
    _normalise_type,
    _yaml_str,
)
from schema_data.registry import (
    bare_table_name,
    effective_qualifier,
    validate_schema_yaml_text,
)
from schema_data.relationship_proposals import Proposal, ProposalSet, propose_relationships
from schema_data.sync import SyncResult, draft_schema_text

__all__ = [
    "DRAFT_HEADER_LINE",
    "INFERRED_COMMENT",
    "RULE_CHOSEN",
    "RULE_EXCLUDE_TABLES",
    "RULE_NOT_INCLUDED",
    "RULE_STAGING",
    "SAMPLE_REVIEW_FILENAME",
    "DraftSet",
    "SelectedSource",
    "Skipped",
    "build_drafts",
    "describe_skipped",
    "listed_datasources",
    "read_selected",
    "select_tables",
]

#: Rules beyond the schema exclusion of :mod:`database.schema_filter`.
#: ``chosen`` is the setup wizard's own: schemas an operator left out when asked.
RULE_CHOSEN = "chosen"
RULE_NOT_INCLUDED = "not-included"
RULE_EXCLUDE_TABLES = "exclude-tables"
RULE_STAGING = "staging"

#: The first line of every draft file; ``core.config_doctor.DRAFT_HEADER`` is
#: the part the preflight looks for.
DRAFT_HEADER_LINE = f"# {doctor.DRAFT_HEADER} - review and edit before moving to project_config/"

#: The comment above a relationship that was guessed from column names.
INFERRED_COMMENT = "# INFERRED from column names (not a declared foreign key) -- REVIEW before use"

#: Name of the file the real sample values go to (``--sample-values``).
SAMPLE_REVIEW_FILENAME = "sample_values.review.yaml"

_RULE_ORDER = (
    RULE_SYSTEM, RULE_CONFIGURED, RULE_CLI, RULE_CHOSEN, RULE_NOT_INCLUDED, RULE_EXCLUDE_TABLES,
    RULE_STAGING,
)

_RULE_LABELS = {
    RULE_SYSTEM: "system schemas",
    RULE_CONFIGURED: "configured exclusion (EXCLUDE_SCHEMAS / exclude_schemas:)",
    RULE_CLI: "--exclude-schemas",
    RULE_CHOSEN: "left out interactively",
    RULE_NOT_INCLUDED: "outside --include-schemas",
    RULE_EXCLUDE_TABLES: "--exclude-tables",
    RULE_STAGING: "staging (STAGING_PATTERNS; --include-staging keeps them)",
}


def listed_datasources(sources: Sequence[str], found_in: Sequence[str]) -> tuple[str, ...]:
    """The ``datasource:`` names a table is written with.

    The rule of ``schema.yaml``'s sync and of every draft: a deployment with a
    single data source has no ``datasource:`` lines, one with several lists, for
    each table, the sources it was found in. *sources* is every configured
    source, not only those a run read, so a run narrowed with ``--datasource``
    still writes the lines.

    Parameters
    ----------
    sources:
        Every configured source.
    found_in:
        The sources the table was found in (and kept).

    Returns
    -------
    tuple[str, ...]
        *found_in* with several configured sources, else ``()``.

    Examples
    --------
    >>> listed_datasources(["sales", "reports"], ["reports"])
    ('reports',)
    >>> listed_datasources(["sales"], ["sales"])
    ()
    """
    return tuple(found_in) if len(sources) > 1 else ()


@dataclass
class Skipped:
    """What one source's selection left out, by rule.

    Attributes
    ----------
    tables:
        ``{rule: number of tables}``.
    schemas:
        ``{rule: schema names}`` of the skipped tables, in lower case.

    Examples
    --------
    >>> skipped = Skipped()
    >>> skipped.add("system", "cdc"); skipped.add("system", "CDC"); skipped.add("staging", "Sales_Stg")
    >>> skipped.tables["system"], len(skipped.schemas["system"]), skipped.total
    (2, 1, 3)
    >>> skipped.add("cli", "Replay", 4); skipped.tables["cli"]
    4
    """

    tables: dict[str, int] = field(default_factory=dict)
    schemas: dict[str, set[str]] = field(default_factory=dict)

    def add(self, rule: str, schema: str, tables: int = 1) -> None:
        """Count *tables* tables (one by default) in *schema* as skipped by *rule*."""
        self.tables[rule] = self.tables.get(rule, 0) + tables
        self.schemas.setdefault(rule, set()).add(schema.lower())

    @property
    def total(self) -> int:
        """Tables skipped by any rule."""
        return sum(self.tables.values())


@dataclass(frozen=True)
class SelectedSource:
    """One data source after the selection.

    Attributes
    ----------
    name:
        The source name.
    label:
        How the header of a draft names it (the name, and the redacted URL).
    engine:
        The engine it was read through (row counts and sample values use it).
    read:
        Tables and views the catalogue had.
    tables:
        ``{(schema, table): info}`` (lower-cased key) that were kept.
    skipped:
        What the selection left out.
    """

    name: str
    label: str
    engine: Engine
    read: int
    tables: Mapping[tuple[str, str], CatalogueTable]
    skipped: Skipped


def select_tables(
    catalogue: Mapping[tuple[str, str], CatalogueTable],
    *,
    exclusion: SchemaExclusion,
    staging: Sequence[doctor.StagingPattern] = (),
    include_schemas: Sequence[str] | None = None,
    exclude_tables: Sequence[str] | None = None,
) -> tuple[dict[tuple[str, str], CatalogueTable], Skipped]:
    """Keep the tables of *catalogue* no rule skips, and count the ones that were.

    Parameters
    ----------
    catalogue:
        :func:`database.catalogue.read_catalogue`'s result.
    exclusion:
        The schema exclusion of the source (:func:`database.schema_filter.schema_exclusion`).
    staging:
        Staging patterns; empty keeps staging tables.
    include_schemas:
        Names (case-insensitive) of the only schemas to keep; ``None`` for all.
    exclude_tables:
        Table names (case-insensitive) to skip.

    Returns
    -------
    tuple[dict, Skipped]
        The kept tables in catalogue order, and the counts.

    Examples
    --------
    >>> from database.catalogue import ColumnInfo as Col
    >>> def info(schema, name):
    ...     return CatalogueTable(schema, name, False, (Col("ID", "int", False),))
    >>> catalogue = {(s.lower(), n.lower()): info(s, n) for s, n in
    ...              [("sales", "Order"), ("sales_Stg", "Order"), ("cdc", "T"), ("sales", "Order_Inserts")]}
    >>> kept, skipped = select_tables(
    ...     catalogue, exclusion=SchemaExclusion(),
    ...     staging=doctor.parse_staging_patterns("*_Stg, table:*_Inserts"),
    ... )
    >>> list(kept), sorted(skipped.tables.items())
    ([('sales', 'order')], [('staging', 2), ('system', 1)])
    """
    wanted = {s.lower() for s in include_schemas} if include_schemas else None
    dropped = {t.lower() for t in exclude_tables or ()}
    kept: dict[tuple[str, str], CatalogueTable] = {}
    skipped = Skipped()
    for location, info in catalogue.items():
        rule = exclusion.reason(info.schema)
        if rule is None and wanted is not None and info.schema.lower() not in wanted:
            rule = RULE_NOT_INCLUDED
        if rule is None and info.name.lower() in dropped:
            rule = RULE_EXCLUDE_TABLES
        if rule is None and doctor.is_staging(info.schema, info.name, staging):
            rule = RULE_STAGING
        if rule is None:
            kept[location] = info
        else:
            skipped.add(rule, info.schema)
    return kept, skipped


def describe_skipped(per_source: Mapping[str, Skipped]) -> list[str]:
    """The end-of-run lines saying how many tables and schemas each rule skipped.

    Parameters
    ----------
    per_source:
        ``{source: Skipped}``.

    Returns
    -------
    list[str]
        One line per rule that skipped something, summed over the sources
        (with several sources, also one line per source and rule). Empty when
        nothing was skipped.

    Examples
    --------
    >>> a, b = Skipped(), Skipped()
    >>> a.add("system", "cdc"); a.add("staging", "x_Stg"); b.add("staging", "y_Stg")
    >>> for line in describe_skipped({"a": a, "b": b}):
    ...     print(line)
      system schemas: 1 table(s) in 1 schema(s)
        a: 1 table(s) in 1 schema(s)
      staging (STAGING_PATTERNS; --include-staging keeps them): 2 table(s) in 2 schema(s)
        a: 1 table(s) in 1 schema(s)
        b: 1 table(s) in 1 schema(s)
    """
    lines: list[str] = []
    for rule in _RULE_ORDER:
        tables = sum(s.tables.get(rule, 0) for s in per_source.values())
        if not tables:
            continue
        schemas = {(name, schema) for name, s in per_source.items() for schema in s.schemas.get(rule, ())}
        distinct = len({schema for _, schema in schemas})
        lines.append(f"  {_RULE_LABELS[rule]}: {tables} table(s) in {distinct} schema(s)")
        if len(per_source) > 1:
            for name, skipped in per_source.items():
                if skipped.tables.get(rule):
                    lines.append(
                        f"    {name}: {skipped.tables[rule]} table(s) in {len(skipped.schemas[rule])} schema(s)"
                    )
    return lines


@dataclass(frozen=True)
class DraftSet:
    """The draft files and what they were built from.

    Attributes
    ----------
    schema_yaml, entities_yaml, aliases_yaml, relationships_yaml:
        The text of each draft.
    sample_values_yaml:
        The text of ``sample_values.review.yaml``; ``None`` unless values were
        requested.
    snapshot:
        The tables drafted (one per distinct ``schema.table``).
    sync:
        The sync engine's result: the tables written and those it could not.
    proposals:
        The relationships written to ``relationships.yaml``.
    """

    schema_yaml: str
    entities_yaml: str
    aliases_yaml: str
    relationships_yaml: str
    sample_values_yaml: str | None
    snapshot: SchemaSnapshot
    sync: SyncResult
    proposals: ProposalSet


def _legacy_table(
    info: CatalogueTable,
    sources: tuple[str, ...],
    engine: Engine,
    row_counts: bool,
    names: Mapping[tuple[str, str], str],
) -> TableInfo:
    """The inspector's own table record for the entities draft and the summary."""
    primary = {c.lower() for c in info.primary_key}
    table = TableInfo(name=info.name, schema=info.schema or None, datasources=sources)
    table.columns = [
        ColumnInfo(
            name=c.name, type=_normalise_type(c.data_type),
            is_pk=c.name.lower() in primary, is_nullable=c.nullable,
        )
        for c in info.columns
    ]
    for fk in info.foreign_keys:
        ref_schema, ref_name = (names.get(fk.ref_location, p) for p in fk.ref_location)
        for column, ref_column in zip(fk.columns, fk.ref_columns):
            table.foreign_keys.append(ForeignKeyInfo(column, ref_schema or None, ref_name, ref_column))
    if row_counts:
        table.row_count = _fetch_row_count(engine, info.schema or None, info.name)
    table.classification = _classify_table(table)
    return table


def _relationships_block(
    proposals: Sequence[Proposal], key_of: Callable[[Proposal, bool], str | None],
) -> list[str]:
    """The ``relationships:`` lines of ``schema.yaml``; inferred entries carry :data:`INFERRED_COMMENT`."""
    entries: list[str] = []
    for proposal in proposals:
        start, end = key_of(proposal, True), key_of(proposal, False)
        if start is None or end is None:
            continue
        if proposal.confidence != "declared":
            entries.append(f"  {INFERRED_COMMENT}")
        entries.append(f"  - from_table: {_yaml_str(start)}")
        entries.append(f"    to_table: {_yaml_str(end)}")
        entries.append(f"    join_sql: {_yaml_str(proposal.join_hint)}")
        entries.append("")
    return ["relationships:", *entries] if entries else ["relationships: []"]


def _relationships_yaml(proposals: Sequence[Proposal], header: Sequence[str]) -> str:
    """The text of the ``relationships.yaml`` draft (the shape ``database.relationship_map`` reads)."""
    lines = [*header, f"# Relationships found: {len(proposals)}", ""]
    if not proposals:
        return "\n".join([*lines, "relationships: []"])
    lines.append("relationships:")
    for p in proposals:
        if p.confidence != "declared":
            lines.append(f"  {INFERRED_COMMENT}")
        lines.append(f"  - from_table: {_yaml_str(p.from_table)}")
        if p.from_schema:
            lines.append(f"    from_schema: {_yaml_str(p.from_schema)}")
        lines.append(f"    from_column: {_yaml_str(p.from_column)}")
        lines.append(f"    to_table: {_yaml_str(p.to_table)}")
        if p.to_schema:
            lines.append(f"    to_schema: {_yaml_str(p.to_schema)}")
        lines.append(f"    to_column: {_yaml_str(p.to_column)}")
        lines.append(f"    join_hint: {_yaml_str(p.join_hint)}")
        lines.append("")
    return "\n".join(lines)


def _sample_review_yaml(samples: Mapping[str, Mapping[str, list[str]]], header: Sequence[str]) -> str:
    lines = [
        header[0],
        "# sample_values.review.yaml -- REAL VALUES read from the live rows (distinct, non-NULL, truncated).",
        "# Nothing loads this file. It exists so a person can write real column descriptions;",
        "# do not copy values into schema.yaml (they would be sent to the model), do not commit",
        "# or share this file. Delete it when the review is done.",
        *header[1:],
        "",
        "sample_values:" if samples else "sample_values: {}",
    ]
    for key, columns in samples.items():
        lines.append(f"  {_yaml_str(key)}:")
        for column, values in columns.items():
            lines.append(f"    {_yaml_str(column)}: [{', '.join(_yaml_str(v) for v in values)}]")
    return "\n".join(lines) + "\n"


def build_drafts(
    selected: Sequence[SelectedSource],
    *,
    sources: Sequence[str],
    default: str,
    row_counts: bool = True,
    sample_rows: int = 0,
) -> DraftSet:
    """Build the four drafts (and the sample-value review file) from the selected tables.

    Parameters
    ----------
    selected:
        The sources that were read, after :func:`select_tables`.
    sources:
        Every configured source, default first. A source that was not read
        (``--datasource`` narrowed the run) still counts, so the ``datasource:``
        lines are written whenever the deployment has several sources.
    default:
        The default source.
    row_counts:
        Issue ``COUNT(*)`` per table (the number goes to ``entities.yaml`` only).
    sample_rows:
        Distinct values to read per string column for the review file; ``0``
        reads none and no file is built.

    Returns
    -------
    DraftSet
        The draft texts.

    Raises
    ------
    ValueError
        If the sync engine's output does not validate (a defect, not input).
    """
    catalogues: dict[str, Mapping[tuple[str, str], CatalogueTable]] = {name: {} for name in sources}
    catalogues.update({s.name: s.tables for s in selected})
    generated = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    label = ", ".join(s.label for s in selected)
    header = [DRAFT_HEADER_LINE, f"# Generated: {generated} from {label}"]

    # One record per distinct schema.table; its first source reads row counts and values.
    first: dict[tuple[str, str], tuple[SelectedSource, CatalogueTable]] = {}
    where: dict[tuple[str, str], list[str]] = {}
    for source in selected:
        for location, info in source.tables.items():
            first.setdefault(location, (source, info))
            where.setdefault(location, []).append(source.name)
    names = {loc: info.name for loc, (_, info) in first.items()}

    tables = [
        _legacy_table(info, listed_datasources(sources, where[loc]), source.engine, row_counts, names)
        for loc, (source, info) in first.items()
    ]
    snapshot = SchemaSnapshot(tables=tables, source_url=label, generated_at=generated)

    sync = draft_schema_text(sources, default, catalogues)
    base = validate_schema_yaml_text(sync.text)
    key_at = {
        (bare_table_name(key).lower(), ".".join(effective_qualifier(key, table.db_schema)).lower()): key
        for key, table in base.tables.items()
    }
    proposals = propose_relationships(base, sources, catalogues)

    def key_of(proposal: Proposal, start: bool) -> str | None:
        name, schema = (
            (proposal.from_table, proposal.from_schema) if start else (proposal.to_table, proposal.to_schema)
        )
        return key_at.get((name.lower(), schema.lower()))

    in_schema = {
        loc: key_at[(info.name.lower(), info.schema.lower())]
        for loc, (_, info) in first.items()
        if (info.name.lower(), info.schema.lower()) in key_at
    }
    schema_header = [
        *header,
        f"# Tables found: {len(snapshot.tables)} | Fact tables: {len(snapshot.fact_tables)} "
        f"| Dim tables: {len(snapshot.dim_tables)}",
        "# Built with the rules of scripts/sync_schema.py --add-tables: the keys, db_schema and",
        "# datasource lines are what a later sync writes. No value read from a row is in this file.",
        "# Relationships marked INFERRED are guesses from column names: review each one.",
        "",
    ]
    body = _relationships_block(proposals.proposals, key_of)
    schema_yaml = "\n".join([*schema_header, sync.text.rstrip("\n"), "", *body]) + "\n"
    validate_schema_yaml_text(schema_yaml)

    samples: dict[str, dict[str, list[str]]] = {}
    if sample_rows > 0:
        for loc, (source, info) in first.items():
            key = in_schema.get(loc)
            if key is None:
                continue
            columns: dict[str, list[str]] = {}
            for column in info.columns:
                if not _is_string_type(_normalise_type(column.data_type)):
                    continue
                values = _fetch_samples(source.engine, info.schema or None, info.name, column.name, sample_rows)
                if values:
                    columns[column.name] = values
            if columns:
                samples[key] = columns

    inspector = SchemaInspector("")  # only its draft_* renderers are used; it never connects
    return DraftSet(
        schema_yaml=schema_yaml,
        entities_yaml=inspector.draft_entities_yaml(snapshot, keys={loc: in_schema.get(loc, info.name) for loc, (_, info) in first.items()}),
        aliases_yaml=inspector.draft_aliases_yaml(snapshot),
        relationships_yaml=_relationships_yaml(proposals.proposals, header),
        sample_values_yaml=_sample_review_yaml(samples, header) if sample_rows > 0 else None,
        snapshot=snapshot,
        sync=sync,
        proposals=proposals,
    )


def read_selected(
    name: str,
    label: str,
    engine: Engine,
    *,
    exclusion: SchemaExclusion,
    staging: Sequence[doctor.StagingPattern],
    include_schemas: Sequence[str] | None,
    exclude_tables: Sequence[str] | None,
) -> SelectedSource:
    """Read *engine*'s catalogue and select the tables to draft.

    Raises
    ------
    sqlalchemy.exc.SQLAlchemyError
        If the catalogue cannot be read.
    """
    catalogue = read_catalogue(engine)
    kept, skipped = select_tables(
        catalogue, exclusion=exclusion, staging=staging,
        include_schemas=include_schemas, exclude_tables=exclude_tables,
    )
    return SelectedSource(name, label, engine, len(catalogue), kept, skipped)

