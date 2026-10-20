# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Is ``project_config/`` a curated configuration, or a raw draft?

``database.schema_inspector_cli`` writes *drafts*: every table of the
database, a placeholder description, column notes that quote live sample
values, an empty ``ring_aliases`` and synonym seeds taken from sample values.
Moving those files over a curated ``project_config/`` produces a deployment
that loads, starts and answers, badly: tables the curated file left out
(staging and change-capture tables) crowd the prompt, a whole data source can
silently vanish, and the vocabulary the retrieval layer relies on is gone.
None of that is an error to the loaders, so nothing told the operator.

This module reads the files of a ``project_config/`` directory (file reads
only, no database) and answers eight questions, each as plain data that
``scripts/verify_deployment.py`` turns into a preflight check:

* :func:`find_draft_headers`, :func:`placeholder_descriptions`,
  :func:`name_only_descriptions` and :func:`type_only_column_notes`: is any
  file a draft (the header of the inspector, of the setup wizard or of
  ``scripts/draft_descriptions.py``), are the table descriptions still the
  inspector's placeholder, or are they only the table's own name and
  ``<type> column`` (what ``sync_schema.py`` writes for a table it adds)?
  The same tests, one description at a time, are
  :func:`is_placeholder_description`, :func:`is_name_only_description` and
  :func:`is_type_only_note`; ``scripts/draft_descriptions.py`` uses them to
  find what it has to describe.
* :func:`non_word_synonym_keys`: are the synonyms real words?
* :func:`raw_value_synonym_keys`: are any of them raw data values (dates,
  phone numbers, addresses), counted by shape?
* :func:`entity_key_findings`: does every entity name a ``schema.yaml`` table
  by its key, once?
* :func:`sources_without_tables`: does every data source have tables?
* :func:`staging_tables`: are there staging or change-capture tables?
* :func:`sample_value_notes`: do descriptions quote copied sample values?
* :func:`tables_in_excluded_schemas`: does ``schema.yaml`` list a table whose
  schema the setup tools are told never to scan?

Nothing here ever returns a value read from a description or a note: the
answers are counts and table keys, so a message built from them cannot leak
the data a draft quotes.

``schema.yaml`` is read tolerantly (:func:`read_schema`): a file written
before 6.9.3 repeats a table key when the warehouse repeats a table name in
several schemas, which the strict loader refuses and which is exactly the
file an operator needs told about, so every occurrence is kept and counted.

Public API
----------
DRAFT_HEADER, WIZARD_DRAFT_HEADER, DESCRIBE_DRAFT_HEADER, DRAFT_HEADERS
    The first words of the comment the inspector, the setup wizard and
    ``scripts/draft_descriptions.py`` write at the top of a draft, and the
    three together.
DRAFT_DESCRIPTION_SHARE, NON_WORD_SYNONYM_SHARE, REVIEW_MARKED_SHARE,
SYNC_DRAFT_SHARE
    The shares above which a file counts as a draft.
RAW_VALUE_SYNONYM_LIMIT
    Synonym keys shaped like data values above which ``aliases.yaml`` fails.
SchemaTable, SchemaView
    One ``schema.yaml`` table, and the file as a whole.
read_schema(directory) -> SchemaView
read_schema_text(text) -> SchemaView
find_draft_headers(directory) -> list[str]
find_draft_header_kinds(directory) -> dict[str, str]
placeholder_descriptions(view) -> tuple[int, int]
name_only_descriptions(view) -> tuple[int, int]
type_only_column_notes(view) -> tuple[int, int]
is_placeholder_description(description) -> bool
is_name_only_description(table) -> bool
is_type_only_note(note) -> bool
read_aliases(directory) -> AliasesView | None
non_word_synonym_keys(keys) -> int
raw_value_synonym_keys(keys) -> dict[str, int]
read_entities(directory) -> tuple[EntityRow, ...] | None
entity_key_findings(view, entities, ring_keys) -> EntityKeyFindings
parse_staging_patterns(raw) -> list[StagingPattern]
is_staging(schema, name, patterns) -> bool
staging_tables(view, patterns) -> dict[str, int]
tables_in_excluded_schemas(view, patterns_by_source, default) -> list[SchemaTable]
sample_value_notes(view) -> SampleValueFindings
sources_without_tables(view, sources, default) -> list[str]
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from core.persian import normalize_for_matching

__all__ = [
    "DESCRIBE_DRAFT_HEADER",
    "DRAFT_DESCRIPTION_SHARE",
    "DRAFT_HEADER",
    "DRAFT_HEADERS",
    "NON_WORD_SYNONYM_SHARE",
    "RAW_VALUE_SYNONYM_LIMIT",
    "REVIEW_MARKED_SHARE",
    "SYNC_DRAFT_SHARE",
    "WIZARD_DRAFT_HEADER",
    "AliasesView",
    "EntityKeyFindings",
    "EntityRow",
    "SampleValueFindings",
    "SchemaTable",
    "SchemaView",
    "StagingPattern",
    "entity_key_findings",
    "find_draft_header_kinds",
    "find_draft_headers",
    "is_name_only_description",
    "is_placeholder_description",
    "is_staging",
    "is_type_only_note",
    "name_only_descriptions",
    "non_word_synonym_keys",
    "parse_staging_patterns",
    "placeholder_descriptions",
    "raw_value_synonym_keys",
    "read_aliases",
    "read_entities",
    "read_schema",
    "read_schema_text",
    "sample_value_notes",
    "sources_without_tables",
    "staging_tables",
    "tables_in_excluded_schemas",
    "type_only_column_notes",
]

#: What the inspector writes on the first line of every draft file. A file
#: that carries it is a draft until somebody deletes the line, which is what
#: reviewing it is supposed to end with.
DRAFT_HEADER = "AUTO-GENERATED DRAFT"

#: What ``setup_project.py`` writes on the first line of every file it makes.
#: The wizard writes drafts too (aliases, rules and examples a model suggested),
#: so a file that still carries the line has not been reviewed either.
WIZARD_DRAFT_HEADER = "AUTO-GENERATED by setup_project.py"

#: What ``scripts/draft_descriptions.py`` writes on the first line of the
#: ``schema.described.yaml`` it makes: descriptions a model drafted from names
#: and types, to be read by a person. The line stays until somebody has
#: reviewed the file and deleted it.
DESCRIBE_DRAFT_HEADER = "AUTO-GENERATED by draft_descriptions.py"

#: Every header that makes a file a draft, in the order the preflight reports them.
DRAFT_HEADERS = (DRAFT_HEADER, WIZARD_DRAFT_HEADER, DESCRIBE_DRAFT_HEADER)

#: How many leading lines of a file are searched for :data:`DRAFT_HEADER`.
#: The inspector writes it on line 1; a few more allow for a licence comment
#: or a blank line an editor put above it.
_HEADER_LINES = 10

#: More than this share of ``schema.yaml``'s tables with the inspector's
#: placeholder description makes the file a draft. A curated file adds a
#: table with ``sync_schema.py --add-tables`` and a description that is just
#: its name, which is not the placeholder, so a curated file normally sits at
#: zero; a quarter is generous enough for a file mid-way through curation of
#: a few new tables and far below the whole file a raw draft is.
DRAFT_DESCRIPTION_SHARE = 0.25

#: More than this share of ``schema.yaml``'s tables described by nothing but
#: their own name (``Sales_Fact.Trade``), or of its column notes that are only
#: ``<type> column``, makes the file the draft ``sync_schema.py --add-tables``
#: and ``schema_inspector_cli`` write: "most" of a file, not a few tables added
#: since the last review.
SYNC_DRAFT_SHARE = 0.5

#: More than this many synonym keys shaped like data values (a date, a phone
#: number, an address) make ``aliases.yaml`` fail. A curated vocabulary has
#: none; a few are allowed for a word that happens to look like a code.
RAW_VALUE_SYNONYM_LIMIT = 5

#: More than this share of ``aliases.yaml``'s synonym keys that are not words
#: (timestamps, numbers) makes the synonyms a draft's seeds.
NON_WORD_SYNONYM_SHARE = 0.5

#: More than this share of ``aliases.yaml``'s synonym lines still ending in
#: the inspector's ``# REVIEW`` marker makes the synonyms a draft's seeds,
#: whatever the keys look like (a seed is any sample value of a string column).
REVIEW_MARKED_SHARE = 0.5

#: The description the inspector writes: ``<schema.table> — auto-detected
#: <classification> table (~N rows)``. Matched after YAML loading, so the
#: ``# TO BE FILLED`` comment next to it is not there to match.
_PLACEHOLDER_DESCRIPTION = re.compile(r"auto-detected\b[^\n]{0,40}?\btable\b", re.IGNORECASE)

#: The same placeholder in raw text, for a ``schema.yaml`` that does not
#: parse at all. Only the inspector's own comment counts (``TO BE FILLED:``),
#: not the one ``sync_schema.py`` writes after a table it appended
#: (``TO BE FILLED (added by sync_schema.py)``).
_PLACEHOLDER_RAW = re.compile(
    r"^\s{2,}description:.*(?:auto-detected\b[^\n]{0,40}?\btable\b|TO BE FILLED:)",
    re.IGNORECASE,
)
_DESCRIPTION_RAW = re.compile(r"^\s{2,}description:")


# ---------------------------------------------------------------------------
# Reading the files
# ---------------------------------------------------------------------------

class _TolerantLoader(yaml.SafeLoader):
    """``SafeLoader`` that keeps both of two identical keys in a mapping.

    ``yaml.safe_load`` keeps the last and drops the others; the strict loader
    of :mod:`core.yaml_loading` refuses the file. To count what a file with a
    repeated key holds, the second and later occurrences are kept under
    ``key + _DUPLICATE_MARK + n``; :func:`_plain_key` takes the mark off.
    """


_DUPLICATE_MARK = "\x00#"


def _construct_mapping(loader: _TolerantLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        value = loader.construct_object(value_node, deep=deep)
        if not isinstance(key, (str, int, float, bool, type(None))):
            continue
        if key in result:
            n = 2
            while f"{key}{_DUPLICATE_MARK}{n}" in result:
                n += 1
            key = f"{key}{_DUPLICATE_MARK}{n}"
        result[key] = value
    return result


_TolerantLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _plain_key(key: object) -> str:
    """A mapping key as written, without the mark :class:`_TolerantLoader` added."""
    return str(key).split(_DUPLICATE_MARK, 1)[0]


def _read_text(path: Path) -> str | None:
    """The text of *path* (UTF-8, a BOM tolerated), or ``None`` if it cannot be read."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _load(text: str) -> Any:
    """Parse *text* with :class:`_TolerantLoader`; ``None`` for an empty document.

    Raises:
        yaml.YAMLError: If the text is not YAML at all.
    """
    return yaml.load(text, Loader=_TolerantLoader)  # noqa: S506 - SafeLoader subclass


@dataclass(frozen=True)
class SchemaTable:
    """One entry of ``schema.yaml``'s ``tables`` map."""

    key: str
    """The key as written (``Schema.Table`` or the bare name)."""
    schema: str
    """The schema: ``db_schema``, else the key's qualifier, else ``""``."""
    name: str
    """The table's own name, without any qualifier."""
    description: str
    datasource: tuple[str, ...]
    """The ``datasource:`` value as a tuple; ``()`` when the line is absent."""
    notes: tuple[str, ...]
    """Column notes: the string values of the table's ``columns`` map."""


@dataclass(frozen=True)
class SchemaView:
    """``schema.yaml`` as :func:`read_schema` found it."""

    present: bool = False
    """The file exists and could be read as text."""
    parsed: bool = False
    """The text is YAML with a ``tables`` mapping, so :attr:`tables` is real."""
    tables: tuple[SchemaTable, ...] = ()
    raw_tables: int = 0
    """Description lines found by text search; set when the file does not parse."""
    raw_placeholders: int = 0
    """Placeholder description lines found by text search; set when the file does not parse."""


def _split_key(key: str) -> tuple[str, str]:
    """``(qualifier, name)`` of a table key, the qualifier being its last schema part.

    >>> _split_key("sales.Invoice")
    ('sales', 'Invoice')
    >>> _split_key("Invoice")
    ('', 'Invoice')
    >>> _split_key("[Run.Log]")
    ('', 'Run.Log')
    >>> _split_key("Db.dbo.Invoice")
    ('dbo', 'Invoice')
    """
    if "." not in key and not key.startswith("["):
        return "", key
    try:
        from schema_data.registry import split_table_key

        ref = split_table_key(key)
        return (ref.qualifier[-1] if ref.qualifier else ""), ref.name
    except Exception:  # noqa: BLE001 - a key the registry cannot parse is still a table
        head, _, tail = key.rpartition(".")
        return head.rsplit(".", 1)[-1], tail or key


#: ``<schema.table> \u2014 auto-detected ...``: the draft names the table's full
#: name first. A draft from before 6.9.3 has no ``db_schema`` and bare keys,
#: so this is the only place left that says which schema a table is in.
_PLACEHOLDER_PREFIX = re.compile(r"^(?P<full>\S+)\s+[\u2014\u2013-]+\s+auto-detected\b", re.IGNORECASE)


def _schema_from_placeholder(description: object) -> str:
    """The schema named at the start of a placeholder description, or ``""``.

    >>> _schema_from_placeholder("sales.Invoice \u2014 auto-detected fact table (~10 rows)")
    'sales'
    >>> _schema_from_placeholder("Invoice \u2014 auto-detected fact table")
    ''
    >>> _schema_from_placeholder("Invoices of the month")
    ''
    """
    if not isinstance(description, str):
        return ""
    match = _PLACEHOLDER_PREFIX.match(description)
    if match is None:
        return ""
    return match.group("full").rpartition(".")[0].rsplit(".", 1)[-1]


def _datasources_of(value: object) -> tuple[str, ...]:
    """A ``datasource:`` value as a tuple of names, however it was written."""
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if isinstance(v, str) and v.strip())
    return ()


def read_schema(directory: Path) -> SchemaView:
    """Read ``schema.yaml`` of *directory* without refusing a repeated key.

    A file that is not YAML, or has no ``tables`` mapping, gives a view with
    ``parsed=False`` and, for the placeholder count, a text search of its
    ``description:`` lines. Never raises.
    """
    text = _read_text(directory / "schema.yaml")
    if text is None:
        return SchemaView()
    return _schema_from_text(text)


def read_schema_text(text: str) -> SchemaView:
    """:class:`SchemaView` of a ``schema.yaml`` *text*; never raises.

    The same reading as :func:`read_schema`, for a text that is not (yet) the
    file of a ``project_config/`` directory.

    >>> view = read_schema_text("tables:\\n  T:\\n    description: 'sales.T'\\n    db_schema: sales\\n")
    >>> [(t.key, t.schema, t.name, t.description) for t in view.tables]
    [('T', 'sales', 'T', 'sales.T')]
    """
    return _schema_from_text(text)


@lru_cache(maxsize=4)
def _schema_from_text(text: str) -> SchemaView:
    """:class:`SchemaView` of *text*; cached on the text, since five checks read the same file.

    Parsing a few hundred tables with the pure-Python YAML loader takes a
    fraction of a second, which the admin card would otherwise pay five times.
    """
    try:
        document = _load(text)
    except yaml.YAMLError:
        document = None
    tables = document.get("tables") if isinstance(document, dict) else None
    if not isinstance(tables, dict):
        lines = text.splitlines()
        return SchemaView(
            present=True,
            raw_tables=sum(1 for line in lines if _DESCRIPTION_RAW.match(line)),
            raw_placeholders=sum(1 for line in lines if _PLACEHOLDER_RAW.match(line)),
        )
    rows: list[SchemaTable] = []
    for raw_key, definition in tables.items():
        key = _plain_key(raw_key)
        qualifier, name = _split_key(key)
        body = definition if isinstance(definition, dict) else {}
        schema = body.get("db_schema")
        columns = body.get("columns")
        description = body.get("description")
        if isinstance(schema, str) and schema.strip():
            where = schema.strip()
        else:
            where = qualifier or _schema_from_placeholder(description)
        rows.append(SchemaTable(
            key=key,
            schema=where,
            name=name,
            description=description if isinstance(description, str) else "",
            datasource=_datasources_of(body.get("datasource")),
            notes=tuple(v for v in columns.values() if isinstance(v, str)) if isinstance(columns, dict) else (),
        ))
    return SchemaView(present=True, parsed=True, tables=tuple(rows))


# ---------------------------------------------------------------------------
# 1. Drafts
# ---------------------------------------------------------------------------

def find_draft_header_kinds(directory: Path) -> dict[str, str]:
    """``{file name: header}`` for the ``.yaml`` files that still carry a draft header.

    The header is :data:`DRAFT_HEADER` (the inspector's),
    :data:`WIZARD_DRAFT_HEADER` (the setup wizard's) or
    :data:`DESCRIBE_DRAFT_HEADER` (``scripts/draft_descriptions.py``'s),
    whichever the file's first lines hold. Looks at the first lines of every ``*.yaml`` / ``*.yml``
    directly inside *directory* (not in sub-directories such as
    ``_test_fixtures``), sorted by name. A missing directory gives ``{}``.
    """
    found: dict[str, str] = {}
    try:
        candidates = sorted(
            p for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
        )
    except OSError:
        return {}
    for path in candidates:
        try:
            with path.open(encoding="utf-8-sig", errors="replace") as handle:
                head = [next(handle, "") for _ in range(_HEADER_LINES)]
        except OSError:
            continue
        for line in head:
            if not line.lstrip().startswith("#"):
                continue
            kind = next((h for h in DRAFT_HEADERS if h in line), "")
            if kind:
                found[path.name] = kind
                break
    return found


def find_draft_headers(directory: Path) -> list[str]:
    """Names of the ``.yaml`` files in *directory* that still carry a draft header.

    Any of the three headers (:data:`DRAFT_HEADERS`); see
    :func:`find_draft_header_kinds` for which. Sorted by name; a missing directory gives an empty list.
    """
    return list(find_draft_header_kinds(directory))


def is_placeholder_description(description: str) -> bool:
    """Whether *description* is the inspector's placeholder.

    It is when it reads ``... auto-detected <kind> table ...`` or contains
    ``TO BE FILLED``.

    >>> is_placeholder_description("sales.Trade \u2014 auto-detected fact table (~10 rows)")
    True
    >>> is_placeholder_description("TO BE FILLED"), is_placeholder_description("One row per trade")
    (True, False)
    """
    return bool(_PLACEHOLDER_DESCRIPTION.search(description)) or "TO BE FILLED" in description


def placeholder_descriptions(view: SchemaView) -> tuple[int, int]:
    """``(placeholder, total)``: tables whose description is still the inspector's.

    A description counts when :func:`is_placeholder_description` says so. For
    a file that did not parse, the same is counted from its ``description:``
    lines.
    """
    if not view.parsed:
        return view.raw_placeholders, view.raw_tables
    placeholders = sum(1 for t in view.tables if is_placeholder_description(t.description))
    return placeholders, len(view.tables)


#: The SQL type names ``sync_schema.py`` and the inspector put in front of
#: ``column`` in a draft note (``bigint column``, ``nvarchar(100) column``).
_TYPE_NAMES = (
    "bigint|int|integer|smallint|tinyint|bit|boolean|bool|decimal|numeric|float|real|double|money|"
    "smallmoney|char|nchar|varchar|nvarchar|text|ntext|string|date|datetime|datetime2|smalldatetime|"
    "datetimeoffset|time|timestamp|uniqueidentifier|uuid|binary|varbinary|blob|image|xml|"
    "sql_variant|geography|geometry|hierarchyid|rowversion|unknown"
)
_TYPE_ONLY_NOTE = re.compile(
    rf"^\s*(?:{_TYPE_NAMES})(?:\s+precision)?(?:\s*\([^)]*\))?\s+column\s*$", re.IGNORECASE,
)


def _plain(text: str) -> str:
    """*text* lower-cased without brackets, a trailing ``(view)`` and surrounding space.

    >>> _plain("[Sales].[Trade] (view)")
    'sales.trade'
    """
    text = text.replace("[", "").replace("]", "").strip().casefold()
    return text[: -len("(view)")].rstrip() if text.endswith("(view)") else text


def is_name_only_description(table: SchemaTable) -> bool:
    """Whether *table*'s description is only the table's own name.

    It is when it equals the table's key, its bare name or ``schema.name``
    (brackets, case and a trailing ``(view)`` ignored). An empty description
    is not "only the name"; it says nothing at all, and the caller decides
    what to do with that.

    >>> is_name_only_description(SchemaTable("ref.Trade", "ref", "Trade", "ref.Trade (view)", (), ()))
    True
    >>> is_name_only_description(SchemaTable("Trade", "sales", "Trade", "One row per trade", (), ()))
    False
    >>> is_name_only_description(SchemaTable("Trade", "sales", "Trade", "", (), ()))
    False
    """
    said = _plain(table.description)
    own = {
        _plain(table.key), _plain(table.name),
        _plain(f"{table.schema}.{table.name}" if table.schema else table.name),
    }
    return bool(said) and said in own


def name_only_descriptions(view: SchemaView) -> tuple[int, int]:
    """``(name_only, total)``: tables whose description is only the table's own name.

    ``sync_schema.py --add-tables`` and the inspector's sync-format draft
    describe a table by ``Schema.Table`` (with ``(view)`` for a view), so a
    description equal to the table's key, its bare name or ``schema.name``
    says nothing the key did not. A file that did not parse has no tables to
    count.

    >>> view = SchemaView(parsed=True, tables=(
    ...     SchemaTable("Trade", "sales", "Trade", "sales.Trade", (), ()),
    ...     SchemaTable("ref.Trade", "ref", "Trade", "ref.Trade (view)", (), ()),
    ...     SchemaTable("Customer", "sales", "Customer", "The customers we invoice", (), ()),
    ... ))
    >>> name_only_descriptions(view)
    (2, 3)
    """
    return sum(1 for table in view.tables if is_name_only_description(table)), len(view.tables)


def is_type_only_note(note: str) -> bool:
    """Whether a column description is only ``<sql type> column``.

    >>> is_type_only_note("nvarchar(100) column"), is_type_only_note("Invoice date")
    (True, False)
    """
    return _TYPE_ONLY_NOTE.match(note) is not None


def type_only_column_notes(view: SchemaView) -> tuple[int, int]:
    """``(type_only, total)``: column descriptions that are only ``<sql type> column``.

    >>> view = SchemaView(parsed=True, tables=(
    ...     SchemaTable("T", "", "T", "", (), ("bigint column", "nvarchar(100) column", "Invoice date")),
    ... ))
    >>> type_only_column_notes(view)
    (2, 3)
    """
    notes = [note for table in view.tables for note in table.notes]
    return sum(1 for note in notes if is_type_only_note(note)), len(notes)


# ---------------------------------------------------------------------------
# 2. Aliases
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AliasesView:
    """``aliases.yaml`` as far as the preflight needs it."""

    ring_aliases: int
    """Entries in ``ring_aliases``."""
    synonym_keys: tuple[str, ...]
    """The keys of ``synonyms``."""
    parsed: bool = True
    """False when the file is not YAML and the counts come from its lines."""
    review_marked: int = 0
    """Synonym lines still ending in the inspector's ``# REVIEW`` marker."""
    ring_keys: tuple[str, ...] = ()
    """The keys of ``ring_aliases``; empty when the file is not YAML."""


_TOP_LEVEL_KEY = re.compile(r"^(\w+):")
_SYNONYM_LINE = re.compile(r"^\s+[\"'](?P<key>.*)[\"']\s*:\s*\[")


#: What the inspector writes after each synonym seed (``"key": [...]  # REVIEW``);
#: curating the seed removes it.
_REVIEW_MARK = re.compile(r"\]\s*#\s*REVIEW\s*$")


def _review_marked(text: str) -> int:
    """How many lines of *text* end in the inspector's ``# REVIEW`` marker.

    >>> _review_marked('  "a": ["x"]  # REVIEW\\n  "b": ["y"]\\n')
    1
    """
    return sum(1 for line in text.splitlines() if _REVIEW_MARK.search(line))


def _read_aliases_text(text: str) -> AliasesView:
    """:class:`AliasesView` of an ``aliases.yaml`` that is not YAML, from its lines.

    A draft from before 6.9.3 breaks on a synonym key holding a quote, and
    the file still has to be judged. Counts the top-level entries of
    ``ring_aliases`` and the quoted keys of ``synonyms``.
    """
    section = ""
    ring = 0
    keys: list[str] = []
    for line in text.splitlines():
        top = _TOP_LEVEL_KEY.match(line)
        if top:
            section = top.group(1)
            continue
        if section == "ring_aliases":
            stripped = line.lstrip()
            if re.match(r"^\s{1,4}\S", line) and not stripped.startswith(("#", "-")):
                ring += 1
        elif section == "synonyms":
            match = _SYNONYM_LINE.match(line)
            if match:
                keys.append(match.group("key"))
    return AliasesView(
        ring_aliases=ring, synonym_keys=tuple(keys), parsed=False, review_marked=_review_marked(text),
    )


def read_aliases(directory: Path) -> AliasesView | None:
    """Read ``aliases.yaml`` of *directory*, or ``None`` if it is absent.

    A file that is not YAML is read from its lines instead
    (:attr:`AliasesView.parsed` is then false). A document that is YAML but
    not a mapping gives an empty view.
    """
    text = _read_text(directory / "aliases.yaml")
    if text is None:
        return None
    return _aliases_from_text(text)


@lru_cache(maxsize=4)
def _aliases_from_text(text: str) -> AliasesView:
    """:class:`AliasesView` of *text*, cached on the text like :func:`_schema_from_text`."""
    try:
        document = _load(text)
    except yaml.YAMLError:
        return _read_aliases_text(text)
    if not isinstance(document, dict):
        return AliasesView(ring_aliases=0, synonym_keys=())
    ring = document.get("ring_aliases")
    synonyms = document.get("synonyms")
    return AliasesView(
        ring_aliases=len(ring) if isinstance(ring, dict) else 0,
        synonym_keys=tuple(_plain_key(k) for k in synonyms) if isinstance(synonyms, dict) else (),
        review_marked=_review_marked(text),
        ring_keys=tuple(_plain_key(k) for k in ring) if isinstance(ring, dict) else (),
    )


_WORD = re.compile(r"[^\W\d_]{2,}")


def non_word_synonym_keys(keys: tuple[str, ...] | list[str]) -> int:
    """How many of *keys* are not words: timestamps, numbers, codes with no letters.

    A word has a run of at least two letters in any script. ``2024-05-01
    10:00:00`` and ``12345`` have none, nor does an ISO timestamp, whose only
    letters are single ``T`` and ``Z``.

    >>> non_word_synonym_keys(["2024-05-01 10:00:00", "12345", "invoice", "فروش"])
    2
    >>> non_word_synonym_keys(["2024-05-01T10:00:00Z"])
    1
    >>> non_word_synonym_keys([])
    0
    """
    return sum(1 for key in keys if not _WORD.search(key))


def raw_value_synonym_keys(keys: Sequence[str]) -> dict[str, int]:
    """Count the synonym keys that look like raw data values, by shape.

    A key shaped like a date, a time, a phone number, an IP address, an
    e-mail address, a URL, a masked value (``xxx``), a hash, a long digit run
    or an address was copied out of a row, not written as a word. Only the
    *shape* name is returned (``date``, ``phone``, ...), never the key: the
    caller can print it without printing the data.

    >>> raw_value_synonym_keys(["1387/03/24", "01/01/2010", "09120000000", "invoice", "ab xxxxx cd"])
    {'date': 2, 'phone': 1, 'masked': 1}
    >>> raw_value_synonym_keys(["customer", "1402", "q1"])
    {}
    """
    from core.value_shapes import value_shape

    counts: Counter[str] = Counter()
    for key in keys:
        shape = value_shape(key)
        if shape:
            counts[shape] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# 4. Staging and change-capture tables
# ---------------------------------------------------------------------------

class StagingPattern(NamedTuple):
    """One pattern of the ``STAGING_PATTERNS`` setting."""

    pattern: str
    """Lower-cased ``fnmatch`` pattern."""
    on_table: bool
    """Matched against the table name (``table:`` prefix) rather than the schema."""


def parse_staging_patterns(raw: str) -> list[StagingPattern]:
    """Parse the ``STAGING_PATTERNS`` setting.

    Comma-separated ``fnmatch`` patterns; a ``table:`` prefix matches the
    table name, a bare pattern the schema name. Blank entries are ignored,
    and so is a ``table:`` with nothing after it.

    >>> parse_staging_patterns("*_Stg, table:*_Inserts")
    [StagingPattern(pattern='*_stg', on_table=False), StagingPattern(pattern='*_inserts', on_table=True)]
    >>> parse_staging_patterns("")
    []
    >>> parse_staging_patterns("table:")
    []
    """
    patterns: list[StagingPattern] = []
    for item in (raw or "").split(","):
        item = item.strip()
        on_table = item.lower().startswith("table:")
        if on_table:
            item = item[len("table:"):].strip()
        if item:
            patterns.append(StagingPattern(item.lower(), on_table))
    return patterns


def is_staging(schema: str, name: str, patterns: Sequence[StagingPattern]) -> bool:
    """Whether a staging pattern matches the table *name* in *schema*.

    A bare pattern is compared with the last part of the schema, a ``table:``
    pattern with the table name; both lower-cased. The inspector uses the same
    test to leave staging tables out of a draft.

    >>> patterns = parse_staging_patterns("*_Stg, table:*_Inserts")
    >>> is_staging("Sales_Stg", "Invoice", patterns), is_staging("sales", "Invoice_Inserts", patterns)
    (True, True)
    >>> is_staging("sales", "Invoice", patterns), is_staging("sales", "Invoice", [])
    (False, False)
    """
    schema_low = schema.rsplit(".", 1)[-1].lower()
    name_low = name.lower()
    return any(
        fnmatch.fnmatchcase(name_low if p.on_table else schema_low, p.pattern)
        for p in patterns
    )


def staging_tables(view: SchemaView, patterns: list[StagingPattern]) -> dict[str, int]:
    """Count the tables a staging pattern matches, per schema.

    ``{schema: count}``, a table without a schema under ``""``; a table that
    several patterns match counts once. Empty when *patterns* is empty or the
    file did not parse.
    """
    counts: Counter[str] = Counter()
    if not patterns:
        return {}
    for table in view.tables:
        if is_staging(table.schema, table.name, patterns):
            counts[table.schema] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# 4b. Tables in schemas the setup tools are told not to scan
# ---------------------------------------------------------------------------

def tables_in_excluded_schemas(
    view: SchemaView,
    patterns_by_source: Mapping[str, Sequence[str]],
    default: str,
) -> list[SchemaTable]:
    """The ``schema.yaml`` tables whose schema is excluded in every source they are in.

    The exclusion (``database.schema_filter``) only decides what the setup
    tools *propose* from a database; a table ``schema.yaml`` already lists is
    kept and synced. A listed table in an excluded schema is a contradiction
    the operator should resolve, either by removing the table or by narrowing
    the exclusion, so it is reported. A table belongs to the sources its
    ``datasource:`` line names, to *default* when it has none, and counts only
    when every one of them excludes its schema. A table whose schema is not
    known (a bare key with no ``db_schema``) is never reported.

    >>> view = SchemaView(parsed=True, tables=(
    ...     SchemaTable("Run_Log", "Replay", "Run_Log", "", (), ()),
    ...     SchemaTable("Invoice", "sales", "Invoice", "", (), ()),
    ...     SchemaTable("Bare", "", "Bare", "", (), ()),
    ... ))
    >>> [t.key for t in tables_in_excluded_schemas(view, {"main": ["replay", "sys"]}, "main")]
    ['Run_Log']
    >>> tables_in_excluded_schemas(view, {"main": []}, "main")
    []
    """
    from database.schema_filter import schema_is_excluded

    found: list[SchemaTable] = []
    for table in view.tables:
        schema = table.schema.rsplit(".", 1)[-1]
        if not schema:
            continue
        sources = table.datasource or (default,)
        if all(schema_is_excluded(schema, patterns_by_source.get(s, ())) for s in sources):
            found.append(table)
    return found


# ---------------------------------------------------------------------------
# 5. Sample values in descriptions
# ---------------------------------------------------------------------------

_E_G = re.compile(r"\be\.g\.", re.IGNORECASE)
_DIGIT_RUN = re.compile(r"(?<![\w.])\d{7,}(?![\w.])")
_SPACED_PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{3,4}(?!\d)")
_DATE_LIKE = re.compile(r"^(?:1[34]|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:[01]\d[0-5]\d[0-5]\d)?$")
_ADDRESS_WORD = re.compile(
    r"\b(?:street|st\.|avenue|ave\.?|road|rd\.|boulevard|blvd|lane|suite|apt\.?|floor|unit|plot)\b"
    r"|خیابان|کوچه|بلوار|پلاک|میدان|طبقه|واحد|بزرگراه|خ\.",
    re.IGNORECASE,
)
_ADDRESS_LENGTH = 30


@dataclass
class SampleValueFindings:
    """Where descriptions seem to quote sample values; counts and table keys only."""

    tables: list[str] = field(default_factory=list)
    """Keys of the tables with at least one such note, in file order."""
    notes: int = 0
    """Notes with at least one finding (a note can have several kinds)."""
    phone_like: int = 0
    """Notes holding a run of seven or more digits (not ten, not a date)."""
    national_id_like: int = 0
    """Notes holding a run of exactly ten digits."""
    address_like: int = 0
    """Notes holding a long segment with digits, or a street word next to digits."""


def _classify_example_tail(tail: str) -> tuple[bool, bool, bool]:
    """``(phone_like, national_id_like, address_like)`` for the text after an ``e.g.``.

    >>> _classify_example_tail(" 09123456789, 09351234567")
    (True, False, False)
    >>> _classify_example_tail(" 1234567890")
    (False, True, False)
    >>> _classify_example_tail(" 20240131, 1402/05/01, 2024-01-31")
    (False, False, False)
    >>> _classify_example_tail(" Active, Closed")
    (False, False, False)
    >>> _classify_example_tail(" 12 Example Street Building 4 Floor 2")
    (False, False, True)
    """
    # Persian and Arabic-Indic digits to ASCII (and the other foldings), so a copied
    # phone number is seen whatever script the warehouse stores it in.
    folded = normalize_for_matching(tail)
    phone = national = False
    for match in _DIGIT_RUN.finditer(folded):
        digits = match.group()
        if _DATE_LIKE.match(digits):
            continue
        if len(digits) == 10:
            national = True
        else:
            phone = True
    if not phone and _SPACED_PHONE.search(folded):
        phone = True
    address = False
    for segment in re.split(r"[;|]|,\s", folded):
        words = segment.split()
        if (
            len(words) >= 3
            and any(ch.isdigit() for ch in segment)
            and (len(segment.strip()) >= _ADDRESS_LENGTH or _ADDRESS_WORD.search(segment))
        ):
            address = True
            break
    if (
        not address
        and len(folded.split()) >= 3
        and any(ch.isdigit() for ch in folded)
        and _ADDRESS_WORD.search(folded)
    ):
        address = True
    return phone, national, address


def _list_of_examples(text: str) -> re.Match[str] | None:
    """The first ``e.g.`` of *text* that starts a list of values, or ``None``.

    The inspector appends its values after the last ``;`` of a note, outside
    any parenthesis. A hand-written note that explains a format puts the
    ``e.g.`` inside one (``(e.g. 1402/05/01)``), which is prose, not a copied
    value, so an ``e.g.`` after an unclosed ``(`` is skipped.

    >>> bool(_list_of_examples("varchar column; e.g. a, b"))
    True
    >>> bool(_list_of_examples("A code (e.g. 12345678). Used for joins"))
    False
    >>> bool(_list_of_examples("no examples here"))
    False
    """
    for match in _E_G.finditer(text):
        before = text[: match.start()]
        if before.count("(") <= before.count(")"):
            return match
    return None


def sample_value_notes(view: SchemaView) -> SampleValueFindings:
    """Find descriptions and column notes that seem to quote live sample values.

    The inspector writes a column note as ``<type> column; e.g. a, b, c``
    with up to three values read from the live rows. Only the text after an
    ``e.g.`` is examined, and a hand-written note such as ``e.g. 1402-05-01``
    (a date) or ``e.g. 'Active', 'Closed'`` (status words) is not a finding.
    Counts per kind and the keys of the tables concerned are all that is
    kept; no value leaves this function.
    """
    findings = SampleValueFindings()
    for table in view.tables:
        hit = False
        for text in (table.description, *table.notes):
            match = _list_of_examples(text)
            if match is None:
                continue
            phone, national, address = _classify_example_tail(text[match.end():])
            if not (phone or national or address):
                continue
            hit = True
            findings.notes += 1
            findings.phone_like += phone
            findings.national_id_like += national
            findings.address_like += address
        if hit:
            findings.tables.append(table.key)
    return findings


# ---------------------------------------------------------------------------
# 3. Data sources without tables
# ---------------------------------------------------------------------------

def sources_without_tables(view: SchemaView, sources: tuple[str, ...], default: str) -> list[str]:
    """The data sources of *sources* that no ``schema.yaml`` table is assigned to.

    A table belongs to the sources its ``datasource:`` line names, to
    *default* when it has none (which, with a single source, is the whole
    file). A table listed under several sources counts for each.

    >>> view = SchemaView(parsed=True, tables=(
    ...     SchemaTable("A", "", "A", "", ("main",), ()),
    ...     SchemaTable("B", "", "B", "", (), ()),
    ... ))
    >>> sources_without_tables(view, ("main", "archive"), "main")
    ['archive']
    >>> sources_without_tables(view, ("main",), "main")
    []
    """
    used: set[str] = set()
    for table in view.tables:
        used.update(table.datasource or (default,))
    return [s for s in sources if s not in used]


# ---------------------------------------------------------------------------
# 6. Entities and the schema.yaml keys
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EntityRow:
    """One entry of ``entities.yaml``."""

    key: str
    """The entity name, which retrieval uses as the name of a table."""
    table: str
    schema: str
    """``schema_name:`` (or ``schema:``) as written; ``""`` when absent."""
    aliases: tuple[str, ...] = ()
    """``aliases:`` as written: the text entries of the list, ``()`` when absent."""


def read_entities(directory: Path) -> tuple[EntityRow, ...] | None:
    """The entries of ``entities.yaml`` in *directory*, or ``None`` when it cannot be read.

    ``None`` for a missing file, text that is not YAML, and a document with
    no ``entities`` mapping; an entry that is not a mapping is skipped.
    """
    text = _read_text(directory / "entities.yaml")
    if text is None:
        return None
    try:
        document = _load(text)
    except yaml.YAMLError:
        return None
    entities = document.get("entities") if isinstance(document, dict) else None
    if not isinstance(entities, dict):
        return None
    rows: list[EntityRow] = []
    for raw_key, body in entities.items():
        if not isinstance(body, dict):
            continue
        table = body.get("table")
        schema = body.get("schema_name", body.get("schema"))
        aliases = body.get("aliases")
        rows.append(EntityRow(
            key=_plain_key(raw_key),
            table=table.strip() if isinstance(table, str) else "",
            schema=schema.strip() if isinstance(schema, str) else "",
            aliases=tuple(a.strip() for a in aliases if isinstance(a, str) and a.strip())
            if isinstance(aliases, list) else (),
        ))
    return tuple(rows)


@dataclass
class EntityKeyFindings:
    """Where ``entities.yaml`` and ``aliases.yaml`` name tables that ``schema.yaml`` does not key so."""

    unknown_entities: list[tuple[str, str]] = field(default_factory=list)
    """``(entity key, the schema.yaml key of its table or "")`` for each key that is no table key."""
    unknown_rings: list[tuple[str, str]] = field(default_factory=list)
    """The same for a ``ring_aliases`` key that names a table but not by its key."""
    duplicates: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    """``(table, entity keys)`` for each table that two or more entities name."""

    @property
    def found(self) -> int:
        """How many findings there are in all."""
        return len(self.unknown_entities) + len(self.unknown_rings) + len(self.duplicates)


def _key_parts(key: str) -> tuple[str, ...]:
    """The lower-cased parts of a table key; the whole text when it does not parse."""
    try:
        from schema_data.registry import split_table_key

        return tuple(p.strip().lower() for p in split_table_key(key).parts)
    except ValueError:
        return (key.strip().lower(),)


class _SchemaKeys:
    """The ``schema.yaml`` keys, indexed the way retrieval resolves a configured table name.

    A reference is a key itself (brackets and case ignored); otherwise the
    keys with its bare name, narrowed by the qualifier the reference writes
    *against the qualifier of the key* -- ``db_schema`` is not consulted, as
    ``retrieval.table_keys.resolve_table_ref`` does not, which is why a
    ``Schema.Table`` entity name for a table keyed by its bare name finds
    nothing.
    """

    def __init__(self, tables: Sequence[SchemaTable]) -> None:
        self.tables = list(tables)
        self.exact: dict[str, str] = {}
        self.by_bare: dict[str, list[str]] = {}
        self.qualifiers: dict[str, tuple[str, ...]] = {}
        self.identity: dict[str, tuple[str, str]] = {}
        self.by_name: dict[str, list[SchemaTable]] = {}
        for table in tables:
            parts = _key_parts(table.key)
            self.exact.setdefault(".".join(parts), table.key)
            self.by_bare.setdefault(parts[-1], []).append(table.key)
            self.qualifiers[table.key] = parts[:-1]
            self.identity[table.key] = (table.schema.rsplit(".", 1)[-1].lower(), table.name.lower())
            self.by_name.setdefault(table.name.lower(), []).append(table)

    def resolve(self, ref: str) -> tuple[str, ...]:
        """The keys *ref* can mean; empty when it names no table."""
        if ref in self.qualifiers:
            return (ref,)
        parts = _key_parts(ref)
        whole = self.exact.get(".".join(parts))
        if whole is not None:
            return (whole,)
        found = self.by_bare.get(parts[-1], [])
        wanted = parts[:-1]
        if wanted:
            found = [k for k in found if self.qualifiers[k][-len(wanted):] == wanted]
        return tuple(found)

    def table_of(self, row: EntityRow) -> tuple[str, str]:
        """``(schema, name)`` of the table *row* stands for, lower-cased."""
        found = self.resolve(row.key)
        if len(found) == 1:
            return self.identity[found[0]]
        parts = _key_parts(row.key)
        name = (row.table or parts[-1]).lower()
        schema = (row.schema or (parts[-2] if len(parts) > 1 else "")).rsplit(".", 1)[-1].lower()
        if not schema:
            named = self.by_name.get(name, [])
            if len(named) == 1:
                return self.identity[named[0].key]
        return schema, name

    def key_for(self, row: EntityRow) -> str:
        """The ``schema.yaml`` key of the table *row* stands for, or ``""``."""
        schema, name = self.table_of(row)
        for table in self.by_name.get(name, []):
            if self.identity[table.key] == (schema, name):
                return table.key
        return ""


def entity_key_findings(
    view: SchemaView,
    entities: Sequence[EntityRow],
    ring_keys: Sequence[str] = (),
) -> EntityKeyFindings:
    """Which entity and ring-alias keys miss ``schema.yaml``'s keys, and which tables have two entities.

    Retrieval treats an entity's key as the name of a table: it must be the
    table's ``schema.yaml`` key (a bare name that is unique, or a spelling
    that retrieval resolves to one), or the table never reaches the prompt
    and its aliases match nothing. A ``ring_aliases`` key is a hall name in a
    curated file, so it is reported only when it names a table of
    ``schema.yaml`` by some other spelling than the key. Two entities that
    stand for the same table are reported once, with both keys.

    Parameters
    ----------
    view:
        ``schema.yaml``, from :func:`read_schema` (it must have parsed).
    entities:
        The entries of ``entities.yaml``.
    ring_keys:
        The keys of ``ring_aliases``.

    Returns
    -------
    EntityKeyFindings
        Table keys and entity names only; nothing read from a row.

    Examples
    --------
    >>> view = SchemaView(parsed=True, tables=(
    ...     SchemaTable("ActionType", "Sales_Dim", "ActionType", "", (), ()),
    ...     SchemaTable("sales.Trade", "sales", "Trade", "", (), ()),
    ...     SchemaTable("ref.Trade", "ref", "Trade", "", (), ()),
    ... ))
    >>> rows = (EntityRow("ActionType", "ActionType", ""), EntityRow("Sales_Dim.ActionType", "ActionType", "Sales_Dim"),
    ...         EntityRow("sales.Trade", "Trade", "sales"))
    >>> found = entity_key_findings(view, rows, ["Sales_Dim.ActionType", "Hall A"])
    >>> found.unknown_entities, found.unknown_rings
    ([('Sales_Dim.ActionType', 'ActionType')], [('Sales_Dim.ActionType', 'ActionType')])
    >>> found.duplicates
    [('Sales_Dim.ActionType', ('ActionType', 'Sales_Dim.ActionType'))]
    """
    index = _SchemaKeys(view.tables)
    findings = EntityKeyFindings()
    owners: dict[tuple[str, str], list[str]] = {}
    for row in entities:
        if not index.resolve(row.key):
            findings.unknown_entities.append((row.key, index.key_for(row)))
        owners.setdefault(index.table_of(row), []).append(row.key)
    for (schema, name), keys in owners.items():
        if len(keys) > 1:
            shown = f"{schema}.{name}" if schema else name
            for table in index.by_name.get(name, []):
                if index.identity[table.key] == (schema, name):
                    shown = f"{table.schema}.{table.name}" if table.schema else table.name
            findings.duplicates.append((shown, tuple(keys)))
    for key in ring_keys:
        if index.resolve(key):
            continue
        parts = _key_parts(key)
        named = index.by_name.get(parts[-1], [])
        if not named:
            continue
        schema = parts[-2] if len(parts) > 1 else ""
        match = [t for t in named if not schema or index.identity[t.key][0] == schema]
        findings.unknown_rings.append((key, match[0].key if len(match) == 1 else ""))
    return findings
