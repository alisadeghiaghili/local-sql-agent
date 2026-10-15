# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""CLI entry point for the first-install schema inspector.

Usage
-----
::

    # the normal way: every data source of datasources.yaml (or the single
    # DB_CONNECTION_URL), read through the login the application itself uses
    python -m database.schema_inspector_cli
    python -m database.schema_inspector_cli --datasource sales --datasource reports

    # before any configuration exists: one database, by URL
    python -m database.schema_inspector_cli \\
        --db-url "mssql+pyodbc://server/db?driver=ODBC+Driver+17+for+SQL+Server"

Without ``--db-url`` the tool reads ``project_config/datasources.yaml`` and
inspects each source through the engine the server uses for it
(:func:`database.connection.get_engine`), so a draft lists only what the
application's login can query. A Windows or administrator login sees far more
(change-capture, temporary and staging tables the application cannot read),
and drafts built under one ended up in live configuration. ``--datasource NAME``
(repeatable) narrows the run. Without ``datasources.yaml`` the one implicit
source (``DB_CONNECTION_URL``) is used. ``--db-url`` keeps working for a first
install: one source, named as the default source, read with the login in that
URL, and the tool says the application's login may see less.

The drafts are in the format ``scripts/sync_schema.py`` writes (it is the same
code, see :mod:`database.schema_draft`): ``datasource:`` lines with several
sources, ``Schema.Table`` keys only for names shared across schemas, a table
present in several sources once with ``datasource: [a, b]``. Relationships come
from declared foreign keys, and from column names (``<Table>_ID``) when none is
declared; the guessed ones carry a ``REVIEW`` comment.

Which tables are skipped
------------------------
System schemas (``sys``, ``INFORMATION_SCHEMA``, ``guest``, ``cdc``, the fixed
``db_*`` role schemas, ``pg_*``), the ``EXCLUDE_SCHEMAS`` setting and a source's
``exclude_schemas:`` (:mod:`database.schema_filter`), ``--exclude-schemas``
(comma-separated ``fnmatch`` patterns, case-insensitive), schemas outside
``--include-schemas``, ``--exclude-tables``, and staging or change-capture
tables (``STAGING_PATTERNS``) unless ``--include-staging``. The end-of-run
summary says how many tables and schemas each rule skipped.

Sample values
-------------
No value read from a row goes into ``schema.yaml``, ``entities.yaml`` or
``aliases.yaml``: nothing in the application reads sample values from those
files, and the ``e.g.`` text that used to be drafted into column descriptions
went straight into the model's prompt. ``--sample-values`` writes the values to
``sample_values.review.yaml`` in the draft directory (owner-only permissions),
which nothing loads.

The drafts are for a **first install**: they describe every table they find
as a fresh entry. An existing deployment adds tables to its curated
``schema.yaml`` with ``python scripts/sync_schema.py --add-tables
'Schema.*'``, which keeps the text already written. At the end of a run the
tool prints the table count per schema to stderr, and warns when more than
:data:`LARGE_DRAFT_TABLES` tables were written.

All progress messages are written to **stderr** so stdout can be piped or
redirected without contamination.  With ``--dry-run`` the YAML is printed
to stdout instead of written to disk (the sample-value file is never printed).

Exit codes: 0 done; 1 a database could not be read; 2 the request cannot be
carried out (unknown source, invalid configuration, protected output directory).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from sqlalchemy.engine import Engine

from core.console import use_utf8_console
from database.setup_sources import SourceSelectionError, configured_sources
from database.setup_sources import first_line as _first_line

# Directory names this tool will never write to, regardless of --output-dir:
# the live, git-ignored config an app instance actually runs on, and the
# committed template CI runs against. A draft is unreviewed -- silently
# overwriting either of these would mean a tool that can widen the SQL guard's
# allowlist with no human review step in between. This is enforced here, not
# merely documented, precisely because it must hold even if a caller mistypes
# --output-dir.
_PROTECTED_DIR_NAMES = ("project_config", "project_config.example")

#: A draft with more tables than this earns a warning: every table described
#: in ``schema.yaml`` costs prompt tokens, and a wide warehouse is better
#: narrowed with ``--include-schemas`` / ``--exclude-schemas`` first.
LARGE_DRAFT_TABLES = 150

#: What ``--sample-rows`` reads per string column when ``--sample-values`` is given.
DEFAULT_SAMPLE_ROWS = 10

#: The one line ``--db-url`` prints: the login in the URL is not the application's.
DB_URL_NOTE = (
    "Note: --db-url connects with the login in that URL; the application's own login "
    "may see less. Run without --db-url to inspect what the application can query."
)

#: ``(source name, label for the draft headers, opens the engine)``.
_Opener = tuple[str, str, Callable[[], Engine]]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m database.schema_inspector_cli",
        description=(
            "Draft project_config/ files for a first install. Without --db-url every "
            "data source of datasources.yaml (or DB_CONNECTION_URL) is read through the "
            "application's own login."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # every data source, as the application sees it\n"
            "  python -m database.schema_inspector_cli --output-dir project_config_draft/\n"
            "\n"
            "  # only two of them\n"
            "  python -m database.schema_inspector_cli --datasource sales --datasource reports\n"
            "\n"
            "  # before any config exists: one database, by URL\n"
            "  python -m database.schema_inspector_cli \\\n"
            '      --db-url "mssql+pyodbc://server/db?driver=ODBC+Driver+17+for+SQL+Server" \\\n'
            "      --include-schemas sales,ref\n"
            "\n"
            "  # skip staging and scratch schemas for this run (fnmatch patterns,\n"
            "  # case-insensitive; exclusion wins over --include-schemas). To record\n"
            "  # the rule, set EXCLUDE_SCHEMAS in .env or exclude_schemas: in datasources.yaml.\n"
            '  python -m database.schema_inspector_cli --exclude-schemas "*_Stg,Replay,Alerts"\n'
            "\n"
            "The drafts are for a first install. An existing deployment adds tables with\n"
            "  python scripts/sync_schema.py --add-tables 'Schema.*'\n"
            "which keeps the text already curated in schema.yaml.\n"
        ),
    )
    p.add_argument(
        "--db-url",
        metavar="URL",
        default=None,
        help=(
            "SQLAlchemy connection string, for a first install before datasources.yaml or "
            "DB_CONNECTION_URL exist. One source, read with the login in the URL (which may "
            "see more than the application's). Default: the application's data sources."
        ),
    )
    p.add_argument(
        "--datasource",
        metavar="NAME",
        action="append",
        default=None,
        help="Inspect only this data source of datasources.yaml (repeatable; default: all).",
    )
    p.add_argument(
        "--output-dir",
        metavar="DIR",
        default="project_config_draft",
        help="Directory to write draft YAML files (default: project_config_draft/).",
    )
    p.add_argument(
        "--sample-values",
        action="store_true",
        default=False,
        help=(
            "Read distinct values of string columns and write them to "
            "sample_values.review.yaml in the output directory (owner-only permissions). "
            "They are never written to schema.yaml, entities.yaml or aliases.yaml, and "
            "nothing loads the file. Default: no value is read."
        ),
    )
    p.add_argument(
        "--sample-rows",
        metavar="N",
        type=int,
        default=None,
        help=f"With --sample-values: distinct values per string column (default: {DEFAULT_SAMPLE_ROWS}).",
    )
    p.add_argument(
        "--include-schemas",
        metavar="SCHEMAS",
        default=None,
        help="Comma-separated schema names to include (default: all user schemas).",
    )
    p.add_argument(
        "--exclude-schemas",
        metavar="PATTERNS",
        default=None,
        help=(
            "Comma-separated fnmatch patterns of schemas to skip in this run, matched "
            'case-insensitively (e.g. "*_Stg,Replay,Alerts"), on top of the system schemas, '
            "EXCLUDE_SCHEMAS and each source's exclude_schemas:. Wins over --include-schemas."
        ),
    )
    p.add_argument(
        "--exclude-tables",
        metavar="TABLES",
        default=None,
        help="Comma-separated table names to exclude (any schema).",
    )
    p.add_argument(
        "--include-staging",
        action="store_true",
        default=False,
        help=(
            "Keep staging and change-capture tables, which STAGING_PATTERNS matches and "
            "which are skipped by default."
        ),
    )
    p.add_argument(
        "--no-row-counts",
        action="store_true",
        default=False,
        help="Skip COUNT(*) per table (faster for very large databases).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print generated YAML to stdout instead of writing files.",
    )
    return p


def _guard_output_dir(output_dir: Path) -> None:
    """Refuse to run if *output_dir* resolves to a protected directory.

    Checked against the current working directory (where this tool is
    normally invoked from, the repo root) so ``--output-dir project_config``
    is caught regardless of what the caller's shell happens to expand it to.
    """
    resolved = output_dir.resolve()
    for name in _PROTECTED_DIR_NAMES:
        if resolved == Path(name).resolve():
            print(
                f"ERROR: --output-dir must not be '{name}'.\n"
                "Generated drafts are unreviewed and must never silently overwrite "
                "the live config directory or the committed project_config.example/ "
                "template used by CI. Write to a scratch directory (default: "
                "project_config_draft/) and copy files across by hand, after "
                "review, once you are satisfied with them.",
                file=sys.stderr,
            )
            raise SystemExit(2)


def _split_csv(value: str | None) -> list[str] | None:
    """Split a comma-separated option into trimmed, non-empty items."""
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()] or None


def _print_run_summary(snapshot: object, skipped: Mapping[str, object] | None = None) -> None:
    """Print the end-of-run summary to stderr.

    The table count per schema, how many tables and schemas each skip rule
    left out (*skipped*, ``{source: database.schema_draft.Skipped}``), one line
    saying the drafts are for a first install (and how an existing deployment
    adds tables), and a warning when more than :data:`LARGE_DRAFT_TABLES`
    tables were written.
    """
    tables = getattr(snapshot, "tables", [])
    per_schema = Counter(t.schema or "(no schema)" for t in tables)
    print("", file=sys.stderr)
    print("Tables per schema:", file=sys.stderr)
    for schema in sorted(per_schema, key=str.lower):
        print(f"  {schema}: {per_schema[schema]}", file=sys.stderr)
    print(f"  Total: {len(tables)}", file=sys.stderr)
    if skipped:
        from database.schema_draft import describe_skipped

        lines = describe_skipped(skipped)  # type: ignore[arg-type]
        if lines:
            print("Skipped (not in the drafts):", file=sys.stderr)
            for line in lines:
                print(line, file=sys.stderr)
    print(
        "These drafts are for a first install only. An existing deployment adds "
        "tables with: python scripts/sync_schema.py --add-tables 'Schema.*' "
        "(keeps the curated text).",
        file=sys.stderr,
    )
    if len(tables) > LARGE_DRAFT_TABLES:
        print(
            f"WARNING: {len(tables)} tables were written (more than "
            f"{LARGE_DRAFT_TABLES}). Every table described in schema.yaml costs "
            "prompt tokens; narrow the run with --include-schemas or "
            "--exclude-schemas, then check the size with scripts/prompt_budget.py.",
            file=sys.stderr,
        )


def _write_or_print(path: Path, content: str, dry_run: bool) -> None:
    if dry_run:
        print(f"\n{'=' * 70}")
        print(f"# FILE: {path}")
        print(f"{'=' * 70}")
        print(content)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"  Written: {path}", file=sys.stderr)


def _resolve_sources(
    args: argparse.Namespace, parser: argparse.ArgumentParser,
) -> tuple[list[_Opener], list[str], str]:
    """What to read: ``(sources to read, every configured source name, the default)``.

    With ``--db-url`` the single source is named as the default source and its
    engine is opened from the URL; otherwise each selected source of
    ``datasources.yaml`` (or the implicit ``default``) is read through
    :func:`database.connection.get_engine`, the engine the server uses
    (:func:`database.setup_sources.configured_sources`, shared with the setup
    wizard).
    """
    if args.db_url:
        if args.datasource:
            parser.error("--datasource cannot be combined with --db-url")
        from database.datasources import default_datasource_name
        from database.schema_inspector import SchemaInspector, connect_url

        try:
            default = default_datasource_name()
        except ValueError as exc:
            print(f"ERROR: datasources.yaml cannot be used: {_first_line(exc)}", file=sys.stderr)
            raise SystemExit(2) from exc
        url = args.db_url
        label = f"{default} ({SchemaInspector._redact_url(url)})"
        return [(default, label, lambda: connect_url(url))], [default], default

    try:
        sources, names, default = configured_sources(args.datasource)
    except SourceSelectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return [(s.name, s.label, s.open_engine) for s in sources], names, default


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    include_schemas = _split_csv(args.include_schemas)
    exclude_schemas = _split_csv(args.exclude_schemas) or []
    exclude_tables = _split_csv(args.exclude_tables)
    if args.sample_rows is not None and args.sample_rows < 0:
        parser.error("--sample-rows must not be negative")

    output_dir = Path(args.output_dir)
    _guard_output_dir(output_dir)

    # Deferred imports so the module stays importable without DB deps installed
    import config as cfg
    from core import config_doctor as doctor
    from core.fileperms import restrict_file
    from database.schema_draft import SAMPLE_REVIEW_FILENAME, build_drafts, read_selected
    from database.schema_filter import schema_exclusion

    openers, all_sources, default = _resolve_sources(args, parser)

    rows = (args.sample_rows if args.sample_rows is not None else DEFAULT_SAMPLE_ROWS) if args.sample_values else 0
    staging = [] if args.include_staging else doctor.parse_staging_patterns(cfg.settings.staging_patterns)

    print("Schema Auto-Discovery", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    if args.db_url:
        print(DB_URL_NOTE, file=sys.stderr)
    print(f"  Sources : {', '.join(name for name, _, _ in openers)}", file=sys.stderr)
    if include_schemas:
        print(f"  Schemas : {', '.join(include_schemas)}", file=sys.stderr)
    if exclude_schemas:
        print(f"  Skipping schemas: {', '.join(exclude_schemas)}", file=sys.stderr)
    if exclude_tables:
        print(f"  Excluded: {', '.join(exclude_tables)}", file=sys.stderr)
    samples = (
        f"{rows} values/column, to {SAMPLE_REVIEW_FILENAME} only"
        if rows else "none (--sample-values reads them into a review file)"
    )
    print(f"  Samples : {samples}", file=sys.stderr)
    if args.sample_rows is not None and not args.sample_values:
        print("  Note    : --sample-rows has no effect without --sample-values", file=sys.stderr)
    print(
        f"  Staging : {'kept (--include-staging)' if args.include_staging else 'skipped (STAGING_PATTERNS)'}",
        file=sys.stderr,
    )
    print(f"  Output  : {'(dry-run, stdout)' if args.dry_run else output_dir}", file=sys.stderr)
    print("", file=sys.stderr)

    selected = []
    opened: list[Engine] = []
    try:
        for name, label, open_engine in openers:
            try:
                engine = open_engine()
                if args.db_url:
                    opened.append(engine)  # the application's engines are not ours to dispose
                source = read_selected(
                    name, label, engine, exclusion=schema_exclusion(name, exclude_schemas),
                    staging=staging, include_schemas=include_schemas, exclude_tables=exclude_tables,
                )
            except ValueError as exc:
                print(f"\nERROR: configuration cannot be used: {_first_line(exc)}", file=sys.stderr)
                return 2
            except Exception as exc:  # noqa: BLE001 - reported per source
                print(
                    f"\nERROR: {name}: could not read the catalogue ({type(exc).__name__}: {_first_line(exc)})",
                    file=sys.stderr,
                )
                return 1
            print(f"  {name}: {source.read} tables and views read, {len(source.tables)} kept", file=sys.stderr)
            selected.append(source)

        drafts = build_drafts(
            selected, sources=all_sources, default=default,
            row_counts=not args.no_row_counts, sample_rows=rows,
        )
    finally:
        for engine in opened:
            engine.dispose()

    _write_or_print(output_dir / "entities.yaml",      drafts.entities_yaml,      args.dry_run)
    _write_or_print(output_dir / "aliases.yaml",       drafts.aliases_yaml,       args.dry_run)
    _write_or_print(output_dir / "relationships.yaml", drafts.relationships_yaml, args.dry_run)
    _write_or_print(output_dir / "schema.yaml",        drafts.schema_yaml,        args.dry_run)
    if drafts.sample_values_yaml is not None:
        if args.dry_run:
            print(
                f"  Not printed: {SAMPLE_REVIEW_FILENAME} (real values; run without --dry-run to write it)",
                file=sys.stderr,
            )
        else:
            review = output_dir / SAMPLE_REVIEW_FILENAME
            review.parent.mkdir(parents=True, exist_ok=True)
            review.write_text(drafts.sample_values_yaml, encoding="utf-8")
            restrict_file(review)
            print(f"  Written: {review} (owner-only; real values, never loaded by the application)", file=sys.stderr)

    _print_run_summary(drafts.snapshot, {s.name: s.skipped for s in selected})
    print(
        f"Relationships: {drafts.proposals.declared} declared, {drafts.proposals.inferred} inferred "
        "from column names (marked REVIEW in the drafts)",
        file=sys.stderr,
    )
    for name, why in drafts.sync.plan.skipped_new:
        print(f"  not drafted: {name} ({why})", file=sys.stderr)
    print("", file=sys.stderr)
    print("Done.", file=sys.stderr)
    if not args.dry_run:
        print(
            f"  Review files in {output_dir}/ then copy to project_config/ when ready.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    use_utf8_console()
    sys.exit(main())
