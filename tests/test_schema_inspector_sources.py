# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The first-install inspector over the application's own data sources.

Two SQLite databases stand in for two data sources; each schema is an
``ATTACH``ed file (``tests/_inspector_fixtures.py``). Pins:

* the drafts are in the format ``scripts/sync_schema.py`` writes: ``datasource:``
  lines with several sources, a table in both sources written once with a list,
  ``Schema.Table`` keys only for names shared across schemas, and a sync of the
  draft against the same databases changes nothing;
* ``--datasource`` narrows the run, ``--db-url`` keeps the single-database path
  and says the application's login may see less;
* the skip rules (system schemas, configured exclusion, ``--exclude-schemas``,
  staging) and the summary that counts them;
* no value read from a row is written to the drafts, and ``--sample-values``
  writes them to an owner-only file nothing loads;
* relationships guessed from column names are marked, declared ones are not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

import config as cfg
from core.yaml_loading import safe_load_strict
from database import schema_inspector_cli
from database.catalogue import read_catalogue
from database.datasources import reset_datasources_cache
from database.schema_draft import INFERRED_COMMENT, SAMPLE_REVIEW_FILENAME
from schema_data.registry import validate_schema_yaml_text
from schema_data.sync import sync_schema_text
from tests._inspector_fixtures import attached, build_database, engines_for

SALES_LAYOUT = {
    "sales": {
        "Customer": None,
        "Trade": "ID INTEGER PRIMARY KEY, Customer_ID INTEGER, Note VARCHAR(40)",
        "Order": "ID INTEGER PRIMARY KEY, Customer_ID INTEGER REFERENCES Customer(ID), Total DECIMAL(10,2)",
        "Trade_Inserts": None,
    },
    "ref": {"Trade": None},
    "Sales_Stg": {"Customer": None, "Load": None},
    "Scratch": {"Tmp": None},
    "cdc": {"change_table": None},
    "INFORMATION_SCHEMA": {"Views": None},
}

REPORTS_LAYOUT = {
    "sales": {"Customer": None},
    "rep": {"Summary": "ID INTEGER PRIMARY KEY, Region VARCHAR(20), Total INTEGER"},
}

ROWS = {
    "sales.Customer": [(1, "Real Person One"), (2, "Real Person Two")],
    "sales.Trade": [(1, 1, "secret note")],
}

TWO_SOURCES = """\
default: sales
datasources:
  sales:
    url_env: DB_URL_SALES
  reports:
    url_env: DB_URL_REPORTS
"""


@dataclass
class Run:
    code: int
    out: str
    err: str
    directory: Path

    def text(self, name: str) -> str:
        return (self.directory / name).read_text(encoding="utf-8")

    def tables(self):
        return validate_schema_yaml_text(self.text("schema.yaml")).tables


@dataclass
class World:
    """Two databases, a ``project_config/`` for them, and a way to run the CLI."""

    tmp_path: Path
    project: Path
    sales: object
    reports: object
    capsys: pytest.CaptureFixture[str]

    def run(self, *argv: str, name: str = "draft") -> Run:
        directory = self.tmp_path / name
        with attached(self.sales, self.reports):
            engines = engines_for({"sales": self.sales, "reports": self.reports})
            try:
                with patch("database.connection.get_engine", lambda source=None: engines["sales" if source in (None, "default") else source]):
                    code = schema_inspector_cli.main(["--output-dir", str(directory), *argv])
            finally:
                for engine in engines.values():
                    engine.dispose()
        captured = self.capsys.readouterr()
        return Run(code, captured.out, captured.err, directory)

    def catalogues(self):
        with attached(self.sales, self.reports):
            engines = engines_for({"sales": self.sales, "reports": self.reports})
            try:
                return {name: read_catalogue(engine) for name, engine in engines.items()}
            finally:
                for engine in engines.values():
                    engine.dispose()


@pytest.fixture()
def world(tmp_path, monkeypatch, capsys):
    sales = build_database(tmp_path, "sales_db", SALES_LAYOUT, ROWS)
    reports = build_database(tmp_path, "reports_db", REPORTS_LAYOUT)
    project = tmp_path / "project_config"
    project.mkdir()
    (project / "datasources.yaml").write_text(TWO_SOURCES, encoding="utf-8")
    monkeypatch.setenv("DB_URL_SALES", sales.url)
    monkeypatch.setenv("DB_URL_REPORTS", reports.url)
    monkeypatch.delenv("EXCLUDE_SCHEMAS", raising=False)
    reset_datasources_cache()
    with cfg.override_settings(project_config_dir=str(project), exclude_schemas=""):
        yield World(tmp_path, project, sales, reports, capsys)
    reset_datasources_cache()


class TestDraftFormat:
    def test_datasource_lines_a_shared_table_once_and_keys_qualified_only_when_shared(self, world):
        done = world.run()
        tables = done.tables()

        assert done.code == 0
        # `Customer` is in both sources under the same schema: one entry, a list.
        assert tables["Customer"].datasource == ("sales", "reports")
        assert "    datasource: [sales, reports]\n" in done.text("schema.yaml")
        # `Trade` exists in two schemas of the first source: both keys are qualified.
        assert {"sales.Trade", "ref.Trade"} <= set(tables) and "Trade" not in tables
        assert tables["sales.Trade"].datasource == ("sales",)
        # Unique names stay bare, with their schema.
        assert tables["Summary"].datasource == ("reports",) and tables["Summary"].db_schema == "rep"
        assert tables["Order"].db_schema == "sales"
        assert sorted(tables) == ["Customer", "Order", "Summary", "Tmp", "ref.Trade", "sales.Trade"]

    def test_the_other_drafts_use_the_same_keys(self, world):
        done = world.run()
        keys = set(done.tables())

        entities = safe_load_strict(done.text("entities.yaml"))["entities"]
        assert set(entities) == keys
        assert entities["Customer"]["datasource"] == ["sales", "reports"]
        assert entities["sales.Trade"]["schema"] == "sales"
        assert "relationships" in safe_load_strict(done.text("relationships.yaml"))
        assert safe_load_strict(done.text("aliases.yaml"))["synonyms"] == {}

    def test_a_sync_of_the_draft_against_the_same_databases_changes_nothing(self, world):
        done = world.run(name="draft_a")

        result = sync_schema_text(done.text("schema.yaml"), ["sales", "reports"], "sales", world.catalogues())

        assert result.changed is False
        assert result.plan.new_tables == ()

    def test_every_draft_carries_the_draft_header(self, world):
        done = world.run()

        for name in ("schema.yaml", "entities.yaml", "aliases.yaml", "relationships.yaml"):
            assert done.text(name).startswith("# AUTO-GENERATED DRAFT")

    def test_including_staging_makes_customer_a_shared_name(self, world):
        done = world.run("--include-staging")

        assert {"sales.Customer", "Sales_Stg.Customer", "Load", "Trade_Inserts"} <= set(done.tables())
        assert "Customer" not in done.tables()


class TestSelectingSources:
    def test_datasource_narrows_the_run_but_keeps_the_datasource_lines(self, world):
        done = world.run("--datasource", "reports")

        assert done.code == 0
        assert sorted(done.tables()) == ["Customer", "Summary"]
        assert done.tables()["Customer"].datasource == ("reports",)
        assert "sales_db" not in done.err and "Sources : reports" in done.err

    def test_datasource_is_repeatable(self, world):
        done = world.run("--datasource", "reports", "--datasource", "sales")

        assert "Sources : sales, reports" in done.err
        assert "Summary" in done.tables() and "Order" in done.tables()

    def test_an_unknown_source_is_refused_naming_the_configured_ones(self, world):
        with pytest.raises(SystemExit) as stop:
            world.run("--datasource", "nope")

        err = world.capsys.readouterr().err
        assert stop.value.code == 2
        assert "unknown data source nope; configured: sales, reports" in err
        assert not (world.tmp_path / "draft").exists()

    def test_without_datasources_yaml_the_implicit_default_source_is_used(self, world, monkeypatch):
        (world.project / "datasources.yaml").unlink()
        reset_datasources_cache()
        monkeypatch.setenv("DB_CONNECTION_URL", world.sales.url)

        done = world.run()

        assert done.code == 0
        assert "Sources : default" in done.err
        assert "datasource:" not in done.text("schema.yaml")
        assert sorted(done.tables()) == ["Customer", "Order", "Tmp", "ref.Trade", "sales.Trade"]


class TestDbUrl:
    def run_url(self, world, *extra: str) -> Run:
        directory = world.tmp_path / "url_draft"
        with attached(world.sales):
            code = schema_inspector_cli.main(
                ["--db-url", world.sales.url, "--output-dir", str(directory), *extra]
            )
        captured = world.capsys.readouterr()
        return Run(code, captured.out, captured.err, directory)

    def test_one_source_named_as_the_default_without_datasource_lines(self, world):
        done = self.run_url(world)

        assert done.code == 0
        assert "datasource:" not in done.text("schema.yaml")
        assert sorted(done.tables()) == ["Customer", "Order", "Tmp", "ref.Trade", "sales.Trade"]
        assert "Sources : sales" in done.err

    def test_it_says_once_that_the_applications_login_may_see_less(self, world):
        done = self.run_url(world)

        assert done.err.count("the application's own login may see less") == 1
        assert "Note: --db-url" in done.err

    def test_the_password_in_the_url_is_not_printed(self, world, tmp_path):
        database = build_database(tmp_path, "pw", {"sales": {"T": None}})
        url = "sqlite+pysqlite://user:s3cretpw@/" + str(database.main)
        with attached(database):
            with patch("database.schema_inspector.create_engine", lambda *_a, **_k: create_engine(database.url)):
                schema_inspector_cli.main(["--db-url", url, "--output-dir", str(tmp_path / "o"), "--dry-run"])

        captured = world.capsys.readouterr()
        assert "s3cretpw" not in captured.err + captured.out

    def test_it_cannot_be_combined_with_datasource(self, world):
        with pytest.raises(SystemExit) as stop:
            schema_inspector_cli.main(["--db-url", world.sales.url, "--datasource", "sales"])

        assert stop.value.code == 2
        assert "cannot be combined" in world.capsys.readouterr().err


class TestSkipRules:
    def test_system_schemas_and_staging_are_skipped_by_default_and_counted(self, world):
        done = world.run()

        assert "system schemas: 2 table(s) in 2 schema(s)" in done.err
        assert "staging (STAGING_PATTERNS; --include-staging keeps them): 3 table(s) in 2 schema(s)" in done.err
        assert not {"cdc", "INFORMATION_SCHEMA"} & {t.db_schema for t in done.tables().values()}
        assert done.tables()["Tmp"].db_schema == "Scratch"       # an ordinary user schema is kept

    def test_the_setting_excludes_a_schema_for_every_source(self, world):
        with cfg.override_settings(exclude_schemas="Scratch, rep"):
            done = world.run()

        assert "Tmp" not in done.tables() and "Summary" not in done.tables()
        assert "configured exclusion (EXCLUDE_SCHEMAS / exclude_schemas:): 2 table(s) in 2 schema(s)" in done.err

    def test_a_sources_own_exclude_schemas_applies_to_that_source_only(self, world):
        (world.project / "datasources.yaml").write_text(
            TWO_SOURCES.replace("url_env: DB_URL_SALES", "url_env: DB_URL_SALES\n    exclude_schemas: [ref, rep]"),
            encoding="utf-8",
        )
        reset_datasources_cache()

        done = world.run()

        tables = done.tables()
        # `ref` is gone from `sales`, so `Trade` is no longer a shared name and is keyed bare.
        assert "Trade" in tables and tables["Trade"].db_schema == "sales" and "ref.Trade" not in tables
        assert "Summary" in tables                      # `rep` is excluded in `sales`, not in `reports`
        assert "configured exclusion (EXCLUDE_SCHEMAS / exclude_schemas:): 1 table(s) in 1 schema(s)" in done.err

    def test_the_command_line_patterns_are_a_separate_rule(self, world):
        done = world.run("--exclude-schemas", "REF, r*")

        assert not any(k.endswith("Trade") and k.startswith("ref") for k in done.tables())
        assert "Summary" not in done.tables()
        assert "--exclude-schemas: 2 table(s) in 2 schema(s)" in done.err

    def test_include_schemas_and_exclude_tables_are_counted_too(self, world):
        done = world.run("--include-schemas", "sales,rep", "--exclude-tables", "order")

        assert sorted(done.tables()) == ["Customer", "Summary", "Trade"]
        assert "outside --include-schemas" in done.err and "--exclude-tables: 1 table(s)" in done.err

    def test_nothing_skipped_prints_no_skipped_block(self, world):
        with patch("database.schema_draft.read_catalogue", lambda engine: {}):
            done = world.run()

        assert "Skipped (not in the drafts)" not in done.err


class TestSampleValues:
    def test_no_value_is_in_any_draft_by_default(self, world):
        done = world.run()

        for name in ("schema.yaml", "entities.yaml", "aliases.yaml", "relationships.yaml"):
            text = done.text(name)
            assert "Real Person" not in text and "secret note" not in text and "e.g." not in text
        assert not (done.directory / SAMPLE_REVIEW_FILENAME).exists()
        assert "Samples : none" in done.err

    def test_the_review_file_holds_the_values_and_only_it(self, world):
        done = world.run("--sample-values", "--sample-rows", "5")
        review = safe_load_strict(done.text(SAMPLE_REVIEW_FILENAME))["sample_values"]

        assert review["Customer"]["Name"] == ["Real Person One", "Real Person Two"]
        assert review["sales.Trade"]["Note"] == ["secret note"]
        assert set(review) <= set(done.tables())
        for name in ("schema.yaml", "entities.yaml", "aliases.yaml", "relationships.yaml"):
            assert "Real Person" not in done.text(name) and "secret note" not in done.text(name)

    @pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
    def test_the_review_file_is_owner_only(self, world):
        done = world.run("--sample-values")

        assert (done.directory / SAMPLE_REVIEW_FILENAME).stat().st_mode & 0o777 == 0o600

    def test_a_dry_run_does_not_print_the_values(self, world):
        done = world.run("--sample-values", "--dry-run")

        assert "Real Person" not in done.out + done.err
        assert not (done.directory / SAMPLE_REVIEW_FILENAME).exists()
        assert "Not printed" in done.err

    def test_sample_rows_alone_reads_nothing_and_says_so(self, world):
        done = world.run("--sample-rows", "3")

        assert "has no effect without --sample-values" in done.err
        assert not (done.directory / SAMPLE_REVIEW_FILENAME).exists()


class TestRelationships:
    def test_a_declared_key_is_plain_and_a_guess_from_a_column_name_is_marked(self, world):
        done = world.run()
        schema_text = done.text("schema.yaml")

        relationships = validate_schema_yaml_text(schema_text).relationships
        ends = {(r.from_table, r.to_table) for r in relationships}
        assert ("Order", "Customer") in ends               # declared: Order.Customer_ID -> Customer.ID
        assert ("sales.Trade", "Customer") in ends         # inferred: Trade.Customer_ID
        lines = schema_text.splitlines()
        marked = [i for i, ln in enumerate(lines) if ln.strip() == INFERRED_COMMENT]
        assert len(marked) == 1
        assert 'from_table: "sales.Trade"' in lines[marked[0] + 1]
        order = next(i for i, ln in enumerate(lines) if 'from_table: "Order"' in ln)
        assert lines[order - 1].strip() != INFERRED_COMMENT

    def test_relationships_yaml_marks_only_the_guess(self, world):
        done = world.run()
        text = done.text("relationships.yaml")

        entries = safe_load_strict(text)["relationships"]
        assert {e["from_table"] for e in entries} == {"Order", "Trade"}
        assert text.count(INFERRED_COMMENT) == 1
        assert "Relationships: 1 declared, 1 inferred" in done.err

    def test_a_relationship_to_a_skipped_table_is_not_drafted(self, world):
        done = world.run("--exclude-tables", "customer")

        assert validate_schema_yaml_text(done.text("schema.yaml")).relationships == []


class TestSummaryAndGuards:
    def test_the_per_schema_count_covers_the_drafted_tables(self, world):
        done = world.run()

        assert "  Total: 6" in done.err and "  sales: 3" in done.err and "  ref: 1" in done.err

    def test_the_protected_directories_are_still_refused(self, world):
        with pytest.raises(SystemExit) as stop:
            schema_inspector_cli.main(["--output-dir", "project_config"])

        assert stop.value.code == 2

    def test_a_source_that_cannot_be_read_fails_the_run_with_one(self, world):
        with patch("database.schema_draft.read_catalogue", side_effect=RuntimeError("boom\nsecond line")):
            done = world.run()

        assert done.code == 1
        assert "sales: could not read the catalogue (RuntimeError: boom)" in done.err
        assert "second line" not in done.err
