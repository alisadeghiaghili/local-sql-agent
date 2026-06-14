"""CLI entry point for the schema auto-discovery tool.

Usage
-----
::

    python -m database.schema_inspector_cli \\
        --db-url "mssql+pyodbc://server/db?driver=ODBC+Driver+17+for+SQL+Server" \\
        --output-dir project_config_draft/ \\
        --sample-rows 10 \\
        --include-schemas Auction_Dim,Auction_Fact,General_Dim

All progress messages are written to **stderr** so stdout can be piped or
redirected without contamination.  With ``--dry-run`` the YAML is printed
to stdout instead of written to disk.

Environment variables
---------------------
``DATABASE_URL``
    Fallback for ``--db-url`` if the flag is not provided.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m database.schema_inspector_cli",
        description="Auto-discover database schema and generate draft YAML config files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m database.schema_inspector_cli \\\n"
            '      --db-url "mssql+pyodbc://server/db?driver=ODBC+Driver+17+for+SQL+Server" \\\n'
            "      --output-dir project_config_draft/ \\\n"
            "      --include-schemas Auction_Dim,Auction_Fact\n"
        ),
    )
    p.add_argument(
        "--db-url",
        metavar="URL",
        default=os.getenv("DATABASE_URL") or os.getenv("DB_CONNECTION_URL"),
        help=(
            "SQLAlchemy connection string.  "
            "Defaults to DATABASE_URL or DB_CONNECTION_URL env var."
        ),
    )
    p.add_argument(
        "--output-dir",
        metavar="DIR",
        default="project_config_draft",
        help="Directory to write draft YAML files (default: project_config_draft/).",
    )
    p.add_argument(
        "--sample-rows",
        metavar="N",
        type=int,
        default=10,
        help="Distinct sample values to fetch per string column (0 to skip, default: 10).",
    )
    p.add_argument(
        "--include-schemas",
        metavar="SCHEMAS",
        default=None,
        help="Comma-separated schema names to include (default: all user schemas).",
    )
    p.add_argument(
        "--exclude-tables",
        metavar="TABLES",
        default=None,
        help="Comma-separated table names to exclude.",
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args   = parser.parse_args(argv)

    if not args.db_url:
        parser.error(
            "--db-url is required (or set DATABASE_URL / DB_CONNECTION_URL env var)."
        )

    include_schemas: list[str] | None = (
        [s.strip() for s in args.include_schemas.split(",") if s.strip()]
        if args.include_schemas
        else None
    )
    exclude_tables: list[str] | None = (
        [t.strip() for t in args.exclude_tables.split(",") if t.strip()]
        if args.exclude_tables
        else None
    )

    output_dir = Path(args.output_dir)

    # Deferred import so the module stays importable without DB deps installed
    from database.schema_inspector import SchemaInspector

    print("Schema Auto-Discovery", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    if include_schemas:
        print(f"  Schemas : {', '.join(include_schemas)}", file=sys.stderr)
    if exclude_tables:
        print(f"  Excluded: {', '.join(exclude_tables)}", file=sys.stderr)
    print(f"  Samples : {args.sample_rows} values/column", file=sys.stderr)
    print(f"  Output  : {'(dry-run, stdout)' if args.dry_run else output_dir}", file=sys.stderr)
    print("", file=sys.stderr)

    inspector = SchemaInspector(
        db_url=args.db_url,
        sample_rows=args.sample_rows,
    )

    try:
        snapshot = inspector.inspect(
            include_schemas=include_schemas,
            exclude_tables=exclude_tables,
            fetch_row_counts=not args.no_row_counts,
        )
    except ConnectionError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        inspector.close()

    entities_yaml      = inspector.draft_entities_yaml(snapshot)
    aliases_yaml       = inspector.draft_aliases_yaml(snapshot)
    relationships_yaml = inspector.draft_relationships_yaml(snapshot)

    _write_or_print(output_dir / "entities.yaml",      entities_yaml,      args.dry_run)
    _write_or_print(output_dir / "aliases.yaml",       aliases_yaml,       args.dry_run)
    _write_or_print(output_dir / "relationships.yaml", relationships_yaml, args.dry_run)

    print("", file=sys.stderr)
    print("Done.", file=sys.stderr)
    if not args.dry_run:
        print(
            f"  Review files in {output_dir}/ then copy to project_config/ when ready.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
