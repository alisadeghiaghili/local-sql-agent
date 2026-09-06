# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 6, §2 — schema drift. Frozen spec.

Exercises :func:`schema_data.drift.check_schema_drift` against a REAL
SQLite fixture warehouse (a temp file, no mocking at the boundary under
test) and a REAL, temp-file ``schema.yaml`` loaded through
``schema_data.registry`` -- never a fake/stubbed comparison function.
"""

from __future__ import annotations

import json

import pytest
import yaml
from sqlalchemy import create_engine, text

import schema_data.drift as drift_module
import schema_data.registry as registry_module
from config import override_settings
from schema_data.drift import check_schema_drift


def _write_schema(tmp_path, tables: dict) -> None:
    (tmp_path / "schema.yaml").write_text(
        yaml.dump({"tables": tables, "relationships": []}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


@pytest.fixture()
def schema_dir(tmp_path):
    d = tmp_path / "project_config"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    """schema_data.registry caches on first access and never re-reads on
    its own (config.override_settings changes WHERE it reads from, not
    WHEN) -- clear the module-level cache around every test in this file
    so each one sees its own temp schema.yaml, and restore a clean slate
    for whatever runs after this module."""
    registry_module._cache.clear()
    yield
    registry_module._cache.clear()


@pytest.fixture()
def baseline_file(tmp_path):
    path = tmp_path / "schema_drift_baseline.json"
    drift_module._DRIFT_BASELINE_FILE = str(path)
    yield path
    drift_module._DRIFT_BASELINE_FILE = ""


def _sqlite_engine(db_path, *, read_only: bool = False):
    if read_only:
        return create_engine(f"sqlite:///file:{db_path}?mode=ro&uri=true")
    return create_engine(f"sqlite:///{db_path}")


class TestThreeWayDiff:
    def test_all_three_sets_reported_against_a_fixture_warehouse(
        self, schema_dir, baseline_file, tmp_path,
    ):
        _write_schema(schema_dir, {
            "Widget": {
                "description": "a test table",
                "columns": {"ID": "primary key", "Retired": "no longer in the warehouse"},
            },
        })

        db_path = tmp_path / "warehouse.db"
        write_engine = create_engine(f"sqlite:///{db_path}")
        with write_engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE Widget (ID INTEGER, Name TEXT)"  # 'Retired' missing, 'Name' extra
            ))
            conn.execute(text("CREATE TABLE Ghost (X INTEGER)"))  # never in schema.yaml at all
        write_engine.dispose()

        read_engine = _sqlite_engine(db_path, read_only=True)
        try:
            with override_settings(project_config_dir=str(schema_dir)):
                registry_module._cache.clear()
                report = check_schema_drift(engine=read_engine)
        finally:
            read_engine.dispose()

        assert "Widget.Name" in report.warehouse_only
        assert "Ghost.X" in report.warehouse_only
        assert "Widget.Retired" in report.schema_only
        assert "Widget.ID" not in report.warehouse_only
        assert "Widget.ID" not in report.schema_only

    def test_type_change_detected_against_this_tools_own_baseline(
        self, schema_dir, baseline_file, tmp_path,
    ):
        _write_schema(schema_dir, {
            "Widget": {"description": "t", "columns": {"ID": "primary key"}},
        })
        db_path = tmp_path / "warehouse.db"
        write_engine = create_engine(f"sqlite:///{db_path}")
        with write_engine.begin() as conn:
            conn.execute(text("CREATE TABLE Widget (ID INTEGER)"))
        write_engine.dispose()

        # Seed a baseline claiming ID used to be TEXT -- deterministic,
        # rather than depending on SQLite's ALTER TABLE type support.
        baseline_file.write_text(
            json.dumps({"checked_at": "irrelevant", "types": {"Widget.ID": "text"}}),
            encoding="utf-8",
        )

        read_engine = _sqlite_engine(db_path, read_only=True)
        try:
            with override_settings(project_config_dir=str(schema_dir)):
                registry_module._cache.clear()
                report = check_schema_drift(engine=read_engine, persist_baseline=False)
        finally:
            read_engine.dispose()

        assert report.baseline_available is True
        assert {"column": "Widget.ID", "previous_type": "text", "current_type": "integer"} in (
            list(report.type_changed)
        )

    def test_no_prior_baseline_reports_zero_type_changes_and_says_so(
        self, schema_dir, baseline_file, tmp_path,
    ):
        _write_schema(schema_dir, {
            "Widget": {"description": "t", "columns": {"ID": "primary key"}},
        })
        db_path = tmp_path / "warehouse.db"
        write_engine = create_engine(f"sqlite:///{db_path}")
        with write_engine.begin() as conn:
            conn.execute(text("CREATE TABLE Widget (ID INTEGER)"))
        write_engine.dispose()

        assert not baseline_file.exists()
        read_engine = _sqlite_engine(db_path, read_only=True)
        try:
            with override_settings(project_config_dir=str(schema_dir)):
                registry_module._cache.clear()
                report = check_schema_drift(engine=read_engine)
        finally:
            read_engine.dispose()

        assert report.type_changed == ()
        assert report.baseline_available is False


class TestNeverApplies:
    def test_schema_yaml_is_byte_identical_after_a_clean_check(self, schema_dir, baseline_file, tmp_path):
        _write_schema(schema_dir, {
            "Widget": {"description": "t", "columns": {"ID": "primary key"}},
        })
        schema_path = schema_dir / "schema.yaml"
        before = schema_path.read_bytes()

        db_path = tmp_path / "warehouse.db"
        write_engine = create_engine(f"sqlite:///{db_path}")
        with write_engine.begin() as conn:
            conn.execute(text("CREATE TABLE Widget (ID INTEGER)"))
        write_engine.dispose()

        with override_settings(project_config_dir=str(schema_dir)):
            registry_module._cache.clear()
            check_schema_drift(engine=_sqlite_engine(db_path))

        assert schema_path.read_bytes() == before

    def test_schema_yaml_is_byte_identical_even_when_drift_is_found(
        self, schema_dir, baseline_file, tmp_path,
    ):
        """The whole point of §2: a drift check that FINDS differences
        must still never touch schema.yaml -- it proposes, it never
        applies."""
        _write_schema(schema_dir, {
            "Widget": {"description": "t", "columns": {"ID": "primary key", "Retired": "gone"}},
        })
        schema_path = schema_dir / "schema.yaml"
        before = schema_path.read_bytes()

        db_path = tmp_path / "warehouse.db"
        write_engine = create_engine(f"sqlite:///{db_path}")
        with write_engine.begin() as conn:
            conn.execute(text("CREATE TABLE Widget (ID INTEGER, NewColumn TEXT)"))
            conn.execute(text("CREATE TABLE BrandNewTable (Y INTEGER)"))
        write_engine.dispose()

        with override_settings(project_config_dir=str(schema_dir)):
            registry_module._cache.clear()
            report = check_schema_drift(engine=_sqlite_engine(db_path))

        assert report.warehouse_only or report.schema_only  # drift really was found
        assert schema_path.read_bytes() == before


class TestReadOnlyConnection:
    def test_works_against_a_read_only_sqlite_connection(self, schema_dir, baseline_file, tmp_path):
        """No elevated credentials: a SQLite connection opened with
        mode=ro (the DB file itself made genuinely read-only to this
        process) is sufficient -- there is no separate credential
        parameter anywhere on check_schema_drift's signature for one to
        even be supplied."""
        _write_schema(schema_dir, {
            "Widget": {"description": "t", "columns": {"ID": "primary key"}},
        })
        db_path = tmp_path / "warehouse.db"
        write_engine = create_engine(f"sqlite:///{db_path}")
        with write_engine.begin() as conn:
            conn.execute(text("CREATE TABLE Widget (ID INTEGER)"))
        write_engine.dispose()

        read_only_engine = _sqlite_engine(db_path, read_only=True)
        try:
            with override_settings(project_config_dir=str(schema_dir)):
                registry_module._cache.clear()
                report = check_schema_drift(engine=read_only_engine)
        finally:
            read_only_engine.dispose()

        assert report.warehouse_only == ()
        assert report.schema_only == ()

    def test_signature_has_no_credential_parameter(self):
        import inspect

        params = inspect.signature(check_schema_drift).parameters
        assert set(params) == {"engine", "persist_baseline"}
