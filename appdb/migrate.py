# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Move the application database between backends -- admin panel phase 5.

``docs/admin-panel-architecture.md`` §5.4 is the design contract this
module implements: ``SQLite -> SQL Server``, ``SQLite -> PostgreSQL``, and
the reverse, as one shot, offline, verified, and reversible by virtue of
the source being left untouched. ``scripts/migrate_app_db.py`` is the CLI
wrapper; every real decision lives here so it can be tested directly,
against real SQLAlchemy engines on both sides, with no mock at the
boundary under test.

Type mapping (§4 -- decided here, next to the schema)
-------------------------------------------------------
Every column :mod:`appdb.models` declares is ``String``, ``Integer``, or
``Text`` -- see that module's docstring. None of the three ambiguous
SQLAlchemy types the architecture doc calls out by name (``Boolean``,
``DateTime``, a bespoke JSON column type) appears anywhere in this schema:
timestamps are ISO-8601 strings in ``String`` columns (``appdb.key_store``
etc. all write ``datetime.now(timezone.utc).isoformat()``), JSON payloads
are pre-serialised to text before they are ever stored
(``denied_columns_json``, ``content_json``), and what would elsewhere be a
boolean flag is instead a nullable timestamp tombstone (``disabled_at``,
``revoked_at``) -- a deliberate choice in :mod:`appdb.models` itself that
sidesteps the "``1`` on one side, ``true`` on the other" hazard rather than
needing this module to paper over it. Copying therefore never touches raw
row values with anything but plain ``str``/``int``/``None`` in transit:
every read goes through :func:`sqlalchemy.select` against the *same*
``Table`` objects :mod:`appdb.models` defines, and every write goes
through the same tables' ``.insert()``, so SQLAlchemy Core's own,
already-correct per-dialect type compilation is what binds each value on
the way in and out -- never a hand-rolled string-to-native conversion this
module would have to get right a second time. If a future phase adds a
genuine ``Boolean``/``DateTime``/JSON-typed column, the mapping decision
belongs there, in :mod:`appdb.models`, next to that column -- not here.

Identifiers are preserved, never renumbered (§3)
---------------------------------------------------
Every row is copied with its primary key given explicitly
(``table.insert()`` with the source's own column values, including the
key column) rather than left for the target to assign -- the only way a
feedback row's ``config_version_id`` or a config version's own
``version_id`` can still resolve on the far side. For the two
autoincrement integer primary keys this schema has
(:data:`AUTOINCREMENT_PK_COLUMNS`), :func:`_reset_autoincrement` reseeds
the target's own sequence to ``MAX(id) + 1`` afterwards, per backend, so
the *next* row the running application inserts there (with no explicit id
of its own) never collides with one this tool just copied in.

It must be verifiable (§5)
------------------------------
:func:`verify_migration` is independent of the copy step -- it re-reads
both databases from scratch and compares, per table, row counts and a
content hash. The hash reuses :func:`eval.fingerprint.fingerprint_dataframe`
rather than a second hasher: the "same data regardless of row/column
order, robust to the usual numpy/Decimal/None noise" property that module
was built for is exactly what a cross-backend row comparison needs too.
:func:`hash_database` gives the same idea one level up -- a single
fingerprint of the *entire* source database, taken both before and after
every run (even a failed one) to prove this tool never mutated the source
it read from (§6).

Maintenance mode does not exist yet (§7)
--------------------------------------------
Phase 6 defines it. Until then, :func:`check_quiescent` (run as part of
:func:`export_database`, before anything is written anywhere) refuses
whenever the source's own timestamp columns show activity newer than
``cfg.settings.migration_quiet_window_seconds`` -- a refusal that names
maintenance mode explicitly in its message, rather than a tool that
silently copies past writes still in flight.

Schema versions must match (§8)
------------------------------------
:func:`current_schema_version` is Alembic's own head revision for
``appdb/migrations`` (not a number invented for this phase) -- read from
the migration scripts on disk, independent of what either database's own
``alembic_version`` table happens to say. Every export carries the
exporting process's current head as its stamp; :func:`check_schema_version`
refuses to import an export whose stamp does not match the *importing*
process's own head, exactly the case where the two sides are running code
that disagree about the shape of this schema.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Table, func, inspect, select, text
from sqlalchemy.engine import Engine

import config as cfg
from appdb.engine import _canonical_endpoint, build_engine
from appdb.models import (
    admin_api_keys,
    admin_principal_roles,
    config_bundle_versions,
    metadata,
    turn_feedback,
)
from eval.fingerprint import fingerprint_dataframe

#: Every table this tool moves, in the order rows are written on import.
#: None of these columns are declared with a real ``ForeignKey`` constraint
#: (see ``appdb/models.py`` -- the relationships are enforced by the
#: application, not the database), so this order is not required for
#: referential integrity to hold at the database level, but it is kept
#: parent-before-child anyway (a config version before the feedback rows
#: that may cite its id) so a partial copy interrupted mid-run is at least
#: never backwards.
TABLES_IN_MIGRATION_ORDER: tuple[Table, ...] = (
    admin_api_keys,
    admin_principal_roles,
    config_bundle_versions,
    turn_feedback,
)

#: ``{table_name: primary_key_column_name}`` for every table whose primary
#: key is a database-assigned autoincrement integer -- the only columns
#: :func:`_reset_autoincrement` ever needs to touch. ``admin_api_keys``
#: (keyed by ``key_sha256``) and ``admin_principal_roles`` (keyed by
#: ``(principal_id, capability)``) both have caller-supplied, non-integer
#: keys and need no reseeding.
AUTOINCREMENT_PK_COLUMNS: dict[str, str] = {
    "config_bundle_versions": "version_id",
    "turn_feedback": "feedback_id",
}

#: Column-name suffix :func:`_max_activity_timestamp` treats as a
#: timestamp worth scanning for recent activity. Every such column in this
#: schema stores ``datetime.now(timezone.utc).isoformat()`` (see the
#: module docstring's "Type mapping" section).
_TIMESTAMP_COLUMN_SUFFIX = "_at"


class MigrationRefusedError(RuntimeError):
    """The migration tool refused to proceed. The message says why --
    recent write activity, a schema-version mismatch, a non-empty target,
    or source and target naming the same database."""


@dataclass(frozen=True)
class TableSnapshot:
    """One table's rows, read at one point in time, plus its content hash."""

    name: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    row_count: int
    content_hash: str


@dataclass(frozen=True)
class MigrationExport:
    """Everything read from a source database -- "the exported artefact"
    (§9). Contains every issued key's ``key_sha256`` and every key's
    ``denied_columns_json`` ACL verbatim: not a secret in the ``.env``
    sense, but not something to leave in a shared folder either -- see
    ``scripts/migrate_app_db.py`` and ``docs/deployment-runbook.md``.
    """

    schema_version: str
    exported_at: str
    tables: dict[str, TableSnapshot]


@dataclass(frozen=True)
class TableVerification:
    """One table's post-copy comparison -- row counts and content hashes,
    read independently from both databases."""

    table: str
    source_row_count: int
    target_row_count: int
    source_hash: str
    target_hash: str

    @property
    def ok(self) -> bool:
        return (
            self.source_row_count == self.target_row_count
            and self.source_hash == self.target_hash
        )


@dataclass(frozen=True)
class VerificationReport:
    """Every table's :class:`TableVerification` from one comparison pass."""

    tables: tuple[TableVerification, ...]

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.tables)


@dataclass(frozen=True)
class MigrationResult:
    """The outcome of one :func:`run_migration` call -- always returned,
    never raised, for every refusal this module recognises (§11's "must say
    so, loudly, rather than reporting success" applies to a caller checking
    ``.ok``, not to catching an exception). A genuinely unexpected failure
    (e.g. the target is unreachable) still propagates as whatever
    SQLAlchemy raises -- that is an infrastructure fault, not a decision
    this tool makes."""

    dry_run: bool
    ok: bool
    message: str
    schema_version: str
    source_hash_before: str
    source_hash_after: str
    export_row_counts: dict[str, int]
    verification: VerificationReport | None


# ---------------------------------------------------------------------------
# Schema version (§8)
# ---------------------------------------------------------------------------

def current_schema_version() -> str:
    """Alembic's current head revision for ``appdb/migrations`` -- read from
    the migration scripts on disk (never from either database's own
    ``alembic_version`` table), so this is exactly the schema version the
    *running code* expects, independent of what has or has not actually
    been applied to any particular database.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_root = Path(__file__).resolve().parent.parent
    alembic_cfg = Config()
    alembic_cfg.set_main_option(
        "script_location", str(repo_root / "appdb" / "migrations")
    )
    return ScriptDirectory.from_config(alembic_cfg).get_current_head()


def check_schema_version(export: MigrationExport) -> None:
    """Refuse if *export*'s stamp does not match this installation's own
    current schema version (§8).

    Raises
    ------
    MigrationRefusedError
    """
    current = current_schema_version()
    if export.schema_version != current:
        raise MigrationRefusedError(
            "schema-version mismatch: this export was produced at schema "
            f"revision {export.schema_version!r}, but this installation's "
            f"migrations are at {current!r}. Run this installation's "
            "migrations against the target (`alembic upgrade head`) -- or "
            "re-export from a build whose revision matches -- before "
            "importing. Silently loading a different shape is how a "
            "subtly broken installation gets created."
        )


# ---------------------------------------------------------------------------
# Reading a table (used for both export and verification)
# ---------------------------------------------------------------------------

def _table_columns(table: Table) -> tuple[str, ...]:
    return tuple(c.name for c in table.columns)


def _rows_as_dataframe(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> pd.DataFrame:
    """A DataFrame with exactly *columns*, even when *rows* is empty --
    :func:`eval.fingerprint.fingerprint_dataframe` gives an empty frame a
    well-defined fingerprint only when its column set is present."""
    if rows:
        return pd.DataFrame(rows, columns=list(columns))
    return pd.DataFrame({c: pd.Series([], dtype="object") for c in columns})


def _empty_snapshot(table: Table) -> TableSnapshot:
    columns = _table_columns(table)
    return TableSnapshot(
        name=table.name,
        columns=columns,
        rows=(),
        row_count=0,
        content_hash=fingerprint_dataframe(_rows_as_dataframe([], columns)),
    )


def _table_snapshot(conn, table: Table) -> TableSnapshot:
    columns = _table_columns(table)
    rows = [dict(r) for r in conn.execute(select(table)).mappings().all()]
    df = _rows_as_dataframe(rows, columns)
    return TableSnapshot(
        name=table.name,
        columns=columns,
        rows=tuple(rows),
        row_count=len(rows),
        content_hash=fingerprint_dataframe(df),
    )


def _snapshot_all_tables(engine: Engine) -> dict[str, TableSnapshot]:
    insp = inspect(engine)
    out: dict[str, TableSnapshot] = {}
    with engine.connect() as conn:
        for table in TABLES_IN_MIGRATION_ORDER:
            out[table.name] = (
                _table_snapshot(conn, table)
                if insp.has_table(table.name)
                else _empty_snapshot(table)
            )
    return out


# ---------------------------------------------------------------------------
# Non-mutation proof (§6) -- a whole-database fingerprint
# ---------------------------------------------------------------------------

def hash_database(url: str) -> str:
    """A single fingerprint of every migrated table in the database at
    *url* -- computed before and after every :func:`run_migration` call
    (success or failure) to prove the source was never mutated.
    """
    engine = build_engine(url)
    try:
        per_table = {
            name: snapshot.content_hash
            for name, snapshot in _snapshot_all_tables(engine).items()
        }
        payload = json.dumps(per_table, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Recent-write-activity refusal (§7) -- the maintenance-mode stand-in
# ---------------------------------------------------------------------------

def _max_activity_timestamp(export: MigrationExport) -> datetime | None:
    """The most recent parseable ``*_at`` timestamp across every row of
    every table in *export*, or ``None`` if there is none (an empty
    database, or one whose rows carry no timestamp at all)."""
    latest: datetime | None = None
    for snapshot in export.tables.values():
        timestamp_columns = [c for c in snapshot.columns if c.endswith(_TIMESTAMP_COLUMN_SUFFIX)]
        if not timestamp_columns:
            continue
        for row in snapshot.rows:
            for col in timestamp_columns:
                value = row.get(col)
                if not value:
                    continue
                try:
                    parsed = datetime.fromisoformat(value)
                except (TypeError, ValueError):
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if latest is None or parsed > latest:
                    latest = parsed
    return latest


def check_quiescent(export: MigrationExport) -> None:
    """Refuse if *export* shows write activity inside the quiet window
    (``cfg.settings.migration_quiet_window_seconds``).

    Raises
    ------
    MigrationRefusedError
    """
    latest = _max_activity_timestamp(export)
    if latest is None:
        return
    window = cfg.settings.migration_quiet_window_seconds
    age_seconds = (datetime.now(timezone.utc) - latest).total_seconds()
    if age_seconds < window:
        raise MigrationRefusedError(
            "the source application database has write activity from "
            f"{max(age_seconds, 0.0):.1f}s ago, inside the {window:.0f}s "
            "quiet window this tool requires -- refusing to migrate. This "
            "tool requires the application to be stopped or in maintenance "
            "mode (maintenance mode itself does not exist yet -- see "
            "docs/admin-panel-architecture.md §3): a write that lands in "
            "the source after this tool has read past it is lost with no "
            "error at all. Stop the application (or wait for it to go "
            "quiet) and re-run."
        )


# ---------------------------------------------------------------------------
# Export (read-only against the source)
# ---------------------------------------------------------------------------

def export_database(source_url: str) -> MigrationExport:
    """Read every row of every migrated table from *source_url*.

    Read-only: nothing here ever writes to the source. Refuses
    (:class:`MigrationRefusedError`) if the source shows write activity
    inside the quiet window (:func:`check_quiescent`) -- checked here,
    before any target is ever touched.
    """
    engine = build_engine(source_url)
    try:
        tables = _snapshot_all_tables(engine)
    finally:
        engine.dispose()

    export = MigrationExport(
        schema_version=current_schema_version(),
        exported_at=datetime.now(timezone.utc).isoformat(),
        tables=tables,
    )
    check_quiescent(export)
    return export


def export_to_json(export: MigrationExport) -> str:
    """Serialise *export* -- the on-disk shape of "the exported artefact"
    (§9) that ``scripts/migrate_app_db.py`` writes to a temporary file.
    Sensitive: see the module docstring's :class:`MigrationExport` note."""
    payload = {
        "schema_version": export.schema_version,
        "exported_at": export.exported_at,
        "tables": {
            name: {
                "columns": list(snapshot.columns),
                "rows": [dict(r) for r in snapshot.rows],
                "row_count": snapshot.row_count,
                "content_hash": snapshot.content_hash,
            }
            for name, snapshot in export.tables.items()
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def export_from_json(payload: str) -> MigrationExport:
    """The inverse of :func:`export_to_json` -- rebuilds a
    :class:`MigrationExport` from a previously written export file."""
    data = json.loads(payload)
    tables = {
        name: TableSnapshot(
            name=name,
            columns=tuple(t["columns"]),
            rows=tuple(t["rows"]),
            row_count=t["row_count"],
            content_hash=t["content_hash"],
        )
        for name, t in data["tables"].items()
    }
    return MigrationExport(
        schema_version=data["schema_version"],
        exported_at=data["exported_at"],
        tables=tables,
    )


# ---------------------------------------------------------------------------
# Target-side checks
# ---------------------------------------------------------------------------

def check_not_same_database(source_url: str, target_url: str) -> None:
    """Refuse if *source_url* and *target_url* resolve to the same
    database -- reusing :func:`appdb.engine._canonical_endpoint`'s own
    after-parsing comparison (``localhost``/``127.0.0.1``, a resolved
    SQLite path, ...) rather than a second, string-only implementation.

    Raises
    ------
    MigrationRefusedError
    """
    if _canonical_endpoint(source_url) == _canonical_endpoint(target_url):
        raise MigrationRefusedError(
            "source and target resolve to the same database -- nothing to "
            f"migrate. source: {source_url!r}; target: {target_url!r}."
        )


def check_target_is_empty(engine: Engine) -> None:
    """Refuse if any migrated table already has rows in it -- this tool
    only migrates into an empty target, to avoid ambiguous merge semantics
    (does an existing row win, or the incoming one?) that nothing in the
    spec resolves.

    Raises
    ------
    MigrationRefusedError
    """
    insp = inspect(engine)
    with engine.connect() as conn:
        for table in TABLES_IN_MIGRATION_ORDER:
            if not insp.has_table(table.name):
                continue
            count = conn.execute(select(func.count()).select_from(table)).scalar()
            if count:
                raise MigrationRefusedError(
                    f"target table {table.name!r} already has {count} row(s) "
                    "-- this tool only migrates into an empty target. Point "
                    "--to at a fresh database, or clear it first."
                )


def _reset_autoincrement(conn, engine: Engine, table: Table, pk_column: str) -> None:
    """Reseed *table*'s autoincrement sequence to ``MAX(pk_column) + 1`` on
    the target, after inserting rows with explicit primary-key values --
    without this, the running application's *next* insert (which supplies
    no explicit id) could collide with one this tool just copied in.
    """
    max_id = conn.execute(select(func.max(table.c[pk_column]))).scalar()
    if max_id is None:
        return  # the table is empty after the copy -- nothing to reseed

    dialect = engine.dialect.name
    if dialect == "postgresql":
        conn.execute(
            text("SELECT setval(pg_get_serial_sequence(:t, :c), :v, true)"),
            {"t": table.name, "c": pk_column, "v": max_id},
        )
    elif dialect == "mssql":
        # DBCC CHECKIDENT does not accept the reseed value as a bind
        # parameter; table.name and max_id are both this module's own
        # values, never user input, so inlining them is safe here.
        conn.execute(text(f"DBCC CHECKIDENT ('{table.name}', RESEED, {int(max_id)})"))
    elif dialect == "mysql":
        conn.execute(text(f"ALTER TABLE {table.name} AUTO_INCREMENT = {int(max_id) + 1}"))
    elif dialect == "sqlite":
        # appdb.models declares these columns without Table's
        # sqlite_autoincrement=True kwarg, so the DDL SQLAlchemy emits for
        # them (see appdb.models's own module docstring) is a plain
        # "INTEGER PRIMARY KEY" -- a rowid alias, not a monotonic
        # AUTOINCREMENT counter. SQLite computes the next rowid as
        # MAX(rowid) + 1 from the table's *actual current contents* on
        # every insert that omits one, so there is no persistent counter
        # here to desync -- nothing to reset. Kept in step defensively only
        # if a sqlite_sequence table exists anyway (a future schema change
        # opting into AUTOINCREMENT).
        #
        # The inspector is built on *conn*, never on the engine. A SQLite
        # engine here uses ``StaticPool`` (see
        # ``appdb.engine.build_engine``), so every checkout shares one
        # DBAPI connection: ``inspect(engine)`` opens a second Connection
        # facade over the *same* underlying connection, and closing that
        # facade returns it to the pool, which resets it with a ROLLBACK.
        # That rollback lands on this transaction. Every row written so far
        # is discarded -- silently, with no exception and no warning, so
        # ``import_export`` returns normally having copied nothing.
        #
        # The per-table verification in :func:`verify_migration` is what
        # catches that, and is how it was found: every copy step reported
        # success and the target was empty.
        if inspect(conn).has_table("sqlite_sequence"):
            conn.execute(
                text("INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES (:n, :v)"),
                {"n": table.name, "v": max_id},
            )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def preview_migration(export: MigrationExport, target_url: str) -> dict[str, int]:
    """Read-only preview for ``--dry-run``: the same refusal checks a real
    import would make (schema version, empty target), but never creates a
    table or writes a row anywhere. Returns ``{table_name: row_count}`` for
    what a real import would copy.

    Raises
    ------
    MigrationRefusedError
    """
    check_schema_version(export)
    engine = build_engine(target_url)
    try:
        check_target_is_empty(engine)
    finally:
        engine.dispose()
    return {name: snapshot.row_count for name, snapshot in export.tables.items()}


def import_export(export: MigrationExport, target_url: str) -> None:
    """Write *export* into *target_url*: create tables if needed (idempotent,
    same as the zero-configuration path -- ``docs/admin-panel-architecture.md``
    §5.3), refuse if the target is not empty, then insert every row with its
    original primary key and reseed the two autoincrement sequences.

    Raises
    ------
    MigrationRefusedError
    """
    check_schema_version(export)
    engine = build_engine(target_url)
    try:
        metadata.create_all(engine, checkfirst=True)
        check_target_is_empty(engine)
        with engine.begin() as conn:
            for table in TABLES_IN_MIGRATION_ORDER:
                snapshot = export.tables[table.name]
                if not snapshot.rows:
                    continue
                conn.execute(table.insert(), [dict(r) for r in snapshot.rows])
                if table.name in AUTOINCREMENT_PK_COLUMNS:
                    _reset_autoincrement(
                        conn, engine, table, AUTOINCREMENT_PK_COLUMNS[table.name]
                    )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Verification (§5) -- independent of the copy step above
# ---------------------------------------------------------------------------

def verify_migration(source_url: str, target_url: str) -> VerificationReport:
    """Re-read both databases from scratch and compare, per table, row
    counts and content hash. Independent of :func:`import_export`: this
    reads both sides fresh, so it catches a target corrupted *after* a
    successful copy just as readily as an incomplete one.
    """
    source_engine = build_engine(source_url)
    target_engine = build_engine(target_url)
    try:
        source_tables = _snapshot_all_tables(source_engine)
        target_tables = _snapshot_all_tables(target_engine)
    finally:
        source_engine.dispose()
        target_engine.dispose()

    results = tuple(
        TableVerification(
            table=table.name,
            source_row_count=source_tables[table.name].row_count,
            target_row_count=target_tables[table.name].row_count,
            source_hash=source_tables[table.name].content_hash,
            target_hash=target_tables[table.name].content_hash,
        )
        for table in TABLES_IN_MIGRATION_ORDER
    )
    return VerificationReport(tables=results)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_migration(
    source_url: str,
    target_url: str,
    *,
    dry_run: bool = False,
    on_export: Callable[[MigrationExport], None] | None = None,
) -> MigrationResult:
    """Migrate the application database from *source_url* to *target_url*.

    Always returns a :class:`MigrationResult` -- every refusal this module
    recognises (recent write activity, a schema-version mismatch, a
    non-empty target, source and target naming the same database) sets
    ``.ok = False`` with an explanatory ``.message`` rather than raising, so
    a caller (the CLI, or a test) checks one flag instead of catching
    exceptions for expected outcomes. ``source_hash_before``/
    ``source_hash_after`` are always computed, including when this call
    fails, to prove the source was never mutated (§6).

    *on_export*, if given, is called once with the freshly read
    :class:`MigrationExport` for a real (non-dry-run) migration, after the
    quiescence check has passed and before anything is written to the
    target -- ``scripts/migrate_app_db.py`` uses this to persist "the
    exported artefact" (§9) to a temporary file at exactly the point it
    exists, rather than reading the source a second time to do so. Never
    called for a dry run, which writes nothing anywhere.

    A genuinely unexpected error (e.g. the target is unreachable) still
    propagates as whatever SQLAlchemy raises.
    """
    hash_before = hash_database(source_url)
    ok = True
    message = ""
    schema_version = ""
    verification: VerificationReport | None = None
    export_row_counts: dict[str, int] = {}

    try:
        check_not_same_database(source_url, target_url)
        export = export_database(source_url)
        schema_version = export.schema_version
        if dry_run:
            export_row_counts = preview_migration(export, target_url)
            message = "dry run: no rows written to the target"
        else:
            if on_export is not None:
                on_export(export)
            import_export(export, target_url)
            export_row_counts = {
                name: snapshot.row_count for name, snapshot in export.tables.items()
            }
            verification = verify_migration(source_url, target_url)
            if verification.ok:
                message = (
                    "migration verified: every table's row count and "
                    "content hash match on both sides"
                )
            else:
                ok = False
                message = (
                    "verification FAILED after copy -- the target must not "
                    "be used; see the per-table report"
                )
    except MigrationRefusedError as exc:
        ok = False
        message = str(exc)
    finally:
        hash_after = hash_database(source_url)

    return MigrationResult(
        dry_run=dry_run,
        ok=ok,
        message=message,
        schema_version=schema_version,
        source_hash_before=hash_before,
        source_hash_after=hash_after,
        export_row_counts=export_row_counts,
        verification=verification,
    )
