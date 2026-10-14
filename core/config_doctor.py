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
only, no database) and answers five questions, each as plain data that
``scripts/verify_deployment.py`` turns into a preflight check:

* :func:`find_draft_headers` and :func:`placeholder_descriptions`: is any file
  a draft, or are the table descriptions still the inspector's placeholder?
* :func:`non_word_synonym_keys`: are the synonyms real words?
* :func:`sources_without_tables`: does every data source have tables?
* :func:`staging_tables`: are there staging or change-capture tables?
* :func:`sample_value_notes`: do descriptions quote copied sample values?

Nothing here ever returns a value read from a description or a note: the
answers are counts and table keys, so a message built from them cannot leak
the data a draft quotes.

``schema.yaml`` is read tolerantly (:func:`read_schema`): a file written
before 6.9.3 repeats a table key when the warehouse repeats a table name in
several schemas, which the strict loader refuses and which is exactly the
file an operator needs told about, so every occurrence is kept and counted.

Public API
----------
DRAFT_HEADER
    The first words of the comment the inspector writes at the top of a draft.
DRAFT_DESCRIPTION_SHARE, NON_WORD_SYNONYM_SHARE, REVIEW_MARKED_SHARE
    The shares above which a file counts as a draft.
SchemaTable, SchemaView
    One ``schema.yaml`` table, and the file as a whole.
read_schema(directory) -> SchemaView
find_draft_headers(directory) -> list[str]
placeholder_descriptions(view) -> tuple[int, int]
read_aliases(directory) -> AliasesView | None
non_word_synonym_keys(keys) -> int
parse_staging_patterns(raw) -> list[StagingPattern]
staging_tables(view, patterns) -> dict[str, int]
sample_value_notes(view) -> SampleValueFindings
sources_without_tables(view, sources, default) -> list[str]
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from core.persian import normalize_for_matching

__all__ = [
    "DRAFT_DESCRIPTION_SHARE",
    "DRAFT_HEADER",
    "NON_WORD_SYNONYM_SHARE",
    "REVIEW_MARKED_SHARE",
    "AliasesView",
    "SampleValueFindings",
    "SchemaTable",
    "SchemaView",
    "StagingPattern",
    "find_draft_headers",
    "non_word_synonym_keys",
    "parse_staging_patterns",
    "placeholder_descriptions",
    "read_aliases",
    "read_schema",
    "sample_value_notes",
    "sources_without_tables",
    "staging_tables",
]

#: What the inspector writes on the first line of every draft file. A file
#: that carries it is a draft until somebody deletes the line, which is what
#: reviewing it is supposed to end with.
DRAFT_HEADER = "AUTO-GENERATED DRAFT"

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

def find_draft_headers(directory: Path) -> list[str]:
    """Names of the ``.yaml`` files in *directory* that still carry the draft header.

    Looks at the first lines of every ``*.yaml`` / ``*.yml`` directly inside
    *directory* (not in sub-directories such as ``_test_fixtures``), sorted by
    name. A missing directory gives an empty list.
    """
    found: list[str] = []
    try:
        candidates = sorted(
            p for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
        )
    except OSError:
        return []
    for path in candidates:
        try:
            with path.open(encoding="utf-8-sig", errors="replace") as handle:
                head = [next(handle, "") for _ in range(_HEADER_LINES)]
        except OSError:
            continue
        if any(line.lstrip().startswith("#") and DRAFT_HEADER in line for line in head):
            found.append(path.name)
    return found


def placeholder_descriptions(view: SchemaView) -> tuple[int, int]:
    """``(placeholder, total)``: tables whose description is still the inspector's.

    A description counts when it reads ``... auto-detected <kind> table ...``
    or contains ``TO BE FILLED``. For a file that did not parse, the same is
    counted from its ``description:`` lines.
    """
    if not view.parsed:
        return view.raw_placeholders, view.raw_tables
    placeholders = sum(
        1 for t in view.tables
        if _PLACEHOLDER_DESCRIPTION.search(t.description) or "TO BE FILLED" in t.description
    )
    return placeholders, len(view.tables)


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
        schema = table.schema.rsplit(".", 1)[-1].lower()
        name = table.name.lower()
        if any(
            fnmatch.fnmatchcase(name if p.on_table else schema, p.pattern)
            for p in patterns
        ):
            counts[table.schema] += 1
    return dict(counts)


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
