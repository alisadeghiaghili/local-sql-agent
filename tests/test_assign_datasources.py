# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``scripts/assign_datasources.py``.

The database layer is injected (``main(..., load_catalogue=...)``): a fake
catalogue stands in for each source's ``INFORMATION_SCHEMA``, so nothing
here connects to anything. The schema.yaml used throughout is a realistic
one -- header comments, blank lines, quoted keys, a wrong ``datasource:``
already present (inline and as a block list), a table that lives in both
sources, and one that lives in neither.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from config import override_settings
from database.datasources import reset_datasources_cache
from schema_data.registry import validate_schema_yaml_text
from scripts import assign_datasources as script
from scripts.assign_datasources import (
    EXIT_CHECK_FAILED,
    EXIT_ERROR,
    EXIT_OK,
    NOT_FOUND_COMMENT,
    LayoutError,
    build_report,
    locate_tables,
    main,
    render_schema_yaml,
)

SCHEMA = '''\
# Warehouse schema for the exchange DMs.
# Curated by hand -- keep the comments.

tables:

  # ---- Auction ----
  Auction_Dim.Broker:   # brokers
    description: "Broker master data"
    columns:
      ID: "Primary key"
      Code: "Broker code"          # not in the database any more

  "Auction_Fact.Trade":
    description: "One row per trade"
    columns:
      TradeID: "Primary key"
      Amount: "Value"

  # ---- Future ----
  'Future_Fact.Contract':
    description: "Contracts"
    datasource: Auction_DM  # copied from the auction tables
    columns:
      ContractID: "Primary key"

  Symbol:
    db_schema: Future_Dim
    description: "Symbols"
    datasource:
      - Auction_DM
    columns:
      SymbolID: "Primary key"

  # ---- Shared ----
  General_Dim.Date:
    description: "Calendar"
    columns:
      DateID: "Primary key"
      SeqID: "Sequence"

  # ---- Gone ----
  Old_Dim.Nothing:
    description: "Dropped long ago"
    columns:
      ID: "Primary key"

relationships:
  - from_table: Auction_Fact.Trade
    to_table: Auction_Dim.Broker
    join_sql: "[Auction_Fact].[Trade].[BrokerID] = [Auction_Dim].[Broker].[ID]"
'''

EXPECTED = '''\
# Warehouse schema for the exchange DMs.
# Curated by hand -- keep the comments.

tables:

  # ---- Auction ----
  Auction_Dim.Broker:   # brokers
    datasource: Auction_DM
    description: "Broker master data"
    columns:
      ID: "Primary key"
      Code: "Broker code"          # not in the database any more

  "Auction_Fact.Trade":
    datasource: Auction_DM
    description: "One row per trade"
    columns:
      TradeID: "Primary key"
      Amount: "Value"

  # ---- Future ----
  'Future_Fact.Contract':
    description: "Contracts"
    datasource: Future_DM  # copied from the auction tables
    columns:
      ContractID: "Primary key"

  Symbol:
    db_schema: Future_Dim
    description: "Symbols"
    datasource: Future_DM
    columns:
      SymbolID: "Primary key"

  # ---- Shared ----
  General_Dim.Date:
    datasource: [Auction_DM, Future_DM]
    description: "Calendar"
    columns:
      DateID: "Primary key"
      SeqID: "Sequence"

  # ---- Gone ----
  Old_Dim.Nothing:
    # not found in any data source
    description: "Dropped long ago"
    columns:
      ID: "Primary key"

relationships:
  - from_table: Auction_Fact.Trade
    to_table: Auction_Dim.Broker
    join_sql: "[Auction_Fact].[Trade].[BrokerID] = [Auction_Dim].[Broker].[ID]"
'''


def _cat(**tables: set[str]) -> dict[tuple[str, str], frozenset[str]]:
    """``{"Schema__Table": {cols}}`` -> a lower-cased catalogue."""
    out = {}
    for name, columns in tables.items():
        schema, table = name.split("__")
        out[(schema.lower(), table.lower())] = frozenset(c.lower() for c in columns)
    return out


AUCTION = _cat(
    Auction_Dim__Broker={"ID"},
    Auction_Fact__Trade={"TradeID", "Amount", "Spare"},
    General_Dim__Date={"DateID"},
)
FUTURE = _cat(
    Future_Fact__Contract={"ContractID"},
    Future_Dim__Symbol={"SymbolID"},
    General_Dim__Date={"DateID", "SeqID"},
)

SOURCES = ("Auction_DM", "Future_DM")


def _loader(catalogues: dict):
    def load(source: str):
        value = catalogues[source]
        if isinstance(value, Exception):
            raise value
        return value

    return load


@pytest.fixture()
def project(tmp_path):
    """A project_config with two sources and the realistic schema.yaml."""
    (tmp_path / "datasources.yaml").write_text(
        "default: Auction_DM\n"
        "datasources:\n"
        "  Auction_DM:\n    url_env: DB_URL_AUCTION\n"
        "  Future_DM:\n    url_env: DB_URL_FUTURE\n",
        encoding="utf-8",
    )
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    reset_datasources_cache()
    with override_settings(project_config_dir=str(tmp_path)):
        yield tmp_path
    reset_datasources_cache()


def _placements(text: str = SCHEMA):
    schema = validate_schema_yaml_text(text)
    placements = locate_tables(
        schema, SOURCES, "Auction_DM", {"Auction_DM": AUCTION, "Future_DM": FUTURE},
    )
    return schema, placements


# ---------------------------------------------------------------------------
# Matching tables to catalogues
# ---------------------------------------------------------------------------

class TestLocateTables:
    def test_each_table_is_matched_to_the_sources_that_have_it(self):
        _, placements = _placements()
        found = {p.key: p.found_in for p in placements}
        assert found == {
            "Auction_Dim.Broker": ("Auction_DM",),
            "Auction_Fact.Trade": ("Auction_DM",),
            "Future_Fact.Contract": ("Future_DM",),
            "Symbol": ("Future_DM",),               # via db_schema
            "General_Dim.Date": ("Auction_DM", "Future_DM"),
            "Old_Dim.Nothing": (),
        }

    def test_current_and_effective_assignment(self):
        _, placements = _placements()
        by_key = {p.key: p for p in placements}
        assert by_key["Auction_Dim.Broker"].current == ()
        assert by_key["Auction_Dim.Broker"].effective == ("Auction_DM",)  # the default
        assert by_key["Future_Fact.Contract"].effective == ("Auction_DM",)
        assert by_key["Symbol"].current == ("Auction_DM",)

    def test_columns_schema_yaml_lists_but_the_database_lacks(self):
        _, placements = _placements()
        missing = {p.key: dict(p.missing_columns) for p in placements if p.missing_columns}
        assert missing == {
            "Auction_Dim.Broker": {"Auction_DM": ("Code",)},
            "General_Dim.Date": {"Auction_DM": ("SeqID",)},   # only one copy lacks it
        }

    def test_matching_ignores_case_and_a_multi_part_db_schema_uses_its_schema(self):
        schema = validate_schema_yaml_text(
            "tables:\n"
            "  CUSTOMER:\n    db_schema: OtherDb.DBO\n    columns: {id: x}\n"
            "  Bare:\n    columns: {ID: x}\n"
        )
        catalogues = {"a": _cat(dbo__customer={"ID"}, dbo__bare={"id"}), "b": {}}
        placements = locate_tables(schema, ["a", "b"], "a", catalogues)
        assert [(p.key, p.found_in) for p in placements] == [
            ("CUSTOMER", ("a",)), ("Bare", ("a",)),
        ]


# ---------------------------------------------------------------------------
# Editing schema.yaml
# ---------------------------------------------------------------------------

class TestRenderSchemaYaml:
    def test_inserts_and_replaces_exactly_the_datasource_lines(self):
        schema, placements = _placements()
        assert render_schema_yaml(SCHEMA, schema, placements) == EXPECTED

    def test_every_original_line_and_comment_survives(self):
        schema, placements = _placements()
        new = render_schema_yaml(SCHEMA, schema, placements).splitlines()

        def kept(lines):
            return [
                line for line in lines
                if not line.strip().startswith(("datasource:", "- Auction_DM", NOT_FOUND_COMMENT))
            ]

        assert kept(new) == kept(SCHEMA.splitlines())
        assert "      Code: \"Broker code\"          # not in the database any more" in new
        assert "  # ---- Shared ----" in new

    def test_the_result_validates_with_the_projects_own_validator(self):
        schema, placements = _placements()
        parsed = validate_schema_yaml_text(render_schema_yaml(SCHEMA, schema, placements))
        assert {k: t.datasource for k, t in parsed.tables.items()} == {
            "Auction_Dim.Broker": ("Auction_DM",),
            "Auction_Fact.Trade": ("Auction_DM",),
            "Future_Fact.Contract": ("Future_DM",),
            "Symbol": ("Future_DM",),
            "General_Dim.Date": ("Auction_DM", "Future_DM"),
            "Old_Dim.Nothing": (),
        }

    def test_an_existing_datasource_is_replaced_not_duplicated(self):
        # The strict YAML loader refuses a repeated key; validating is the proof.
        schema, placements = _placements()
        new = render_schema_yaml(SCHEMA, schema, placements)
        contract = new.split("'Future_Fact.Contract':")[1].split("Symbol:")[0]
        assert contract.count("datasource:") == 1
        symbol = new.split("  Symbol:")[1].split("General_Dim")[0]
        assert symbol.count("datasource:") == 1 and "- Auction_DM" not in symbol

    def test_a_table_already_assigned_correctly_is_left_exactly_as_written(self):
        text = (
            "tables:\n"
            "  Date:\n"
            "    datasource:\n"
            "      - Future_DM\n"
            "      - Auction_DM\n"
            "    columns: {ID: x}\n"
        )
        schema = validate_schema_yaml_text(text)
        catalogues = {
            "Auction_DM": _cat(dbo__date={"ID"}), "Future_DM": _cat(dbo__date={"ID"}),
        }
        placements = locate_tables(schema, SOURCES, "Auction_DM", catalogues)
        assert render_schema_yaml(text, schema, placements) == text

    def test_running_it_again_on_its_own_output_changes_nothing(self):
        schema, placements = _placements()
        once = render_schema_yaml(SCHEMA, schema, placements)
        schema2, placements2 = _placements(once)
        assert render_schema_yaml(once, schema2, placements2) == once
        assert once.count(NOT_FOUND_COMMENT) == 1

    def test_a_stale_not_found_comment_is_dropped_once_the_table_is_found(self):
        text = (
            "tables:\n"
            "  Date:\n"
            f"    {NOT_FOUND_COMMENT}\n"
            "    columns: {ID: x}\n"
        )
        schema = validate_schema_yaml_text(text)
        placements = locate_tables(
            schema, SOURCES, "Auction_DM", {"Auction_DM": _cat(dbo__date={"ID"}), "Future_DM": {}},
        )
        assert render_schema_yaml(text, schema, placements) == (
            "tables:\n  Date:\n    datasource: Auction_DM\n    columns: {ID: x}\n"
        )

    def test_a_multi_line_flow_list_is_replaced_whole(self):
        text = (
            "tables:\n"
            "  Date:\n"
            "    datasource: [Auction_DM,\n"
            "                 Future_DM]\n"
            "    columns: {ID: x}\n"
        )
        schema = validate_schema_yaml_text(text)
        placements = locate_tables(
            schema, SOURCES, "Auction_DM", {"Auction_DM": _cat(dbo__date={"ID"}), "Future_DM": {}},
        )
        assert render_schema_yaml(text, schema, placements) == (
            "tables:\n  Date:\n    datasource: Auction_DM\n    columns: {ID: x}\n"
        )

    def test_the_files_own_indentation_is_used(self):
        text = "tables:\n    Date:\n        description: d\n"
        schema = validate_schema_yaml_text(text)
        placements = locate_tables(
            schema, SOURCES, "Auction_DM", {"Auction_DM": _cat(dbo__date=set()), "Future_DM": {}},
        )
        assert render_schema_yaml(text, schema, placements) == (
            "tables:\n    Date:\n        datasource: Auction_DM\n        description: d\n"
        )

    def test_windows_line_endings_and_a_missing_final_newline_are_preserved(self):
        schema, placements = _placements()
        crlf = SCHEMA.replace("\n", "\r\n")
        out = render_schema_yaml(crlf, schema, placements)
        assert out == EXPECTED.replace("\n", "\r\n")
        assert not re.search(r"(?<!\r)\n", out)
        bare = SCHEMA.rstrip("\n")
        assert render_schema_yaml(bare, schema, placements) == EXPECTED.rstrip("\n")

    def test_a_flow_style_table_is_refused_by_name(self):
        text = "tables:\n  Date: {description: d, columns: {ID: x}}\n"
        schema = validate_schema_yaml_text(text)
        placements = locate_tables(
            schema, SOURCES, "Auction_DM", {"Auction_DM": _cat(dbo__date={"ID"}), "Future_DM": {}},
        )
        with pytest.raises(LayoutError, match="Date"):
            render_schema_yaml(text, schema, placements)

    def test_a_flow_style_tables_mapping_is_refused(self):
        text = "tables: {Date: {description: d}}\n"
        schema = validate_schema_yaml_text(text)
        placements = locate_tables(
            schema, SOURCES, "Auction_DM", {"Auction_DM": _cat(dbo__date=set()), "Future_DM": {}},
        )
        with pytest.raises(LayoutError, match="tables"):
            render_schema_yaml(text, schema, placements)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class TestReport:
    def test_sections_for_each_source_shared_missing_disagreeing_and_columns(self):
        _, placements = _placements()
        report = build_report(placements, SOURCES, "Auction_DM")
        assert "== tables found only in Auction_DM: 2 ==" in report
        assert "== tables found only in Future_DM: 2 ==" in report
        assert "  Future_Fact.Contract" in report
        assert "== tables found in more than one data source: 1 ==" in report
        assert "  General_Dim.Date: Auction_DM, Future_DM" in report
        assert "== tables found in no data source: 1 ==" in report
        assert "  Old_Dim.Nothing" in report
        assert "== tables whose datasource: disagrees with what was found: 3 ==" in report
        assert (
            "  Auction_Fact.Trade" not in report.split("disagrees")[1]
        )  # agrees: no key, default source, found there
        assert "  Symbol: schema.yaml says Auction_DM, found in Future_DM" in report
        assert (
            "  Future_Fact.Contract: schema.yaml says Auction_DM, found in Future_DM" in report
        )
        assert (
            "  General_Dim.Date: schema.yaml says Auction_DM (the default), "
            "found in Auction_DM, Future_DM" in report
        )
        assert "  Auction_Dim.Broker [Auction_DM]: Code" in report
        assert "  General_Dim.Date [Auction_DM]: SeqID" in report

    def test_a_table_that_exists_nowhere_is_not_a_disagreement(self):
        _, placements = _placements()
        old = next(p for p in placements if p.key == "Old_Dim.Nothing")
        assert not old.disagrees


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

class TestMain:
    def test_writes_the_proposed_file_next_to_schema_yaml_and_leaves_schema_yaml_alone(
        self, project, capsys,
    ):
        code = main([], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}))
        out = capsys.readouterr().out
        assert code == EXIT_OK
        proposed = project / "schema.with_datasources.yaml"
        assert proposed.read_text(encoding="utf-8") == EXPECTED
        assert (project / "schema.yaml").read_text(encoding="utf-8") == SCHEMA
        assert f"written: {proposed}" in out
        assert "data sources: Auction_DM, Future_DM | default: Auction_DM" in out
        assert "Auction_DM: 3 tables and views" in out
        assert "General_Dim.Date: Auction_DM, Future_DM" in out

    def test_output_option_overrides_the_path(self, project, tmp_path_factory):
        target = tmp_path_factory.mktemp("elsewhere") / "proposed.yaml"
        code = main(
            ["--output", str(target)],
            load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}),
        )
        assert code == EXIT_OK
        assert target.read_text(encoding="utf-8") == EXPECTED
        assert not (project / "schema.with_datasources.yaml").exists()

    def test_it_refuses_to_overwrite_schema_yaml(self, project, capsys):
        code = main(
            ["--output", str(project / "schema.yaml")],
            load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}),
        )
        assert code == EXIT_ERROR
        assert "refusing to overwrite" in capsys.readouterr().err
        assert (project / "schema.yaml").read_text(encoding="utf-8") == SCHEMA

    def test_a_byte_order_mark_is_kept(self, project):
        (project / "schema.yaml").write_bytes(b"\xef\xbb\xbf" + SCHEMA.encode("utf-8"))
        main([], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}))
        raw = (project / "schema.with_datasources.yaml").read_bytes()
        assert raw == b"\xef\xbb\xbf" + EXPECTED.encode("utf-8")

    def test_an_unreadable_source_stops_the_run_and_writes_nothing(self, project, capsys):
        code = main([], load_catalogue=_loader({
            "Auction_DM": AUCTION, "Future_DM": RuntimeError("login failed for user x"),
        }))
        captured = capsys.readouterr()
        assert code == EXIT_ERROR
        assert "Future_DM: could not list tables (RuntimeError: login failed" in captured.err
        assert "shared table" in captured.err
        assert not (project / "schema.with_datasources.yaml").exists()

    def test_an_error_message_is_cut_to_its_first_line(self, project, capsys):
        long_error = RuntimeError("first line\nsecond line with detail " + "x" * 500)
        main([], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": long_error}))
        err = capsys.readouterr().err
        assert "first line" in err and "second line" not in err

    def test_an_output_that_would_not_validate_is_never_written(
        self, project, monkeypatch, capsys,
    ):
        monkeypatch.setattr(
            script, "render_schema_yaml",
            lambda original, schema, placements: original.replace(
                "  Symbol:\n", "  Symbol:\n    datasource: A\n    datasource: B\n",
            ),
        )
        code = main([], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}))
        assert code == EXIT_ERROR
        assert "not written" in capsys.readouterr().err
        assert not (project / "schema.with_datasources.yaml").exists()

    def test_an_output_that_changed_more_than_datasource_is_never_written(
        self, project, monkeypatch, capsys,
    ):
        real = script.render_schema_yaml
        monkeypatch.setattr(
            script, "render_schema_yaml",
            lambda original, schema, placements: real(original, schema, placements).replace(
                "Broker master data", "Something else",
            ),
        )
        code = main([], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}))
        assert code == EXIT_ERROR
        assert "changed more than datasource" in capsys.readouterr().err
        assert not (project / "schema.with_datasources.yaml").exists()

    def test_a_layout_it_cannot_edit_is_reported_not_written(self, project, capsys):
        (project / "schema.yaml").write_text(
            "tables:\n  Auction_Fact.Trade: {columns: {TradeID: x}}\n", encoding="utf-8",
        )
        code = main([], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}))
        assert code == EXIT_ERROR
        assert "Auction_Fact.Trade" in capsys.readouterr().err
        assert not (project / "schema.with_datasources.yaml").exists()

    def test_an_invalid_schema_yaml_is_an_error(self, project, capsys):
        (project / "schema.yaml").write_text("tables:\n  A:\n    datasource: []\n", encoding="utf-8")
        code = main([], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}))
        assert code == EXIT_ERROR
        assert "datasource" in capsys.readouterr().err

    def test_a_missing_schema_yaml_is_an_error(self, project, capsys):
        (project / "schema.yaml").unlink()
        code = main([], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}))
        assert code == EXIT_ERROR
        assert "cannot read" in capsys.readouterr().err

    def test_one_data_source_reports_but_writes_no_file(self, project, capsys):
        (project / "datasources.yaml").unlink()
        reset_datasources_cache()
        code = main([], load_catalogue=_loader({"default": AUCTION}))
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "only one data source" in out
        assert "== tables found in no data source" in out
        assert not (project / "schema.with_datasources.yaml").exists()


class TestCheckMode:
    def test_a_disagreement_exits_non_zero_and_writes_nothing(self, project, capsys):
        code = main(
            ["--check"], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}),
        )
        out = capsys.readouterr().out
        assert code == EXIT_CHECK_FAILED
        assert "CHECK FAILED: 3 table(s)" in out
        assert not (project / "schema.with_datasources.yaml").exists()
        assert (project / "schema.yaml").read_text(encoding="utf-8") == SCHEMA

    def test_a_correct_schema_exits_zero(self, project, capsys):
        proposed = EXPECTED
        (project / "schema.yaml").write_text(proposed, encoding="utf-8")
        code = main(
            ["--check"], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}),
        )
        assert code == EXIT_OK
        assert "CHECK OK" in capsys.readouterr().out

    def test_the_proposed_file_passes_its_own_check(self, project):
        loader = _loader({"Auction_DM": AUCTION, "Future_DM": FUTURE})
        main([], load_catalogue=loader)
        proposed = (project / "schema.with_datasources.yaml").read_text(encoding="utf-8")
        (project / "schema.yaml").write_text(proposed, encoding="utf-8")
        assert main(["--check"], load_catalogue=loader) == EXIT_OK

    def test_an_unreadable_source_is_an_error_not_a_pass(self, project):
        code = main(["--check"], load_catalogue=_loader({
            "Auction_DM": AUCTION, "Future_DM": RuntimeError("down"),
        }))
        assert code == EXIT_ERROR

    def test_a_table_assigned_to_a_source_that_lacks_it_is_a_disagreement(self, project):
        (project / "schema.yaml").write_text(
            "tables:\n  Auction_Fact.Trade:\n    datasource: [Auction_DM, Future_DM]\n"
            "    columns: {TradeID: x}\n",
            encoding="utf-8",
        )
        code = main(
            ["--check"], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}),
        )
        assert code == EXIT_CHECK_FAILED


class TestNoRowDataOrCredentials:
    def test_the_default_loader_asks_the_catalogue_only(self, monkeypatch):
        """The production loader reads names through ``database.catalogue``
        (two ``INFORMATION_SCHEMA`` queries) and nothing else."""
        calls: list[str] = []
        monkeypatch.setattr("database.connection.get_engine", lambda name: calls.append(name) or "engine")
        monkeypatch.setattr(script, "list_tables", lambda engine: frozenset({("dbo", "t")}))
        monkeypatch.setattr(
            script, "list_columns", lambda engine: {("dbo", "t"): frozenset({"id"})},
        )
        catalogue = script.default_catalogue_loader("Auction_DM")
        assert calls == ["Auction_DM"]
        assert catalogue == {("dbo", "t"): frozenset({"id"})}

    def test_a_table_with_no_visible_column_is_still_listed(self, monkeypatch):
        monkeypatch.setattr("database.connection.get_engine", lambda name: "engine")
        monkeypatch.setattr(script, "list_tables", lambda engine: frozenset({("dbo", "t")}))
        monkeypatch.setattr(script, "list_columns", lambda engine: {})
        assert script.default_catalogue_loader("x") == {("dbo", "t"): frozenset()}

    def test_the_report_names_tables_only(self, project, capsys, monkeypatch):
        monkeypatch.setenv("DB_URL_AUCTION", "mssql+pyodbc://svc:hunter2@h/db")
        main(["--check"], load_catalogue=_loader({"Auction_DM": AUCTION, "Future_DM": FUTURE}))
        captured = capsys.readouterr()
        assert "hunter2" not in captured.out + captured.err
        assert "mssql" not in captured.out + captured.err


class TestRunsAsAScript:
    def test_it_is_runnable_from_the_repo_root(self):
        import subprocess
        import sys

        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "scripts/assign_datasources.py", "--help"],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0
        assert "--check" in result.stdout and "--output" in result.stdout
