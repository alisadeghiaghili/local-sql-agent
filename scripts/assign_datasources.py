# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Work out which data source each ``schema.yaml`` table lives in.

Usage (from the repo root, with the server's environment active)::

    python scripts/assign_datasources.py
    python scripts/assign_datasources.py --output /tmp/schema.proposed.yaml
    python scripts/assign_datasources.py --check

With more than one data source (``project_config/datasources.yaml``) every
table in ``schema.yaml`` says where it lives with ``datasource:``; a table
that says nothing runs on the default source, and fails there when it is
really in another database (``Invalid object name ...``). Writing those
lines for a few dozen tables by hand is the part this script does once, so
the file can then be curated rather than typed.

It connects to every configured source through the application's own
engines (:func:`database.connection.get_engine`), lists the tables, views
and columns each has (two ``INFORMATION_SCHEMA`` queries, see
:mod:`database.catalogue`) and matches every ``schema.yaml`` table against
them: by schema and name, ignoring case, with a multi-part ``db_schema``
(``OtherDb.dbo``) matched on its last part. It never writes to a
database, never reads a row, and prints names only, no credentials.

Default mode
------------
Writes ``schema.with_datasources.yaml`` next to ``schema.yaml`` (or the
path given with ``--output``; ``schema.yaml`` itself is never
overwritten). The file is ``schema.yaml`` with every line and comment kept
and one ``datasource:`` line under each table key, inserted, or replacing
an existing ``datasource:`` entry of that table:

* found in one source: ``datasource: Auction_DM``;
* found in several (a replicated date dimension, say): ``datasource:
  [Auction_DM, Future_DM]``, in ``datasources.yaml`` order;
* found in none: the table is left as it is and the comment ``# not found
  in any data source`` is added directly under its key.

A table whose ``datasource:`` already equals what was found is left
untouched. The script checks its own output with
:func:`schema_data.registry.validate_schema_yaml_text` and refuses to
write a file that does not validate, or that differs from the original in
anything but ``datasource``. Review it, then replace ``schema.yaml``.

The report
----------
Printed in every mode: the tables found only in each source, the shared
tables, the tables found nowhere, the tables whose current ``datasource:``
disagrees with what was found, and -- for found tables -- the columns
``schema.yaml`` lists that the database does not have. (A column the
read-only login may not see through ``INFORMATION_SCHEMA`` is reported as
missing too: check ``docs/db-hardening.md`` ``DENY`` grants before deleting
a column from ``schema.yaml``.)

``--check``
-----------
Writes nothing; exits 1 when any table's ``datasource:`` (the default
source for a table that has none) is not the set of sources it was found
in. For CI and deploy pipelines.

Exit codes
----------
0 -- done (``--check``: every assignment matches); 1 -- ``--check`` found a
disagreement; 2 -- nothing could be decided or written: a source's
catalogue could not be read (a source that cannot be listed would make a
shared table look single, so the run stops), ``schema.yaml`` is invalid or
written in a layout this script cannot edit, the output would not
validate, or ``--output`` names ``schema.yaml``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

# Run as `python scripts/assign_datasources.py` from the repo root: Python
# puts only this script's own directory on sys.path, so the repo root is
# added for `import config` and the packages below -- same as
# scripts/verify_deployment.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg  # noqa: F401,E402 - loads .env, like every entry point
from database.catalogue import list_columns, list_tables, table_location  # noqa: E402
from database.datasources import datasource_names, default_datasource_name  # noqa: E402
from schema_data.registry import (  # noqa: E402
    SchemaConfig,
    schema_yaml_path,
    validate_schema_yaml_text,
)

__all__ = [
    "EXIT_CHECK_FAILED",
    "EXIT_ERROR",
    "EXIT_OK",
    "LayoutError",
    "NOT_FOUND_COMMENT",
    "Placement",
    "SourceCatalogue",
    "build_report",
    "default_catalogue_loader",
    "locate_tables",
    "main",
    "render_schema_yaml",
]

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_ERROR = 2

#: Name of the proposed file written next to ``schema.yaml``.
OUTPUT_FILENAME = "schema.with_datasources.yaml"

#: The comment put under the key of a table found in no data source.
NOT_FOUND_COMMENT = "# not found in any data source"

#: ``{(schema, table): {column, ...}}``, all lower-cased: one source's
#: tables and views with their columns.
SourceCatalogue = Mapping[tuple[str, str], frozenset[str]]

#: ``source name -> its catalogue``; raises when the source cannot be read.
CatalogueLoader = Callable[[str], SourceCatalogue]

#: Schema used for a table with no qualifier at all (SQL Server).
_DEFAULT_SCHEMA = "dbo"


class LayoutError(ValueError):
    """``schema.yaml`` is valid but not laid out the way this script can edit."""


@dataclass(frozen=True)
class Placement:
    """Where one ``schema.yaml`` table was found.

    Attributes
    ----------
    key:
        The table key as written under ``tables:``.
    found_in:
        Every source whose catalogue has the table, in ``datasources.yaml``
        order; empty when it is nowhere.
    current:
        The table's ``datasource:`` as written (``()`` when it has none).
    effective:
        *current*, or ``(default,)`` when the table has none.
    missing_columns:
        ``{source: columns listed in schema.yaml that the source's table
        does not have}``, only for sources where the table was found and
        only where something is missing.
    """

    key: str
    found_in: tuple[str, ...]
    current: tuple[str, ...]
    effective: tuple[str, ...]
    missing_columns: Mapping[str, tuple[str, ...]]

    @property
    def disagrees(self) -> bool:
        """Found somewhere, and not in exactly the sources it is assigned to."""
        return bool(self.found_in) and set(self.found_in) != set(self.effective)


# ---------------------------------------------------------------------------
# Reading the catalogues and matching tables
# ---------------------------------------------------------------------------

def default_catalogue_loader(source: str) -> SourceCatalogue:
    """Read *source*'s tables and columns through the application's engine.

    Two ``INFORMATION_SCHEMA`` queries (:func:`database.catalogue.list_tables`,
    :func:`database.catalogue.list_columns`), nothing else.

    Parameters
    ----------
    source:
        A configured data source name.

    Returns
    -------
    SourceCatalogue
        Every table and view with its columns.
    """
    from database.connection import get_engine

    engine = get_engine(source)
    tables = list_tables(engine)
    columns = list_columns(engine)
    return {location: columns.get(location, frozenset()) for location in tables}


def locate_tables(
    schema: SchemaConfig,
    sources: Sequence[str],
    default: str,
    catalogues: Mapping[str, SourceCatalogue],
) -> list[Placement]:
    """Match every ``schema.yaml`` table against every source's catalogue.

    Parameters
    ----------
    schema:
        The validated ``schema.yaml``.
    sources:
        Source names in ``datasources.yaml`` order (default first).
    default:
        The default source, where a table with no ``datasource:`` lives.
    catalogues:
        ``{source: catalogue}`` for every name in *sources*.

    Returns
    -------
    list[Placement]
        One per table, in ``schema.yaml`` order.

    Examples
    --------
    >>> from schema_data.registry import validate_schema_yaml_text
    >>> schema = validate_schema_yaml_text(
    ...     "tables:\\n  Date:\\n    db_schema: dim\\n    columns: {ID: x, Gone: y}\\n"
    ... )
    >>> found = {("dim", "date"): frozenset({"id"})}
    >>> [(p.key, p.found_in, dict(p.missing_columns)) for p in locate_tables(
    ...     schema, ["a", "b"], "a", {"a": found, "b": {}})]
    [('Date', ('a',), {'a': ('Gone',)})]
    """
    placements: list[Placement] = []
    for key, table in schema.tables.items():
        location = table_location(key, table.db_schema, _DEFAULT_SCHEMA)
        found = tuple(name for name in sources if location in catalogues[name])
        missing: dict[str, tuple[str, ...]] = {}
        if table.columns:
            for name in found:
                have = catalogues[name][location]
                gone = tuple(col for col in table.columns if col.lower() not in have)
                if gone:
                    missing[name] = gone
        placements.append(Placement(
            key=key,
            found_in=found,
            current=table.datasource,
            effective=table.datasource or (default,),
            missing_columns=missing,
        ))
    return placements


# ---------------------------------------------------------------------------
# Editing schema.yaml line by line
# ---------------------------------------------------------------------------

#: A ``tables:`` entry key line: a plain or quoted key, then ``:`` and
#: nothing but a comment (the entry's body is the lines below it).
_KEY_LINE = re.compile(
    r"""^(?P<indent>[ ]*)
        (?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s#'"\[\]{},&*!|>%@`-][^#]*?)
        [ \t]*:[ \t]*(?:\#.*)?\r?$""",
    re.VERBOSE,
)
_TABLES_LINE = re.compile(r"^tables:[ \t]*(?:#.*)?\r?$")
_TRAILING_COMMENT = re.compile(r"(?:^|\s)(#.*)$")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _format_value(sources: Sequence[str]) -> str:
    return sources[0] if len(sources) == 1 else "[" + ", ".join(sources) + "]"


def _datasource_span(lines: list[str], start: int, end: int, indent: int) -> tuple[int, int] | None:
    """The ``datasource:`` entry among ``lines[start:end]``: ``(first, stop)``.

    ``first`` is the entry's own line; ``stop`` is one past its last line,
    which extends over a multi-line list (a block list, or a flow list
    whose ``]`` is on a later line).
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
                if _is_blank_or_comment(nxt):
                    break
                if _indent_of(nxt) > indent or (
                    _indent_of(nxt) == indent and nxt.lstrip().startswith("- ")
                ):
                    stop += 1
                else:
                    break
        return first, stop
    return None


def render_schema_yaml(
    original: str, schema: SchemaConfig, placements: Sequence[Placement],
) -> str:
    """*original* ``schema.yaml`` text with each table's ``datasource:`` set.

    Every original line and comment is kept; lines are only inserted, or a
    table's ``datasource:`` entry replaced in place (keeping a trailing
    comment on it). Line endings, a missing final newline and the file's
    own indentation are preserved.

    Parameters
    ----------
    original:
        The ``schema.yaml`` text, byte-order mark removed.
    schema:
        *original*, validated.
    placements:
        :func:`locate_tables`'s result for it.

    Returns
    -------
    str
        The new text.

    Raises
    ------
    LayoutError
        If a table's entry cannot be located (a flow-style ``tables:``
        mapping or a table written as ``Name: {...}``).

    Examples
    --------
    >>> from schema_data.registry import validate_schema_yaml_text
    >>> text = "tables:\\n  Date:  # dim\\n    description: d\\n"
    >>> placement = Placement("Date", ("a", "b"), (), ("a",), {})
    >>> print(render_schema_yaml(text, validate_schema_yaml_text(text), [placement]), end="")
    tables:
      Date:  # dim
        datasource: [a, b]
        description: d
    """
    lines = original.split("\n")
    cr = "\r" if "\r\n" in original else ""

    start = next((i for i, line in enumerate(lines) if _TABLES_LINE.match(line)), None)
    if start is None:
        if schema.tables:
            raise LayoutError("no block-style `tables:` mapping found in schema.yaml")
        return original
    stop = len(lines)
    for i in range(start + 1, len(lines)):
        if not _is_blank_or_comment(lines[i]) and _indent_of(lines[i]) == 0:
            stop = i
            break
    body = [i for i in range(start + 1, stop) if not _is_blank_or_comment(lines[i])]
    key_indent = _indent_of(lines[body[0]]) if body else 0

    entries: dict[str, int] = {}
    for i in body:
        if _indent_of(lines[i]) != key_indent:
            continue
        match = _KEY_LINE.match(lines[i])
        if match is None:
            continue
        try:
            key = str(yaml.safe_load(match.group("key")))
        except yaml.YAMLError:
            continue
        entries[key] = i
    unplaced = [p.key for p in placements if p.key not in entries]
    if unplaced:
        raise LayoutError(
            "cannot find these tables as `Name:` lines with an indented body "
            f"in schema.yaml (flow-style entries are not edited): {', '.join(unplaced)}"
        )

    ordered = sorted(entries.values())
    ends = dict(zip(ordered, [*ordered[1:], stop]))
    edits: list[tuple[int, int, list[str]]] = []  # (first, stop, replacement)
    for placement in placements:
        at = entries[placement.key]
        first, end = at + 1, ends[at]
        inner = [i for i in range(first, end) if not _is_blank_or_comment(lines[i])]
        child_indent = _indent_of(lines[inner[0]]) if inner else key_indent + 2
        pad = " " * child_indent
        marker = first < end and lines[first].strip() == NOT_FOUND_COMMENT
        if not placement.found_in:
            if not marker:
                edits.append((first, first, [f"{pad}{NOT_FOUND_COMMENT}{cr}"]))
            continue
        if set(placement.found_in) == set(placement.current):
            if marker:
                edits.append((first, first + 1, []))
            continue
        new_value = _format_value(placement.found_in)
        span = _datasource_span(lines, first, end, child_indent)
        if span is None:
            replaced = 1 if marker else 0
            edits.append((first, first + replaced, [f"{pad}datasource: {new_value}{cr}"]))
            continue
        old_first, old_stop = span
        head = lines[old_first].rstrip("\r")
        comment = _TRAILING_COMMENT.search(head.split(":", 1)[1])
        tail = f"  {comment.group(1)}" if comment else ""
        edits.append((old_first, old_stop, [f"{pad}datasource: {new_value}{tail}{cr}"]))
        if marker:
            edits.append((first, first + 1, []))

    for first, end, replacement in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        lines[first:end] = replacement
    return "\n".join(lines)


def _check_output(
    original: SchemaConfig, rendered: str, placements: Sequence[Placement],
) -> SchemaConfig:
    """Validate *rendered* and prove it differs from *original* only as intended.

    Raises
    ------
    ValueError
        If *rendered* fails :func:`~schema_data.registry.validate_schema_yaml_text`,
        or a table's ``datasource`` is not what was meant, or anything other
        than ``datasource`` changed.
    """
    parsed = validate_schema_yaml_text(rendered)
    wanted = {
        p.key: (
            p.current if not p.found_in or set(p.found_in) == set(p.current) else p.found_in
        )
        for p in placements
    }
    if list(parsed.tables) != list(original.tables):
        raise ValueError("the proposed file lists different tables than schema.yaml")
    for key, table in parsed.tables.items():
        if table.datasource != wanted[key]:
            raise ValueError(
                f"the proposed file gives {key!r} datasource {table.datasource!r}, "
                f"expected {wanted[key]!r}"
            )
        before = original.tables[key].model_dump(exclude={"datasource"})
        if table.model_dump(exclude={"datasource"}) != before:
            raise ValueError(f"the proposed file changed more than datasource for {key!r}")
    if parsed.relationships != original.relationships:
        raise ValueError("the proposed file changed schema.yaml's relationships")
    return parsed


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _sources_text(sources: Sequence[str]) -> str:
    return ", ".join(sources)


def build_report(
    placements: Sequence[Placement], sources: Sequence[str], default: str,
) -> str:
    """The human-readable report: table names only.

    Parameters
    ----------
    placements:
        :func:`locate_tables`'s result.
    sources:
        Source names in ``datasources.yaml`` order.
    default:
        The default source.

    Returns
    -------
    str
        Multi-line text without a trailing newline.

    Examples
    --------
    >>> print(build_report([Placement("T", ("a",), (), ("a",), {})], ["a", "b"], "a"))
    == tables found only in a: 1 ==
      T
    <BLANKLINE>
    == tables found only in b: 0 ==
    <BLANKLINE>
    == tables found in more than one data source: 0 ==
    <BLANKLINE>
    == tables found in no data source: 0 ==
    <BLANKLINE>
    == tables whose datasource: disagrees with what was found: 0 ==
    <BLANKLINE>
    == columns listed in schema.yaml that the database does not have: 0 ==
    """
    out: list[str] = []

    def section(title: str, rows: Sequence[str]) -> None:
        if out:
            out.append("")
        out.append(f"== {title}: {len(rows)} ==")
        out.extend(f"  {row}" for row in rows)

    for source in sources:
        section(
            f"tables found only in {source}",
            [p.key for p in placements if p.found_in == (source,)],
        )
    section(
        "tables found in more than one data source",
        [f"{p.key}: {_sources_text(p.found_in)}" for p in placements if len(p.found_in) > 1],
    )
    section(
        "tables found in no data source", [p.key for p in placements if not p.found_in],
    )
    section(
        "tables whose datasource: disagrees with what was found",
        [
            f"{p.key}: schema.yaml says {_sources_text(p.effective)}"
            f"{'' if p.current else ' (the default)'}, found in {_sources_text(p.found_in)}"
            for p in placements
            if p.disagrees
        ],
    )
    section(
        "columns listed in schema.yaml that the database does not have",
        [
            f"{p.key} [{source}]: {', '.join(columns)}"
            for p in placements
            for source, columns in p.missing_columns.items()
        ],
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _describe(exc: BaseException) -> str:
    """One short line about *exc*: its class and the start of its message."""
    first = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first[:160]}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assign_datasources.py",
        description=(
            "Find which data source each schema.yaml table lives in and "
            f"write {OUTPUT_FILENAME} with the datasource: lines. Read-only "
            "against the databases; schema.yaml itself is never changed."
        ),
    )
    parser.add_argument(
        "--output", type=Path, metavar="PATH",
        help=f"Where to write the proposed file (default: {OUTPUT_FILENAME} next to schema.yaml).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help=(
            "Write nothing; exit 1 if any table's datasource: differs from "
            "the sources it was found in."
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None, *, load_catalogue: CatalogueLoader | None = None,
) -> int:
    """Run the script; returns the process exit code.

    Parameters
    ----------
    argv:
        Command-line arguments; ``sys.argv[1:]`` when ``None``.
    load_catalogue:
        Reads one source's tables and columns. Defaults to
        :func:`default_catalogue_loader`; tests inject a fake catalogue.

    Returns
    -------
    int
        ``EXIT_OK``, ``EXIT_CHECK_FAILED`` or ``EXIT_ERROR`` (see the module
        docstring).
    """
    args = _build_parser().parse_args(argv)
    loader = load_catalogue or default_catalogue_loader

    schema_path = schema_yaml_path()
    try:
        raw = schema_path.read_bytes()
    except OSError as exc:
        print(f"cannot read {schema_path}: {_describe(exc)}", file=sys.stderr)
        return EXIT_ERROR
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        original = raw.decode("utf-8-sig")
        schema = validate_schema_yaml_text(original)
        sources = datasource_names()
        default = default_datasource_name()
    except (UnicodeDecodeError, ValueError) as exc:
        print(f"cannot use {schema_path}: {_describe(exc)}", file=sys.stderr)
        return EXIT_ERROR

    output = args.output or schema_path.with_name(OUTPUT_FILENAME)
    if not args.check and output.resolve() == schema_path.resolve():
        print(f"refusing to overwrite {schema_path}; choose another --output", file=sys.stderr)
        return EXIT_ERROR

    print(f"data sources: {_sources_text(sources)} | default: {default}")
    catalogues: dict[str, SourceCatalogue] = {}
    failed = False
    for source in sources:
        try:
            catalogues[source] = loader(source)
        except Exception as exc:  # noqa: BLE001 - reported per source below
            print(f"  {source}: could not list tables ({_describe(exc)})", file=sys.stderr)
            failed = True
        else:
            print(f"  {source}: {len(catalogues[source])} tables and views")
    if failed:
        print(
            "stopping: a source that cannot be read would make a shared table "
            "look like it is in one source only",
            file=sys.stderr,
        )
        return EXIT_ERROR

    placements = locate_tables(schema, sources, default, catalogues)
    print()
    print(build_report(placements, sources, default))

    mismatches = [p for p in placements if p.disagrees]
    if args.check:
        print()
        if mismatches:
            print(f"CHECK FAILED: {len(mismatches)} table(s) disagree with the databases")
            return EXIT_CHECK_FAILED
        print("CHECK OK: every table's datasource: matches the databases")
        return EXIT_OK

    if len(sources) < 2:
        print("\nonly one data source is configured: no datasource: lines to write")
        return EXIT_OK
    try:
        rendered = render_schema_yaml(original, schema, placements)
        _check_output(schema, rendered, placements)
    except ValueError as exc:  # LayoutError is a ValueError
        print(f"\nnot written: {_describe(exc)}", file=sys.stderr)
        return EXIT_ERROR
    try:
        output.write_bytes((b"\xef\xbb\xbf" if has_bom else b"") + rendered.encode("utf-8"))
    except OSError as exc:
        print(f"\ncannot write {output}: {_describe(exc)}", file=sys.stderr)
        return EXIT_ERROR
    print(f"\nwritten: {output} -- review it before replacing schema.yaml")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
