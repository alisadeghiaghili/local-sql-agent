# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The setup wizard over the data sources of ``datasources.yaml``.

Two SQLite databases stand in for two data sources; each schema is an
``ATTACH``ed file (``tests/_inspector_fixtures.py``), the model is the
recording fake of ``tests/test_setup_wizard_drafts.py``, and the application's
engines are replaced the way ``tests/test_schema_inspector_sources.py`` does.
Pinned here:

* with ``datasources.yaml`` and no ``--db-url`` the wizard reads every source
  (or those named with ``--datasource``) through the application's engine, lists
  names first, plans and keys the tables of all sources together, and writes
  ``datasource:`` lines; a ``schema.table`` in two sources is one entity;
* the system schemas, ``EXCLUDE_SCHEMAS`` and a source's ``exclude_schemas:``
  are never read, and the summary names the rule;
* the schemas an operator leaves out interactively can be recorded in
  ``datasources.yaml`` (backup, comments kept, validated, otherwise untouched), or
  printed as the ``EXCLUDE_SCHEMAS=`` line for ``.env``;
* ``--db-url`` keeps reading one database with the login in the URL.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

import config as cfg
import setup_project as sp
from database.datasources import reset_datasources_cache, validate_datasources_yaml_text
from database.schema_inspector import SchemaInspector
from tests._inspector_fixtures import attached, build_database, engines_for
from tests.test_setup_wizard_drafts import RecordingLLM, read_yaml

SALES_LAYOUT = {
    "sales": {
        "Customer": None,
        "Trade": "ID INTEGER PRIMARY KEY, Customer_ID INTEGER, Note VARCHAR(40)",
    },
    "ref": {"Trade": None},
    "Scratch": {"Tmp": None},
    "cdc": {"change_table": None},
}

REPORTS_LAYOUT = {
    "sales": {"Customer": None},
    "rep": {"Summary": "ID INTEGER PRIMARY KEY, Region VARCHAR(20), Total INTEGER"},
    "Sandbox": {"Draft": None},
}

DATASOURCES = """\
# Warehouses of this deployment
default: sales

datasources:
  # the main one
  sales:
    url_env: DB_URL_SALES   # set in .env
    description: Sales warehouse

  reports:
    url_env: DB_URL_REPORTS
"""


@pytest.fixture(autouse=True)
def _unwrapped_console(monkeypatch):
    """Keep the wizard's console from wrapping its lines (see test_setup_wizard.py)."""
    if hasattr(sp.console, "_width"):
        monkeypatch.setattr(sp.console, "_width", 10_000)


@dataclass
class Run:
    code: int
    out: str
    directory: Path
    read: list[tuple[str, str]]

    def entities(self) -> dict:
        return read_yaml(self.directory / "entities.yaml")["entities"]

    def log(self) -> dict:
        return json.loads((self.directory / ".setup_log.json").read_text(encoding="utf-8"))


@dataclass
class World:
    tmp_path: Path
    project: Path
    sales: object
    reports: object
    capsys: pytest.CaptureFixture[str]

    @property
    def datasources_path(self) -> Path:
        return self.project / "datasources.yaml"

    def run(
        self, *argv: str, name: str = "draft", answers: dict[str, str] | None = None,
        record: bool | Callable[..., bool] | None = None, llm: RecordingLLM | None = None,
    ) -> Run:
        """Run the wizard. *answers* makes it interactive: ``{question prefix: answer}``; *record* answers the yes/no."""
        directory = self.tmp_path / name
        read: list[tuple[str, str]] = []
        original = SchemaInspector._inspect_table

        def spy(inspector, engine, sa_inspector, schema, table_name, fetch_row_counts):
            read.append((schema, table_name))
            return original(inspector, engine, sa_inspector, schema, table_name, fetch_row_counts)

        args = ["--language", "en", "--output", str(directory), "--allow-repeated-answers", *argv]
        with attached(self.sales, self.reports):
            engines = engines_for({"sales": self.sales, "reports": self.reports})
            try:
                with ExitStack() as stack:
                    stack.enter_context(patch(
                        "database.connection.get_engine",
                        lambda source=None: engines["sales" if source in (None, "default") else source],
                    ))
                    stack.enter_context(patch.object(SchemaInspector, "_inspect_table", spy))
                    stack.enter_context(patch.object(sp, "setup_wizard_llm", return_value=llm or RecordingLLM()))
                    if answers is None:
                        args.append("--non-interactive")
                    else:
                        def ask(question, default="", non_interactive=False):
                            return next((a for q, a in answers.items() if question.startswith(q)), "")

                        stack.enter_context(patch.object(sp, "_ask", side_effect=ask))
                        stack.enter_context(patch.object(
                            sp, "_choose", side_effect=lambda q, choices, default, non_interactive=False: "Accept",
                        ))
                        stack.enter_context(patch.object(
                            sp, "_yn",
                            side_effect=record if callable(record)
                            else (lambda q, default=True, non_interactive=False: bool(record)),
                        ))
                    code = sp.main(args)
            finally:
                for engine in engines.values():
                    engine.dispose()
        return Run(code, self.capsys.readouterr().out, directory, read)


@pytest.fixture()
def world(tmp_path, monkeypatch, capsys):
    sales = build_database(tmp_path, "sales_db", SALES_LAYOUT)
    reports = build_database(tmp_path, "reports_db", REPORTS_LAYOUT)
    project = tmp_path / "project_config"
    project.mkdir()
    (project / "datasources.yaml").write_text(DATASOURCES, encoding="utf-8")
    monkeypatch.setenv("DB_URL_SALES", sales.url)
    monkeypatch.setenv("DB_URL_REPORTS", reports.url)
    for variable in ("EXCLUDE_SCHEMAS", "DB_CONNECTION_URL", "DATABASE_URL"):
        monkeypatch.delenv(variable, raising=False)
    reset_datasources_cache()
    with cfg.override_settings(project_config_dir=str(project), exclude_schemas=""):
        yield World(tmp_path, project, sales, reports, capsys)
    reset_datasources_cache()


def schemas(run: Run) -> set[str]:
    """The schemas whose tables were read (columns and all)."""
    return {schema for schema, _ in run.read}


# ---------------------------------------------------------------------------
# Every source, together
# ---------------------------------------------------------------------------

class TestEverySourceTogether:
    def test_one_merged_set_with_datasource_lines_and_a_shared_table_once(self, world):
        run = world.run("--exclude-schemas", "Scratch,Sandbox")
        assert run.code == 0, run.out
        entities = run.entities()
        # `Trade` is in two schemas of one source: qualified. `Customer` is in `sales` of both sources: one entity.
        assert sorted(entities) == ["Customer", "Summary", "ref.Trade", "sales.Trade"]
        assert entities["Customer"]["datasource"] == ["sales", "reports"]
        assert entities["sales.Trade"]["datasource"] == ["sales"]
        assert entities["Summary"]["datasource"] == ["reports"]
        text = (run.directory / "entities.yaml").read_text(encoding="utf-8")
        assert "datasource: [sales, reports]" in text
        assert [key for key, entry in entities.items() if entry["table"] == "Customer"] == ["Customer"]

    def test_the_names_are_listed_before_anything_is_read_and_the_application_engine_is_used(self, world):
        run = world.run("--exclude-schemas", "Scratch,Sandbox")
        assert "Data source sales: " in run.out and "Data source reports: " in run.out
        assert "read through the application's own login" in run.out
        log = run.log()["step1_connection"]
        assert set(log["datasources"]) == {"sales", "reports"} and "db_url_redacted" not in log

    def test_the_skip_summary_names_the_rule_per_source(self, world):
        run = world.run("--exclude-schemas", "Scratch")
        assert "Skipped, by rule:" in run.out
        assert "system schemas: 1 table(s) in 1 schema(s)" in run.out
        assert "--exclude-schemas: 1 table(s) in 1 schema(s)" in run.out
        assert "    sales: 1 table(s) in 1 schema(s)" in run.out

    def test_datasource_narrows_the_run_and_the_lines_are_still_written(self, world):
        run = world.run("--datasource", "reports", "--exclude-schemas", "Sandbox")
        assert run.code == 0
        assert {s for s, _ in run.read} == {"sales", "rep"}
        entities = run.entities()
        assert sorted(entities) == ["Customer", "Summary"]
        assert entities["Customer"]["datasource"] == ["reports"]
        assert "Data source sales" not in run.out

    def test_two_narrowed_runs_merge_into_the_union_of_sources(self, world):
        first = world.run("--datasource", "sales", "--exclude-schemas", "Scratch,Sandbox", name="out")
        assert first.entities()["Customer"]["datasource"] == ["sales"]
        second = world.run("--datasource", "reports", "--exclude-schemas", "Scratch,Sandbox", name="out")
        entities = second.entities()
        assert entities["Customer"]["datasource"] == ["sales", "reports"]
        assert entities["sales.Trade"]["datasource"] == ["sales"] and entities["Summary"]["datasource"] == ["reports"]

    def test_an_unknown_source_is_refused_before_anything_happens(self, world):
        llm = RecordingLLM()
        run = world.run("--datasource", "nope", llm=llm)
        assert run.code == 2 and "unknown data source nope; configured: sales, reports" in run.out
        assert llm.calls == 0 and not run.directory.exists()

    def test_datasource_without_a_file_is_refused(self, world):
        world.datasources_path.unlink()
        reset_datasources_cache()
        run = world.run("--datasource", "sales")
        assert run.code == 2 and "there is none" in run.out

    def test_datasource_with_db_url_is_a_usage_error(self, world):
        with pytest.raises(SystemExit) as stopped:
            sp.main(["--db-url", world.sales.url, "--datasource", "sales", "--non-interactive"])
        assert stopped.value.code == 2

    def test_a_source_whose_variable_is_unset_is_reported(self, world, monkeypatch):
        monkeypatch.delenv("DB_URL_REPORTS")
        run = world.run()
        assert run.code == 2 and "data source 'reports' cannot be used" in run.out

    def test_the_fingerprint_includes_the_sources(self, world):
        both = world.run("--exclude-schemas", "Scratch,Sandbox,rep", name="a")
        only = world.run("--datasource", "sales", "--exclude-schemas", "Scratch,Sandbox,rep", name="b")
        assert (
            both.log()["step2_schema"]["schema_fingerprint"]
            != only.log()["step2_schema"]["schema_fingerprint"]
        )
        assert both.log()["step2_schema"]["sources"] == ["sales", "reports"]

    def test_resume_reuses_the_results_only_for_the_same_sources(self, world):
        out = "out"
        world.run("--exclude-schemas", "Scratch,Sandbox", name=out)
        (world.tmp_path / out / "entities.yaml").unlink()
        same = RecordingLLM()
        world.run("--exclude-schemas", "Scratch,Sandbox", "--resume", name=out, llm=same)
        assert same.calls == 0
        (world.tmp_path / out / "entities.yaml").unlink()
        other = RecordingLLM()
        world.run("--datasource", "sales", "--exclude-schemas", "Scratch,Sandbox", "--resume", name=out, llm=other)
        assert other.calls > 0


# ---------------------------------------------------------------------------
# Configured exclusion is applied, and never read
# ---------------------------------------------------------------------------

class TestConfiguredExclusion:
    def test_a_source_s_exclude_schemas_means_that_schema_is_never_read(self, world):
        world.datasources_path.write_text(
            DATASOURCES.replace("    description: Sales warehouse\n", "    exclude_schemas: [Scratch]\n"),
            encoding="utf-8",
        )
        reset_datasources_cache()
        run = world.run("--exclude-schemas", "Sandbox")
        assert run.code == 0
        assert "Scratch" not in schemas(run)
        assert "Tmp" not in " ".join(run.entities())
        assert "configured exclusion (EXCLUDE_SCHEMAS / exclude_schemas:): 1 table(s) in 1 schema(s)" in run.out
        assert "schemas excluded by configuration" in run.out

    def test_it_is_that_source_s_only(self, world):
        # `Draft` lives in `Sandbox` of the reports source only.
        world.datasources_path.write_text(
            DATASOURCES.replace("    url_env: DB_URL_REPORTS\n", "    url_env: DB_URL_REPORTS\n    exclude_schemas: [Scratch]\n"),
            encoding="utf-8",
        )
        reset_datasources_cache()
        run = world.run("--exclude-schemas", "Sandbox")
        assert ("Scratch", "Tmp") in run.read  # excluded for reports, not for sales

    def test_the_global_exclude_schemas_applies_to_every_source(self, world):
        with cfg.override_settings(exclude_schemas="rep,Sandbox,Scratch"):
            run = world.run()
        assert not schemas(run) & {"rep", "Sandbox", "Scratch"}
        assert sorted(run.entities()) == ["Customer", "ref.Trade", "sales.Trade"]
        assert "configured exclusion (EXCLUDE_SCHEMAS / exclude_schemas:): 3 table(s) in 3 schema(s)" in run.out

    def test_the_system_schemas_are_skipped_and_counted(self, world):
        run = world.run("--exclude-schemas", "Scratch,Sandbox")
        assert "cdc" not in schemas(run)
        assert "1 in system schemas" in run.out

    def test_the_cli_patterns_come_after_the_configured_ones(self, world):
        with cfg.override_settings(exclude_schemas="Scratch"):
            run = world.run("--exclude-schemas", "Scratch,Sandbox")
        # Attributed to the first rule that skips the schema.
        assert "configured exclusion (EXCLUDE_SCHEMAS / exclude_schemas:): 1 table(s)" in run.out
        assert "--exclude-schemas: 1 table(s)" in run.out


# ---------------------------------------------------------------------------
# Recording the interactive choice
# ---------------------------------------------------------------------------

LEAVE_OUT = "Schemas to leave out"


class TestRecordingTheChoice:
    def _backups(self, world) -> list[Path]:
        return sorted(world.project.glob("datasources.yaml.bak-*"))

    def test_yes_writes_each_source_its_own_patterns_with_a_backup_and_the_comments(self, world):
        run = world.run(answers={LEAVE_OUT: "Scratch, Sandbox"}, record=True)
        assert run.code == 0, run.out
        text = world.datasources_path.read_text(encoding="utf-8")
        config = validate_datasources_yaml_text(text)
        assert config.datasources["sales"].exclude_schemas == ["Scratch"]
        assert config.datasources["reports"].exclude_schemas == ["Sandbox"]
        assert "# Warehouses of this deployment" in text and "# the main one" in text
        assert "url_env: DB_URL_SALES   # set in .env" in text
        (backup,) = self._backups(world)
        assert backup.read_text(encoding="utf-8") == DATASOURCES
        assert "Backed up: datasources.yaml ->" in run.out and "Recorded exclude_schemas" in run.out
        assert run.log()["step2_schema"]["exclusion_recorded"]["recorded"] is True
        # The answer still decided what was read in this run.
        assert not schemas(run) & {"Scratch", "Sandbox"}

    def test_the_next_run_does_not_need_the_answer(self, world):
        world.run(answers={LEAVE_OUT: "Scratch, Sandbox"}, record=True)
        second = world.run(name="second")
        assert not schemas(second) & {"Scratch", "Sandbox"}
        assert "configured exclusion" in second.out

    def test_the_question_defaults_to_no_and_no_is_respected(self, world):
        asked: list[tuple[str, bool]] = []

        def yn(question, default=True, non_interactive=False):
            asked.append((question, default))
            return False

        run = world.run(answers={LEAVE_OUT: "Scratch"}, record=yn)
        assert any(default is False and "exclude_schemas" in q for q, default in asked)
        assert world.datasources_path.read_text(encoding="utf-8") == DATASOURCES
        assert not self._backups(world)
        assert 'exclude_schemas: ["Scratch"]' in run.out  # the snippet to add by hand

    def test_a_pattern_that_matched_nothing_is_not_recorded(self, world):
        run = world.run(answers={LEAVE_OUT: "Nothing_Like_It"}, record=True)
        assert world.datasources_path.read_text(encoding="utf-8") == DATASOURCES
        assert "You left these schemas out" not in run.out

    def test_a_layout_the_edit_cannot_handle_leaves_the_file_untouched_and_prints_the_snippet(self, world):
        inline = "default: sales\ndatasources:\n  sales: {url_env: DB_URL_SALES}\n  reports: {url_env: DB_URL_REPORTS}\n"
        world.datasources_path.write_text(inline, encoding="utf-8")
        reset_datasources_cache()
        run = world.run(answers={LEAVE_OUT: "Scratch"}, record=True)
        assert run.code == 0
        assert world.datasources_path.read_text(encoding="utf-8") == inline
        assert not self._backups(world)
        assert "Not recorded:" in run.out and "was not changed. Add this by hand" in run.out
        assert 'exclude_schemas: ["Scratch"]' in run.out

    def test_an_invalid_result_is_never_written(self, world):
        # The edit is made, then rejected by the validation: nothing is replaced.
        with patch("database.datasources_edit.validate_datasources_yaml_text", side_effect=[
            validate_datasources_yaml_text(DATASOURCES), ValueError("rejected"),
        ]):
            run = world.run(answers={LEAVE_OUT: "Scratch"}, record=True)
        assert world.datasources_path.read_text(encoding="utf-8") == DATASOURCES
        assert not self._backups(world) and "the edited file would not be valid" in run.out

    def test_non_interactive_never_asks_or_writes(self, world):
        with patch.object(sp, "_yn", side_effect=AssertionError("asked")):
            run = world.run("--exclude-schemas", "Scratch")
        assert run.code == 0 and world.datasources_path.read_text(encoding="utf-8") == DATASOURCES

    def test_a_dry_run_shows_the_lines_and_writes_nothing(self, world):
        run = world.run("--dry-run", answers={LEAVE_OUT: "Scratch"}, record=True)
        assert world.datasources_path.read_text(encoding="utf-8") == DATASOURCES
        assert "Dry run: datasources.yaml is not changed" in run.out and 'exclude_schemas: ["Scratch"]' in run.out


class TestWithoutDatasourcesYaml:
    def test_the_env_line_is_printed_and_env_is_not_touched(self, world, monkeypatch, tmp_path):
        world.datasources_path.unlink()
        reset_datasources_cache()
        monkeypatch.setenv("DB_CONNECTION_URL", world.sales.url)
        monkeypatch.chdir(tmp_path)
        env = tmp_path / ".env"
        env.write_text("EXISTING=1\n", encoding="utf-8")
        with cfg.override_settings(exclude_schemas="Alerts"):
            run = world.run(answers={LEAVE_OUT: "Scratch"}, record=True)
        assert run.code == 0, run.out
        assert "EXCLUDE_SCHEMAS=Alerts,Scratch" in run.out
        assert env.read_text(encoding="utf-8") == "EXISTING=1\n"
        assert not list(world.project.glob("*.bak-*")) and not world.datasources_path.exists()
        assert not schemas(run) & {"Scratch"}

    def test_db_url_still_reads_one_database_and_prints_the_env_line(self, world):
        run = world.run("--db-url", world.sales.url, answers={LEAVE_OUT: "Scratch"}, record=True)
        assert run.code == 0, run.out
        assert "EXCLUDE_SCHEMAS=Scratch" in run.out
        # One database: no data source names, no datasource: lines, and nothing written to datasources.yaml.
        assert all("datasource" not in entry for entry in run.entities().values())
        assert world.datasources_path.read_text(encoding="utf-8") == DATASOURCES

    def test_db_url_ignores_the_exclusion_of_the_configured_sources(self, world):
        world.datasources_path.write_text(
            DATASOURCES.replace("    description: Sales warehouse\n", "    exclude_schemas: [Scratch]\n"),
            encoding="utf-8",
        )
        reset_datasources_cache()
        run = world.run("--db-url", world.sales.url)
        assert run.code == 0
        assert ("Scratch", "Tmp") in run.read  # a URL is not one of the configured sources
        assert sorted(run.entities()) == ["Customer", "Tmp", "ref.Trade", "sales.Trade"]
