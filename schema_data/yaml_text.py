# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Line-level editing of ``schema.yaml`` text that keeps every comment.

``schema.yaml`` is curated by hand: header comments, section dividers,
trailing notes, quoting and blank lines all carry meaning for the people
who maintain it, and a YAML round trip (``yaml.dump``) would throw every
one of them away. The tools that keep the file in step with the databases
(``scripts/assign_datasources.py``, ``scripts/sync_schema.py``) therefore
edit the *text*: they locate a table, a table's ``columns:`` block or one
of its single-line entries by indentation, and replace or insert whole
lines. Nothing else is touched.

This module is the shared part: finding the ``tables:`` block and each
table's region in it (:func:`parse_tables`), finding a table's child
(:func:`child_span`, :func:`datasource_span`), reading a mapping block's
entries (:func:`parse_entries`), writing a key or value as YAML
(:func:`yaml_key`, :func:`quote`) and applying a set of non-overlapping
line edits (:func:`apply_edits`). It reads and writes text only; it never
looks at a database.

Layouts it can edit are block-style: ``tables:`` as a block mapping, each
table as ``Name:`` followed by an indented body, ``columns:`` as a block
mapping. A flow-style table (``Name: {...}``) or ``columns: {...}`` is
refused with :class:`LayoutError` rather than rewritten.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import yaml

__all__ = [
    "Edit",
    "Entry",
    "LayoutError",
    "TableBlock",
    "TablesLayout",
    "apply_edits",
    "child_span",
    "datasource_span",
    "indent_of",
    "is_blank_or_comment",
    "parse_entries",
    "parse_tables",
    "quote",
    "trailing_comment",
    "yaml_key",
]


class LayoutError(ValueError):
    """``schema.yaml`` is valid but not laid out the way these tools can edit."""


#: ``(first, stop, replacement)``: lines ``[first, stop)`` become *replacement*
#: (an insertion when ``first == stop``).
Edit = tuple[int, int, list[str]]

#: A ``tables:`` entry key line: a plain or quoted key, then ``:`` and
#: nothing but a comment (the entry's body is the lines below it).
KEY_LINE = re.compile(
    r"""^(?P<indent>[ ]*)
        (?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s#'"\[\]{},&*!|>%@`-][^#]*?)
        [ \t]*:[ \t]*(?:\#.*)?\r?$""",
    re.VERBOSE,
)

#: A ``columns:`` / ``column_types:`` entry line: a key, ``:`` and a value
#: (possibly empty, possibly followed by a comment).
ENTRY_LINE = re.compile(
    r"""^(?P<indent>[ ]*)
        (?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s#'"\[\]{},&*!|>%@`-](?:(?!:(?:\s|$))[^#])*?)
        [ \t]*:(?P<rest>(?:[ \t].*)?)\r?$""",
    re.VERBOSE,
)

_TABLES_LINE = re.compile(r"^tables:[ \t]*(?:#.*)?\r?$")
_TRAILING_COMMENT = re.compile(r"(?:^|\s)(#.*)$")
_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_YAML_WORD = re.compile(r"^(?:y|n|yes|no|on|off|true|false|null|~)$", re.IGNORECASE)


def indent_of(line: str) -> int:
    """Number of leading spaces of *line*.

    Examples
    --------
    >>> indent_of("    columns:")
    4
    """
    return len(line) - len(line.lstrip(" "))


def is_blank_or_comment(line: str) -> bool:
    """True for an empty line or one that holds only a comment.

    Examples
    --------
    >>> is_blank_or_comment("   # note"), is_blank_or_comment("  a: 1"), is_blank_or_comment("")
    (True, False, True)
    """
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def trailing_comment(text: str) -> str:
    """The ``# ...`` comment at the end of the YAML fragment *text*, or ``""``.

    Examples
    --------
    >>> trailing_comment(' "x"  # note')
    '# note'
    >>> trailing_comment(' "x"')
    ''
    """
    match = _TRAILING_COMMENT.search(text)
    return match.group(1) if match else ""


def quote(value: str) -> str:
    """*value* as a double-quoted YAML scalar (non-ASCII kept as is).

    Examples
    --------
    >>> quote('int column')
    '"int column"'
    >>> quote('say "hi"')
    '"say \\\\"hi\\\\""'
    """
    return json.dumps(value, ensure_ascii=False)


def yaml_key(name: str) -> str:
    """*name* as a mapping key: bare when that is unambiguous, else quoted.

    Parameters
    ----------
    name:
        A column or table name.

    Returns
    -------
    str
        The name itself for a plain identifier that YAML does not read as
        a boolean, null or number, otherwise a double-quoted scalar.

    Examples
    --------
    >>> yaml_key("CustomerID"), yaml_key("Total Amount"), yaml_key("on"), yaml_key("2024")
    ('CustomerID', '"Total Amount"', '"on"', '"2024"')
    """
    if _PLAIN_KEY.match(name) and not _YAML_WORD.match(name):
        return name
    return quote(name)


def _key_text(raw: str) -> str:
    """The key a quoted or plain key token stands for."""
    raw = raw.strip()
    if raw[:1] in ('"', "'"):
        return str(yaml.safe_load(raw))
    return raw


# ---------------------------------------------------------------------------
# The tables: block
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TableBlock:
    """Where one table sits in the text.

    Attributes
    ----------
    key:
        The table key.
    line:
        Index of the ``Name:`` line.
    end:
        One past the table's region: the next table's line, or the end of
        the ``tables:`` block. Trailing blank and comment lines up to the
        next table are inside it.
    content_end:
        One past the table's last line that is neither blank nor a comment.
    child_indent:
        Indentation of the table's own keys (``description:`` ...).
    """

    key: str
    line: int
    end: int
    content_end: int
    child_indent: int


@dataclass(frozen=True)
class TablesLayout:
    """The ``tables:`` block of a ``schema.yaml``.

    Attributes
    ----------
    start:
        Index of the ``tables:`` line.
    stop:
        One past the block (the next top-level key, or the end of the text).
    key_indent:
        Indentation of the table keys.
    content_end:
        One past the last non-blank, non-comment line of the block (the
        place to append a new table).
    tables:
        Every table found as a ``Name:`` line, in file order.
    """

    start: int
    stop: int
    key_indent: int
    content_end: int
    tables: Mapping[str, TableBlock]


def parse_tables(lines: Sequence[str], expected: Sequence[str]) -> TablesLayout | None:
    """Locate the ``tables:`` block and every table in it.

    Parameters
    ----------
    lines:
        The file split on ``"\\n"``.
    expected:
        The table keys the validated schema has.

    Returns
    -------
    TablesLayout | None
        ``None`` when there is no ``tables:`` line and *expected* is empty.

    Raises
    ------
    LayoutError
        If there is no block-style ``tables:`` mapping but *expected* is
        not empty, or a table in *expected* is not a ``Name:`` line with
        an indented body (a flow-style entry).

    Examples
    --------
    >>> text = "tables:\\n  A:\\n    description: d\\n  B:\\n    description: e\\n"
    >>> layout = parse_tables(text.split("\\n"), ["A", "B"])
    >>> [(t.key, t.line, t.end, t.child_indent) for t in layout.tables.values()]
    [('A', 1, 3, 4), ('B', 3, 6, 4)]
    """
    start = next((i for i, line in enumerate(lines) if _TABLES_LINE.match(line)), None)
    if start is None:
        if expected:
            raise LayoutError("no block-style `tables:` mapping found in schema.yaml")
        return None
    stop = len(lines)
    for i in range(start + 1, len(lines)):
        if not is_blank_or_comment(lines[i]) and indent_of(lines[i]) == 0:
            stop = i
            break
    body = [i for i in range(start + 1, stop) if not is_blank_or_comment(lines[i])]
    key_indent = indent_of(lines[body[0]]) if body else 0
    content_end = (body[-1] + 1) if body else start + 1

    entries: dict[str, int] = {}
    for i in body:
        if indent_of(lines[i]) != key_indent:
            continue
        match = KEY_LINE.match(lines[i])
        if match is None:
            continue
        try:
            key = str(yaml.safe_load(match.group("key")))
        except yaml.YAMLError:
            continue
        entries[key] = i
    unplaced = [key for key in expected if key not in entries]
    if unplaced:
        raise LayoutError(
            "cannot find these tables as `Name:` lines with an indented body "
            f"in schema.yaml (flow-style entries are not edited): {', '.join(unplaced)}"
        )

    ordered = sorted(entries.items(), key=lambda item: item[1])
    tables: dict[str, TableBlock] = {}
    for n, (key, at) in enumerate(ordered):
        end = ordered[n + 1][1] if n + 1 < len(ordered) else stop
        inner = [i for i in range(at + 1, end) if not is_blank_or_comment(lines[i])]
        tables[key] = TableBlock(
            key=key,
            line=at,
            end=end,
            content_end=(inner[-1] + 1) if inner else at + 1,
            child_indent=indent_of(lines[inner[0]]) if inner else key_indent + 2,
        )
    return TablesLayout(
        start=start, stop=stop, key_indent=key_indent, content_end=content_end, tables=tables,
    )


# ---------------------------------------------------------------------------
# One table's children
# ---------------------------------------------------------------------------

def datasource_span(lines: Sequence[str], start: int, end: int, indent: int) -> tuple[int, int] | None:
    """The ``datasource:`` entry among ``lines[start:end]``: ``(first, stop)``.

    ``first`` is the entry's own line; ``stop`` is one past its last line,
    which extends over a multi-line list (a block list, or a flow list
    whose ``]`` is on a later line).

    Examples
    --------
    >>> datasource_span(["  datasource: [a,", "    b]", "  x: 1"], 0, 3, 2)
    (0, 2)
    >>> datasource_span(["  x: 1"], 0, 1, 2) is None
    True
    """
    entry = re.compile(rf"^ {{{indent}}}datasource[ \t]*:(?P<rest>.*)$")
    for first in range(start, end):
        match = entry.match(lines[first].rstrip("\r"))
        if match is None:
            continue
        rest = _TRAILING_COMMENT.sub("", match.group("rest")).strip()
        stop = first + 1
        if rest.startswith("[") and "]" not in rest:
            while stop < end and "]" not in lines[stop - 1]:
                stop += 1
        elif not rest:
            while stop < end:
                nxt = lines[stop]
                if is_blank_or_comment(nxt):
                    break
                if indent_of(nxt) > indent or (
                    indent_of(nxt) == indent and nxt.lstrip().startswith("- ")
                ):
                    stop += 1
                else:
                    break
        return first, stop
    return None


def child_span(
    lines: Sequence[str], block: TableBlock, name: str,
) -> tuple[int, int] | None:
    """The block-mapping child *name* of *block*: ``(line, content_end)``.

    Parameters
    ----------
    lines:
        The file split on ``"\\n"``.
    block:
        A table from :func:`parse_tables`.
    name:
        A plain key such as ``columns`` or ``column_types``.

    Returns
    -------
    tuple[int, int] | None
        The index of the ``name:`` line and one past the last non-blank,
        non-comment line that belongs to it; ``None`` when the table has no
        such child.

    Raises
    ------
    LayoutError
        If the child is written inline (``columns: {A: x}``), which cannot
        be edited line by line.

    Examples
    --------
    >>> text = ["T:", "  columns:", "    A: x", "    B: y", "  description: d"]
    >>> block = TableBlock("T", 0, 5, 5, 2)
    >>> child_span(text, block, "columns")
    (1, 4)
    >>> child_span(text, block, "column_types") is None
    True
    """
    pattern = re.compile(rf"^ {{{block.child_indent}}}{re.escape(name)}[ \t]*:(?P<rest>.*)$")
    for first in range(block.line + 1, block.end):
        match = pattern.match(lines[first].rstrip("\r"))
        if match is None:
            continue
        rest = _TRAILING_COMMENT.sub("", match.group("rest")).strip()
        if rest:
            raise LayoutError(
                f"table {block.key!r}: `{name}:` is written inline; only a block "
                "mapping (one column per line) can be edited"
            )
        stop = first + 1
        last = first + 1
        while stop < block.end:
            line = lines[stop]
            if not is_blank_or_comment(line):
                if indent_of(line) <= block.child_indent:
                    break
                last = stop + 1
            stop += 1
        return first, last
    return None


@dataclass(frozen=True)
class Entry:
    """One ``key: value`` line of a ``columns:`` or ``column_types:`` block.

    Attributes
    ----------
    name:
        The key (``ID``).
    line:
        Index of the line.
    stop:
        One past the entry's last line (continuation lines of a multi-line
        value included, trailing comments and blank lines not).
    indent:
        Indentation of the key.
    rest:
        The text after the colon (value and comment).
    """

    name: str
    line: int
    stop: int
    indent: int
    rest: str


def parse_entries(lines: Sequence[str], first: int, stop: int) -> list[Entry]:
    """The entries of the block mapping whose header is ``lines[first]``.

    Parameters
    ----------
    lines:
        The file split on ``"\\n"``.
    first:
        Index of the ``columns:`` line.
    stop:
        One past its content (:func:`child_span`'s second value).

    Returns
    -------
    list[Entry]
        In file order. Lines deeper than the entries (continuation lines)
        belong to the entry above them.

    Examples
    --------
    >>> lines = ["  columns:", "    A: x  # n", "    # c", '    "B C": "y', '      z"']
    >>> [(e.name, e.line, e.stop) for e in parse_entries(lines, 0, 5)]
    [('A', 1, 2), ('B C', 3, 5)]
    """
    body = [i for i in range(first + 1, stop) if not is_blank_or_comment(lines[i])]
    if not body:
        return []
    indent = indent_of(lines[body[0]])
    heads: list[tuple[int, str, str]] = []
    for i in body:
        if indent_of(lines[i]) != indent:
            continue
        match = ENTRY_LINE.match(lines[i])
        if match is None:
            continue
        heads.append((i, _key_text(match.group("key")), match.group("rest")))
    entries: list[Entry] = []
    for n, (at, name, rest) in enumerate(heads):
        limit = heads[n + 1][0] if n + 1 < len(heads) else stop
        inner = [i for i in range(at, limit) if not is_blank_or_comment(lines[i])]
        entries.append(Entry(name=name, line=at, stop=inner[-1] + 1, indent=indent, rest=rest))
    return entries


# ---------------------------------------------------------------------------
# Applying edits
# ---------------------------------------------------------------------------

def apply_edits(lines: list[str], edits: Sequence[Edit]) -> list[str]:
    """Apply non-overlapping line edits to *lines*; returns the new list.

    Insertions at the same index keep the order they are given in.

    Parameters
    ----------
    lines:
        The original lines (not modified).
    edits:
        ``(first, stop, replacement)`` triples; ranges must not overlap.

    Returns
    -------
    list[str]

    Raises
    ------
    LayoutError
        If two edits overlap (a bug in the caller, never silently merged).

    Examples
    --------
    >>> apply_edits(["a", "b", "c"], [(1, 2, ["B"]), (3, 3, ["d"]), (0, 0, ["z"]), (0, 0, ["y"])])
    ['z', 'y', 'a', 'B', 'c', 'd']
    """
    indexed = sorted(enumerate(edits), key=lambda item: (item[1][0], item[1][1], item[0]))
    out = list(lines)
    previous_stop = -1
    for _, (first, stop, _) in indexed:
        if first < previous_stop:
            raise LayoutError("internal error: two edits to schema.yaml overlap")
        previous_stop = max(previous_stop, stop)
    for _, (first, stop, replacement) in reversed(indexed):
        out[first:stop] = replacement
    return out
