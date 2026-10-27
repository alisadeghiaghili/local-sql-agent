# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Recording schema and table exclusions in ``datasources.yaml`` without touching anything else.

``datasources.yaml`` is written by hand and documented in place: the header,
the commented-out alternatives and the note above each source all carry
meaning, and a YAML round trip (``yaml.dump``) would throw every one of them
away. The setup wizard therefore edits the *text*, the way
:mod:`schema_data.yaml_text` edits ``schema.yaml`` (and with its helpers): it
finds the source's own lines by indentation and adds one line, or the items of
the list that is already there. Nothing else changes.

What an edit may do
-------------------
:func:`add_exclude_schemas` adds patterns to the ``exclude_schemas:`` list of a
source, and only that:

* no ``exclude_schemas:`` line yet: one flow-list line is inserted after the
  source's last setting (a commented-out example above or below stays);
* a block list (``- Replay`` lines): the new items are appended in the same style;
* a flow list on one line (``["*_Stg", Replay]  # note``): the items go before
  the closing bracket and the trailing comment stays;
* a pattern already listed (compared in lower case) is not added again.

A layout it cannot edit safely (a source written inline as ``name: {...}``, a
flow list that runs over several lines) is refused with
:class:`DatasourcesEditError`; the caller then shows :func:`exclude_schemas_snippet`
for the operator to paste.

:func:`add_exclude_tables` does the same for the ``exclude_tables:`` list
(patterns on ``schema.table``), with the same layouts and the same refusals.

The result is validated twice before anything is replaced: it must pass
:func:`database.datasources.validate_datasources_yaml_text`, and the parsed
file must equal the original except for the added patterns.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.wizard_drafts import back_up
from database.datasources import validate_datasources_yaml_text
from schema_data.yaml_text import (
    KEY_LINE,
    apply_edits,
    indent_of,
    is_blank_or_comment,
    quote,
    yaml_key,
)

__all__ = [
    "DatasourcesEditError",
    "Recorded",
    "add_exclude_schemas",
    "add_exclude_tables",
    "exclude_schemas_snippet",
    "exclude_tables_snippet",
    "record_exclude_schemas",
    "record_exclude_tables",
]

#: The key :func:`add_exclude_schemas` edits.
_KEY = "exclude_schemas"
#: The key :func:`add_exclude_tables` edits.
_TABLES_KEY = "exclude_tables"

_DATASOURCES_LINE = re.compile(r"^datasources:[ \t]*(?:#.*)?\r?$")


class DatasourcesEditError(ValueError):
    """``datasources.yaml`` cannot be edited; the file is left as it was.

    The message says why and is ready to print.
    """


@dataclass(frozen=True)
class Recorded:
    """What :func:`record_exclude_schemas` did.

    Attributes
    ----------
    path:
        The file.
    backup:
        The copy made before the file was replaced; ``None`` when nothing had
        to be written.
    added:
        ``{source: patterns written}``; empty when every pattern was listed already.
    """

    path: Path
    backup: Path | None
    added: Mapping[str, tuple[str, ...]]


def _snippet(additions: Mapping[str, Sequence[str]], key: str) -> str:
    """The ``datasources:`` fragment that adds *additions* under *key*."""
    lines = ["datasources:"]
    for source, patterns in additions.items():
        lines.append(f"  {yaml_key(source)}:")
        lines.append(f"    {key}: [{', '.join(quote(p) for p in patterns)}]")
    return "\n".join(lines)


def exclude_tables_snippet(additions: Mapping[str, Sequence[str]]) -> str:
    """The lines to add to ``datasources.yaml`` by hand for ``exclude_tables:``.

    Examples
    --------
    >>> print(exclude_tables_snippet({"sales": ["X_Fact.Order", "X_Stg.*_Delete"]}))
    datasources:
      sales:
        exclude_tables: ["X_Fact.Order", "X_Stg.*_Delete"]
    """
    return _snippet(additions, _TABLES_KEY)


def exclude_schemas_snippet(additions: Mapping[str, Sequence[str]]) -> str:
    """The lines to add to ``datasources.yaml`` by hand.

    Parameters
    ----------
    additions:
        ``{source: patterns}``.

    Returns
    -------
    str
        A ``datasources:`` fragment; merge each ``exclude_schemas:`` into the
        source's existing entry.

    Examples
    --------
    >>> print(exclude_schemas_snippet({"sales": ["Scratch", "*_Tmp"]}))
    datasources:
      sales:
        exclude_schemas: ["Scratch", "*_Tmp"]
    """
    return _snippet(additions, _KEY)


def _flow_end(rest: str, key: str = _KEY) -> tuple[int, int]:
    """Where the one-line flow list in *rest* ends: ``(index of "]", items in it)``.

    Raises
    ------
    DatasourcesEditError
        If the list is not closed on this line.
    """
    depth = 0
    items = 0
    try:
        for token in yaml.scan(rest):
            if isinstance(token, yaml.FlowSequenceStartToken):
                depth += 1
            elif isinstance(token, yaml.FlowSequenceEndToken):
                depth -= 1
                if depth == 0:
                    return token.start_mark.index, items
            elif isinstance(token, yaml.ScalarToken) and depth == 1:
                items += 1
    except yaml.YAMLError as exc:
        raise DatasourcesEditError(
            f"`{key}:` is a flow list that does not end on its own line, so it cannot be edited"
        ) from exc
    raise DatasourcesEditError(
        f"`{key}:` is a flow list that does not end on its own line, so it cannot be edited"
    )


def _extend_flow(rest: str, items: Sequence[str], key: str = _KEY) -> str:
    """*rest* (the text after ``exclude_schemas:``) with *items* added inside its brackets."""
    close, existing = _flow_end(rest, key)
    base = rest[:close].rstrip()
    separator = "" if not existing else " " if base.endswith(",") else ", "
    return base + separator + ", ".join(quote(p) for p in items) + rest[close:]


def _sources_block(lines: Sequence[str]) -> tuple[int, int]:
    """``(first line, stop)`` of the body of the top-level ``datasources:`` mapping."""
    start = next((i for i, line in enumerate(lines) if _DATASOURCES_LINE.match(line)), None)
    if start is None:
        raise DatasourcesEditError("no block-style `datasources:` mapping found")
    stop = len(lines)
    for i in range(start + 1, len(lines)):
        if not is_blank_or_comment(lines[i]) and indent_of(lines[i]) == 0:
            stop = i
            break
    return start + 1, stop


def _source_regions(lines: Sequence[str], first: int, stop: int) -> dict[str, tuple[int, int]]:
    """``{source: (its name line, one past its region)}`` for the sources written as ``name:`` lines."""
    body = [i for i in range(first, stop) if not is_blank_or_comment(lines[i])]
    if not body:
        raise DatasourcesEditError("`datasources:` has no sources")
    indent = indent_of(lines[body[0]])
    heads: list[tuple[int, str]] = []
    for i in body:
        if indent_of(lines[i]) != indent:
            continue
        match = KEY_LINE.match(lines[i])
        if match is None:
            continue
        try:
            heads.append((i, str(yaml.safe_load(match.group("key")))))
        except yaml.YAMLError:
            continue
    return {
        name: (at, heads[n + 1][0] if n + 1 < len(heads) else stop)
        for n, (at, name) in enumerate(heads)
    }


def _edit_source(
    lines: Sequence[str], name: str, head: int, end: int, patterns: Sequence[str], eol: str,
    key: str = _KEY,
) -> tuple[int, int, list[str]]:
    """The line edit that adds *patterns* to source *name*, whose region is ``lines[head:end]``."""
    inner = [i for i in range(head + 1, end) if not is_blank_or_comment(lines[i])]
    if not inner:
        raise DatasourcesEditError(f"data source {name!r} has no settings written under its name")
    child = indent_of(lines[inner[0]])
    pattern = re.compile(rf"^(?P<head> {{{child}}}{key}[ \t]*:)(?P<rest>.*)$")
    found = [(i, m) for i in inner if indent_of(lines[i]) == child and (m := pattern.match(lines[i].rstrip("\r")))]
    if not found:
        value = ", ".join(quote(p) for p in patterns)
        return inner[-1] + 1, inner[-1] + 1, [f"{' ' * child}{key}: [{value}]{eol}"]

    entry, match = found[0]
    rest = match.group("rest")
    if rest.strip().startswith("["):
        return entry, entry + 1, [match.group("head") + _extend_flow(rest, patterns, key) + eol]
    if rest.strip() and not rest.strip().startswith("#"):
        raise DatasourcesEditError(f"data source {name!r}: `{key}:` is not a list")

    # A block list: the items are the lines below that start with a dash.
    last = item_indent = None
    for i in range(entry + 1, end):
        if is_blank_or_comment(lines[i]):
            continue
        dash = lines[i].lstrip(" ").startswith("-")
        if indent_of(lines[i]) > child or (indent_of(lines[i]) == child and dash):
            last = i
            if item_indent is None and dash:
                item_indent = indent_of(lines[i])
        else:
            break
    if last is None or item_indent is None:
        raise DatasourcesEditError(f"data source {name!r}: `{key}:` has no list items to extend")
    return last + 1, last + 1, [f"{' ' * item_indent}- {quote(p)}{eol}" for p in patterns]


def _add_patterns(
    text: str, additions: Mapping[str, Sequence[str]], key: str,
) -> tuple[str, dict[str, tuple[str, ...]]]:
    """The body of :func:`add_exclude_schemas` and :func:`add_exclude_tables`, for the list *key*."""
    try:
        before = validate_datasources_yaml_text(text)
    except ValueError as exc:
        raise DatasourcesEditError(f"datasources.yaml is not valid as it stands: {exc}") from exc

    wanted: dict[str, list[str]] = {}
    for source, patterns in additions.items():
        if source not in before.datasources:
            raise DatasourcesEditError(f"unknown data source {source!r}")
        listed = {p.lower() for p in getattr(before.datasources[source], key)}
        fresh: list[str] = []
        for pattern in (p.strip() for p in patterns):
            if pattern and pattern.lower() not in listed:
                listed.add(pattern.lower())
                fresh.append(pattern)
        if fresh:
            wanted[source] = fresh
    if not wanted:
        return text, {}

    lines = text.split("\n")
    eol = "\r" if "\r\n" in text else ""
    first, stop = _sources_block(lines)
    regions = _source_regions(lines, first, stop)
    edits = []
    for source, patterns in wanted.items():
        if source not in regions:
            raise DatasourcesEditError(
                f"data source {source!r} is not written as a `{source}:` block, so it cannot be edited"
            )
        head, end = regions[source]
        edits.append(_edit_source(lines, source, head, end, patterns, eol, key))
    new = "\n".join(apply_edits(lines, edits))

    try:
        after = validate_datasources_yaml_text(new)
    except ValueError as exc:
        raise DatasourcesEditError(f"the edited file would not be valid: {exc}") from exc
    expected = before.model_dump()
    for source, patterns in wanted.items():
        expected["datasources"][source][key] = [*getattr(before.datasources[source], key), *patterns]
    if after.model_dump() != expected:
        raise DatasourcesEditError(f"the edit would change more than the {key} lists")
    return new, {source: tuple(patterns) for source, patterns in wanted.items()}


def add_exclude_tables(
    text: str, additions: Mapping[str, Sequence[str]],
) -> tuple[str, dict[str, tuple[str, ...]]]:
    """*text* with *additions* added to the sources' ``exclude_tables:`` lists.

    The same edit as :func:`add_exclude_schemas`, for the list of table
    patterns (``schema.table``, ``fnmatch``).

    Examples
    --------
    >>> text = "datasources:\\n  sales:\\n    url_env: DB_URL_SALES\\n"
    >>> new, added = add_exclude_tables(text, {"sales": ["X_Fact.Order", "X_Stg.*_Delete"]})
    >>> print(new, end="")
    datasources:
      sales:
        url_env: DB_URL_SALES
        exclude_tables: ["X_Fact.Order", "X_Stg.*_Delete"]
    >>> added
    {'sales': ('X_Fact.Order', 'X_Stg.*_Delete')}
    """
    return _add_patterns(text, additions, _TABLES_KEY)


def add_exclude_schemas(
    text: str, additions: Mapping[str, Sequence[str]],
) -> tuple[str, dict[str, tuple[str, ...]]]:
    """*text* with *additions* added to the sources' ``exclude_schemas:`` lists.

    Parameters
    ----------
    text:
        The contents of ``datasources.yaml``.
    additions:
        ``{source: patterns}``; a pattern the source already lists (compared
        in lower case) is left out, and a source with nothing left is not touched.

    Returns
    -------
    tuple[str, dict[str, tuple[str, ...]]]
        The new text (*text* itself when there is nothing to add) and the
        patterns written per source.

    Raises
    ------
    DatasourcesEditError
        If *text* is not a valid ``datasources.yaml``, a source is unknown, its
        layout cannot be edited line by line, or the edited text does not
        parse to the original plus the added patterns.

    Examples
    --------
    >>> text = (
    ...     "# the warehouse\\n"
    ...     "datasources:\\n"
    ...     "  sales:  # main\\n"
    ...     "    url_env: DB_URL_SALES\\n"
    ...     "    # exclude_schemas: [Replay]\\n"
    ... )
    >>> new, added = add_exclude_schemas(text, {"sales": ["Scratch", "*_Tmp"]})
    >>> print(new, end="")
    # the warehouse
    datasources:
      sales:  # main
        url_env: DB_URL_SALES
        exclude_schemas: ["Scratch", "*_Tmp"]
        # exclude_schemas: [Replay]
    >>> added
    {'sales': ('Scratch', '*_Tmp')}
    >>> again, none = add_exclude_schemas(new, {"sales": ["scratch"]})
    >>> again == new, none
    (True, {})
    """
    return _add_patterns(text, additions, _KEY)


def _record(
    path: Path, additions: Mapping[str, Sequence[str]], stamp: str, key: str,
) -> Recorded:
    """The body of :func:`record_exclude_schemas` and :func:`record_exclude_tables`."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DatasourcesEditError(f"cannot read {path}: {exc}") from exc
    new, added = _add_patterns(text, additions, key)
    if not added:
        return Recorded(path, None, {})
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        (backup,) = back_up([path], stamp)
        temporary.write_bytes(new.encode("utf-8"))
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise DatasourcesEditError(f"cannot write {path}: {exc}") from exc
    return Recorded(path, backup, added)


def record_exclude_tables(
    path: Path, additions: Mapping[str, Sequence[str]], *, stamp: str,
) -> Recorded:
    """Add *additions* to the ``exclude_tables:`` lists of the file at *path*.

    The same careful write as :func:`record_exclude_schemas`: validated first,
    backed up to ``<name>.bak-<stamp>``, replaced atomically, line endings kept.

    Raises
    ------
    DatasourcesEditError
        If the file cannot be edited, or read, copied or written; it is
        untouched then.
    """
    return _record(path, additions, stamp, _TABLES_KEY)


def record_exclude_schemas(
    path: Path, additions: Mapping[str, Sequence[str]], *, stamp: str,
) -> Recorded:
    """Add *additions* to the ``exclude_schemas:`` lists of the file at *path*.

    The new text is built and validated first (:func:`add_exclude_schemas`);
    only then is the file copied to ``<name>.bak-<stamp>`` and replaced, by
    writing a temporary file beside it and renaming it over the original, so
    the file is never left half written. Line endings are kept as they were.

    Parameters
    ----------
    path:
        ``datasources.yaml``.
    additions:
        ``{source: patterns}``.
    stamp:
        ``YYYYmmdd-HHMMSS`` for the backup's name.

    Returns
    -------
    Recorded
        What was written and where the backup is.

    Raises
    ------
    DatasourcesEditError
        If the file cannot be edited, or read, copied or written; it is
        untouched then (the backup, if already made, is kept).
    """
    return _record(path, additions, stamp, _KEY)
