# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``scripts/sync_schema.py``: modes, exit codes, files written,
and what it never prints. The database layer is injected
(``main(..., load_catalogue=...)``); nothing here connects to anything."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from config import override_settings
from database.catalogue import COLUMN_DETAILS_SQL, FOREIGN_KEYS_SQL, PRIMARY_KEYS_SQL
from database.datasources import reset_datasources_cache
from schema_data.registry import validate_schema_yaml_text
from scripts import sync_schema as script
from scripts.sync_schema import EXIT_CHECK_FAILED, EXIT_ERROR, EXIT_OK, main
from tests.test_schema_sync import CATALOGUES, SCHEMA

SYNCED = "schema.synced.yaml"
PROPOSED = "relationships.proposed.yaml"


def loader(catalogues=CATALOGUES):
    def load(source: str):
        value = catalogues[source]
        if isinstance(value, Exception):
            raise value
        return value

    return load


@pytest.fixture()
def project(tmp_path):
    """A project_config with two data sources and the realistic schema.yaml."""
    (tmp_path / "datasources.yaml").write_text(
        "default: sales\n"
        "datasources:\n"
        "  sales:\n    url_env: DB_URL_SALES\n"
        "  inventory:\n    url_env: DB_URL_INVENTORY\n",
        encoding="utf-8",
    )
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    reset_datasources_cache()
    with override_settings(project_config_dir=str(tmp_path)):
        yield tmp_path
    reset_datasources_cache()


#: The same schema without its inline ``relationships:`` (which already
#: covers the one declared foreign key of the fake databases).
NO_RELATIONSHIPS = SCHEMA.split("relationships:")[0]


def without_relationships(project: Path) -> None:
    (project / "schema.yaml").write_text(NO_RELATIONSHIPS, encoding="utf-8")


def schema_text(project: Path) -> str:
    return (project / "schema.yaml").read_text(encoding="utf-8")


class TestDefaultMode:
    def test_it_writes_the_synced_file_and_leaves_schema_yaml_alone(self, project, capsys):
        code = main([], load_catalogue=loader())
        assert code == EXIT_OK
        assert schema_text(project) == SCHEMA
        synced = (project / SYNCED).read_text(encoding="utf-8")
        assert synced != SCHEMA
        validate_schema_yaml_text(synced)
        assert f"written: {project / SYNCED}" in capsys.readouterr().out

    def test_the_synced_file_is_what_the_engine_computes(self, project):
        from tests.test_schema_sync import EXPECTED_DEFAULT

        main([], load_catalogue=loader())
        assert (project / SYNCED).read_text(encoding="utf-8") == EXPECTED_DEFAULT

    def test_relationship_proposals_go_to_a_separate_file(self, project, capsys):
        without_relationships(project)
        main([], load_catalogue=loader())
        data = yaml.safe_load((project / PROPOSED).read_text(encoding="utf-8"))
        assert [(r["from_table"], r["from_column"], r["to_table"], r["basis"][:8]) for r in data["relationships"]] == [
            ("Trade", "BrokerID", "Broker", "declared"),
        ]
        assert "declared foreign keys: 1" in capsys.readouterr().out

    def test_relationships_yaml_is_never_touched(self, project):
        existing = "relationships:\n  - from_table: A\n    to_table: B\n    join_hint: x\n"
        (project / "relationships.yaml").write_text(existing, encoding="utf-8")
        main([], load_catalogue=loader())
        assert (project / "relationships.yaml").read_text(encoding="utf-8") == existing

    def test_a_relationship_already_in_relationships_yaml_is_not_proposed(self, project, capsys):
        without_relationships(project)
        (project / "relationships.yaml").write_text(
            "relationships:\n  - from_table: Trade\n    from_column: BrokerID\n"
            "    to_table: Broker\n    to_column: ID\n    join_hint: x\n",
            encoding="utf-8",
        )
        main([], load_catalogue=loader())
        assert not (project / PROPOSED).exists()
        assert "already in relationships.yaml or schema.yaml: 1" in capsys.readouterr().out

    def test_no_relationships_skips_the_proposal_file(self, project):
        main(["--no-relationships"], load_catalogue=loader())
        assert (project / SYNCED).exists() and not (project / PROPOSED).exists()

    def test_output_options_override_the_paths(self, project, tmp_path_factory):
        without_relationships(project)
        elsewhere = tmp_path_factory.mktemp("out")
        main(
            ["--output", str(elsewhere / "a.yaml"), "--relationships-output", str(elsewhere / "b.yaml")],
            load_catalogue=loader(),
        )
        assert (elsewhere / "a.yaml").exists() and (elsewhere / "b.yaml").exists()
        assert not (project / SYNCED).exists() and not (project / PROPOSED).exists()

    def test_it_refuses_to_overwrite_schema_yaml(self, project, capsys):
        code = main(["--output", str(project / "schema.yaml")], load_catalogue=loader())
        assert code == EXIT_ERROR
        assert "refusing to overwrite" in capsys.readouterr().err
        assert schema_text(project) == SCHEMA

    def test_it_refuses_to_overwrite_relationships_yaml(self, project, capsys):
        (project / "relationships.yaml").write_text("relationships: []\n", encoding="utf-8")
        code = main(["--relationships-output", str(project / "relationships.yaml")], load_catalogue=loader())
        assert code == EXIT_ERROR
        assert "refusing to overwrite" in capsys.readouterr().err
        assert (project / "relationships.yaml").read_text(encoding="utf-8") == "relationships: []\n"

    def test_a_byte_order_mark_is_kept(self, project):
        (project / "schema.yaml").write_bytes(b"\xef\xbb\xbf" + SCHEMA.encode("utf-8"))
        main([], load_catalogue=loader())
        assert (project / SYNCED).read_bytes().startswith(b"\xef\xbb\xbf")

    def test_prune_and_add_tables_reach_the_engine(self, project):
        main(["--prune", "--add-tables", "sales_fact.*", "--add-tables", "stock_dim.Symbol"],
             load_catalogue=loader())
        tables = validate_schema_yaml_text((project / SYNCED).read_text(encoding="utf-8")).tables
        assert "Old_Dim.Nothing" not in tables
        assert "Code" not in tables["sales_dim.Broker"].columns
        assert {"Audit", "Symbol"} <= set(tables)

    def test_an_in_sync_schema_writes_no_file_and_says_so(self, project, capsys):
        main([], load_catalogue=loader())
        (project / "schema.yaml").write_text((project / SYNCED).read_text(encoding="utf-8"), encoding="utf-8")
        (project / SYNCED).unlink()
        capsys.readouterr()
        assert main([], load_catalogue=loader()) == EXIT_OK
        assert "already in sync" in capsys.readouterr().out
        assert not (project / SYNCED).exists()

    def test_a_leftover_synced_file_is_pointed_out(self, project, capsys):
        main([], load_catalogue=loader())
        (project / "schema.yaml").write_text((project / SYNCED).read_text(encoding="utf-8"), encoding="utf-8")
        capsys.readouterr()
        main([], load_catalogue=loader())
        assert "left over from an earlier run" in capsys.readouterr().out

    def test_running_twice_with_the_result_in_place_changes_nothing(self, project):
        main(["--prune"], load_catalogue=loader())
        (project / "schema.yaml").write_text((project / SYNCED).read_text(encoding="utf-8"), encoding="utf-8")
        before = schema_text(project)
        assert main(["--prune", "--check"], load_catalogue=loader()) == EXIT_OK
        assert schema_text(project) == before

    def test_a_stale_proposal_file_is_emptied_when_nothing_is_left_to_propose(self, project):
        without_relationships(project)
        main([], load_catalogue=loader())
        assert yaml.safe_load((project / PROPOSED).read_text(encoding="utf-8"))["relationships"]
        (project / "relationships.yaml").write_text(
            "relationships:\n  - from_table: Trade\n    from_column: BrokerID\n"
            "    to_table: Broker\n    to_column: ID\n    join_hint: x\n",
            encoding="utf-8",
        )
        main([], load_catalogue=loader())
        assert yaml.safe_load((project / PROPOSED).read_text(encoding="utf-8")) == {"relationships": []}


class TestDryRun:
    def test_it_prints_the_report_and_writes_nothing(self, project, capsys):
        code = main(["--dry-run"], load_catalogue=loader())
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "== columns added" in out and "sales_dim.Broker.Region nvarchar(20)" in out
        assert "dry run: nothing written" in out
        assert sorted(p.name for p in project.iterdir()) == ["datasources.yaml", "schema.yaml"]
        assert schema_text(project) == SCHEMA


class TestCheckMode:
    def test_out_of_sync_exits_one_and_writes_nothing(self, project, capsys):
        code = main(["--check"], load_catalogue=loader())
        out = capsys.readouterr().out
        assert code == EXIT_CHECK_FAILED
        assert "CHECK FAILED" in out and "sync_schema.py" in out
        assert sorted(p.name for p in project.iterdir()) == ["datasources.yaml", "schema.yaml"]

    def test_in_sync_exits_zero(self, project, capsys):
        main([], load_catalogue=loader())
        (project / "schema.yaml").write_text((project / SYNCED).read_text(encoding="utf-8"), encoding="utf-8")
        capsys.readouterr()
        assert main(["--check"], load_catalogue=loader()) == EXIT_OK
        assert "CHECK OK" in capsys.readouterr().out

    def test_marked_stale_columns_are_reported_but_do_not_fail_the_check(self, project, capsys):
        main([], load_catalogue=loader())
        (project / "schema.yaml").write_text((project / SYNCED).read_text(encoding="utf-8"), encoding="utf-8")
        capsys.readouterr()
        assert main(["--check"], load_catalogue=loader()) == EXIT_OK
        assert "sales_dim.Broker.Code (kept, marked)" in capsys.readouterr().out

    def test_check_with_prune_fails_while_there_is_something_to_prune(self, project):
        main([], load_catalogue=loader())
        (project / "schema.yaml").write_text((project / SYNCED).read_text(encoding="utf-8"), encoding="utf-8")
        assert main(["--check", "--prune"], load_catalogue=loader()) == EXIT_CHECK_FAILED

    def test_check_with_add_tables_fails_until_the_tables_are_listed(self, project):
        main([], load_catalogue=loader())
        (project / "schema.yaml").write_text((project / SYNCED).read_text(encoding="utf-8"), encoding="utf-8")
        assert main(["--check", "--add-tables", "sales_fact.*"], load_catalogue=loader()) == EXIT_CHECK_FAILED

    def test_a_new_column_in_the_database_fails_the_check(self, project):
        main([], load_catalogue=loader())
        (project / "schema.yaml").write_text((project / SYNCED).read_text(encoding="utf-8"), encoding="utf-8")
        from tests._sync_fixtures import tbl

        sales = dict(CATALOGUES["sales"])
        sales[("sales_dim", "broker")] = tbl(
            "sales_dim", "Broker",
            [("ID", "int"), ("Name", "nvarchar(80)"), ("Region", "nvarchar(20)"), ("Added", "int")],
        )
        assert main(["--check"], load_catalogue=loader({"sales": sales, "inventory": CATALOGUES["inventory"]})) == EXIT_CHECK_FAILED

    def test_a_changed_column_type_fails_the_check(self, project):
        main([], load_catalogue=loader())
        text = (project / SYNCED).read_text(encoding="utf-8").replace('Region: "nvarchar(20)"', 'Region: "nvarchar(5)"')
        (project / "schema.yaml").write_text(text, encoding="utf-8")
        assert main(["--check"], load_catalogue=loader()) == EXIT_CHECK_FAILED

    def test_an_unreadable_source_is_an_error_not_a_pass(self, project):
        code = main(["--check"], load_catalogue=loader({"sales": CATALOGUES["sales"], "inventory": RuntimeError("down")}))
        assert code == EXIT_ERROR


class TestErrors:
    def test_an_unreadable_source_stops_the_run_and_writes_nothing(self, project, capsys):
        code = main([], load_catalogue=loader({"sales": CATALOGUES["sales"], "inventory": RuntimeError("down")}))
        captured = capsys.readouterr()
        assert code == EXIT_ERROR
        assert "inventory: could not read the catalogue (RuntimeError: down)" in captured.err
        assert "shared table" in captured.err
        assert not (project / SYNCED).exists() and not (project / PROPOSED).exists()

    def test_an_invalid_schema_yaml_is_an_error(self, project, capsys):
        (project / "schema.yaml").write_text("tables:\n  A:\n    datasource: []\n", encoding="utf-8")
        assert main([], load_catalogue=loader()) == EXIT_ERROR
        assert "datasource" in capsys.readouterr().err

    def test_a_missing_schema_yaml_is_an_error(self, project, capsys):
        (project / "schema.yaml").unlink()
        assert main([], load_catalogue=loader()) == EXIT_ERROR
        assert "cannot read" in capsys.readouterr().err

    def test_a_layout_it_cannot_edit_is_reported_not_written(self, project, capsys):
        (project / "schema.yaml").write_text(
            "tables:\n  sales_fact.Trade: {columns: {TradeID: x}}\n", encoding="utf-8",
        )
        assert main([], load_catalogue=loader()) == EXIT_ERROR
        assert "sales_fact.Trade" in capsys.readouterr().err
        assert not (project / SYNCED).exists()

    def test_an_output_that_would_not_validate_is_never_written(self, project, monkeypatch, capsys):
        import schema_data.sync as sync

        real = sync.render_synced_text
        monkeypatch.setattr(
            sync, "render_synced_text",
            lambda o, s, p: (lambda r: (r[0].replace("    columns:\n", "    columns:\n    columns:\n", 1), r[1]))(real(o, s, p)),
        )
        assert main([], load_catalogue=loader()) == EXIT_ERROR
        assert "not written" in capsys.readouterr().err
        assert not (project / SYNCED).exists()

    def test_an_output_that_changed_a_description_is_never_written(self, project, monkeypatch, capsys):
        import schema_data.sync as sync

        real = sync.render_synced_text
        monkeypatch.setattr(
            sync, "render_synced_text",
            lambda o, s, p: (lambda r: (r[0].replace("Broker master data", "Something else"), r[1]))(real(o, s, p)),
        )
        assert main([], load_catalogue=loader()) == EXIT_ERROR
        assert "unexpectedly" in capsys.readouterr().err
        assert not (project / SYNCED).exists()

    def test_an_unwritable_destination_is_an_error(self, project, capsys):
        code = main(["--output", str(project / "missing-dir" / "x.yaml")], load_catalogue=loader())
        assert code == EXIT_ERROR
        assert "cannot write" in capsys.readouterr().err


class TestOneDataSource:
    def test_no_datasource_lines_are_written(self, project):
        (project / "datasources.yaml").unlink()
        reset_datasources_cache()
        main([], load_catalogue=loader({"default": CATALOGUES["sales"]}))
        parsed = validate_schema_yaml_text((project / SYNCED).read_text(encoding="utf-8"))
        assert parsed.tables["sales_dim.Broker"].datasource == ()
        assert "Region" in parsed.tables["sales_dim.Broker"].columns


class TestNoCredentialsOrRows:
    def test_the_default_loader_runs_the_three_catalogue_queries_and_nothing_else(self, monkeypatch):
        executed: list[str] = []

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, statement):
                executed.append(str(statement))
                return SimpleNamespace(fetchall=lambda: [])

        engine = SimpleNamespace(dialect=SimpleNamespace(name="mssql"), connect=lambda: Conn())
        asked: list[str] = []
        monkeypatch.setattr("database.connection.get_engine", lambda name: asked.append(name) or engine)
        assert script.default_catalogue_loader("sales") == {}
        assert asked == ["sales"]
        assert executed == [COLUMN_DETAILS_SQL, PRIMARY_KEYS_SQL, FOREIGN_KEYS_SQL]

    def test_the_output_names_tables_and_columns_only(self, project, capsys, monkeypatch):
        without_relationships(project)
        monkeypatch.setenv("DB_URL_SALES", "mssql+pyodbc://svc:hunter2@h/db")
        main([], load_catalogue=loader())
        captured = capsys.readouterr()
        assert "hunter2" not in captured.out + captured.err
        assert "mssql" not in captured.out + captured.err
        for path in (project / SYNCED, project / PROPOSED):
            assert "hunter2" not in path.read_text(encoding="utf-8")

    @pytest.mark.parametrize("message, secret", [
        ("Login failed for mssql+pyodbc://svc:hunter2@host/db", "hunter2"),
        ("Driver={x};Server=h;UID=svc;PWD=hunter2;Database=d", "hunter2"),
        ("password = hunter2 rejected", "hunter2"),
    ])
    def test_a_driver_error_is_cut_to_one_line_without_the_password(self, project, capsys, message, secret):
        main([], load_catalogue=loader({"sales": RuntimeError(message + "\nsecond line"), "inventory": CATALOGUES["inventory"]}))
        err = capsys.readouterr().err
        assert secret not in err and "second line" not in err
        assert "RuntimeError" in err


class TestRunsAsAScript:
    def test_it_is_runnable_from_the_repo_root(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "scripts/sync_schema.py", "--help"],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0
        for option in ("--check", "--dry-run", "--prune", "--add-tables", "--output"):
            assert option in result.stdout
