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


class TestMultipleDataSources:
    """Multiple warehouse data sources (``database.datasources``): each
    source's tables are scanned against that source's own engine -- see
    :func:`schema_data.drift._scan_every_datasource`. Real SQLite fixture
    warehouses throughout, one per source, exactly like the rest of this
    file; only ``database.connection.get_engine`` is patched, to route
    each source name to its own fixture engine without a real connection
    string anywhere."""

    def _sources_project_dir(self, schema_dir) -> None:
        (schema_dir / "datasources.yaml").write_text(
            "default: main\n"
            "datasources:\n"
            "  main:\n    url_env: DB_URL_MAIN\n"
            "  archive:\n    url_env: DB_URL_ARCHIVE\n",
            encoding="utf-8",
        )

    def test_each_sources_tables_are_scanned_against_its_own_warehouse(
        self, schema_dir, baseline_file, tmp_path,
    ):
        import database.datasources as datasources_module

        self._sources_project_dir(schema_dir)
        _write_schema(schema_dir, {
            "Widget": {
                "description": "t", "datasource": "main",
                "columns": {"ID": "primary key"},
            },
            "Gadget": {
                "description": "t", "datasource": "archive",
                "columns": {"ID": "primary key"},
            },
        })

        main_db = tmp_path / "main.db"
        archive_db = tmp_path / "archive.db"
        main_engine = create_engine(f"sqlite:///{main_db}")
        with main_engine.begin() as conn:
            conn.execute(text("CREATE TABLE Widget (ID INTEGER, Extra TEXT)"))
        archive_engine = create_engine(f"sqlite:///{archive_db}")
        with archive_engine.begin() as conn:
            conn.execute(text("CREATE TABLE Gadget (ID INTEGER)"))

        engines_by_name = {"main": main_engine, "archive": archive_engine}
        try:
            with override_settings(project_config_dir=str(schema_dir)):
                registry_module._cache.clear()
                datasources_module.reset_datasources_cache()
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(
                        "database.connection.get_engine",
                        lambda name=None: engines_by_name[name or "main"],
                    )
                    report = check_schema_drift()
        finally:
            main_engine.dispose()
            archive_engine.dispose()
            registry_module._cache.clear()
            datasources_module.reset_datasources_cache()

        # Widget's extra column was found on "main"; Gadget matched
        # "archive" exactly -- proof each table was checked against its
        # OWN source, not against whichever engine happened to run first.
        assert "Widget.Extra" in report.warehouse_only
        assert report.schema_only == ()

    def test_an_unreachable_source_does_not_hide_the_others(
        self, schema_dir, baseline_file, tmp_path,
    ):
        import database.datasources as datasources_module

        self._sources_project_dir(schema_dir)
        _write_schema(schema_dir, {
            "Widget": {
                "description": "t", "datasource": "main",
                "columns": {"ID": "primary key"},
            },
            "Gadget": {
                "description": "t", "datasource": "archive",
                "columns": {"ID": "primary key"},
            },
        })

        main_db = tmp_path / "main.db"
        main_engine = create_engine(f"sqlite:///{main_db}")
        with main_engine.begin() as conn:
            conn.execute(text("CREATE TABLE Widget (ID INTEGER)"))

        def _get_engine(name=None):
            if name == "archive":
                raise RuntimeError("archive unreachable")
            return main_engine

        try:
            with override_settings(project_config_dir=str(schema_dir)):
                registry_module._cache.clear()
                datasources_module.reset_datasources_cache()
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr("database.connection.get_engine", _get_engine)
                    report = check_schema_drift()
        finally:
            main_engine.dispose()
            registry_module._cache.clear()
            datasources_module.reset_datasources_cache()

        assert "Gadget" in report.unverifiable_tables
        assert "Widget" not in report.unverifiable_tables


class TestSourceWithNoTables:
    def test_a_source_that_owns_no_table_is_never_connected_to(
        self, schema_dir, baseline_file, tmp_path,
    ):
        """A configured source no ``schema.yaml`` table belongs to has
        nothing to compare, so its engine is never built."""
        import database.datasources as datasources_module

        (schema_dir / "datasources.yaml").write_text(
            "default: main\n"
            "datasources:\n"
            "  main:\n    url_env: DB_URL_MAIN\n"
            "  archive:\n    url_env: DB_URL_ARCHIVE\n",
            encoding="utf-8",
        )
        _write_schema(schema_dir, {
            "Widget": {"description": "t", "columns": {"ID": "primary key"}},
        })
        main_engine = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
        with main_engine.begin() as conn:
            conn.execute(text("CREATE TABLE Widget (ID INTEGER)"))
        requested: list[str | None] = []

        def _get_engine(name=None):
            requested.append(name)
            return main_engine

        try:
            with override_settings(project_config_dir=str(schema_dir)):
                registry_module._cache.clear()
                datasources_module.reset_datasources_cache()
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr("database.connection.get_engine", _get_engine)
                    report = check_schema_drift()
        finally:
            main_engine.dispose()
            registry_module._cache.clear()
            datasources_module.reset_datasources_cache()

        assert requested == ["main"]
        assert report.schema_only == ()


class TestSharedTablesAndPlacement:
    """A table in several sources is checked in EACH of them, and a table
    missing from its assigned source but present in another is reported
    with the ``datasource:`` line to write (``misplaced_tables``)."""

    def _project(self, schema_dir, tables: dict, sources=("main", "archive")) -> None:
        (schema_dir / "datasources.yaml").write_text(
            f"default: {sources[0]}\ndatasources:\n"
            + "".join(f"  {s}:\n    url_env: DB_URL_{s.upper()}\n" for s in sources),
            encoding="utf-8",
        )
        _write_schema(schema_dir, tables)

    def _warehouses(self, tmp_path, contents: dict[str, list[str]]) -> dict:
        engines = {}
        for name, statements in contents.items():
            engine = create_engine(f"sqlite:///{tmp_path / (name + '.db')}")
            with engine.begin() as conn:
                for statement in statements:
                    conn.execute(text(statement))
            engines[name] = engine
        return engines

    def _run(self, schema_dir, engines, requested=None):
        import database.datasources as datasources_module

        def _get_engine(name=None):
            if requested is not None:
                requested.append(name)
            return engines[name or "main"]

        try:
            with override_settings(project_config_dir=str(schema_dir)):
                registry_module._cache.clear()
                datasources_module.reset_datasources_cache()
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr("database.connection.get_engine", _get_engine)
                    return check_schema_drift()
        finally:
            for engine in engines.values():
                engine.dispose()
            registry_module._cache.clear()
            datasources_module.reset_datasources_cache()

    def test_a_shared_table_is_checked_in_each_source_and_reported_per_source(
        self, schema_dir, baseline_file, tmp_path,
    ):
        self._project(schema_dir, {
            "Date": {
                "description": "t", "datasource": ["main", "archive"],
                "columns": {"ID": "pk", "Name": "name"},
            },
            "Widget": {"description": "t", "columns": {"ID": "pk"}},
        })
        engines = self._warehouses(tmp_path, {
            "main": [
                "CREATE TABLE Date (ID INTEGER, Name TEXT, Extra TEXT)",
                "CREATE TABLE Widget (ID INTEGER, Spare TEXT)",
            ],
            "archive": ["CREATE TABLE Date (ID INTEGER)"],
        })
        report = self._run(schema_dir, engines)

        # The column archive's copy lacks, and the one main's copy adds,
        # are reported against THEIR source -- not merged away.
        assert "Date.Name [archive]" in report.schema_only
        assert "Date.Name [main]" not in report.schema_only
        assert "Date.Extra [main]" in report.warehouse_only
        # A table in one source keeps the plain id it always had.
        assert "Widget.Spare" in report.warehouse_only
        assert report.unverifiable_tables == ()
        assert report.misplaced_tables == ()

    def test_a_table_missing_from_its_source_but_in_another_gets_a_hint(
        self, schema_dir, baseline_file, tmp_path,
    ):
        self._project(schema_dir, {
            "Broker": {"description": "t", "columns": {"ID": "pk", "Code": "code"}},
            "Widget": {"description": "t", "columns": {"ID": "pk"}},
        })
        engines = self._warehouses(tmp_path, {
            "main": ["CREATE TABLE Widget (ID INTEGER)"],
            "archive": ["CREATE TABLE Broker (ID INTEGER, Code TEXT)"],
        })
        report = self._run(schema_dir, engines)

        assert report.misplaced_tables == ({
            "table": "Broker",
            "assigned": ["main"],
            "missing_from": ["main"],
            "found_in": ["archive"],
            "suggested_datasource": "archive",
            "hint": "Broker: not in main, found in archive — set datasource: archive",
        },)
        assert report.as_dict()["misplaced_tables"] == list(report.misplaced_tables)
        # The column-level drift is still reported as before.
        assert {"Broker.ID", "Broker.Code"} <= set(report.schema_only)

    def test_a_shared_table_missing_from_one_of_its_sources_is_hinted(
        self, schema_dir, baseline_file, tmp_path,
    ):
        self._project(schema_dir, {
            "Date": {
                "description": "t", "datasource": ["main", "archive"],
                "columns": {"ID": "pk"},
            },
        })
        engines = self._warehouses(tmp_path, {
            "main": ["CREATE TABLE Other (X INTEGER)"],
            "archive": ["CREATE TABLE Date (ID INTEGER)"],
        })
        report = self._run(schema_dir, engines)

        (hint,) = report.misplaced_tables
        assert hint["missing_from"] == ["main"]
        assert hint["found_in"] == ["archive"]
        assert hint["hint"].endswith("set datasource: archive")

    def test_a_table_found_in_several_other_sources_suggests_a_list(
        self, schema_dir, baseline_file, tmp_path,
    ):
        self._project(schema_dir, {
            "Date": {"description": "t", "columns": {"ID": "pk"}},
            "Widget": {"description": "t", "columns": {"ID": "pk"}},
        }, sources=("main", "archive", "cold"))
        engines = self._warehouses(tmp_path, {
            "main": ["CREATE TABLE Widget (ID INTEGER)"],
            "archive": ["CREATE TABLE Date (ID INTEGER)"],
            "cold": ["CREATE TABLE Date (ID INTEGER)"],
        })
        report = self._run(schema_dir, engines)

        (hint,) = report.misplaced_tables
        assert hint["found_in"] == ["archive", "cold"]
        assert hint["suggested_datasource"] == ["archive", "cold"]
        assert hint["hint"] == (
            "Date: not in main, found in archive, cold — set datasource: [archive, cold]"
        )

    def test_a_table_that_is_nowhere_gets_no_hint(
        self, schema_dir, baseline_file, tmp_path,
    ):
        self._project(schema_dir, {
            "Ghost": {"description": "t", "columns": {"ID": "pk"}},
            "Widget": {"description": "t", "columns": {"ID": "pk"}},
        })
        engines = self._warehouses(tmp_path, {
            "main": ["CREATE TABLE Widget (ID INTEGER)"],
            "archive": ["CREATE TABLE Other (X INTEGER)"],
        })
        report = self._run(schema_dir, engines)
        assert report.misplaced_tables == ()
        assert "Ghost.ID" in report.schema_only

    def test_a_table_with_some_of_its_columns_is_not_misplaced(
        self, schema_dir, baseline_file, tmp_path,
    ):
        self._project(schema_dir, {
            "Widget": {"description": "t", "columns": {"ID": "pk", "Retired": "gone"}},
        })
        engines = self._warehouses(tmp_path, {
            "main": ["CREATE TABLE Widget (ID INTEGER)"],
            "archive": ["CREATE TABLE Widget (ID INTEGER)"],
        })
        report = self._run(schema_dir, engines)
        assert report.misplaced_tables == ()
        assert "Widget.Retired" in report.schema_only

    def test_no_other_source_is_asked_when_no_table_is_absent(
        self, schema_dir, baseline_file, tmp_path,
    ):
        self._project(schema_dir, {
            "Widget": {"description": "t", "columns": {"ID": "pk"}},
        }, sources=("main", "archive", "cold"))
        engines = self._warehouses(tmp_path, {"main": ["CREATE TABLE Widget (ID INTEGER)"]})
        requested: list = []
        self._run(schema_dir, engines, requested)
        assert requested == ["main"]

    def test_each_other_source_is_asked_once_however_many_tables_are_misplaced(
        self, schema_dir, baseline_file, tmp_path,
    ):
        self._project(schema_dir, {
            "A": {"description": "t", "columns": {"ID": "pk"}},
            "B": {"description": "t", "columns": {"ID": "pk"}},
            "C": {"description": "t", "columns": {"ID": "pk"}},
        })
        engines = self._warehouses(tmp_path, {
            "main": ["CREATE TABLE Other (X INTEGER)"],
            "archive": [f"CREATE TABLE {n} (ID INTEGER)" for n in "ABC"],
        })
        calls: list = []
        import database.catalogue as catalogue_module

        real = catalogue_module.list_tables

        def _counting(engine):
            calls.append(engine)
            return real(engine)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("database.catalogue.list_tables", _counting)
            report = self._run(schema_dir, engines)
        assert [m["table"] for m in report.misplaced_tables] == ["A", "B", "C"]
        assert len(calls) == 1

    def test_a_source_whose_catalogue_cannot_be_read_gives_no_hint_and_no_crash(
        self, schema_dir, baseline_file, tmp_path,
    ):
        self._project(schema_dir, {
            "Broker": {"description": "t", "columns": {"ID": "pk"}},
            "Widget": {"description": "t", "columns": {"ID": "pk"}},
        })
        engines = self._warehouses(tmp_path, {
            "main": ["CREATE TABLE Widget (ID INTEGER)"],
            "archive": ["CREATE TABLE Broker (ID INTEGER)"],
        })
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "database.catalogue.list_tables",
                lambda engine: (_ for _ in ()).throw(RuntimeError("no access")),
            )
            report = self._run(schema_dir, engines)
        assert report.misplaced_tables == ()

    def test_an_injected_engine_never_computes_placement(
        self, schema_dir, baseline_file, tmp_path,
    ):
        _write_schema(schema_dir, {"Ghost": {"description": "t", "columns": {"ID": "pk"}}})
        engine = create_engine(f"sqlite:///{tmp_path / 'one.db'}")
        try:
            with override_settings(project_config_dir=str(schema_dir)):
                registry_module._cache.clear()
                report = check_schema_drift(engine=engine)
        finally:
            engine.dispose()
        assert report.misplaced_tables == ()
        assert report.as_dict()["misplaced_tables"] == []
