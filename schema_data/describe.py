# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Find the descriptions ``schema.yaml`` lacks, and write drafts of them into its text.

A first install has no curated ``schema.yaml``: the inspector and
``scripts/sync_schema.py --add-tables`` describe a table by its own name
(``Sales_Fact.Trade``) and a column by its type (``bigint column``), and the
preflight rightly refuses that (``core.config_doctor``). ``scripts/draft_descriptions.py``
asks a model for the descriptions; this module is the part that needs neither
a model nor a database, so it can be tested on its own:

* :func:`find_targets` names, for every table, whether its description and
  which of its column descriptions are still a draft (empty, the inspector's
  placeholder, only the table's name, only ``<type> column``), using the
  detectors of :mod:`core.config_doctor`;
* :func:`render_described_text` puts the drafted descriptions into the text of
  ``schema.yaml`` line by line, the way :mod:`schema_data.sync` puts structure
  into it: every other line, comment, key, flag and the order of everything
  stay exactly as they were. A drafted line ends with ``# REVIEW`` and the file
  starts with a header :func:`core.config_doctor.find_draft_header_kinds`
  recognises, so the preflight fails on the file until somebody has read it
  and deleted the header line;
* :func:`verify_described` parses the result and proves it is the original plus
  exactly the drafted descriptions.

Public API
----------
REVIEW_COMMENT, HEADER_LINE
    The marker at the end of a drafted line, and the first line of the file.
TableTarget
    One table and what of it needs a description.
TableDraft
    The descriptions drafted for one table.
find_targets(text) -> list[TableTarget]
render_described_text(original, schema, drafts) -> str
verify_described(original, rendered, drafts) -> None
carried_drafts(original, previous) -> dict[str, TableDraft]
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from core import config_doctor as doctor
from schema_data.registry import SchemaConfig, validate_schema_yaml_text
from schema_data.yaml_text import (
    Edit,
    LayoutError,
    TableBlock,
    apply_edits,
    child_span,
    indent_of,
    is_blank_or_comment,
    parse_entries,
    parse_tables,
    quote,
)

__all__ = [
    "HEADER_LINE",
    "REVIEW_COMMENT",
    "TableDraft",
    "TableTarget",
    "carried_drafts",
    "find_targets",
    "render_described_text",
    "verify_described",
]

#: The marker at the end of every line this module writes; the project's own
#: word for "a person has not read this yet" (``aliases.yaml``, ``relationships.yaml``).
REVIEW_COMMENT = "# REVIEW"

#: First line of the file. It carries :data:`core.config_doctor.DESCRIBE_DRAFT_HEADER`.
HEADER_LINE = (
    f"# {doctor.DESCRIBE_DRAFT_HEADER} \u2014 descriptions drafted by a model from names, types "
    f"and keys; read every line marked REVIEW, then delete this line"
)

#: What ``sync_schema.py`` leaves after a description it wrote as a draft; once
#: the description is drafted the note has been answered.
_FILL_ME = "# TO BE FILLED"

_DESCRIPTION_KEY = "description"

#: The text after ``key:`` -- a quoted or plain value, then an optional comment.
#: A ``#`` inside quotes belongs to the value, not to a comment.
_VALUE_AND_COMMENT = re.compile(
    r"""^\s*(?:"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|(?:(?!\s\#).)*?)\s*(?P<comment>\#.*)?$"""
)


def _comment_of(rest: str) -> str:
    """The ``# ...`` comment at the end of the text after ``key:``, or ``""``.

    >>> _comment_of(' "a # b"  # note'), _comment_of(" plain # note"), _comment_of(' "x"')
    ('# note', '# note', '')
    """
    match = _VALUE_AND_COMMENT.match(rest.rstrip("\r"))
    return (match.group("comment") or "") if match else ""


@dataclass(frozen=True)
class TableTarget:
    """What of one table of ``schema.yaml`` still needs a description.

    Attributes
    ----------
    key:
        The table key as written.
    describe_table:
        The table's own description is empty, the inspector's placeholder, or
        only the table's name.
    columns:
        The columns whose description is empty or only ``<type> column``, in
        file order. ``()`` for a table without a ``columns:`` map.
    """

    key: str
    describe_table: bool
    columns: tuple[str, ...] = ()

    @property
    def wanted(self) -> int:
        """How many descriptions are wanted: the table's (0 or 1) and its columns'."""
        return int(self.describe_table) + len(self.columns)


@dataclass(frozen=True)
class TableDraft:
    """The descriptions drafted for one table; what is not here stays as it was.

    Attributes
    ----------
    key:
        The table key.
    description:
        The table's description, or ``""`` to leave it.
    columns:
        ``{column: description}`` for the columns that got one.
    """

    key: str
    description: str = ""
    columns: Mapping[str, str] = field(default_factory=dict)

    @property
    def drafted(self) -> int:
        """How many descriptions this draft holds."""
        return int(bool(self.description)) + len(self.columns)


def _needs_column_description(note: str) -> bool:
    return not note.strip() or doctor.is_type_only_note(note)


def find_targets(text: str) -> list[TableTarget]:
    """The tables of a ``schema.yaml`` *text* that have a description to draft.

    A table description is wanted when it is empty or missing, the inspector's
    placeholder (:func:`core.config_doctor.is_placeholder_description`) or only
    the table's own name (:func:`core.config_doctor.is_name_only_description`);
    a column description when it is empty or only ``<type> column``
    (:func:`core.config_doctor.is_type_only_note`). Every other description is
    curated and is not a target. A table with nothing to describe is not in
    the result; neither is one a person described, whatever its columns say.

    Parameters
    ----------
    text:
        The ``schema.yaml`` text, byte-order mark removed.

    Returns
    -------
    list[TableTarget]
        In file order.

    Raises
    ------
    ValueError
        If *text* is not a valid ``schema.yaml``.

    Examples
    --------
    >>> text = (
    ...     "tables:\\n"
    ...     "  sales.Trade:\\n"
    ...     "    description: 'sales.Trade'\\n"
    ...     "    columns:\\n"
    ...     "      ID: 'bigint column'\\n"
    ...     "      Status: 'Lifecycle state of the trade'\\n"
    ...     "  ref.Bank:\\n"
    ...     "    description: 'Banks we trade with'\\n"
    ...     "    columns:\\n"
    ...     "      ID: 'Primary key'\\n"
    ... )
    >>> find_targets(text)
    [TableTarget(key='sales.Trade', describe_table=True, columns=('ID',))]
    """
    schema = validate_schema_yaml_text(text)
    rows = {row.key: row for row in doctor.read_schema_text(text).tables}
    targets: list[TableTarget] = []
    for key, definition in schema.tables.items():
        row = rows.get(key) or doctor.SchemaTable(
            key, definition.db_schema, key, definition.description, (), (),
        )
        describe_table = (
            not row.description.strip()
            or doctor.is_placeholder_description(row.description)
            or doctor.is_name_only_description(row)
        )
        columns = tuple(
            name for name, note in (definition.columns or {}).items() if _needs_column_description(note)
        )
        if describe_table or columns:
            targets.append(TableTarget(key, describe_table, columns))
    return targets


def _review_comment(old_comment: str) -> str:
    """The comment a drafted line ends with, given the comment it had.

    A ``# TO BE FILLED`` note is answered by the draft and goes; a line that
    already ends in ``# REVIEW`` stays as it is; any other comment is kept
    after the marker.

    >>> _review_comment(""), _review_comment("# TO BE FILLED (added by sync_schema.py)")
    ('# REVIEW', '# REVIEW')
    >>> _review_comment("# keep"), _review_comment("# REVIEW  # keep")
    ('# REVIEW  # keep', '# REVIEW  # keep')
    """
    if not old_comment or old_comment.startswith(_FILL_ME):
        return REVIEW_COMMENT
    if old_comment.startswith(REVIEW_COMMENT):
        return old_comment
    return f"{REVIEW_COMMENT}  {old_comment}"


def _description_span(lines: Sequence[str], block: TableBlock) -> tuple[int, int] | None:
    """The table's ``description:`` entry among its lines: ``(first, stop)``, or ``None``."""
    pattern = re.compile(rf"^ {{{block.child_indent}}}{_DESCRIPTION_KEY}[ \t]*:")
    for first in range(block.line + 1, block.end):
        if not pattern.match(lines[first].rstrip("\r")):
            continue
        stop = first + 1
        while stop < block.end and not is_blank_or_comment(lines[stop]) and indent_of(lines[stop]) > block.child_indent:
            stop += 1
        return first, stop
    return None


def _description_edit(lines: Sequence[str], block: TableBlock, text: str, cr: str) -> Edit:
    """The edit that gives the table the description *text*."""
    span = _description_span(lines, block)
    if span is None:
        # No description line at all: add one before the table's first key,
        # below any comment that opens the table's body.
        at = next(
            (i for i in range(block.line + 1, block.end) if not is_blank_or_comment(lines[i])),
            block.line + 1,
        )
        pad = " " * block.child_indent
        return at, at, [f"{pad}{_DESCRIPTION_KEY}: {quote(text)}  {REVIEW_COMMENT}{cr}"]
    first, stop = span
    raw = lines[first].rstrip("\r")
    head = raw[: raw.index(":") + 1]
    comment = _comment_of(raw[len(head):]) if stop == first + 1 else ""
    return first, stop, [f"{head} {quote(text)}  {_review_comment(comment)}{cr}"]


def _column_edits(lines: Sequence[str], block: TableBlock, draft: TableDraft, cr: str) -> list[Edit]:
    """The edits that give the columns of *draft* their descriptions."""
    span = child_span(lines, block, "columns")
    if span is None:
        raise LayoutError(f"table {block.key!r}: cannot find its `columns:` block")
    entries = {entry.name: entry for entry in parse_entries(lines, *span)}
    edits: list[Edit] = []
    for name, text in draft.columns.items():
        entry = entries.get(name)
        if entry is None:
            raise LayoutError(
                f"table {block.key!r}: cannot read the column {name!r} line by line "
                "(check for duplicate or oddly quoted column names)"
            )
        raw = lines[entry.line].rstrip("\r")
        head = raw[: len(raw) - len(entry.rest)]
        comment = _comment_of(entry.rest) if entry.stop == entry.line + 1 else ""
        edits.append((entry.line, entry.stop, [f"{head} {quote(text)}  {_review_comment(comment)}{cr}"]))
    return edits


def render_described_text(
    original: str, schema: SchemaConfig, drafts: Sequence[TableDraft],
) -> str:
    """*original* ``schema.yaml`` text with the drafted descriptions written in.

    Only the description line of a table and the lines of the columns named in
    *drafts* change; each ends with ``# REVIEW``. A table that has no
    ``description:`` line gets one. Line endings, a missing final newline,
    indentation, key spelling, comments and ordering are preserved. The file
    starts with :data:`HEADER_LINE` (unless it already carries the header).

    Parameters
    ----------
    original:
        The ``schema.yaml`` text, byte-order mark removed.
    schema:
        *original*, validated.
    drafts:
        What to write. A draft for a table *schema* does not have is an error.

    Returns
    -------
    str
        The new text.

    Raises
    ------
    LayoutError
        If a table or its ``columns:`` cannot be edited line by line
        (flow-style entries), or a drafted column is not a line of it.

    Examples
    --------
    >>> text = "tables:\\n  T:\\n    description: 'T'  # TO BE FILLED\\n    columns:\\n      ID: 'int column'\\n"
    >>> schema = validate_schema_yaml_text(text)
    >>> drafted = render_described_text(text, schema, [TableDraft("T", "One row per thing", {"ID": "Row id"})])
    >>> print(drafted.split("\\n", 1)[1], end="")
    tables:
      T:
        description: "One row per thing"  # REVIEW
        columns:
          ID: "Row id"  # REVIEW
    """
    lines = original.split("\n")
    cr = "\r" if "\r\n" in original else ""
    layout = parse_tables(lines, list(schema.tables))
    edits: list[Edit] = []
    for draft in drafts:
        if layout is None or draft.key not in layout.tables:
            raise LayoutError(f"table {draft.key!r} is not in schema.yaml")
        block = layout.tables[draft.key]
        if draft.description:
            edits.append(_description_edit(lines, block, draft.description, cr))
        if draft.columns:
            edits.extend(_column_edits(lines, block, draft, cr))
    out = apply_edits(lines, edits)
    if not any(doctor.DESCRIBE_DRAFT_HEADER in line for line in out[:10]):
        out.insert(0, HEADER_LINE + cr)
    return "\n".join(out)


def verify_described(
    original: SchemaConfig, rendered: SchemaConfig, drafts: Sequence[TableDraft],
) -> None:
    """Raise unless *rendered* is *original* plus exactly the drafted descriptions.

    Compared as parsed models, table by table: the same tables in the same
    order, the same columns in the same order, and every field equal except the
    descriptions in *drafts*. The ``relationships:`` list must be unchanged.

    Parameters
    ----------
    original, rendered:
        The validated input and output.
    drafts:
        What was meant to change.

    Raises
    ------
    ValueError
        Naming the table (and column) whose content differs from the expected one.

    Examples
    --------
    >>> text = "tables:\\n  T:\\n    columns:\\n      ID: 'int column'\\n"
    >>> before = validate_schema_yaml_text(text)
    >>> draft = [TableDraft("T", "", {"ID": "Row id"})]
    >>> after = validate_schema_yaml_text(render_described_text(text, before, draft))
    >>> verify_described(before, after, draft)
    >>> verify_described(before, after, [])
    Traceback (most recent call last):
        ...
    ValueError: the described file changed the column 'ID' of table 'T' unexpectedly
    """
    wanted = {draft.key: draft for draft in drafts}
    if list(rendered.tables) != list(original.tables):
        raise ValueError("the described file lists different tables than the original")
    for key, table in rendered.tables.items():
        want = original.tables[key].model_dump()
        draft = wanted.get(key)
        if draft is not None:
            if draft.description:
                want["description"] = draft.description
            if draft.columns:
                columns = dict(want["columns"] or {})
                columns.update(draft.columns)
                want["columns"] = columns
        got = table.model_dump()
        for name, expected in want.items():
            if name == "columns":
                continue
            if got[name] != expected:
                raise ValueError(f"the described file changed {name!r} of table {key!r} unexpectedly")
        if (got["columns"] is None) != (want["columns"] is None):
            raise ValueError(f"the described file changed the columns of table {key!r} unexpectedly")
        if got["columns"] is not None:
            if list(got["columns"]) != list(want["columns"]):
                raise ValueError(f"the described file reordered the columns of table {key!r}")
            for column, note in got["columns"].items():
                if note != want["columns"][column]:
                    raise ValueError(
                        f"the described file changed the column {column!r} of table {key!r} unexpectedly"
                    )
    if rendered.relationships != original.relationships:
        raise ValueError("the described file changed schema.yaml's relationships")


def carried_drafts(original: str, previous: str) -> dict[str, TableDraft]:
    """The drafts an earlier run left in *previous*, for what *original* still wants.

    ``--resume`` of ``scripts/draft_descriptions.py``: a table counts as done
    when *previous* (an earlier ``schema.described.yaml``, possibly edited by
    a person since) has nothing left to describe in it for what *original*
    asked about it. Its descriptions are taken from *previous* as they are
    there now. A table that is only partly done is not carried; it is asked
    again, whole.

    Parameters
    ----------
    original:
        The input ``schema.yaml`` text.
    previous:
        The earlier output text.

    Returns
    -------
    dict[str, TableDraft]
        By table key; only tables in both files.

    Raises
    ------
    ValueError
        If either text is not a valid ``schema.yaml``.

    Examples
    --------
    >>> before = (
    ...     "tables:\\n  A:\\n    description: Things\\n    columns:\\n      ID: 'int column'\\n"
    ...     "  B:\\n    description: Others\\n    columns:\\n      ID: 'int column'\\n"
    ... )
    >>> earlier = before.replace("ID: 'int column'", "ID: 'Row id'", 1)
    >>> carried_drafts(before, earlier)
    {'A': TableDraft(key='A', description='', columns={'ID': 'Row id'})}
    """
    wanted = {target.key: target for target in find_targets(original)}
    schema = validate_schema_yaml_text(previous)
    still = {target.key for target in find_targets(previous)}
    carried: dict[str, TableDraft] = {}
    for key, target in wanted.items():
        table = schema.tables.get(key)
        if table is None or key in still:
            continue
        carried[key] = TableDraft(
            key,
            table.description if target.describe_table else "",
            {name: (table.columns or {})[name] for name in target.columns if name in (table.columns or {})},
        )
    return carried
