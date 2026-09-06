# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 5 -- ``appdb.migrate`` (spec §11, architecture §5.4).

Every test uses real SQLAlchemy engines against real SQLite files on real
``tmp_path`` directories on both sides of a migration -- no mock at the
boundary under test. Only SQLite<->SQLite is exercised here: no PostgreSQL
or SQL Server is reachable in this environment, so the PostgreSQL/SQL
Server branches of ``appdb.migrate._reset_autoincrement`` (and any other
backend-specific code path) are written and reviewed but not run by this
suite.

Fixtures deliberately insert rows with explicit, long-past ``created_at``
timestamps (``_OLD_TIMESTAMP``) rather than going through the real
``appdb.key_store``/``appdb.feedback`` write paths, which always stamp the
current time -- using those directly here would make every happy-path
fixture look like "recent write activity" to
``appdb.migrate.check_quiescent`` and force every test to override the
quiet window just to construct its source database. The one test that
means to exercise that refusal (``TestRecentWriteActivityRefused``)
inserts a row stamped with the real current time instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select

import config as cfg
from appdb.engine import build_engine
from appdb.migrate import (
    MigrationRefusedError,
    check_schema_version,
    current_schema_version,
    export_database,
    export_from_json,
    export_to_json,
    hash_database,
    import_export,
    preview_migration,
    run_migration,
    verify_migration,
)
from appdb.models import (
    admin_api_keys,
    admin_principal_roles,
    config_bundle_versions,
    create_all,
    turn_feedback,
)

_OLD_TIMESTAMP = "2000-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _seed_source(db_path) -> str:
    """A fresh SQLite database at *db_path*, with every table created and
    one representative, long-backdated row in each -- the happy-path
    source most tests below share."""
    url = f"sqlite:///{db_path}"
    engine = build_engine(url)
    create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            admin_api_keys.insert().values(
                key_sha256="a" * 64,
                principal_id="analyst-1",
                name="Analyst One",
                denied_columns_json=json.dumps(["Phone"]),
                source="issued",
                created_at=_OLD_TIMESTAMP,
                updated_at=_OLD_TIMESTAMP,
                disabled_at=None,
                revoked_at=None,
            )
        )
        conn.execute(
            admin_principal_roles.insert().values(
                principal_id="analyst-1",
                capability="operations",
                granted_at=_OLD_TIMESTAMP,
                granted_by="bootstrap",
            )
        )
        conn.execute(
            config_bundle_versions.insert().values(
                version_id=1,
                status="applied",
                content_json=json.dumps({"schema.yaml": "tables: []"}),
                content_hash="deadbeef",
                based_on_version=None,
                restored_from_version=None,
                restored_file=None,
                created_at=_OLD_TIMESTAMP,
                created_by="bootstrap",
                created_by_capability="operations",
                reviewed_by=None,
                reviewed_at=None,
                review_note=None,
                diff_json=None,
                dry_run_json=None,
            )
        )
        conn.execute(
            turn_feedback.insert().values(
                feedback_id=1,
                session_id="s1",
                turn_id="t1",
                request_id="r1",
                reporter_principal_id="analyst-1",
                category="wrong_number",
                note=None,
                config_version_id=1,
                created_at=_OLD_TIMESTAMP,
                status="open",
                resolution_outcome=None,
                resolution_note=None,
                resolution_config_version_id=None,
                resolution_golden_case_id=None,
                resolved_by=None,
                resolved_at=None,
            )
        )
    engine.dispose()
    return url


def _target_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'target.db'}"


def _row_count(url: str, table) -> int:
    engine = build_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(select(table)).mappings().all().__len__()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_sqlite_to_sqlite_round_trip_hashes_match(self, tmp_path):
        source_url = _seed_source(tmp_path / "source.db")
        target_url = _target_url(tmp_path)

        result = run_migration(source_url, target_url)

        assert result.ok, result.message
        assert not result.dry_run
        assert result.verification is not None
        assert result.verification.ok
        for t in result.verification.tables:
            assert t.source_row_count == t.target_row_count > 0, t.table
            assert t.source_hash == t.target_hash, t.table

    def test_round_trip_preserves_plain_python_types(self, tmp_path):
        """Five things most likely to go wrong, #4: an integer must stay an
        integer, not become a string, across the copy."""
        source_url = _seed_source(tmp_path / "source.db")
        target_url = _target_url(tmp_path)

        assert run_migration(source_url, target_url).ok

        engine = build_engine(target_url)
        with engine.connect() as conn:
            row = conn.execute(select(config_bundle_versions)).mappings().first()
        engine.dispose()
        assert isinstance(row["version_id"], int)
        assert row["version_id"] == 1


# ---------------------------------------------------------------------------
# Identifiers survive, never renumbered
# ---------------------------------------------------------------------------

class TestIdentifiersPreserved:
    def test_explicit_ids_with_a_gap_are_not_renumbered(self, tmp_path):
        source_url = f"sqlite:///{tmp_path / 'source.db'}"
        engine = build_engine(source_url)
        create_all(engine)
        with engine.begin() as conn:
            for version_id in (1, 5, 9):
                conn.execute(
                    config_bundle_versions.insert().values(
                        version_id=version_id,
                        status="applied",
                        content_json="{}",
                        content_hash=f"hash{version_id}",
                        based_on_version=None,
                        restored_from_version=None,
                        restored_file=None,
                        created_at=_OLD_TIMESTAMP,
                        created_by="bootstrap",
                        created_by_capability="operations",
                        reviewed_by=None,
                        reviewed_at=None,
                        review_note=None,
                        diff_json=None,
                        dry_run_json=None,
                    )
                )
        engine.dispose()
        target_url = _target_url(tmp_path)

        result = run_migration(source_url, target_url)
        assert result.ok, result.message

        target_engine = build_engine(target_url)
        with target_engine.connect() as conn:
            ids = {
                row[0]
                for row in conn.execute(select(config_bundle_versions.c.version_id))
            }
        assert ids == {1, 5, 9}, "renumbered on insert -- exactly what the spec forbids"

        # A subsequent, ordinary insert (no explicit id, mirroring how the
        # running application creates the *next* version) must not collide
        # with anything just migrated in, proving the sequence was reset
        # (or, for SQLite specifically, needed no reset -- see
        # appdb.migrate._reset_autoincrement's own comment).
        with target_engine.begin() as conn:
            conn.execute(
                config_bundle_versions.insert().values(
                    status="draft",
                    content_json="{}",
                    content_hash="new",
                    based_on_version=9,
                    restored_from_version=None,
                    restored_file=None,
                    created_at=_OLD_TIMESTAMP,
                    created_by="analyst-1",
                    created_by_capability="operations",
                    reviewed_by=None,
                    reviewed_at=None,
                    review_note=None,
                    diff_json=None,
                    dry_run_json=None,
                )
            )
            new_id = conn.execute(
                select(config_bundle_versions.c.version_id).where(
                    config_bundle_versions.c.status == "draft"
                )
            ).scalar()
        target_engine.dispose()
        assert new_id is not None and new_id > 9, (
            f"new row got id {new_id!r}, which collides with or precedes "
            "an id already occupied by a migrated row"
        )

    def test_feedback_join_to_config_version_resolves_on_the_far_side(self, tmp_path):
        """The exact scenario spec §11 names: a feedback row referencing a
        turn (and, here, the config version active when that turn ran)
        must still resolve after migration -- not become a dangling id."""
        source_url = f"sqlite:///{tmp_path / 'source.db'}"
        engine = build_engine(source_url)
        create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                config_bundle_versions.insert().values(
                    version_id=7,
                    status="applied",
                    content_json="{}",
                    content_hash="h7",
                    based_on_version=None,
                    restored_from_version=None,
                    restored_file=None,
                    created_at=_OLD_TIMESTAMP,
                    created_by="bootstrap",
                    created_by_capability="operations",
                    reviewed_by=None,
                    reviewed_at=None,
                    review_note=None,
                    diff_json=None,
                    dry_run_json=None,
                )
            )
            conn.execute(
                turn_feedback.insert().values(
                    feedback_id=42,
                    session_id="session-xyz",
                    turn_id="turn-abc",
                    request_id="req-123",
                    reporter_principal_id="analyst-1",
                    category="wrong_number",
                    note=None,
                    config_version_id=7,
                    created_at=_OLD_TIMESTAMP,
                    status="open",
                    resolution_outcome=None,
                    resolution_note=None,
                    resolution_config_version_id=None,
                    resolution_golden_case_id=None,
                    resolved_by=None,
                    resolved_at=None,
                )
            )
        engine.dispose()
        target_url = _target_url(tmp_path)

        result = run_migration(source_url, target_url)
        assert result.ok, result.message

        target_engine = build_engine(target_url)
        with target_engine.connect() as conn:
            feedback_row = conn.execute(
                select(turn_feedback).where(turn_feedback.c.feedback_id == 42)
            ).mappings().first()
            joined_version = conn.execute(
                select(config_bundle_versions).where(
                    config_bundle_versions.c.version_id == feedback_row["config_version_id"]
                )
            ).mappings().first()
        target_engine.dispose()

        # session_id/turn_id are the join key into the (out-of-scope-for-this-
        # migration) audit log -- must survive byte-for-byte.
        assert feedback_row["session_id"] == "session-xyz"
        assert feedback_row["turn_id"] == "turn-abc"
        # And the in-database join (feedback -> config version) must
        # actually resolve to a real row, not a dangling id.
        assert joined_version is not None
        assert joined_version["version_id"] == 7


# ---------------------------------------------------------------------------
# Verification is real
# ---------------------------------------------------------------------------

class TestVerificationCatchesCorruption:
    def test_verify_fails_after_target_row_is_corrupted(self, tmp_path):
        source_url = _seed_source(tmp_path / "source.db")
        target_url = _target_url(tmp_path)
        result = run_migration(source_url, target_url)
        assert result.ok, result.message
        assert verify_migration(source_url, target_url).ok

        target_engine = build_engine(target_url)
        with target_engine.begin() as conn:
            conn.execute(
                admin_api_keys.update()
                .where(admin_api_keys.c.key_sha256 == "a" * 64)
                .values(name="TAMPERED")
            )
        target_engine.dispose()

        report = verify_migration(source_url, target_url)
        assert not report.ok
        by_table = {t.table: t for t in report.tables}
        corrupted = by_table["admin_api_keys"]
        assert not corrupted.ok
        assert corrupted.source_hash != corrupted.target_hash
        # Row counts alone must NOT have caught this -- proving the hash,
        # not the count, is what is doing the work.
        assert corrupted.source_row_count == corrupted.target_row_count

        # Every other table is untouched and must still report OK -- a
        # real per-table comparison, not one bit flipped for everything.
        for name, t in by_table.items():
            if name != "admin_api_keys":
                assert t.ok, name


# ---------------------------------------------------------------------------
# The source is never mutated
# ---------------------------------------------------------------------------

class TestSourceNeverMutated:
    def test_source_hash_unchanged_after_successful_migration(self, tmp_path):
        source_url = _seed_source(tmp_path / "source.db")
        target_url = _target_url(tmp_path)
        before = hash_database(source_url)

        result = run_migration(source_url, target_url)
        assert result.ok, result.message

        after = hash_database(source_url)
        assert before == after
        assert result.source_hash_before == result.source_hash_after == before

    def test_source_hash_unchanged_after_a_failed_migration(self, tmp_path):
        source_url = _seed_source(tmp_path / "source.db")
        before = hash_database(source_url)

        # Migrating a database into itself is refused (a failure mode with
        # nothing target-side to even build) -- the source must still be
        # provably untouched.
        result = run_migration(source_url, source_url)
        assert not result.ok
        assert "same database" in result.message

        after = hash_database(source_url)
        assert before == after
        assert result.source_hash_before == result.source_hash_after == before

    def test_source_hash_unchanged_after_quiescence_refusal(self, tmp_path):
        source_url = f"sqlite:///{tmp_path / 'source.db'}"
        engine = build_engine(source_url)
        create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                admin_api_keys.insert().values(
                    key_sha256="b" * 64,
                    principal_id="analyst-2",
                    name="Analyst Two",
                    denied_columns_json="[]",
                    source="issued",
                    created_at=datetime.now(timezone.utc).isoformat(),
                    updated_at=_OLD_TIMESTAMP,
                    disabled_at=None,
                    revoked_at=None,
                )
            )
        engine.dispose()
        target_url = _target_url(tmp_path)
        before = hash_database(source_url)

        result = run_migration(source_url, target_url)
        assert not result.ok
        assert "maintenance mode" in result.message

        after = hash_database(source_url)
        assert before == after


# ---------------------------------------------------------------------------
# Schema-version mismatch is refused
# ---------------------------------------------------------------------------

class TestSchemaVersionMismatch:
    def test_import_refuses_a_mismatched_stamp(self, tmp_path):
        source_url = _seed_source(tmp_path / "source.db")
        target_url = _target_url(tmp_path)

        export = export_database(source_url)
        assert export.schema_version == current_schema_version()

        tampered_payload = json.loads(export_to_json(export))
        tampered_payload["schema_version"] = "0000_not_a_real_revision"
        tampered_export = export_from_json(json.dumps(tampered_payload))

        with pytest.raises(MigrationRefusedError, match="schema-version mismatch"):
            check_schema_version(tampered_export)

        with pytest.raises(MigrationRefusedError, match="schema-version mismatch"):
            import_export(tampered_export, target_url)

        with pytest.raises(MigrationRefusedError, match="schema-version mismatch"):
            preview_migration(tampered_export, target_url)

    def test_export_round_trips_through_json_unchanged(self, tmp_path):
        """Sanity check for the (de)serialisation the mismatch test above
        (and the CLI's on-disk artefact) both depend on."""
        source_url = _seed_source(tmp_path / "source.db")
        export = export_database(source_url)
        round_tripped = export_from_json(export_to_json(export))
        assert round_tripped.schema_version == export.schema_version
        assert set(round_tripped.tables) == set(export.tables)
        for name, snapshot in export.tables.items():
            assert round_tripped.tables[name].content_hash == snapshot.content_hash
            assert round_tripped.tables[name].row_count == snapshot.row_count


# ---------------------------------------------------------------------------
# Recent write activity is refused, naming maintenance mode
# ---------------------------------------------------------------------------

class TestRecentWriteActivityRefused:
    def test_refuses_with_a_row_written_just_now(self, tmp_path):
        source_url = f"sqlite:///{tmp_path / 'source.db'}"
        engine = build_engine(source_url)
        create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                admin_api_keys.insert().values(
                    key_sha256="c" * 64,
                    principal_id="analyst-3",
                    name="Analyst Three",
                    denied_columns_json="[]",
                    source="issued",
                    created_at=datetime.now(timezone.utc).isoformat(),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    disabled_at=None,
                    revoked_at=None,
                )
            )
        engine.dispose()
        target_url = _target_url(tmp_path)

        with cfg.override_settings(migration_quiet_window_seconds=120.0):
            result = run_migration(source_url, target_url)

        assert not result.ok
        assert "maintenance mode" in result.message
        assert "recent write activity is refused" not in result.message  # sanity: no leftover template text

    def test_old_writes_outside_the_window_are_allowed(self, tmp_path):
        source_url = _seed_source(tmp_path / "source.db")
        target_url = _target_url(tmp_path)

        with cfg.override_settings(migration_quiet_window_seconds=60.0):
            result = run_migration(source_url, target_url)

        assert result.ok, result.message

    def test_empty_source_has_no_activity_to_refuse(self, tmp_path):
        source_url = f"sqlite:///{tmp_path / 'source.db'}"
        engine = build_engine(source_url)
        create_all(engine)
        engine.dispose()
        target_url = _target_url(tmp_path)

        result = run_migration(source_url, target_url)
        assert result.ok, result.message


# ---------------------------------------------------------------------------
# Dry run writes nothing
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_reports_counts_and_writes_no_rows(self, tmp_path):
        source_url = _seed_source(tmp_path / "source.db")
        target_url = _target_url(tmp_path)

        result = run_migration(source_url, target_url, dry_run=True)

        assert result.ok, result.message
        assert result.dry_run
        assert result.verification is None
        assert result.export_row_counts["admin_api_keys"] == 1
        assert result.export_row_counts["turn_feedback"] == 1

        target_engine = build_engine(target_url)
        insp = inspect(target_engine)
        for table_name in (
            "admin_api_keys", "admin_principal_roles",
            "config_bundle_versions", "turn_feedback",
        ):
            assert not insp.has_table(table_name), (
                f"dry run created {table_name!r} on the target"
            )
        target_engine.dispose()

    def test_dry_run_also_enforces_quiescence(self, tmp_path):
        """A dry run is meant to rehearse the real thing under the same
        preconditions -- it must refuse exactly like a real run would, not
        lull an operator into believing a live application is safe to
        leave running."""
        source_url = f"sqlite:///{tmp_path / 'source.db'}"
        engine = build_engine(source_url)
        create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                admin_api_keys.insert().values(
                    key_sha256="d" * 64,
                    principal_id="analyst-4",
                    name="Analyst Four",
                    denied_columns_json="[]",
                    source="issued",
                    created_at=datetime.now(timezone.utc).isoformat(),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    disabled_at=None,
                    revoked_at=None,
                )
            )
        engine.dispose()
        target_url = _target_url(tmp_path)

        with cfg.override_settings(migration_quiet_window_seconds=120.0):
            result = run_migration(source_url, target_url, dry_run=True)

        assert not result.ok
        assert "maintenance mode" in result.message


# ---------------------------------------------------------------------------
# Target must be empty
# ---------------------------------------------------------------------------

class TestTargetMustBeEmpty:
    def test_refuses_a_non_empty_target(self, tmp_path):
        source_url = _seed_source(tmp_path / "source.db")
        target_url = _seed_source(tmp_path / "also_seeded.db")

        result = run_migration(source_url, target_url)
        assert not result.ok
        assert "already has" in result.message


# ---------------------------------------------------------------------------
# Source and target must differ
# ---------------------------------------------------------------------------

class TestNotSameDatabase:
    def test_refuses_migrating_a_database_into_itself(self, tmp_path):
        source_url = _seed_source(tmp_path / "source.db")
        result = run_migration(source_url, source_url)
        assert not result.ok
        assert "same database" in result.message


# ---------------------------------------------------------------------------
# The copy transaction must survive every step taken inside it
# ---------------------------------------------------------------------------

class TestNothingInTheCopyOpensASecondConnection:
    """A regression test for a bug that copied nothing and said so nowhere.

    ``_reset_autoincrement`` asked whether a ``sqlite_sequence`` table
    exists, and asked it through ``inspect(engine)`` -- which checks out a
    *second* connection to the same SQLite file. Doing that from inside the
    copy's open write transaction discarded every row the transaction had
    written: no exception, no warning, ``import_export`` returning normally
    having copied nothing at all.

    The per-table verification is what caught it, which is the argument for
    having built the verification: every individual step reported success
    and the target was empty. Nothing else in the tool would have noticed.

    This test asserts the property rather than the call, so it still holds
    if the inspector call moves or is replaced by some other lookup that
    reaches for the engine.
    """

    def test_a_read_only_lookup_inside_the_copy_does_not_discard_it(self, tmp_path):
        from sqlalchemy import func, inspect, select

        from appdb.migrate import (
            TABLES_IN_MIGRATION_ORDER,
            build_engine,
            export_database,
            metadata,
        )

        source_url = _seed_source(tmp_path / "source.db")
        target_url = _target_url(tmp_path)
        export = export_database(source_url)

        engine = build_engine(target_url)
        try:
            metadata.create_all(engine, checkfirst=True)
            with engine.begin() as conn:
                for table in TABLES_IN_MIGRATION_ORDER:
                    snapshot = export.tables[table.name]
                    if snapshot.rows:
                        conn.execute(table.insert(), [dict(r) for r in snapshot.rows])
                # The lookup _reset_autoincrement performs, on the
                # transaction's own connection. Reading through the engine
                # here is what silently threw the copy away.
                inspect(conn).has_table("sqlite_sequence")
            with engine.connect() as conn:
                counts = {
                    table.name: conn.execute(
                        select(func.count()).select_from(table)
                    ).scalar()
                    for table in TABLES_IN_MIGRATION_ORDER
                }
        finally:
            engine.dispose()

        assert all(count > 0 for count in counts.values()), (
            "the copy transaction was discarded by a read-only lookup taken "
            f"inside it -- target row counts after commit: {counts}"
        )

    def test_a_full_migration_actually_writes_rows(self, tmp_path):
        """The end-to-end shape of the same bug: every step reports success
        and the target is empty."""
        source_url = _seed_source(tmp_path / "source.db")
        target_url = _target_url(tmp_path)

        result = run_migration(source_url, target_url)

        assert result.verification is not None
        empty = [t.table for t in result.verification.tables if t.target_row_count == 0]
        assert not empty, (
            f"these tables were copied but landed empty on the target: {empty}"
        )
