# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Move the application database between backends.

Usage (from repo root)::

    python -m scripts.migrate_app_db --from <url> --to <url> [--dry-run]

``<url>`` is any SQLAlchemy URL, e.g. ``sqlite:///logs/app.db``,
``postgresql://user:pw@host:5432/appdb``, or
``mssql+pyodbc://user:pw@host:1433/appdb?driver=...``. See
``docs/admin-panel-architecture.md`` §5.4 for the design contract this
implements, and :mod:`appdb.migrate` for the actual logic -- this script
is a thin CLI wrapper: it prints the sensitivity warning below, calls
:func:`appdb.migrate.run_migration`, and reports the result.

Requires the application to be stopped or in maintenance mode (§7): the
tool refuses (a ``[FAIL]`` line naming the reason) if the source shows
write activity inside its quiet window -- see
``config.Settings.migration_quiet_window_seconds``.

The exported artefact is sensitive
-----------------------------------
Migrating writes a temporary export file to disk containing every issued
API key's ``key_sha256`` hash and every key's column-level ACL
(``denied_columns_json``) -- not secrets in the ``.env`` sense, but not
something to leave in a shared folder either. This script deletes that
file automatically once a real (non-dry-run) migration verifies
successfully; pass ``--keep-export`` to keep it deliberately (e.g. to take
a copy elsewhere for debugging, per the architecture doc's "the reverse
for taking a copy somewhere to debug"). It is always left in place if the
migration fails, so its contents can be inspected. See
``docs/deployment-runbook.md`` for the same guidance in writing.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from appdb.migrate import MigrationExport, MigrationResult, export_to_json, run_migration

_SENSITIVITY_WARNING = (
    "This export contains API key hashes (admin_api_keys.key_sha256) and "
    "column-level ACLs (denied_columns_json) -- not secrets in the .env "
    "sense, but not something to leave in a shared folder either. Treat it "
    "like the project_config bundle (see docs/deployment-runbook.md)."
)


def _make_export_writer() -> tuple[Callable[[MigrationExport], None], dict[str, Path]]:
    """A :func:`appdb.migrate.run_migration` ``on_export`` callback that
    writes the export to a temporary file the moment it is produced --
    printing the sensitivity warning right there, per §9's "a warning
    printed where the file is created reaches someone; a warning only in a
    document does not". The path it wrote to is stashed in the returned
    dict under ``"path"`` (``run_migration`` calls this with no return
    value of its own to thread one back through)."""
    written: dict[str, Path] = {}

    def _write(export: MigrationExport) -> None:
        payload = export_to_json(export)
        fd, name = tempfile.mkstemp(prefix="appdb_migration_export_", suffix=".json")
        path = Path(name)
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"Wrote export artefact to {path}")
        print(_SENSITIVITY_WARNING)
        written["path"] = path

    return _write, written


def _print_report(result: MigrationResult, schema_version: str) -> None:
    print(f"\nschema version: {schema_version or '(not reached)'}")

    if result.verification is not None:
        print("\nPer-table verification:")
        for t in result.verification.tables:
            hash_status = "match" if t.source_hash == t.target_hash else "MISMATCH"
            print(
                f"  {t.table:30s} rows: {t.source_row_count:>6} -> "
                f"{t.target_row_count:>6}   hash: {hash_status:8s}   "
                f"[{'OK' if t.ok else 'FAIL'}]"
            )
    elif result.export_row_counts:
        label = "expected row counts (dry run, nothing written)"
        print(f"\n{label}:")
        for name, count in sorted(result.export_row_counts.items()):
            print(f"  {name:30s} {count}")

    print(f"\nsource hash before: {result.source_hash_before}")
    print(f"source hash after:  {result.source_hash_after}")
    if result.source_hash_before != result.source_hash_after:
        print(
            "WARNING: the source database's fingerprint changed during this "
            "run -- this should never happen; treat the source as suspect."
        )

    print(f"\n{result.message}")
    print("PASS" if result.ok else "FAIL")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Copy the application database (key store, role grants, "
            "config-bundle history, feedback) between backends -- one "
            "shot, offline, verified, and reversible by virtue of the "
            "source being left untouched."
        )
    )
    parser.add_argument("--from", dest="source_url", required=True, help="Source SQLAlchemy URL")
    parser.add_argument("--to", dest="target_url", required=True, help="Target SQLAlchemy URL")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only -- refuses and reports exactly as a real run would, but writes nothing to the target",
    )
    parser.add_argument(
        "--keep-export", action="store_true",
        help="Keep the temporary export file even after a successful, verified migration",
    )
    args = parser.parse_args(argv)

    print(
        f"Migrating application database:\n"
        f"  from: {args.source_url}\n"
        f"  to:   {args.target_url}\n"
        f"  dry run: {args.dry_run}\n"
    )

    on_export = None
    written: dict[str, Path] = {}
    if not args.dry_run:
        on_export, written = _make_export_writer()

    result = run_migration(
        args.source_url, args.target_url, dry_run=args.dry_run, on_export=on_export,
    )
    _print_report(result, result.schema_version)

    export_path = written.get("path")
    if export_path is not None:
        if result.ok and not args.keep_export:
            export_path.unlink(missing_ok=True)
            print(f"\nDeleted export artefact at {export_path} (migration verified; pass --keep-export to retain it).")
        else:
            print(f"\nExport artefact left at {export_path} -- {_SENSITIVITY_WARNING}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
