# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Keep ``schema.yaml``'s structure in step with the databases.

Usage (from the repo root, with the server's environment active)::

    python scripts/sync_schema.py                       # write schema.synced.yaml
    python scripts/sync_schema.py --dry-run             # print the report only
    python scripts/sync_schema.py --check               # CI / deploy gate
    python scripts/sync_schema.py --prune               # remove what the databases lack
    python scripts/sync_schema.py --add-tables 'sales.*' --add-tables 'ref.Ring'

``schema.yaml`` is curated by hand, and a real one drifts: columns are added
to the databases, a few it lists were dropped, tables move between servers,
and nobody writes the ``datasource:`` lines. This script reads the structure
from every configured data source and brings those facts into
``schema.yaml`` without touching anything a person wrote: descriptions,
business notes, flags and comments stay exactly as they are. It replaces the
two-step "draft once with the inspector, then fix by hand" with a sync that
can be re-run at every release. ``scripts/assign_datasources.py`` is the
narrower tool that only writes the ``datasource:`` lines; this script does
the same and the rest.

It connects through the application's own engines
(:func:`database.connection.get_engine`) and reads three
``INFORMATION_SCHEMA`` views per source (:func:`database.catalogue.read_catalogue`):
names, data types, nullability and declared keys. It never writes to a
database, never reads a row, and prints names, types and counts only -- no
credentials, no data.

What it changes (see :mod:`schema_data.sync` for the exact rules)
-----------------------------------------------------------------
* ``datasource:`` is set or repaired for every table (single name, or a list
  for a table that exists in several sources), as ``assign_datasources.py``
  does. Only with more than one data source.
* A column the database has and ``schema.yaml`` lacks is appended to the
  table's ``columns:`` as ``<type> column`` with the comment ``# TO BE
  FILLED``; its type is recorded in the table's ``column_types:``.
* A column ``schema.yaml`` lists that no source's table has gets the comment
  ``# not in database (sync_schema.py)`` above it and stays. ``--prune``
  removes it instead (never one still named in ``resolvable_columns`` or
  ``prefetchable_columns``).
* A recorded type that differs from the database is corrected, and each
  correction is reported.
* A table the databases do not have gets ``# not found in any data source``;
  ``--prune`` removes it (unless ``relationships:`` still names it).
* A table the databases have and ``schema.yaml`` lacks is only reported;
  ``--add-tables PATTERN`` (a glob on ``schema.table``, repeatable) adds the
  matching ones. A table with no ``columns:`` key is described-only on purpose
  and is left alone.

Nothing else changes: the script proves it by parsing its own output and
comparing it with the original plus exactly the planned changes, and refuses
to write a file that is invalid or differs in anything else.

Relationship proposals
----------------------
Declared foreign keys, and relationships inferred from column names
(``Order.CustomerID`` -> ``Customer.ID``; see
:mod:`schema_data.relationship_proposals` for what is and is not inferred),
are written to ``relationships.proposed.yaml`` next to ``schema.yaml``. Nothing
reads that file and ``relationships.yaml`` is never touched: review it, and
copy what is right.

Modes
-----
Default
    Writes ``schema.synced.yaml`` (or ``--output``) when anything changes,
    and ``relationships.proposed.yaml`` (or ``--relationships-output``).
    ``schema.yaml`` itself is never overwritten. Review the file, replace
    ``schema.yaml``, run again: the second run changes nothing.
``--dry-run``
    Prints the report; writes nothing.
``--check``
    Writes nothing; exits 1 when ``schema.yaml`` is out of step with the
    databases: running without ``--check`` would add or mark a column or
    table, correct a recorded type, set a ``datasource:`` line, or (if given)
    do what ``--prune`` / ``--add-tables`` say. For CI and deploy pipelines.
    The failure line names the counts (:func:`schema_data.sync.describe_drift`);
    a file that differs in a way none of them covers says so and points at
    ``--dry-run``. Columns marked ``# not in database`` are reported on every
    run but do not fail the check once marked: ``--prune`` is how they go.

    A column type that is not recorded yet is not out of step. A
    ``schema.yaml`` written before 6.9.0 has no ``column_types:`` map, so a
    plain run would add one entry per column and nothing else; ``--check``
    then exits 0, prints how many types are not recorded, and says that a
    plain run records them. Run it once, review ``schema.synced.yaml`` and
    replace ``schema.yaml``, so later type changes are detected.

A column the read-only login may not see through ``INFORMATION_SCHEMA`` is
"not in the database" to this script too: check the ``DENY`` grants in
``docs/db-hardening.md`` before running ``--prune``.

Prompt size
-----------
Every column and table the sync adds is text the model reads on every request
(the static prompt prefix). When anything would change, the report ends with
a ``== prompt size ==`` section: per data source, the estimated prefix tokens
of the current ``schema.yaml`` and of the synced text, the difference, the
path each takes (the static prefix, or retrieval, by
``PROMPT_RETRIEVAL_TOKEN_BUDGET``), and how many added columns and tables
still carry the ``TO BE FILLED`` draft description. Both prefixes are built by
the same code the server and ``scripts/prompt_budget.py`` use
(:func:`prompt_engine.static_prefix.build_static_prefix`, sized by
:func:`scripts.prompt_budget.measure_prefixes`) from the two schema texts, so
the numbers are the estimator's, not real model tokens. A source the sync
would move from the static prefix to retrieval gets a ``WARNING`` line. This
is advice: it changes no exit code, and ``--check`` is not affected. When the
size cannot be computed (no ``system_prompt.md``, say) the section is one line
saying why.

Exit codes
----------
0 -- done (``--check``: in sync); 1 -- ``--check`` found the structure out of
sync; 2 -- nothing could be decided or written: a source's catalogue could
not be read (a source that cannot be listed would make a shared table look
single, so the run stops), ``schema.yaml`` is invalid or written in a layout
this script cannot edit, the output would not validate or would change more
than the structure, or ``--output`` names ``schema.yaml``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# Run as `python scripts/sync_schema.py` from the repo root: Python puts
# only this script's own directory on sys.path, so the repo root is added
# for `import config` and the packages below -- same as
# scripts/assign_datasources.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.console import use_utf8_console
import config as cfg  # noqa: F401,E402 - loads .env, like every entry point
from core.yaml_loading import safe_load_strict  # noqa: E402
from database.catalogue import read_catalogue  # noqa: E402
from database.datasources import datasource_names, default_datasource_name  # noqa: E402
from knowledge.config_loader import (  # noqa: E402
    ConfigNotFoundError,
    load_system_prompt,
    resolve_system_prompt_path,
)
from schema_data.registry import (  # noqa: E402
    SchemaRegistry,
    schema_yaml_path,
    validate_schema_yaml_text,
)
from schema_data.relationship_proposals import (  # noqa: E402
    PROPOSALS_FILENAME,
    ProposalSet,
    propose_relationships,
    render_proposals_yaml,
)
from schema_data.sync import (  # noqa: E402
    SourceTables,
    SyncOptions,
    SyncResult,
    build_report,
    count_drafts,
    describe_drift,
    sync_schema_text,
    unrecorded_types_note,
)

__all__ = [
    "EXIT_CHECK_FAILED",
    "EXIT_ERROR",
    "EXIT_OK",
    "OUTPUT_FILENAME",
    "PromptSizeReport",
    "SourcePromptSize",
    "default_catalogue_loader",
    "main",
    "measure_prompt_size",
    "render_prompt_size",
]

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_ERROR = 2

#: Name of the proposed file written next to ``schema.yaml``.
OUTPUT_FILENAME = "schema.synced.yaml"

#: ``source name -> its catalogue``; raises when the source cannot be read.
CatalogueLoader = Callable[[str], SourceTables]

#: How many dropped relationship guesses the report lists.
_MAX_SKIPPED_LINES = 40

_URL_CREDENTIALS = re.compile(r"(://)[^/@\s]*@")
_ODBC_SECRET = re.compile(r"(?i)\b(pwd|password)\s*=\s*[^;\s]*")


def default_catalogue_loader(source: str) -> SourceTables:
    """Read *source*'s tables, columns and keys through the application's engine.

    Three ``INFORMATION_SCHEMA`` queries (:func:`database.catalogue.read_catalogue`),
    nothing else.

    Parameters
    ----------
    source:
        A configured data source name.

    Returns
    -------
    SourceTables
        Every table and view with its columns, types and declared keys.

    Raises
    ------
    Exception
        Whatever the driver raises when the database cannot be reached.
    """
    from database.connection import get_engine

    return read_catalogue(get_engine(source))


def _describe(exc: BaseException) -> str:
    """One short line about *exc*, without a password or a URL's credentials."""
    first = (str(exc).strip().splitlines() or [""])[0]
    first = _ODBC_SECRET.sub(r"\1=***", _URL_CREDENTIALS.sub(r"\1***@", first))
    return f"{type(exc).__name__}: {first[:160]}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_schema.py",
        description=(
            "Bring schema.yaml's structure (datasource:, columns, types, tables) "
            f"in step with the databases and write {OUTPUT_FILENAME}; propose "
            f"relationships in {PROPOSALS_FILENAME}. Curated text is never "
            "changed; schema.yaml itself is never overwritten; the databases "
            "are only read (names and types, never rows)."
        ),
    )
    parser.add_argument(
        "--output", type=Path, metavar="PATH",
        help=f"Where to write the synced file (default: {OUTPUT_FILENAME} next to schema.yaml).",
    )
    parser.add_argument(
        "--relationships-output", type=Path, metavar="PATH",
        help=f"Where to write the relationship proposals (default: {PROPOSALS_FILENAME} next to schema.yaml).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Write nothing; exit 1 if the structure is out of sync (for CI and deploy).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the report only; write nothing.",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help=(
            "Remove columns and tables schema.yaml lists that the databases do not have "
            "(default: mark them with a comment)."
        ),
    )
    parser.add_argument(
        "--add-tables", action="append", default=[], metavar="PATTERN",
        help=(
            "Add database tables that schema.yaml lacks and whose schema.table matches "
            "this glob, e.g. 'sales.*' (repeatable; default: report them only)."
        ),
    )
    parser.add_argument(
        "--no-relationships", action="store_true",
        help=f"Do not propose relationships ({PROPOSALS_FILENAME} is not written).",
    )
    return parser


def _proposal_report(proposals: ProposalSet) -> str:
    out = [
        "== relationship proposals ==",
        f"  declared foreign keys: {proposals.declared}",
        f"  inferred from column names: {proposals.inferred}",
        f"  already in relationships.yaml or schema.yaml: {proposals.already_known}",
        f"  dropped because they could not be trusted: {len(proposals.skipped)}",
    ]
    out.extend(f"    {what}: {why}" for what, why in proposals.skipped[:_MAX_SKIPPED_LINES])
    if len(proposals.skipped) > _MAX_SKIPPED_LINES:
        out.append(f"    ... and {len(proposals.skipped) - _MAX_SKIPPED_LINES} more")
    return "\n".join(out)


@dataclass(frozen=True)
class SourcePromptSize:
    """One data source's static prefix before and after a sync.

    Attributes
    ----------
    source:
        The data source (``default`` when there is no ``datasources.yaml``).
    before, after:
        :func:`~prompt_engine.static_prefix.estimate_tokens` of the source's
        static prefix built from the current and from the synced
        ``schema.yaml``.
    path_before, path_after:
        ``"static prefix"`` or ``"retrieval"`` (``scripts.prompt_budget``'s
        ``PATH_STATIC`` and ``PATH_RETRIEVAL``): the path the server's gate
        (:func:`~prompt_engine.static_prefix.should_use_static_prefix`)
        gives the source under the current budget.
    flips_to_retrieval:
        The source uses the static prefix now and would use retrieval after.
    draft_columns, draft_tables:
        Columns and tables the sync adds to this source's prefix whose
        description is still the ``TO BE FILLED`` draft.

    Examples
    --------
    >>> size = SourcePromptSize("sales", 900, 1100, "static prefix", "retrieval", True, 4, 1)
    >>> size.delta, size.flips_to_retrieval
    (200, True)
    """

    source: str
    before: int
    after: int
    path_before: str
    path_after: str
    flips_to_retrieval: bool
    draft_columns: int
    draft_tables: int

    @property
    def delta(self) -> int:
        """``after - before``."""
        return self.after - self.before


@dataclass(frozen=True)
class PromptSizeReport:
    """What a sync does to the prompt size, or why that is not known.

    Attributes
    ----------
    sizes:
        One entry per data source; empty when *note* is set.
    budget:
        ``PROMPT_RETRIEVAL_TOKEN_BUDGET`` as configured now.
    note:
        Why the sizes could not be computed (empty when they were).
    """

    sizes: tuple[SourcePromptSize, ...]
    budget: int
    note: str = ""


def measure_prompt_size(
    original: str, result: SyncResult, sources: Sequence[str],
) -> PromptSizeReport:
    """Size each source's static prefix for the current and the synced ``schema.yaml``.

    Both prefixes come from the server's own builder
    (:func:`~prompt_engine.static_prefix.build_static_prefix`, through
    :func:`scripts.prompt_budget.measure_prefixes`, the sizing
    ``prompt_budget.py`` reports), one built with *original* in effect and
    one with ``result.text``
    (:func:`~prompt_engine.schema_preview.schema_text_in_effect`). Nothing
    is written and the process is left as it was. It never raises: whatever
    stops the computation becomes :attr:`PromptSizeReport.note`.

    Parameters
    ----------
    original:
        The current ``schema.yaml`` text, byte-order mark removed.
    result:
        :func:`~schema_data.sync.sync_schema_text`'s result for it.
    sources:
        The configured data source names.

    Returns
    -------
    PromptSizeReport
        With a note and no sizes when the system prompt is missing or the
        prefix cannot be built.

    Examples
    --------
    >>> from config import override_settings
    >>> with override_settings(project_config_dir="/nonexistent"):
    ...     measure_prompt_size("", None, ["a"]).note.startswith("system prompt not found")
    True
    """
    budget = cfg.settings.prompt_retrieval_token_budget
    try:
        system_prompt = load_system_prompt()
    except ConfigNotFoundError:
        return PromptSizeReport((), budget, f"system prompt not found at {resolve_system_prompt_path()}")
    except Exception as exc:  # noqa: BLE001 - advice must never stop the sync
        return PromptSizeReport((), budget, f"cannot read the system prompt ({_describe(exc)})")
    try:
        # Imported here: these load the knowledge base (business rules, metrics,
        # examples), which must not be able to stop the sync itself.
        from prompt_engine.schema_preview import schema_text_in_effect
        from scripts.prompt_budget import PATH_RETRIEVAL, PATH_STATIC, measure_prefixes

        with schema_text_in_effect(original):
            before = measure_prefixes(system_prompt, sources)
        synced = validate_schema_yaml_text(result.text)
        with schema_text_in_effect(result.text):
            after = measure_prefixes(system_prompt, sources)
            drafts = [
                count_drafts(result.plan, synced, SchemaRegistry.tables_for_source(size.name))
                for size in after
            ]
    except Exception as exc:  # noqa: BLE001 - advice must never stop the sync
        return PromptSizeReport((), budget, f"cannot build the static prefix ({_describe(exc)})")
    return PromptSizeReport(
        tuple(
            SourcePromptSize(
                source=old.name, before=old.estimate, after=new.estimate,
                path_before=old.path_now, path_after=new.path_now,
                flips_to_retrieval=old.path_now == PATH_STATIC and new.path_now == PATH_RETRIEVAL,
                draft_columns=columns, draft_tables=tables,
            )
            for old, new, (columns, tables) in zip(before, after, drafts, strict=True)
        ),
        budget,
    )


def render_prompt_size(report: PromptSizeReport) -> str:
    """The ``== prompt size ==`` section, with a ``WARNING`` line per flipped source.

    Parameters
    ----------
    report:
        :func:`measure_prompt_size`'s result.

    Returns
    -------
    str
        Multi-line text without a trailing newline; one ``prompt size: not
        computed (...)`` line when *report* has a note.

    Examples
    --------
    >>> print(render_prompt_size(PromptSizeReport((), 6000, "system prompt not found at x")))
    prompt size: not computed (system prompt not found at x)
    >>> size = SourcePromptSize("sales", 5900, 6100, "static prefix", "retrieval", True, 3, 0)
    >>> print(render_prompt_size(PromptSizeReport((size,), 6000)).splitlines()[1])
      sales: 5900 -> 6100 (+200); static prefix -> retrieval; draft descriptions still to fill: 3 column(s), 0 table(s)
    """
    if report.note:
        return f"prompt size: not computed ({report.note})"
    out = ["== prompt size (estimated static-prefix tokens per data source, current -> synced) =="]
    for size in report.sizes:
        out.append(
            f"  {size.source}: {size.before} -> {size.after} ({size.delta:+d}); "
            f"{size.path_before} -> {size.path_after}; "
            f"draft descriptions still to fill: {size.draft_columns} column(s), {size.draft_tables} table(s)"
        )
    out.append(f"  PROMPT_RETRIEVAL_TOKEN_BUDGET is {report.budget}; the estimate is len(text) // 4, not real tokens")
    out.extend(
        f"WARNING: {size.source}: the static prefix would grow from {size.before} to {size.after} "
        f"estimated tokens, past PROMPT_RETRIEVAL_TOKEN_BUDGET={report.budget}: its questions would "
        "move from the static prefix to retrieval. Review the draft columns (# TO BE FILLED) "
        "or raise the budget after running scripts/prompt_budget.py."
        for size in report.sizes if size.flips_to_retrieval
    )
    return "\n".join(out)


def _existing_relationships(path: Path) -> list[Mapping[str, object]]:
    """The entries of ``relationships.yaml`` (``[]`` when missing or unreadable)."""
    try:
        raw = safe_load_strict(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    entries = raw.get("relationships") if isinstance(raw, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _read_catalogues(
    sources: Sequence[str], loader: CatalogueLoader,
) -> dict[str, SourceTables] | None:
    """Every source's catalogue, or ``None`` after naming the ones that failed."""
    catalogues: dict[str, SourceTables] = {}
    failed = False
    for source in sources:
        try:
            catalogues[source] = loader(source)
        except Exception as exc:  # noqa: BLE001 - reported per source below
            print(f"  {source}: could not read the catalogue ({_describe(exc)})", file=sys.stderr)
            failed = True
        else:
            print(f"  {source}: {len(catalogues[source])} tables and views")
    if failed:
        print(
            "stopping: a source that cannot be read would make a shared table "
            "look like it is in one source only",
            file=sys.stderr,
        )
        return None
    return catalogues


def _write(path: Path, text: str, bom: bool) -> bool:
    try:
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    except OSError as exc:
        print(f"\ncannot write {path}: {_describe(exc)}", file=sys.stderr)
        return False
    return True


def main(
    argv: Sequence[str] | None = None, *, load_catalogue: CatalogueLoader | None = None,
) -> int:
    """Run the script; returns the process exit code.

    Parameters
    ----------
    argv:
        Command-line arguments; ``sys.argv[1:]`` when ``None``.
    load_catalogue:
        Reads one source's catalogue. Defaults to
        :func:`default_catalogue_loader`; tests inject a fake catalogue.

    Returns
    -------
    int
        ``EXIT_OK``, ``EXIT_CHECK_FAILED`` or ``EXIT_ERROR`` (see the module
        docstring).
    """
    args = _build_parser().parse_args(argv)
    loader = load_catalogue or default_catalogue_loader
    options = SyncOptions(prune=args.prune, add_tables=tuple(args.add_tables))

    schema_path = schema_yaml_path()
    try:
        raw = schema_path.read_bytes()
    except OSError as exc:
        print(f"cannot read {schema_path}: {_describe(exc)}", file=sys.stderr)
        return EXIT_ERROR
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        original = raw.decode("utf-8-sig")
        validate_schema_yaml_text(original)
        sources = datasource_names()
        default = default_datasource_name()
    except (UnicodeDecodeError, ValueError) as exc:
        print(f"cannot use {schema_path}: {_describe(exc)}", file=sys.stderr)
        return EXIT_ERROR

    relationships_path = schema_path.with_name("relationships.yaml")
    output = args.output or schema_path.with_name(OUTPUT_FILENAME)
    proposals_output = args.relationships_output or schema_path.with_name(PROPOSALS_FILENAME)
    writing = not (args.check or args.dry_run)
    if writing and output.resolve() == schema_path.resolve():
        print(f"refusing to overwrite {schema_path}; choose another --output", file=sys.stderr)
        return EXIT_ERROR
    if writing and proposals_output.resolve() == relationships_path.resolve():
        print(
            f"refusing to overwrite {relationships_path}; choose another --relationships-output",
            file=sys.stderr,
        )
        return EXIT_ERROR

    print(f"data sources: {', '.join(sources)} | default: {default}")
    catalogues = _read_catalogues(sources, loader)
    if catalogues is None:
        return EXIT_ERROR

    try:
        result: SyncResult = sync_schema_text(original, sources, default, catalogues, options)
    except ValueError as exc:  # LayoutError is a ValueError
        print(f"\nnot written: {_describe(exc)}", file=sys.stderr)
        return EXIT_ERROR
    print()
    print(build_report(result))
    if result.changed:
        print()
        print(render_prompt_size(measure_prompt_size(original, result, sources)))

    proposals: ProposalSet | None = None
    if not args.no_relationships:
        proposals = propose_relationships(
            validate_schema_yaml_text(result.text), sources, catalogues,
            _existing_relationships(relationships_path),
        )
        print()
        print(_proposal_report(proposals))

    if args.check:
        print()
        if result.changed and not result.only_records_types:
            print(f"CHECK FAILED: schema.yaml's structure is out of sync with the databases: {describe_drift(result)}")
            return EXIT_CHECK_FAILED
        note = f"; {unrecorded_types_note(result)}" if result.only_records_types else ""
        print(f"CHECK OK: schema.yaml's structure matches the databases{note}")
        return EXIT_OK
    if args.dry_run:
        print("\ndry run: nothing written")
        return EXIT_OK

    if result.changed:
        if not _write(output, result.text, has_bom):
            return EXIT_ERROR
        print(f"\nwritten: {output} -- review it before replacing schema.yaml")
    else:
        print("\nschema.yaml is already in sync: nothing to write")
        if output.exists():
            print(f"note: {output} is left over from an earlier run; delete it")
    if proposals is not None and (proposals.proposals or proposals_output.exists()):
        if not _write(proposals_output, render_proposals_yaml(proposals.proposals), False):
            return EXIT_ERROR
        print(
            f"written: {proposals_output} -- {len(proposals.proposals)} proposal(s); "
            "nothing reads this file, copy what is right into relationships.yaml"
        )
    return EXIT_OK


if __name__ == "__main__":
    use_utf8_console()
    sys.exit(main())
