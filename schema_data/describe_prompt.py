# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""What a model is told about one table to describe it, and what is kept of its answer.

``scripts/draft_descriptions.py`` asks one question per table. This module is
the part of that question that needs neither the model nor a database:

* :class:`TableFacts` -- the table's key, schema and data source, its columns
  with their types and the notes a person already wrote, the relationships
  that name it, and the aliases ``entities.yaml`` gives it (:func:`table_facts`,
  :func:`relationship_notes`, :func:`read_listed_relationships`,
  :func:`entity_aliases`). No row value is among them unless ``--samples`` was
  asked for, and then only what :func:`core.value_shapes.label_values` lets through
  (:func:`label_samples`);
* :func:`build_prompt` -- the question, in the language the descriptions are
  wanted in;
* :func:`parse_answer` -- the model's JSON reduced to the descriptions that
  can be used: names the table does not have are ignored, a column is never
  dropped, curated notes are never replaced;
* :func:`max_tokens_for` -- how long an answer to ask for.

Public API
----------
ColumnFact, TableFacts, TableAnswer
table_facts(key, definition, target, ...) -> TableFacts
relationship_notes(key, definition, schema, listed) -> tuple[str, ...]
read_listed_relationships(directory) -> list[dict]
entity_aliases(directory) -> dict[str, tuple[str, ...]]
label_samples(table, columns, fetch) -> dict[str, tuple[str, ...]]
build_prompt(facts, language) -> str
parse_answer(answer, facts) -> TableAnswer
max_tokens_for(columns, configured, ceiling) -> int
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import config_doctor as doctor
from core.value_shapes import VALUES_PER_TABLE, label_values
from core.yaml_loading import safe_load_strict
from schema_data.describe import TableTarget
from schema_data.registry import SchemaConfig, TableDefinition, effective_qualifier, split_table_key

__all__ = [
    "COLUMN_TEXT_LIMIT",
    "LANGUAGES",
    "RELATIONSHIP_LIMIT",
    "SAMPLES_PER_COLUMN",
    "TABLE_TEXT_LIMIT",
    "TOKEN_CEILING",
    "ColumnFact",
    "TableAnswer",
    "TableFacts",
    "build_prompt",
    "entity_aliases",
    "label_samples",
    "max_tokens_for",
    "parse_answer",
    "read_listed_relationships",
    "relationship_notes",
    "table_facts",
]

#: The languages descriptions can be drafted in: Persian (the default; the
#: questions are Persian and retrieval matches the words of the description)
#: and English.
LANGUAGES = ("fa", "en")

#: Most relationship lines and aliases shown for one table.
RELATIONSHIP_LIMIT = 12
_ALIAS_LIMIT = 8

#: Most sample values shown for one label column (``--samples``). The table as
#: a whole is held to :data:`core.value_shapes.VALUES_PER_TABLE`.
SAMPLES_PER_COLUMN = 5

#: Longest description kept: a table in a sentence, a column in a few words.
TABLE_TEXT_LIMIT = 300
COLUMN_TEXT_LIMIT = 160

#: The answer is JSON: a fixed part, and about this many tokens per column
#: (the column name, a few Persian words, quotes and punctuation).
_TOKENS_BASE = 300
_TOKENS_PER_COLUMN = 60

#: The most tokens one answer is ever sized for, unless ``--max-tokens`` says more.
TOKEN_CEILING = 8192


@dataclass(frozen=True)
class ColumnFact:
    """One column as the model is told about it.

    Attributes
    ----------
    name:
        The column name.
    type:
        The SQL type from ``column_types:``, ``""`` when schema.yaml does not record it.
    note:
        What a person wrote for it; ``""`` for a draft (empty or ``<type> column``).
    wanted:
        The model is asked to describe this column.
    samples:
        Label values (``--samples`` only), already filtered.
    """

    name: str
    type: str = ""
    note: str = ""
    wanted: bool = False
    samples: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableFacts:
    """Everything the model is told about one table.

    Attributes
    ----------
    key:
        The table key as written in schema.yaml.
    name:
        The table's bare name.
    schema:
        The effective schema (``db_schema``, or the key's qualifier), ``""`` for none.
    datasources:
        The data sources the table lives in; ``()`` when schema.yaml does not say.
    describe_table:
        The model is asked for a description of the table itself.
    columns:
        Every column, wanted or not, in file order.
    relationships:
        Lines naming the table's relationships.
    aliases:
        The names ``entities.yaml`` gives the table.
    """

    key: str
    name: str
    schema: str = ""
    datasources: tuple[str, ...] = ()
    describe_table: bool = True
    columns: tuple[ColumnFact, ...] = ()
    relationships: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    @property
    def wanted_columns(self) -> tuple[str, ...]:
        """Names of the columns the model is asked to describe."""
        return tuple(c.name for c in self.columns if c.wanted)


@dataclass(frozen=True)
class TableAnswer:
    """What was kept of a model's answer for one table.

    Attributes
    ----------
    description:
        The table's description, or ``""`` when none was asked for or usable.
    columns:
        ``{column: description}`` for the wanted columns the model described.
    ignored:
        Entries of the answer that were not used: names the table does not
        have, columns that already had a curated note, texts that said nothing.
    """

    description: str = ""
    columns: Mapping[str, str] = field(default_factory=dict)
    ignored: int = 0


# ---------------------------------------------------------------------------
# The facts
# ---------------------------------------------------------------------------

def table_facts(
    key: str,
    definition: TableDefinition,
    target: TableTarget,
    *,
    relationships: Sequence[str] = (),
    aliases: Sequence[str] = (),
    samples: Mapping[str, Sequence[str]] | None = None,
) -> TableFacts:
    """The facts about one table of ``schema.yaml``, for the model.

    Parameters
    ----------
    key:
        The table key.
    definition:
        The table's entry in the validated ``schema.yaml``.
    target:
        What of the table needs a description
        (:func:`schema_data.describe.find_targets`).
    relationships:
        :func:`relationship_notes` for the table.
    aliases:
        The table's entity aliases.
    samples:
        ``{column: values}`` from :func:`label_samples`; none by default.

    Returns
    -------
    TableFacts
        A column a person has described carries that note; a draft note
        (empty, or ``<type> column``) is not passed on.

    Examples
    --------
    >>> definition = TableDefinition(
    ...     description="sales.Trade", db_schema="sales",
    ...     columns={"ID": "Primary key", "Status": "nvarchar(20) column"},
    ...     column_types={"ID": "int", "Status": "nvarchar(20)"},
    ... )
    >>> facts = table_facts("Trade", definition, TableTarget("Trade", True, ("Status",)))
    >>> facts.schema, facts.wanted_columns, [c.note for c in facts.columns]
    ('sales', ('Status',), ['Primary key', ''])
    """
    wanted = set(target.columns)
    given = samples or {}
    columns = tuple(
        ColumnFact(
            name=name,
            type=definition.column_types.get(name, ""),
            note="" if name in wanted else note.strip(),
            wanted=name in wanted,
            samples=tuple(given.get(name, ())),
        )
        for name, note in (definition.columns or {}).items()
    )
    return TableFacts(
        key=key,
        name=split_table_key(key).name,
        schema=".".join(effective_qualifier(key, definition.db_schema)),
        datasources=tuple(definition.datasource),
        describe_table=target.describe_table,
        columns=columns,
        relationships=tuple(relationships)[:RELATIONSHIP_LIMIT],
        aliases=tuple(aliases)[:_ALIAS_LIMIT],
    )


def _same_table(reference: str, schema_part: str, key: str, name: str, schema: str) -> bool:
    """Whether a relationship entry's table (and optional schema) names the table *key*."""
    text = reference.strip().replace("[", "").replace("]", "").casefold()
    if not text:
        return False
    if "." in text:
        return text == key.casefold() or text == f"{schema}.{name}".casefold()
    if text != name.casefold() and text != key.casefold():
        return False
    return not schema_part.strip() or not schema or schema_part.strip().casefold() == schema.rpartition(".")[2].casefold()


def relationship_notes(
    key: str,
    definition: TableDefinition,
    schema: SchemaConfig,
    listed: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, ...]:
    """Lines naming the relationships of table *key*, at most :data:`RELATIONSHIP_LIMIT`.

    From ``schema.yaml``'s own ``relationships:`` (``A -> B: <join>``) and from
    *listed* entries -- those of ``relationships.yaml`` and the declared ones of
    ``relationships.proposed.yaml`` (``A.col -> B.col``). A relationship is
    in the result when either end is this table; repeats are dropped.

    Parameters
    ----------
    key:
        The table key.
    definition:
        The table's entry.
    schema:
        The validated ``schema.yaml``.
    listed:
        Entries with ``from_table``/``from_schema``/``from_column`` and the
        same for ``to_``; see :func:`read_listed_relationships`.

    Returns
    -------
    tuple[str, ...]

    Examples
    --------
    >>> schema = SchemaConfig.model_validate({
    ...     "tables": {"Order": {"db_schema": "sales"}, "Customer": {"db_schema": "sales"}},
    ...     "relationships": [{"from_table": "Order", "to_table": "Customer",
    ...                        "join_sql": "JOIN Customer c ON o.CustomerID = c.ID"}],
    ... })
    >>> listed = [{"from_table": "Order", "from_schema": "sales", "from_column": "CustomerID",
    ...            "to_table": "Customer", "to_schema": "sales", "to_column": "ID"},
    ...           {"from_table": "Invoice", "from_column": "OrderID", "to_table": "Payment", "to_column": "ID"}]
    >>> for line in relationship_notes("Customer", schema.tables["Customer"], schema, listed):
    ...     print(line)
    Order -> Customer: JOIN Customer c ON o.CustomerID = c.ID
    sales.Order.CustomerID -> sales.Customer.ID
    """
    name = split_table_key(key).name
    where = ".".join(effective_qualifier(key, definition.db_schema))
    lines: list[str] = []
    for rel in schema.relationships:
        if key.casefold() in (rel.from_table.casefold(), rel.to_table.casefold()):
            lines.append(f"{rel.from_table} -> {rel.to_table}: {' '.join(rel.join_sql.split())}")
    for entry in listed:
        ends = []
        for side in ("from", "to"):
            table = str(entry.get(f"{side}_table") or "")
            qualifier = str(entry.get(f"{side}_schema") or "")
            column = str(entry.get(f"{side}_column") or "")
            ends.append((_same_table(table, qualifier, key, name, where), f"{qualifier + '.' if qualifier else ''}{table}.{column}"))
        if any(mine for mine, _ in ends):
            lines.append(f"{ends[0][1]} -> {ends[1][1]}")
    return tuple(dict.fromkeys(lines))[:RELATIONSHIP_LIMIT]


def read_listed_relationships(directory: Path) -> list[dict[str, Any]]:
    """The relationships a project's own files list, for :func:`relationship_notes`.

    ``relationships.yaml`` and ``relationships.proposed.yaml``, keeping the
    entries that are facts: those the database declares (``basis`` says
    *declared foreign key*, a constraint it enforces) and, in
    ``relationships.yaml``, those a person wrote (no ``basis`` at all). A guess
    from column names (any other ``basis``) is left out, and so is every
    entry of the proposals file that is not declared. A file that is missing
    or cannot be read adds nothing.

    Parameters
    ----------
    directory:
        The directory ``schema.yaml`` is in.

    Returns
    -------
    list[dict[str, Any]]
        Entries as written; those that are not mappings are left out.

    Examples
    --------
    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     _ = Path(tmp, "relationships.proposed.yaml").write_text(
    ...         "relationships:\\n"
    ...         "  - {from_table: A, from_column: b_id, to_table: B, to_column: id, basis: declared foreign key FK_A}\\n"
    ...         "  - {from_table: A, from_column: c_id, to_table: C, to_column: id, basis: 'naming convention: c_id'}\\n",
    ...         encoding="utf-8")
    ...     [e["to_table"] for e in read_listed_relationships(Path(tmp))]
    ['B']
    """
    found: list[dict[str, Any]] = []
    for filename, declared_only in (("relationships.yaml", False), ("relationships.proposed.yaml", True)):
        try:
            document = safe_load_strict((directory / filename).read_text(encoding="utf-8-sig")) or {}
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        entries = document.get("relationships") if isinstance(document, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            basis = str(entry.get("basis") or "").lower()
            if (declared_only or basis) and not basis.startswith("declared"):
                continue
            found.append(entry)
    return found


#: The ``entities.yaml`` shipped in ``project_config.example/``. On a first
#: install the descriptions are drafted before the wizard has run, so
#: ``project_config/entities.yaml`` is still this template, and its sample
#: aliases would be offered as facts about any real table with the same key.
SHIPPED_ENTITIES = Path(__file__).resolve().parent.parent / "project_config.example" / "entities.yaml"


def _same_text(path: Path, other: Path) -> bool:
    """Whether two files hold the same text; ``False`` when either cannot be read."""
    try:
        return path.read_bytes() == other.read_bytes()
    except OSError:
        return False


def entity_aliases(directory: Path, template: Path = SHIPPED_ENTITIES) -> dict[str, tuple[str, ...]]:
    """``{table key: aliases}`` from ``entities.yaml`` in *directory*.

    Parameters
    ----------
    directory:
        The configuration directory.
    template:
        The shipped template. An ``entities.yaml`` identical to it holds
        sample entities, not this warehouse's, and gives no alias.

    Returns
    -------
    dict[str, tuple[str, ...]]
        Only entries that have aliases; ``{}`` when the file cannot be read
        or is the unchanged template.

    Raises
    ------
    Nothing. This function never raises.

    Examples
    --------
    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     _ = Path(tmp, "entities.yaml").write_text(
    ...         "entities:\\n  sales.Trade:\\n    table: Trade\\n    aliases: [trade, deal]\\n", encoding="utf-8")
    ...     found = entity_aliases(Path(tmp))
    ...     unchanged = entity_aliases(Path(tmp), template=Path(tmp, "entities.yaml"))
    >>> found, unchanged
    ({'sales.Trade': ('trade', 'deal')}, {})
    """
    if _same_text(directory / "entities.yaml", template):
        return {}
    rows = doctor.read_entities(directory) or ()
    return {row.key: row.aliases for row in rows if row.aliases}


def label_samples(
    table: str,
    columns: Sequence[tuple[str, str]],
    fetch: Callable[[str], Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    """A few safe values of the label columns of one table, for ``--samples``.

    Only text columns are looked at, and :func:`core.value_shapes.label_values`
    decides which of those may be read at all (a label word in the name; never
    a column named like personal or contact data) and which of the values it
    gets may be used (no date, number or code, phone number, address, masked
    value, error text or long text; none from a column of more than 50
    distinct values). At most :data:`SAMPLES_PER_COLUMN` per column and
    :data:`core.value_shapes.VALUES_PER_TABLE` per table.

    Parameters
    ----------
    table:
        The table's bare name.
    columns:
        ``(name, SQL type)`` of the columns to consider.
    fetch:
        ``fetch(column)`` reads the column's distinct values (51 at most).
        It is called only for a column that may be read.

    Returns
    -------
    dict[str, tuple[str, ...]]
        Only columns that have values.

    Examples
    --------
    >>> reads = []
    >>> def fetch(column):
    ...     reads.append(column)
    ...     return ["Open", "09120000001", "Closed"]
    >>> label_samples("Trade", [("Status", "nvarchar(20)"), ("Phone", "varchar(20)"), ("Amount", "int")], fetch)
    {'Status': ('Open', 'Closed')}
    >>> reads
    ['Status']
    """
    found: dict[str, tuple[str, ...]] = {}
    taken = 0
    for name, data_type in columns:
        if taken >= VALUES_PER_TABLE:
            break
        if re.sub(r"\(.*\)", "", data_type).strip().lower() not in ("varchar", "nvarchar", "char", "nchar", "text", "ntext"):
            continue
        values = label_values(table, name, lambda column=name: fetch(column))[:SAMPLES_PER_COLUMN]
        if values:
            found[name] = tuple(values[: VALUES_PER_TABLE - taken])
            taken += len(found[name])
    return found


# ---------------------------------------------------------------------------
# The question
# ---------------------------------------------------------------------------

def build_prompt(facts: TableFacts, language: str = "fa") -> str:
    """The question for one table.

    The model is told what a table and a column description are for, given the
    facts, and asked for JSON: ``{"table": "...", "columns": {"Name": "..."}}``.
    The rules say to describe only what the names, types, keys and
    relationships show, never to invent a business fact, and to end a guess
    with ``(?)``.

    Parameters
    ----------
    facts:
        :func:`table_facts`'s result.
    language:
        ``"fa"`` (Persian) or ``"en"``.

    Returns
    -------
    str

    Raises
    ------
    ValueError
        If *language* is not one of :data:`LANGUAGES`.

    Examples
    --------
    >>> facts = TableFacts("sales.Trade", "Trade", "sales", columns=(ColumnFact("ID", "int", wanted=True),))
    >>> text = build_prompt(facts, "en")
    >>> "Table: sales.Trade" in text and "ID (int)" in text and "(?)" in text
    True
    >>> "Persian" in build_prompt(facts, "fa")
    True
    """
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(LANGUAGES)}, not {language!r}")
    spoken = "Persian (Farsi)" if language == "fa" else "English"
    out = [
        "You write short descriptions of the tables and columns of a SQL Server database. A system "
        f"that answers questions asked in {spoken} matches each question against these descriptions to find "
        "the tables it needs, so a description must say what the table or column holds in the words a user would use.",
        "",
        f"Table: {facts.key}",
    ]
    if facts.schema:
        out.append(f"Schema: {facts.schema}")
    if facts.datasources:
        out.append(f"Data source: {', '.join(facts.datasources)}")
    if facts.aliases:
        out.append(f"Names users already call it by: {', '.join(facts.aliases)}")
    out += ["", "Columns (name, type; a note a person already wrote, if any):"]
    for column in facts.columns:
        line = f"  {column.name}"
        if column.type:
            line += f" ({column.type})"
        if column.note:
            line += f" -- {column.note}"
        if column.samples:
            line += " -- values seen: " + ", ".join(f'"{v}"' for v in column.samples)
        out.append(line)
    if facts.relationships:
        out += ["", "Relationships that name this table (the columns they join are keys):"]
        out += [f"  {line}" for line in facts.relationships]
    out += ["", "Write:"]
    if facts.describe_table:
        out.append('- "table": one sentence saying what one row of this table is.')
    wanted = ", ".join(facts.wanted_columns)
    if wanted:
        out.append(f'- "columns": for each of these columns, and only these, a few words (at most 8) saying what it holds: {wanted}')
    out += [
        "",
        "Rules:",
        "- Describe only what the names, types, keys and relationships show. Do not invent business facts, "
        "units, codes or values.",
        "- When you are guessing, or a name is ambiguous, end the description with (?).",
        f"- Write in {spoken}. Keep table and column names as written, in Latin letters.",
        "- Do not answer with the column name or its type alone.",
        "",
        "Return ONLY this JSON (no markdown):",
        '{"table": "...", "columns": {"ColumnName": "..."}}'
        if facts.describe_table else '{"columns": {"ColumnName": "..."}}',
    ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------

def _clean(value: object, limit: int) -> str:
    """One line of text from a model's value: collapsed spaces, no quotes around it, cut at *limit*."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split()).strip("\"'` ")
    return text[:limit].rstrip()


def _answered_columns(answer: Mapping[str, Any]) -> list[tuple[str, object]]:
    """The ``(name, text)`` pairs of an answer's ``columns``: a mapping, or a list of name/description items."""
    columns = answer.get("columns")
    if isinstance(columns, Mapping):
        return list(columns.items())
    pairs: list[tuple[str, object]] = []
    for item in columns if isinstance(columns, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            pairs.append((item["name"], item.get("description")))
    return pairs


def parse_answer(answer: object, facts: TableFacts) -> TableAnswer:
    """Keep the usable descriptions of a model's *answer* for the table in *facts*.

    The answer is ``{"table": "...", "columns": {"<name>": "..."}}`` (a list of
    ``{"name", "description"}`` items is accepted for the columns too, and
    ``description`` for ``table``). A column name that is not a wanted column of
    the table is ignored -- a name that only differs in letter case is read as
    the real one, and a column that already has a curated note is never
    replaced. A text that says nothing (empty, only the column name, only
    ``<type> column``, a table described by its own name) is ignored. A column
    the model left out is simply not in the result.

    Parameters
    ----------
    answer:
        What ``WizardLLM.generate(..., expect_json=True)`` returned.
    facts:
        The facts the question was built from.

    Returns
    -------
    TableAnswer

    Raises
    ------
    ValueError
        If the answer is not a JSON object, or nothing in it can be used.

    Examples
    --------
    >>> facts = TableFacts("Trade", "Trade", columns=(
    ...     ColumnFact("ID", "int", "Primary key"), ColumnFact("Status", wanted=True), ColumnFact("Amount", wanted=True)))
    >>> got = parse_answer({"table": "One row per trade.", "columns": {
    ...     "status": " Lifecycle state ", "ID": "x", "Nope": "y", "Amount": "Amount"}}, facts)
    >>> got.description, dict(got.columns), got.ignored
    ('One row per trade.', {'Status': 'Lifecycle state'}, 3)
    >>> parse_answer(["not", "an", "object"], facts)
    Traceback (most recent call last):
        ...
    ValueError: the answer is not a JSON object
    """
    if not isinstance(answer, Mapping):
        raise ValueError("the answer is not a JSON object")
    ignored = 0
    description = ""
    if facts.describe_table:
        description = _clean(answer.get("table", answer.get("description")), TABLE_TEXT_LIMIT)
        said_nothing = not description or doctor.is_name_only_description(
            doctor.SchemaTable(facts.key, facts.schema, facts.name, description, (), ())
        ) or doctor.is_placeholder_description(description)
        if said_nothing:
            description = ""
            ignored += 1
    wanted = {name.casefold(): name for name in facts.wanted_columns}
    exact = set(facts.wanted_columns)
    columns: dict[str, str] = {}
    for name, value in _answered_columns(answer):
        real = name if name in exact else wanted.get(str(name).casefold())
        text = _clean(value, COLUMN_TEXT_LIMIT)
        if (
            real is None or real in columns or not text
            or text.casefold() == real.casefold() or doctor.is_type_only_note(text)
        ):
            ignored += 1
            continue
        columns[real] = text
    if not description and not columns:
        raise ValueError("the answer holds no description that can be used")
    ordered = {name: columns[name] for name in facts.wanted_columns if name in columns}
    return TableAnswer(description, ordered, ignored)


def max_tokens_for(columns: int, configured: int, ceiling: int = TOKEN_CEILING) -> int:
    """How many tokens to allow for the answer about a table with *columns* wanted columns.

    A fixed part plus about 60 tokens per column, capped at *ceiling*, and never
    less than the *configured* ``LLM_NUM_PREDICT`` (a model that reasons first
    needs the room; the application's own setting is the floor).

    Examples
    --------
    >>> max_tokens_for(0, 512), max_tokens_for(10, 512), max_tokens_for(500, 512)
    (512, 900, 8192)
    >>> max_tokens_for(500, 512, ceiling=2000), max_tokens_for(5, 16000)
    (2000, 16000)
    """
    wanted = _TOKENS_BASE + _TOKENS_PER_COLUMN * max(columns, 0)
    return max(configured, min(wanted, ceiling))
