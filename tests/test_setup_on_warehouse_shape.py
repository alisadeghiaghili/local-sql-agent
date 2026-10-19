# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Every setup tool, run end to end on a warehouse-shaped fixture.

The rest of the suite uses small, tidy sample schemas, and twice the first real
use of a setup tool found a bug they could not (the inspector keyed tables by
bare name, and a warehouse that repeats names across schemas gave a draft with
about 70 duplicate keys). ``tests/_warehouse_shape.py`` builds the shape such a
warehouse really has (two data sources, many schemas, staging near-copies,
change-capture tables, the same name in up to four schemas, one shared
dimension, no declared foreign key, empty tables, Persian text, a name with a
space and one with a dot) and this module runs on it:

* the schema inspector and its CLI, over the large source;
* ``scripts/sync_schema.py`` and ``scripts/assign_datasources.py``, on a curated
  ``project_config/`` of 34 tables;
* ``scripts/prompt_budget.py``;
* the ``scripts/verify_deployment.py`` checks that need no SQL Server, among them
  the five that tell a curated ``project_config/`` from a raw inspector draft;
* the retrieval layer, through ``python -m eval.cli recall``.

Each must finish without a traceback and give a correct result. A setup tool
that cannot stay green here is not ready for a real warehouse; a new tool, or a
change to one of these, belongs in this module too.

The warehouse is built once per session (``warehouse_shape`` in
``tests/conftest.py``); a schema is an ``ATTACH``ed SQLite database, so every
engine opened on it needs :func:`tests._warehouse_shape.attached`.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

import config as cfg
import schema_data.registry as registry
from core.yaml_loading import safe_load_strict
from database.catalogue import read_catalogue
from database.datasources import reset_datasources_cache
from database.schema_inspector import SchemaInspector
from database.schema_draft import INFERRED_COMMENT, SAMPLE_REVIEW_FILENAME
from database.schema_inspector_cli import main as inspect_main
from prompt_engine.static_prefix import build_static_prefix
from schema_data.registry import split_table_key, validate_schema_yaml_text
from scripts import verify_deployment as verify
from scripts.assign_datasources import main as assign_main
from scripts.sync_schema import main as sync_main
from tests._warehouse_shape import (
    CURATED,
    LARGE,
    SMALL,
    Table,
    Warehouse,
    attached,
    build_warehouse,
    curated_keys,
    shared_names,
    write_golden,
    write_project_config,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

SYNCED = "schema.synced.yaml"
PROPOSED = "relationships.proposed.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class Run:
    """What a tool printed and returned."""

    rc: int
    out: str
    err: str


def _capture(main, argv: list[str]) -> Run:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(argv)
    return Run(rc, out.getvalue(), err.getvalue())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@contextmanager
def environment(warehouse: Warehouse, project_dir: Path) -> Iterator[None]:
    """Run the tools as a deployment with *project_dir* as its ``project_config/``.

    ``PROJECT_CONFIG_DIR`` names the directory, the two sources' URLs are in
    the environment variables ``datasources.yaml`` names, and
    :func:`database.connection.get_engine` returns engines on the fixture
    (the suite refuses to build a real one). The registry and the prefix
    cache are emptied on the way in and restored on the way out.
    """
    engines: dict[str, Engine] = {}

    def get_engine(datasource: str | None = None) -> Engine:
        name = datasource or LARGE
        if name not in engines:
            engines[name] = warehouse.engine(name)
        return engines[name]

    urls = {
        "DB_URL_WH_LARGE": warehouse.sources[LARGE].url,
        "DB_URL_WH_SMALL": warehouse.sources[SMALL].url,
    }
    saved = dict(registry._cache)
    with patch.dict(os.environ, urls), cfg.override_settings(project_config_dir=str(project_dir)), \
            attached(warehouse), patch("database.connection.get_engine", get_engine):
        reset_datasources_cache()
        registry._cache.clear()
        build_static_prefix.cache_clear()
        try:
            yield
        finally:
            for engine in engines.values():
                engine.dispose()
            registry._cache.clear()
            registry._cache.update(saved)
            reset_datasources_cache()
            build_static_prefix.cache_clear()


def reload_config() -> None:
    """Forget the schema and data sources read so far (a test edited the files)."""
    registry._cache.clear()
    reset_datasources_cache()
    build_static_prefix.cache_clear()


@pytest.fixture()
def project(warehouse_shape, tmp_path):
    """A fresh copy of the curated ``project_config/``, in effect for the test."""
    directory = write_project_config(tmp_path / "project_config", warehouse_shape)
    with environment(warehouse_shape, directory):
        yield directory


def _tables(text_: str) -> dict:
    """``schema.yaml`` text -> its validated tables (duplicate keys refused)."""
    return validate_schema_yaml_text(text_).tables


def _key(table: Table, shared: set[str]) -> str:
    """The key the project convention gives *table*: ``Schema.Table`` when its name is shared."""
    if table.name.lower() in shared:
        return f"{table.schema}.{table.name}"
    # The sync engine's spelling: a name that is not a plain identifier is bracketed.
    return table.name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table.name) else f"[{table.name}]"


def _shared_among(tables: list[Table]) -> set[str]:
    schemas: dict[str, set[str]] = {}
    for table in tables:
        schemas.setdefault(table.name.lower(), set()).add(table.schema)
    return {name for name, found in schemas.items() if len(found) > 1}


# ---------------------------------------------------------------------------
# The fixture itself
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def catalogues(warehouse_shape):
    """``read_catalogue`` of each source, read once."""
    result = {}
    with attached(warehouse_shape):
        for name in warehouse_shape.sources:
            engine = warehouse_shape.engine(name)
            try:
                result[name] = read_catalogue(engine)
            finally:
                engine.dispose()
    return result


class TestTheFixtureHasTheShapeOfAWarehouse:
    def test_table_counts_per_source(self, warehouse_shape):
        large, small = (len(warehouse_shape.sources[n].tables) for n in (LARGE, SMALL))
        assert 250 <= large <= 280
        assert 70 <= small <= 80

    def test_schema_kinds(self, warehouse_shape):
        large = warehouse_shape.sources[LARGE]
        small = warehouse_shape.sources[SMALL]
        for source in (large, small):
            assert any(s.endswith("_Fact") for s in source.schemas)
            assert any(s.endswith("_Dim") for s in source.schemas)
            assert any(s.endswith("_Stg") for s in source.schemas)
            assert {"common", "Metadata"} <= set(source.schemas)
        assert {"Replay", "Alerts"} <= set(large.schemas)
        assert "dbo" in small.schemas and small.count("dbo") == 2
        # SQLite attaches at most ten databases to a connection.
        assert max(len(s.schemas) for s in (large, small)) <= 10

    def test_names_repeat_across_schemas(self, warehouse_shape):
        shared = shared_names(warehouse_shape.sources[LARGE])
        assert len(shared["invoice"]) == 4 and len(shared["payment"]) == 4
        assert len(shared["party"]) == 3
        assert len(shared["status"]) == 2 and len(shared["region"]) == 2
        # A fact table and its staging near-copy, with different columns.
        sales = warehouse_shape.sources[LARGE]
        different = [
            t for t in sales.tables
            if t.schema == "sales_Stg"
            and (fact := next((f for f in sales.tables if f.schema == "sales_Fact" and f.name == t.name), None))
            and {c for c, _ in t.columns} != {c for c, _ in fact.columns}
        ]
        assert different
        # Names are compared without regard to case.
        assert {t.name for t in sales.tables if t.name.lower() == "invoice"} == {"Invoice", "INVOICE"}

    def test_the_calendar_is_the_one_table_in_both_sources(self, warehouse_shape):
        locations = [{t.location for t in warehouse_shape.sources[n].tables} for n in (LARGE, SMALL)]
        assert locations[0] & locations[1] == {("common", "calendar")}

    def test_the_reflected_catalogue_sees_every_attached_table(self, warehouse_shape, catalogues):
        for name, source in warehouse_shape.sources.items():
            assert set(catalogues[name]) == {t.location for t in source.tables}

    def test_no_foreign_key_is_declared_and_every_table_has_an_id(self, catalogues):
        for catalogue in catalogues.values():
            assert not any(info.foreign_keys for info in catalogue.values())
            assert all("id" in {c.name.lower() for c in info.columns} for info in catalogue.values())

    def test_the_naming_conventions_are_all_present(self, warehouse_shape):
        columns = {c for t in warehouse_shape.sources[LARGE].tables for c, _ in t.columns}
        assert {"BuyerParty_ID", "SellerParty_ID", "SellerPrty_ID", "SegmentID", "Customer_ID"} <= columns
        names = {t.name for s in warehouse_shape.sources.values() for t in s.tables}
        assert {"Price List", "Run.Log"} <= names
        assert {"Invoice_Inserts", "Invoice_Updates", "Invoice_Deletes"} <= names

    def test_some_tables_are_empty_and_some_hold_persian_text(self, warehouse_shape):
        with attached(warehouse_shape):
            engine = warehouse_shape.engine(LARGE)
            try:
                with engine.connect() as conn:
                    empty = 0
                    for table in warehouse_shape.sources[LARGE].tables[:60]:
                        quoted = f'"{table.schema}"."{table.name}"'
                        empty += conn.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar() == 0
                    names = [r[0] for r in conn.execute(text('SELECT Name FROM "common"."Party"'))]
            finally:
                engine.dispose()
        assert empty > 0
        assert any("شرکت" in n for n in names)

    def test_the_curated_config_mixes_bare_and_qualified_keys(self):
        keys = curated_keys()
        assert 28 <= len(keys) <= 40
        assert any("." not in k.strip("[]") or k.startswith("[") for k in keys)
        assert any("." in k and not k.startswith("[") for k in keys)
        assert {c.source for c in CURATED} == {LARGE, SMALL}


# ---------------------------------------------------------------------------
# 1. The schema inspector and its CLI
# ---------------------------------------------------------------------------

@dataclass
class Inspection:
    """One CLI run: where the drafts are and what it printed."""

    directory: Path
    run: Run
    _parsed: dict = field(default_factory=dict, repr=False)

    def text(self, name: str) -> str:
        return _read(self.directory / name)

    def parsed(self, name: str):
        """The draft *name* loaded with the strict loader, once (the big ones take a while)."""
        if name not in self._parsed:
            self._parsed[name] = safe_load_strict(self.text(name))
        return self._parsed[name]

    def config(self):
        """``schema.yaml`` validated by the registry, once."""
        if "config" not in self._parsed:
            self._parsed["config"] = validate_schema_yaml_text(self.text("schema.yaml"))
        return self._parsed["config"]


def _inspect(warehouse: Warehouse, directory: Path, source: str, *extra: str) -> Inspection:
    with attached(warehouse):
        run = _capture(
            inspect_main,
            ["--db-url", warehouse.sources[source].url, "--output-dir", str(directory), *extra],
        )
    return Inspection(directory, run)


@pytest.fixture(scope="module")
def inspected(warehouse_shape, tmp_path_factory) -> Inspection:
    # The staging and change-capture tables are skipped by default; this run keeps
    # them so the draft covers the whole source (the shape the keys are checked on).
    return _inspect(warehouse_shape, tmp_path_factory.mktemp("inspect_all"), LARGE, "--include-staging")


@pytest.fixture(scope="module")
def inspected_without_staging(warehouse_shape, tmp_path_factory) -> Inspection:
    return _inspect(
        warehouse_shape, tmp_path_factory.mktemp("inspect_excl"), LARGE,
        "--include-staging", "--exclude-schemas", "*_Stg,Replay,Alerts",
    )


@pytest.fixture(scope="module")
def inspected_small(warehouse_shape, tmp_path_factory) -> Inspection:
    return _inspect(warehouse_shape, tmp_path_factory.mktemp("inspect_small"), SMALL, "--include-staging")


class TestSchemaInspector:
    def test_the_run_succeeds_without_a_traceback(self, inspected):
        assert inspected.run.rc == 0
        assert "Traceback" not in inspected.run.err
        assert "Done." in inspected.run.err

    @pytest.mark.parametrize("name", ["schema.yaml", "entities.yaml", "aliases.yaml", "relationships.yaml"])
    def test_no_draft_has_a_duplicate_key_or_fails_to_parse(self, inspected, name):
        # The strict loader refuses a repeated mapping key at any level.
        assert isinstance(inspected.parsed(name), dict)

    def test_the_schema_draft_passes_the_registry_validator_with_every_table(self, inspected, warehouse_shape):
        config = inspected.config()
        assert len(config.tables) == len(warehouse_shape.sources[LARGE].tables)

    def test_shared_names_are_keyed_schema_dot_table_and_unique_names_stay_bare(
        self, inspected, warehouse_shape,
    ):
        tables = warehouse_shape.sources[LARGE].tables
        shared = _shared_among(list(tables))
        keys = inspected.config().tables
        assert set(keys) == {_key(t, shared) for t in tables}
        for table in tables:
            key = _key(table, shared)
            # A qualified key carries its schema; a bare one has db_schema.
            assert _schema_of(key, keys[key]) == table.schema
        assert "sales_Fact.Invoice" in keys and "orders_Stg.INVOICE" in keys    # shared: qualified
        assert "Invoice" not in keys and "Shipment" not in keys                  # and never bare
        assert "sales_Fact.Shipment" in keys and "sales_Stg.Shipment" in keys
        assert "Customer" in keys and "[Run.Log]" in keys                        # unique: bare

    def test_every_key_reads_back_as_its_table(self, inspected, warehouse_shape):
        by_key = {t.location: t for t in warehouse_shape.sources[LARGE].tables}
        for key, definition in inspected.config().tables.items():
            ref = split_table_key(key)
            qualifier = ref.qualifier[-1] if ref.qualifier else definition.db_schema
            assert (qualifier.lower(), ref.name.lower()) in by_key

    def test_the_run_summary_counts_every_schema_and_warns_about_the_size(self, inspected, warehouse_shape):
        source = warehouse_shape.sources[LARGE]
        err = inspected.run.err
        for schema, count in source.by_schema().items():
            assert f"  {schema}: {count}\n" in err
        assert f"Total: {len(source.tables)}" in err
        assert f"WARNING: {len(source.tables)} tables were written (more than 150)" in err

    def test_exclude_schemas_removes_staging_replay_and_alerts(self, inspected_without_staging, warehouse_shape):
        kept = [t for t in warehouse_shape.sources[LARGE].tables
                if t.schema not in ("sales_Stg", "orders_Stg", "Replay", "Alerts")]
        keys = inspected_without_staging.config().tables
        definitions = {d.db_schema for d in keys.values()}
        assert definitions.isdisjoint({"sales_Stg", "orders_Stg", "Replay", "Alerts"})
        assert len(keys) == len(kept)

    def test_with_the_exclusion_the_names_that_only_staging_shared_become_bare(
        self, inspected_without_staging, warehouse_shape,
    ):
        kept = [t for t in warehouse_shape.sources[LARGE].tables
                if t.schema not in ("sales_Stg", "orders_Stg", "Replay", "Alerts")]
        shared = _shared_among(kept)
        keys = inspected_without_staging.config().tables
        assert set(keys) == {_key(t, shared) for t in kept}
        assert "Shipment" in keys and "sales_Fact.Shipment" not in keys
        assert "sales_Fact.Invoice" in keys and "orders_Fact.Invoice" in keys

    def test_the_large_table_warning_disappears_once_the_count_is_below_150(
        self, inspected_without_staging, warehouse_shape,
    ):
        kept = [t for t in warehouse_shape.sources[LARGE].tables
                if t.schema not in ("sales_Stg", "orders_Stg", "Replay", "Alerts")]
        err = inspected_without_staging.run.err
        assert inspected_without_staging.run.rc == 0
        assert len(kept) < 150
        assert "WARNING" not in err
        assert f"Total: {len(kept)}" in err
        per_schema = err.split("Tables per schema:")[1]
        assert "_Stg" not in per_schema and "Replay" not in per_schema and "Alerts" not in per_schema
        assert "Inspecting sales_Stg." not in err and "Inspecting Replay." not in err

    def test_the_small_source_has_dbo_and_bracket_quoted_or_spaced_names(self, inspected_small, warehouse_shape):
        assert inspected_small.run.rc == 0
        assert "WARNING" not in inspected_small.run.err
        tables = warehouse_shape.sources[SMALL].tables
        keys = inspected_small.config().tables
        assert set(keys) == {_key(t, _shared_among(list(tables))) for t in tables}
        assert keys["[Price List]"].db_schema == "ref"        # as sync_schema.py --add-tables spells it
        assert keys["Calendar"].db_schema == "common"         # unique in this source, so bare
        assert keys["AppSetting"].db_schema == "dbo"
        # The fact table and its staging copy share the name, so they are qualified
        # (the key carries the schema, as sync_schema.py writes it).
        assert "ledger_Fact.Invoice" in keys and "ledger_Stg.Invoice" in keys
        assert keys["ledger_Fact.Invoice"].db_schema == ""

    def test_a_database_without_foreign_keys_still_gets_relationships_from_column_names(self, inspected):
        # No foreign key is declared in the fixture: every relationship is inferred, and says so.
        entries = inspected.parsed("relationships.yaml")["relationships"]
        config = inspected.config()

        assert entries and len(config.relationships) == len(entries)
        text = inspected.text("relationships.yaml")
        assert text.count(INFERRED_COMMENT) == len(entries)
        assert inspected.text("schema.yaml").count(INFERRED_COMMENT) == len(config.relationships)
        assert "Relationships: 0 declared" in inspected.run.err

    def test_no_sample_value_is_read_or_written_by_default(self, inspected):
        for name in ("schema.yaml", "entities.yaml", "aliases.yaml", "relationships.yaml"):
            text = inspected.text(name)
            assert "e.g." not in text and "sample_values" not in text
            assert 'Pipe 5" steel' not in text and "شرکت" not in text
        assert not (inspected.directory / SAMPLE_REVIEW_FILENAME).exists()
        assert inspected.parsed("aliases.yaml")["synonyms"] == {}

    def test_sample_values_go_to_the_review_file_only_with_quotes_and_persian_text_intact(
        self, warehouse_shape, tmp_path,
    ):
        done = _inspect(warehouse_shape, tmp_path, LARGE, "--include-staging", "--sample-values")
        review = done.parsed(SAMPLE_REVIEW_FILENAME)["sample_values"]

        assert done.run.rc == 0
        category = next(columns for key, columns in review.items() if key.endswith("Category"))
        assert 'Pipe 5" steel' in category["Name"] and "Path\\to" in category["Name"]
        assert any("شرکت" in v for v in review["common.Party"]["Name"])
        assert all(key in done.config().tables for key in review)
        for name in ("schema.yaml", "entities.yaml", "aliases.yaml", "relationships.yaml"):
            assert 'Pipe 5" steel' not in done.text(name) and "شرکت" not in done.text(name)

    @pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
    def test_the_review_file_is_owner_only(self, warehouse_shape, tmp_path):
        _inspect(warehouse_shape, tmp_path, SMALL, "--sample-values", "--sample-rows", "2")

        assert (tmp_path / SAMPLE_REVIEW_FILENAME).stat().st_mode & 0o777 == 0o600

    def test_naming_inferred_relationships_cope_with_the_misspelled_and_the_underscoreless_column(
        self, inspected,
    ):
        from retrieval import join_paths

        config = inspected.config()
        invoice = config.tables["sales_Fact.Invoice"].columns
        assert {"SellerPrty_ID", "BuyerParty_ID", "SellerParty_ID"} <= set(invoice)
        assert "SegmentID" in config.tables["sales_Fact.Shipment"].columns
        try:
            with registry.schema_in_effect(config):
                graph = join_paths.build_join_graph()
        finally:
            join_paths._CACHE.clear()       # keyed by object ids: nothing may outlive this schema
        neighbours = lambda key: {name for name, _ in graph.adjacency.get(key, ())}  # noqa: E731
        assert "Customer" in neighbours("sales_Fact.Invoice")           # Customer_ID
        assert "Segment" in neighbours("sales_Fact.Shipment")           # SegmentID, no underscore
        # `Party` exists in three schemas: the role-prefixed columns point at no single one.
        assert not neighbours("sales_Fact.Invoice") & {"sales_Dim.Party", "common.Party", "orders_Dim.Party"}


# ---------------------------------------------------------------------------
# 2. scripts/sync_schema.py
# ---------------------------------------------------------------------------

@dataclass
class Sync:
    """One ``sync_schema.py`` run and the text of the files it left."""

    run: Run
    synced: str = ""
    proposed: str = ""
    _tables: list = field(default_factory=list, repr=False)

    def tables(self) -> dict:
        """The synced ``schema.yaml``, validated once (duplicate keys refused)."""
        if not self._tables:
            self._tables.append(validate_schema_yaml_text(self.synced).tables)
        return self._tables[0]


def _sync(*argv: str, catalogues: dict | None = None) -> Run:
    """Run ``sync_schema.py``; with *catalogues* its sources are not read again.

    The default loader reflects every table of both sources through the
    engines of :func:`environment` (half a second a run); the runs whose point
    is elsewhere are given the catalogues read once by the ``catalogues`` fixture.
    """
    if catalogues is None:
        return _capture(sync_main, list(argv))
    return _capture(lambda args: sync_main(args, load_catalogue=catalogues.__getitem__), list(argv))


def _collect(directory: Path, run: Run) -> Sync:
    return Sync(
        run,
        _read(directory / SYNCED) if (directory / SYNCED).exists() else "",
        _read(directory / PROPOSED) if (directory / PROPOSED).exists() else "",
    )


def _replace_schema_yaml(directory: Path, new_text: str) -> None:
    """Put a proposal in place of ``schema.yaml``, as the runbook says to."""
    (directory / "schema.yaml").write_text(new_text, encoding="utf-8", newline="\n")
    (directory / SYNCED).unlink(missing_ok=True)
    reload_config()


def _files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _schema_of(key: str, definition) -> str:
    ref = split_table_key(key)
    return ref.qualifier[-1] if ref.qualifier else definition.db_schema


@pytest.fixture(scope="module")
def default_sync(warehouse_shape, tmp_path_factory) -> Sync:
    """One plain run on the curated config (the proposal is every test's input)."""
    directory = write_project_config(tmp_path_factory.mktemp("sync_default") / "project_config", warehouse_shape)
    with environment(warehouse_shape, directory):
        return _collect(directory, _sync())


@dataclass
class AddedTables:
    """Two runs in a row: add two schemas, put the result in place, add a third."""

    first: Sync
    second: Sync


@pytest.fixture(scope="module")
def added_tables(warehouse_shape, catalogues, tmp_path_factory) -> AddedTables:
    directory = write_project_config(tmp_path_factory.mktemp("sync_add") / "project_config", warehouse_shape)
    with environment(warehouse_shape, directory):
        # The proposals are not what these runs are about (and are the slow part of a big file).
        first = _collect(directory, _sync(
            "--add-tables", "sales_Stg.*", "--add-tables", "ledger_Fact.*", "--no-relationships",
            catalogues=catalogues,
        ))
        _replace_schema_yaml(directory, first.synced)
        second = _collect(directory, _sync(
            "--add-tables", "ledger_Stg.*", "--no-relationships", catalogues=catalogues,
        ))
    return AddedTables(first, second)


class TestSyncSchema:
    def test_dry_run_prints_the_report_and_writes_nothing(self, project, catalogues):
        before = _files(project)

        run = _sync("--dry-run", catalogues=catalogues)

        assert run.rc == 0
        assert "dry run: nothing written" in run.out
        assert "== prompt size" in run.out
        assert "Traceback" not in run.out + run.err
        assert _files(project) == before

    def test_the_hand_written_file_is_out_of_step_so_check_exits_one_and_writes_nothing(self, project):
        before = _files(project)

        run = _sync("--check")          # through the engines, the way the deploy gate runs it

        assert run.rc == 1
        assert "CHECK FAILED" in run.out
        assert _files(project) == before

    def test_the_default_run_writes_a_valid_synced_file_and_proposals(self, default_sync):
        assert default_sync.run.rc == 0, default_sync.run.err
        assert "Traceback" not in default_sync.run.out + default_sync.run.err
        tables = default_sync.tables()                      # validates, duplicate keys refused
        assert isinstance(safe_load_strict(default_sync.synced), dict)
        assert set(curated_keys()) <= set(tables)
        assert isinstance(safe_load_strict(default_sync.proposed), dict)

    def test_the_shared_calendar_names_both_sources(self, default_sync):
        assert default_sync.tables()["common.Calendar"].datasource == (LARGE, SMALL)

    def test_every_other_table_is_assigned_to_the_one_source_that_has_it(self, default_sync, warehouse_shape):
        tables = default_sync.tables()
        in_small = {t.location for t in warehouse_shape.sources[SMALL].tables}
        for entry in CURATED:
            if entry.key == "common.Calendar":
                continue
            location = (entry.schema.lower(), entry.table.lower())
            assert tables[entry.key].datasource == ((SMALL,) if location in in_small else (LARGE,)), entry.key

    def test_the_prompt_size_section_is_present_for_both_sources(self, default_sync):
        out = default_sync.run.out
        assert "== prompt size" in out
        assert f"  {LARGE}: " in out and f"  {SMALL}: " in out
        assert "PROMPT_RETRIEVAL_TOKEN_BUDGET is" in out

    def test_nothing_a_person_wrote_is_changed(self, default_sync, warehouse_shape, tmp_path):
        original = _tables(_read(write_project_config(tmp_path / "orig", warehouse_shape) / "schema.yaml"))
        after = default_sync.tables()
        for key, definition in original.items():
            assert after[key].description == definition.description
            assert after[key].db_schema == definition.db_schema
            assert list(after[key].columns)[: len(definition.columns)] == list(definition.columns)
            assert all(after[key].columns[c] == d for c, d in definition.columns.items())

    def test_a_listed_column_the_database_lacks_is_marked_and_kept(self, default_sync):
        assert "Legacy_Code" in default_sync.tables()["Customer"].columns
        assert "# not in database (sync_schema.py)" in default_sync.synced

    def test_columns_the_database_has_are_added_with_their_types(self, default_sync):
        invoice = default_sync.tables()["sales_Fact.Invoice"]
        assert invoice.column_types["Amount"] == "decimal(18,2)"
        assert {"SellerParty_ID", "SellerPrty_ID", "Amount", "CreatedAt"} <= set(invoice.columns)

    def test_proposals_are_valid_yaml_with_the_inferred_links_and_none_for_the_misspelled_column(
        self, default_sync,
    ):
        entries = safe_load_strict(default_sync.proposed)["relationships"]
        links = {(e.get("from_schema", ""), e["from_table"], e["from_column"], e["to_table"]) for e in entries}
        assert ("sales_Fact", "Invoice", "Product_ID", "Product") in links
        assert ("sales_Fact", "Shipment", "SegmentID", "Segment") in links          # no underscore
        assert not any(e["from_column"] == "SellerPrty_ID" for e in entries)          # misspelled: no table
        assert all(e["basis"].startswith("naming convention") for e in entries)
        assert "dropped because they could not be trusted" in default_sync.run.out
        # A role-prefixed column that fits several `Party` tables is dropped, not guessed.
        assert "BuyerParty_ID: several tables match" in default_sync.run.out

    def test_after_replacing_schema_yaml_with_the_proposal_check_passes(self, project, default_sync, catalogues):
        _replace_schema_yaml(project, default_sync.synced)

        run = _sync("--check", catalogues=catalogues)

        assert run.rc == 0, run.out
        assert "CHECK OK" in run.out
        assert _files(project).count(SYNCED) == 0

    def test_a_schema_yaml_with_windows_line_endings_is_read_the_same(self, project, default_sync, catalogues):
        # A file edited on Windows has CRLF line endings; the byte-for-byte comparison
        # the sync makes of its own output must not trip on them.
        crlf = default_sync.synced.replace("\r\n", "\n").replace("\n", "\r\n")
        (project / "schema.yaml").write_bytes(crlf.encode("utf-8"))

        run = _sync("--check", catalogues=catalogues)

        assert run.rc == 0, run.out + run.err
        assert "CHECK OK" in run.out

    def test_the_first_add_tables_run_adds_a_fact_schema_and_a_staging_schema_without_a_duplicate(
        self, added_tables, warehouse_shape,
    ):
        assert added_tables.first.run.rc == 0, added_tables.first.run.err
        tables = added_tables.first.tables()                # refuses a duplicate key
        for schema, source in (("ledger_Fact", SMALL), ("sales_Stg", LARGE)):
            wanted = {t.name.lower() for t in warehouse_shape.sources[source].tables if t.schema == schema}
            found = [split_table_key(k).name.lower() for k, d in tables.items() if _schema_of(k, d) == schema]
            assert sorted(found) == sorted(wanted), schema         # each table once, under one key
        assert all(d.columns for d in tables.values())
        assert tables["Receivable"].datasource == (SMALL,)         # an added table gets its source too
        assert tables["Receivable"].db_schema == "ledger_Fact"

    def test_a_name_that_collides_with_a_bare_key_is_added_under_a_qualified_key(self, added_tables):
        tables = added_tables.first.tables()
        for name in ("Shipment", "Quote", "Contract"):             # bare keys of sales_Fact in the curated file
            assert tables[name].db_schema == "sales_Fact"          # untouched
            assert f"sales_Stg.{name}" in tables                   # the staging table: qualified
        assert tables["sales_Stg.Shipment"].columns != tables["Shipment"].columns
        for name in ("Invoice", "Payment"):
            assert f"sales_Stg.{name}" in tables
        # A staging-only name nothing else uses stays bare.
        assert tables["LoadControl"].db_schema == "sales_Stg"

    def test_a_second_staging_schema_never_duplicates_the_first_ones_bare_keys(self, added_tables):
        assert added_tables.second.run.rc == 0, added_tables.second.run.err
        tables = added_tables.second.tables()
        assert tables["LoadControl"].db_schema == "sales_Stg"      # still the first one's
        assert "ledger_Stg.LoadControl" in tables                  # the second one, qualified
        assert "ledger_Stg.Invoice" in tables
        bare = Counter(
            split_table_key(key).name.lower() for key in tables if not split_table_key(key).qualifier
        )
        assert max(bare.values()) == 1

    def test_a_source_that_cannot_be_read_stops_the_run(self, project):
        def broken(source: str):
            raise RuntimeError("down")

        run = _capture(lambda argv: sync_main(argv, load_catalogue=broken), [])

        assert run.rc == 2
        assert "stopping" in run.err


class TestAssignDatasources:
    """``scripts/assign_datasources.py``, the narrower tool that only writes the ``datasource:`` lines."""

    def test_check_exits_one_while_ledger_tables_and_the_calendar_say_the_wrong_source(self, project):
        run = _capture(assign_main, ["--check"])

        assert run.rc == 1
        assert "CHECK FAILED" in run.out
        assert "Posting: schema.yaml says wh_large (the default), found in wh_small" in run.out
        assert "common.Calendar: schema.yaml says wh_large (the default), found in wh_large, wh_small" in run.out

    def test_it_writes_a_valid_file_with_every_table_in_its_source(self, project):
        run = _capture(assign_main, [])

        assert run.rc == 0, run.err
        tables = _tables(_read(project / "schema.with_datasources.yaml"))
        assert tables["common.Calendar"].datasource == (LARGE, SMALL)
        assert tables["Posting"].datasource == (SMALL,)
        assert tables["Customer"].datasource == (LARGE,)
        assert "schema.yaml" in _files(project)


class TestSetupWizardOverBothSources:
    """``setup_project.py`` without ``--db-url``: both sources of ``datasources.yaml``, through the application's engines."""

    @pytest.fixture()
    def wizard(self, project, tmp_path):
        import setup_project as sp
        from tests.test_setup_wizard_drafts import RecordingLLM

        llm = RecordingLLM()
        out = tmp_path / "draft"
        with patch.object(sp, "setup_wizard_llm", return_value=llm):
            code = sp.main([
                "--language", "en", "--non-interactive", "--output", str(out), "--allow-repeated-answers",
            ])
        return code, llm, safe_load_strict(_read(out / "entities.yaml"))["entities"]

    def test_it_runs_and_every_entity_names_its_sources(self, wizard):
        code, _, entities = wizard
        assert code == 0
        assert entities and all(entry.get("datasource") for entry in entities.values())

    def test_the_calendar_both_sources_hold_is_one_entity(self, wizard):
        _, _, entities = wizard
        (calendar,) = [entry for entry in entities.values() if entry["table"] == "Calendar"]
        assert calendar["datasource"] == [LARGE, SMALL]

    def test_staging_never_reaches_the_model(self, wizard):
        _, llm, entities = wizard
        assert not llm.asked_about("_Stg.")
        assert not [key for key in entities if key.endswith(("_Inserts", "_Updates", "_Deletes"))]

    def test_its_keys_are_the_keys_of_the_inspector_cli_s_schema_yaml(self, wizard, tmp_path):
        # The same tables (staging left out by both), the same spelling: a bare name when
        # no other schema has it, `Schema.Table` when one does, `[Price List]` and
        # `[Run.Log]` bracketed. A key that is not one of schema.yaml's matches no table.
        _, _, entities = wizard
        done = _capture(inspect_main, ["--output-dir", str(tmp_path / "cli"), "--no-row-counts"])
        assert done.rc == 0, done.err

        keys = set(_tables(_read(tmp_path / "cli" / "schema.yaml")))

        assert set(entities) == keys
        assert {"[Price List]", "[Run.Log]"} <= keys
        assert any("." in key and not key.startswith("[") for key in keys)

    def test_a_second_run_over_the_same_tables_changes_no_key(self, project, tmp_path):
        import setup_project as sp
        from tests.test_setup_wizard_drafts import RecordingLLM

        out = tmp_path / "again"
        keys = []
        for _ in range(2):
            with patch.object(sp, "setup_wizard_llm", return_value=RecordingLLM()):
                assert sp.main([
                    "--language", "en", "--non-interactive", "--output", str(out), "--allow-repeated-answers",
                ]) == 0
            keys.append(list(safe_load_strict(_read(out / "entities.yaml"))["entities"]))
        assert keys[0] == keys[1] and len(set(keys[1])) == len(keys[1])


# ---------------------------------------------------------------------------
# 3. scripts/prompt_budget.py
# ---------------------------------------------------------------------------

def _subprocess_env(project_dir: Path, warehouse: Warehouse) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PROJECT_CONFIG_DIR": str(project_dir),
        "DB_URL_WH_LARGE": warehouse.sources[LARGE].url,
        "DB_URL_WH_SMALL": warehouse.sources[SMALL].url,
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _run_program(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], cwd=_REPO_ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )


class TestPromptBudget:
    def test_it_prints_a_budget_for_both_sources_without_a_traceback(
        self, warehouse_shape, default_sync, tmp_path,
    ):
        # The curated config as a deployment runs it after a sync: every table in its source.
        directory = write_project_config(tmp_path / "project_config", warehouse_shape)
        (directory / "schema.yaml").write_text(default_sync.synced, encoding="utf-8", newline="\n")

        done = _run_program(
            ["scripts/prompt_budget.py", "--no-model", "--context-length", "32768", "--json"],
            _subprocess_env(directory, warehouse_shape),
        )

        assert done.returncode == 0, done.stderr
        assert "Traceback" not in done.stderr
        report = json.loads(done.stdout)
        sources = {entry["name"]: entry for entry in report["sources"]}
        assert set(sources) == {LARGE, SMALL}
        tables = default_sync.tables()
        for name in (LARGE, SMALL):
            assert sources[name]["tables"] == sum(1 for d in tables.values() if name in d.datasource)
            assert sources[name]["estimate"] > 0
        assert report["recommendation"]["env_line"].startswith("PROMPT_RETRIEVAL_TOKEN_BUDGET=")
        assert report["recommendation"]["budget"] > 0


# ---------------------------------------------------------------------------
# 4. scripts/verify_deployment.py, the checks that need no SQL Server
# ---------------------------------------------------------------------------

@pytest.fixture()
def synced_project(project, default_sync):
    """The curated config after ``sync_schema.py`` and the replacement of ``schema.yaml``."""
    _replace_schema_yaml(project, default_sync.synced)
    return project


class TestVerifyDeployment:
    def test_project_config_loads(self, project):
        result = verify.check_project_config_loads()

        assert result.status == "PASS", result.detail

    def test_tables_map_to_data_sources(self, project):
        result = verify.check_table_datasources()

        assert result.status == "PASS", result.detail
        assert LARGE in result.detail and SMALL in result.detail

    def test_the_hand_written_file_leaves_ledger_tables_in_the_default_source_until_it_is_synced(self, project):
        placement = verify.check_tables_in_assigned_sources()
        structure = verify.check_schema_structure_matches_databases()

        assert placement.status == "FAIL"
        assert "Posting" in placement.detail and f"set datasource: {SMALL}" in placement.detail
        assert structure.status == "FAIL"
        assert "datasource: line(s) to set" in structure.detail
        assert "python scripts/sync_schema.py" in structure.detail

    def test_after_a_sync_every_check_passes(self, synced_project):
        for check in (
            verify.check_table_datasources,
            verify.check_tables_in_assigned_sources,
            verify.check_schema_structure_matches_databases,
            verify.check_project_config_loads,
        ):
            result = check()
            assert result.status == "PASS", (result.name, result.detail)

    def test_a_table_moved_to_the_wrong_source_fails_with_the_line_to_write(self, synced_project):
        schema = _read(synced_project / "schema.yaml")
        moved = schema.replace("  Customer:\n    datasource: wh_large\n", "  Customer:\n    datasource: wh_small\n", 1)
        assert moved != schema
        (synced_project / "schema.yaml").write_text(moved, encoding="utf-8", newline="\n")
        reload_config()

        result = verify.check_tables_in_assigned_sources()

        assert result.status == "FAIL"
        assert "Customer: not in wh_small, found in wh_large" in result.detail
        assert "set datasource: wh_large" in result.detail

    def test_a_duplicate_key_fails_with_a_readable_message(self, synced_project):
        schema = _read(synced_project / "schema.yaml")
        duplicated = schema.replace(
            "  Product:\n", '  Customer:\n    description: "again"\n    columns:\n      ID: "key"\n\n  Product:\n', 1,
        )
        assert duplicated != schema
        (synced_project / "schema.yaml").write_text(duplicated, encoding="utf-8", newline="\n")
        reload_config()

        loads = verify.check_project_config_loads()
        mapped = verify.check_table_datasources()

        assert loads.status == "FAIL"
        assert "schema.yaml" in loads.detail and "duplicate key 'Customer'" in loads.detail
        assert "Traceback" not in loads.detail
        assert mapped.status == "FAIL"
        assert "duplicate key 'Customer'" in mapped.detail

    def test_a_missing_required_file_is_named(self, project):
        (project / "metrics.yaml").unlink()

        result = verify.check_project_config_loads()

        assert result.status == "FAIL"
        assert "metrics.yaml" in result.detail


# ---------------------------------------------------------------------------
# 4b. The preflight tells a raw inspector draft from the curated config
# ---------------------------------------------------------------------------

#: The five checks that read ``project_config/`` and nothing else.
CURATION_CHECKS = (
    verify.check_project_config_is_curated,
    verify.check_aliases_are_curated,
    verify.check_every_data_source_is_described,
    verify.check_no_staging_tables,
    verify.check_no_sample_values_in_descriptions,
)

#: Invented contact data, in the shape a live table has: a phone number, a
#: ten-digit national code and a street address. None of it is real.
_CONTACT_ROWS = (
    ("Sample Person One", "09120000001", "0012345001", "12 Example Street, Building 4, Floor 2, Sample City"),
    ("Sample Person Two", "09120000002", "0012345002", "7 Test Avenue, Unit 9, Floor 1, Sample Town"),
)


def _run_curation_checks(directory: Path) -> dict[str, verify.CheckResult]:
    """The five checks, as a deployment whose ``project_config/`` is *directory*, by name."""
    with cfg.override_settings(project_config_dir=str(directory)):
        results = [check() for check in CURATION_CHECKS]
    return {r.name: r for r in results}


@pytest.fixture(scope="module")
def draft_project(tmp_path_factory):
    """What an operator gets by copying the inspector's drafts over a curated ``project_config/``.

    The drafts are what the single-database inspector
    (:class:`database.schema_inspector.SchemaInspector`, which the setup wizard
    still uses) writes over a private copy of the large source: placeholder
    descriptions, live sample values in the notes and as synonym seeds. (A table
    of invented contact data is added to the copy, since the fixture itself
    holds no phone numbers.) They sit next to the curated ``datasources.yaml``,
    which the operator kept: it lists both sources, and the draft describes one
    of them. ``database.schema_inspector_cli`` no longer writes values, so the
    values-free draft is checked in :class:`TestPreflightOnTheCliDraft`.
    """
    private = build_warehouse(tmp_path_factory.mktemp("draft_warehouse"))
    connection = sqlite3.connect(private.sources[LARGE].schemas["Metadata"])
    try:
        connection.execute(
            'CREATE TABLE "PersonDetail" ("ID" INTEGER PRIMARY KEY, "Name" VARCHAR(40), '
            '"Phone" VARCHAR(20), "NationalCode" VARCHAR(20), "Address" VARCHAR(120))'
        )
        connection.executemany(
            'INSERT INTO "PersonDetail" ("Name", "Phone", "NationalCode", "Address") VALUES (?, ?, ?, ?)',
            _CONTACT_ROWS,
        )
        connection.commit()
    finally:
        connection.close()
    directory = tmp_path_factory.mktemp("draft_project")
    with attached(private):
        inspector = SchemaInspector(private.sources[LARGE].url, sample_rows=10)
        try:
            snapshot = inspector.inspect(fetch_row_counts=True)
        finally:
            inspector.close()
    (directory / "schema.yaml").write_text(inspector.draft_schema_yaml(snapshot), encoding="utf-8")
    (directory / "entities.yaml").write_text(inspector.draft_entities_yaml(snapshot), encoding="utf-8")
    (directory / "aliases.yaml").write_text(inspector.draft_aliases_yaml(snapshot), encoding="utf-8")
    (directory / "relationships.yaml").write_text(inspector.draft_relationships_yaml(snapshot), encoding="utf-8")
    curated = write_project_config(tmp_path_factory.mktemp("curated_for_sources"), private)
    shutil.copyfile(curated / "datasources.yaml", directory / "datasources.yaml")
    return directory, private


@pytest.fixture(scope="module")
def draft_results(draft_project):
    return _run_curation_checks(draft_project[0])


@pytest.fixture(scope="module")
def curated_results(warehouse_shape, tmp_path_factory):
    directory = write_project_config(tmp_path_factory.mktemp("curated_project"), warehouse_shape)
    return _run_curation_checks(directory)


class TestPreflightOnAnInspectorDraft:
    """The incident: drafts moved over the curated files, and nothing said so."""

    def test_the_drafts_are_what_the_inspector_wrote(self, draft_project):
        directory, private = draft_project
        for name in ("schema.yaml", "entities.yaml", "aliases.yaml", "relationships.yaml"):
            assert _read(directory / name).startswith("# AUTO-GENERATED DRAFT")
        assert len(_tables(_read(directory / "schema.yaml"))) == len(private.sources[LARGE].tables) + 1

    def test_the_curated_check_fails_naming_the_files_and_the_way_back(self, draft_results):
        result = draft_results["project_config/ is curated, not a raw draft"]

        assert result.status == "FAIL"
        for name in ("aliases.yaml", "entities.yaml", "relationships.yaml", "schema.yaml"):
            assert name in result.detail
        assert "263 of 263 table descriptions (100%)" in result.detail
        assert "sync_schema.py --add-tables" in result.detail
        assert "docs/deployment-runbook.md" in result.detail

    def test_the_aliases_check_fails_on_the_seeds(self, draft_results):
        result = draft_results["Aliases are curated"]

        assert result.status == "FAIL"
        assert "# REVIEW" in result.detail
        assert "ring_aliases is empty" in result.detail

    def test_the_lost_data_source_is_named(self, draft_results):
        result = draft_results["Every data source is described"]

        assert result.status == "FAIL"
        assert f"1 of 2 data source(s) have no table in schema.yaml: {SMALL}" in result.detail

    def test_staging_and_change_capture_tables_are_counted_per_schema(self, draft_results, draft_project):
        _, private = draft_project
        expected: Counter[str] = Counter()
        for table in private.sources[LARGE].tables:
            lowered = table.schema.lower()
            if "stg" in lowered or "staging" in lowered or table.name.lower().endswith(("_inserts", "_updates", "_deletes")):
                expected[table.schema] += 1
        result = draft_results["No staging or change-capture tables"]

        assert result.status == "PASS" and result.detail.startswith("advice: ")
        assert f"advice: {sum(expected.values())} of 263 table(s)" in result.detail
        for schema, count in expected.items():
            assert f"{schema}: {count}" in result.detail
        assert {"sales_Stg", "orders_Stg", "Replay", "sales_Fact"} <= set(expected)

    def test_sample_values_are_reported_as_counts_and_a_key_never_as_values(self, draft_results):
        result = draft_results["No live sample values in descriptions"]

        assert result.status == "PASS" and result.detail.startswith("advice: ")
        assert "PersonDetail" in result.detail
        assert "national-id-like: 1" in result.detail and "address-like: 1" in result.detail
        assert "phone-like: 0" not in result.detail
        for value in [v for row in _CONTACT_ROWS for v in row] + ["Example Street", "Sample City"]:
            assert value not in result.detail

    def test_no_message_quotes_the_draft(self, draft_results, draft_project):
        directory, _ = draft_project
        assert "09120000001" in _read(directory / "schema.yaml")      # the draft does quote it ...
        for result in draft_results.values():                        # ... and no message does
            assert "09120000001" not in result.detail and "0012345001" not in result.detail

    def test_the_cli_would_exit_with_one(self, draft_project):
        directory, _ = draft_project
        out = io.StringIO()
        with cfg.override_settings(project_config_dir=str(directory)), \
                patch.object(verify, "build_checks", lambda: list(CURATION_CHECKS)), redirect_stdout(out):
            code = verify.main()

        assert code == 1
        assert "[FAIL] project_config/ is curated, not a raw draft" in out.getvalue()
        assert "3 failed" in out.getvalue()


class TestPreflightOnTheCliDraft:
    """What the inspector CLI writes today: a draft still, but with no live value in it."""

    @pytest.fixture(scope="class")
    def results(self, inspected, warehouse_shape, tmp_path_factory):
        directory = tmp_path_factory.mktemp("cli_draft")
        for name in ("schema.yaml", "entities.yaml", "aliases.yaml", "relationships.yaml"):
            shutil.copyfile(inspected.directory / name, directory / name)
        curated = write_project_config(tmp_path_factory.mktemp("curated_for_cli_sources"), warehouse_shape)
        shutil.copyfile(curated / "datasources.yaml", directory / "datasources.yaml")
        return _run_curation_checks(directory)

    def test_it_is_still_refused_as_a_draft_by_its_header(self, results):
        result = results["project_config/ is curated, not a raw draft"]

        assert result.status == "FAIL"
        assert "4 file(s) carry the AUTO-GENERATED DRAFT header" in result.detail

    def test_there_is_no_sample_value_to_advise_about_and_no_seed_in_the_aliases(self, results):
        assert results["No live sample values in descriptions"].detail.startswith("no description quotes")
        assert results["Aliases are curated"].status == "PASS"

    def test_the_descriptions_are_not_the_old_placeholder_but_they_are_only_the_name_and_a_type(self, results):
        detail = results["project_config/ is curated, not a raw draft"].detail

        assert "are still the placeholder" not in detail
        assert "table descriptions (100%) are only the table's own name" in detail
        assert "column descriptions (100%) are only `<type> column`" in detail

    def test_it_is_a_draft_even_with_the_header_lines_deleted(self, results, tmp_path_factory, inspected):
        directory = tmp_path_factory.mktemp("cli_draft_reviewed")
        for name in ("schema.yaml", "entities.yaml", "aliases.yaml", "relationships.yaml"):
            text_ = _read(inspected.directory / name)
            (directory / name).write_text(
                "\n".join(line for line in text_.splitlines() if "AUTO-GENERATED DRAFT" not in line), encoding="utf-8",
            )

        curated = _run_curation_checks(directory)["project_config/ is curated, not a raw draft"]

        assert curated.status == "FAIL"
        assert "file(s) carry the" not in curated.detail
        assert "table descriptions (100%) are only the table's own name" in curated.detail


class TestPreflightOnTheCuratedConfig:
    def test_every_check_passes(self, curated_results):
        assert len(curated_results) == 5
        for result in curated_results.values():
            assert result.status == "PASS", (result.name, result.detail)

    def test_the_two_sources_are_both_described(self, curated_results):
        assert curated_results["Every data source is described"].detail.startswith("all 2 data source(s) have tables")

    def test_there_is_no_advice_but_the_change_capture_table_the_author_kept(self, curated_results):
        staging = curated_results["No staging or change-capture tables"]

        assert staging.detail.startswith("advice: 1 of 34 table(s)")
        assert "sales_Fact: 1" in staging.detail
        for name, result in curated_results.items():
            if name != "No staging or change-capture tables":
                assert "advice" not in result.detail, (name, result.detail)

    def test_the_cli_exit_code_stays_zero(self, warehouse_shape, tmp_path):
        directory = write_project_config(tmp_path / "project_config", warehouse_shape)
        out = io.StringIO()
        with cfg.override_settings(project_config_dir=str(directory)), \
                patch.object(verify, "build_checks", lambda: list(CURATION_CHECKS)), redirect_stdout(out):
            code = verify.main()

        assert code == 0
        assert "5 passed, 0 failed, 0 skipped" in out.getvalue()

    def test_a_few_tables_added_with_add_tables_do_not_make_it_a_draft(self, project):
        # sync_schema.py appends a table with its own name as the description and
        # `<type> column` for its columns; a few of them among curated ones are the
        # normal state of a file that is being brought up to date.
        text_ = _read(project / "schema.yaml")
        added = "".join(
            f'  Added{n}:\n    description: "ledger_Stg.Added{n}"  # TO BE FILLED (added by sync_schema.py)\n'
            f'    db_schema: "ledger_Stg"\n    columns:\n      ID: "int column"  # TO BE FILLED (added by sync_schema.py)\n\n'
            for n in range(3)
        )
        assert "\ntables:\n" in text_
        _replace_schema_yaml(project, text_.replace("\ntables:\n", "\ntables:\n" + added, 1))
        curated = _run_curation_checks(project)["project_config/ is curated, not a raw draft"]

        assert curated.status == "PASS", curated.detail
        assert "3 of 37 only repeat the table's name" in curated.detail

    def test_a_file_that_is_mostly_added_tables_is_a_draft_again(self, project, added_tables):
        # 34 curated tables and the 79 that `--add-tables` appended for the staging and
        # fact schemas it was told to add: most of the file says nothing but its names.
        _replace_schema_yaml(project, added_tables.second.synced)
        results = _run_curation_checks(project)

        curated = results["project_config/ is curated, not a raw draft"]
        assert curated.status == "FAIL"
        assert "of 113 table descriptions" in curated.detail and "are only the table's own name" in curated.detail
        assert results["Every data source is described"].status == "PASS"
        staging = results["No staging or change-capture tables"]
        assert staging.detail.startswith("advice: ") and "sales_Stg:" in staging.detail and "ledger_Stg:" in staging.detail


# ---------------------------------------------------------------------------
# 5. Retrieval, through eval.cli recall
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def report(warehouse_shape, tmp_path_factory):
    """``python -m eval.cli recall --json`` on the curated config, and the keys of its ``schema.yaml``."""
    base = tmp_path_factory.mktemp("recall")
    directory = write_project_config(base / "project_config", warehouse_shape)
    golden = write_golden(base / "golden.jsonl")
    done = _run_program(
        ["-m", "eval.cli", "recall", "--golden", str(golden), "--json"],
        _subprocess_env(directory, warehouse_shape),
    )
    assert done.returncode == 0, done.stderr
    assert "Traceback" not in done.stderr
    return json.loads(done.stdout), set(_tables(_read(directory / "schema.yaml")))


class TestRetrievalOnTheCuratedConfig:
    def test_recall_computes_for_every_golden_question(self, report):
        data, _ = report
        assert data["total_cases"] == data["scored"] >= 10
        assert data["skipped"] == []
        assert 0.0 <= data["mean_recall"] <= 1.0          # computed; no threshold is asserted

    def test_every_gold_and_retrieved_table_is_a_key_of_schema_yaml(self, report):
        data, keys = report
        for case in data["cases"]:
            assert set(case["gold"]) <= keys, case["case_id"]
            assert set(case["retrieved"]) <= keys, (case["case_id"], case["retrieved"])

    def test_same_named_tables_are_told_apart(self, report):
        data, _ = report
        by_id = {c["case_id"]: c for c in data["cases"]}
        assert by_id["orders_invoice_count"]["gold"] == ["orders_Fact.Invoice"]
        assert "orders_Fact.Invoice" in by_id["orders_invoice_count"]["retrieved"]
        assert by_id["sales_invoice_by_customer"]["gold"] == ["Customer", "sales_Fact.Invoice"]

    def test_source_selection_is_scored_with_two_sources(self, report):
        data, _ = report
        assert data["source_selection"]["total"] > 0
