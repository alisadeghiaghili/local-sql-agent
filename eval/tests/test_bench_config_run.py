# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Per-database configurations, the runner and the ``bench-*`` commands of ``eval.cli``.

The schema inspector runs for real here, against the SQLite stand-in for SQL
Server (``ATTACH``ed schemas), through a patched ``database.connection.get_engine``
exactly as ``tests/test_schema_inspector_sources.py`` does.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_bench_config_run.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
import yaml

from eval import cli
from eval.benchmarks import bird_run
from eval.benchmarks.bird_config import (
    BASE_COPIED_FILES,
    NEUTRAL_FILES,
    apply_descriptions,
    default_base_config,
    draft_config,
    inspector_runner,
)
from eval.benchmarks.bird_import import ImportOptions, run_import
from eval.benchmarks.bird_run import RunPlan, merge_reports, plan_runs, run_plans
from eval.models import CaseResult
from eval.report import build_report, render_text, save_json_report
from tests._inspector_fixtures import attached

from ._bench_fixtures import build_package, build_target

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def world(tmp_path: Path):
    package = build_package(tmp_path / "download")
    database = build_target(tmp_path)
    with attached(database):
        engine = sa.create_engine(database.url)
        try:
            yield package, engine, database
        finally:
            engine.dispose()


def _run_import_with_inspector(package, out, engine, **overrides):
    """An import whose configurations come from the real inspector over *engine*."""
    with patch("database.connection.get_engine", lambda source=None: engine):
        return run_import(
            ImportOptions(source=package, out=out, max_rows=100, **overrides),
            engine,
            inspect=inspector_runner(None),
            progress=lambda message: None,
        )


class TestConfigurations:
    def test_a_config_per_database_and_one_combined(self, world, tmp_path):
        package, engine, _ = world
        report = _run_import_with_inspector(package, tmp_path / "out", engine)
        assert sorted(report.configs) == ["_combined", "library", "shop"]
        assert all(c["inspector_exit"] == 0 for c in report.configs.values())
        shop = yaml.safe_load((tmp_path / "out" / "shop" / "project_config" / "schema.yaml").read_text(encoding="utf-8"))
        assert sorted(shop["tables"]) == ["Customer", "Review", "[Order Item]"]
        assert {t["db_schema"] for t in shop["tables"].values()} == {"shop"}
        combined = yaml.safe_load(
            (tmp_path / "out" / "_combined" / "project_config" / "schema.yaml").read_text(encoding="utf-8")
        )
        assert {t["db_schema"] for t in combined["tables"].values()} == {"shop", "library"}

    def test_the_inspector_is_restricted_to_the_database_of_the_config(self, world, tmp_path):
        package, engine, _ = world
        _run_import_with_inspector(package, tmp_path / "out", engine)
        library = yaml.safe_load((tmp_path / "out" / "library" / "project_config" / "schema.yaml").read_text(encoding="utf-8"))
        assert sorted(library["tables"]) == ["Author", "Book"]

    def test_the_directory_is_complete_and_the_domain_files_are_empty(self, world, tmp_path):
        package, engine, _ = world
        _run_import_with_inspector(package, tmp_path / "out", engine)
        config = tmp_path / "out" / "shop" / "project_config"
        for name in (*BASE_COPIED_FILES, *NEUTRAL_FILES, "schema.yaml", "entities.yaml", "aliases.yaml"):
            assert (config / name).is_file(), name
        assert yaml.safe_load((config / "examples.yaml").read_text(encoding="utf-8")) == {"examples": []}
        assert yaml.safe_load((config / "business_rules.yaml").read_text(encoding="utf-8")) == {"rules": {}}
        assert (config / "system_prompt.md").read_text(encoding="utf-8") == (
            default_base_config() / "system_prompt.md"
        ).read_text(encoding="utf-8")

    def test_the_drafted_directory_is_a_usable_project_config(self, world, tmp_path):
        package, engine, _ = world
        _run_import_with_inspector(package, tmp_path / "out", engine)
        for name in ("shop", "library", "_combined"):
            config = tmp_path / "out" / name / "project_config"
            code = (
                "from core.config_preflight import SERVER_IMPORT_FILES, project_config_problem;"
                "import sys; problem = project_config_problem(SERVER_IMPORT_FILES);"
                "print(problem); sys.exit(1 if problem else 0)"
            )
            done = subprocess.run(
                [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
                env={**os.environ, "PROJECT_CONFIG_DIR": str(config), "PYTHONPATH": str(REPO_ROOT)},
            )
            assert done.returncode == 0, (name, done.stdout, done.stderr[-400:])

    def test_without_the_flag_descriptions_are_the_raw_draft(self, world, tmp_path):
        package, engine, _ = world
        _run_import_with_inspector(package, tmp_path / "out", engine)
        text = (tmp_path / "out" / "shop" / "project_config" / "schema.yaml").read_text(encoding="utf-8")
        assert "city of residence" not in text

    def test_descriptions_go_into_schema_yaml_when_asked(self, world, tmp_path):
        package, engine, _ = world
        report = _run_import_with_inspector(package, tmp_path / "out", engine, descriptions=True)
        shop = yaml.safe_load((tmp_path / "out" / "shop" / "project_config" / "schema.yaml").read_text(encoding="utf-8"))
        columns = shop["tables"]["Customer"]["columns"]
        assert columns["City"].endswith("-- city of residence (text). e.g. Paris")
        assert columns["ID"].endswith("-- customer identifier (integer)")
        assert "--" not in columns["Joined"]  # the CSV does not describe it
        assert report.configs["shop"]["described_columns"] == 3
        assert report.configs["library"]["described_columns"] == 0
        combined = yaml.safe_load(
            (tmp_path / "out" / "_combined" / "project_config" / "schema.yaml").read_text(encoding="utf-8")
        )
        assert "city of residence" in combined["tables"]["Customer"]["columns"]["City"]

    def test_a_failing_inspector_leaves_the_directory_incomplete_and_says_so(self, tmp_path):
        result = draft_config(
            tmp_path / "cfg", ["x"], inspect=lambda schemas, out: 1, base_config=default_base_config()
        )
        assert result["inspector_exit"] == 1 and "completed_with" not in result
        assert not (tmp_path / "cfg" / "examples.yaml").exists()

    def test_apply_descriptions_matches_qualified_keys(self):
        text = "tables:\n  s1.T:\n    columns:\n      A: x\n  s2.T:\n    columns:\n      A: y\n"
        new, count = apply_descriptions(text, {"s1": {"t": {"a": "first"}}, "s2": {"t": {"a": "second"}}})
        tables = yaml.safe_load(new)["tables"]
        assert (count, tables["s1.T"]["columns"]["A"], tables["s2.T"]["columns"]["A"]) == (
            2, "x -- first", "y -- second",
        )


def _report_file(path: Path, tags_by_case: dict[str, tuple[str, bool]], *, pipeline="production") -> Path:
    results = [
        CaseResult(cid, "q", ["bench:bird", f"db:{db}", "difficulty:simple"], "pass" if ok else "fingerprint_mismatch",
                   "SELECT 1", "f", None, 0.1)
        for cid, (db, ok) in tags_by_case.items()
    ]
    save_json_report(build_report(results, mode="live", reference="stored", pipeline=pipeline), path)
    return path


class TestRunPlanAndMerge:
    def _bench(self, tmp_path):
        for db in ("shop", "library"):
            (tmp_path / db / "project_config").mkdir(parents=True)
            (tmp_path / db / "golden.jsonl").write_text("{}\n", encoding="utf-8")
            (tmp_path / db / "golden.with_evidence.jsonl").write_text("{}\n", encoding="utf-8")
        (tmp_path / "_combined" / "project_config").mkdir(parents=True)
        (tmp_path / "golden.jsonl").write_text("{}\n", encoding="utf-8")
        return tmp_path

    def test_one_run_per_database_each_with_its_own_config(self, tmp_path):
        plans = plan_runs(self._bench(tmp_path), mode="per_db")
        assert [(p.label, p.config.parent.name) for p in plans] == [("library", "library"), ("shop", "shop")]
        assert plans[0].golden.name == "golden.jsonl" and "per_db" in plans[0].report.parts[-2]
        evidence = plan_runs(tmp_path, mode="per_db", evidence=True)
        assert evidence[0].golden.name == "golden.with_evidence.jsonl"
        assert evidence[0].report.parts[-2] == "per_db-evidence"

    def test_the_combined_mode_is_a_single_run_over_the_combined_config(self, tmp_path):
        (plan,) = plan_runs(self._bench(tmp_path), mode="combined")
        assert plan.label == "_combined" and plan.config.parent.name == "_combined"
        assert plan.golden == tmp_path / "golden.jsonl"
        assert plan.report.parts[-2] == "combined"

    def test_a_label_gives_each_model_its_own_reports(self, tmp_path):
        bench = self._bench(tmp_path)
        a = plan_runs(bench, mode="per_db", label="model-a")
        b = plan_runs(bench, mode="per_db", label="model-b")
        assert a[0].report.parent.name == "model-a" and b[0].report.parent.name == "model-b"
        for bad in ("", "../x", "a/b", ".hidden"):
            with pytest.raises(ValueError, match="unusable run label"):
                plan_runs(bench, mode="per_db", label=bad)

    def test_a_missing_import_is_reported(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            plan_runs(tmp_path, mode="per_db")

    def test_each_run_is_its_own_process_with_project_config_dir_set(self, tmp_path):
        plan = RunPlan("shop", tmp_path / "g.jsonl", tmp_path / "cfg", tmp_path / "runs" / "shop.report.json")
        seen = {}

        def fake_run(command, env, check):
            seen.update(command=command, env=env)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(bird_run.subprocess, "run", fake_run):
            bird_run.run_subprocess(plan, ["--single-shot"])
        command = seen["command"]
        assert command[1:4] == ["-m", "eval.cli", "run"]
        assert command[command.index("--reference") + 1] == "stored" and "--live" in command
        assert command[command.index("--golden") + 1] == str(plan.golden)
        assert command[-1] == "--single-shot"
        assert seen["env"]["PROJECT_CONFIG_DIR"] == str(plan.config)

    def test_a_run_without_a_report_is_reported_as_failed(self, tmp_path):
        good = RunPlan("a", tmp_path / "g", tmp_path / "c", tmp_path / "a.json")
        bad = RunPlan("b", tmp_path / "g", tmp_path / "c", tmp_path / "b.json")

        def run(plan):
            if plan is good:
                _report_file(plan.report, {"x": ("a", True)})
            return 0

        assert run_plans([good, bad], run, progress=lambda m: None) == [bad]

    def test_merge_pools_the_cases_and_carries_the_header(self, tmp_path):
        a = _report_file(tmp_path / "a.json", {"bird_1": ("shop", True), "bird_2": ("shop", False)})
        b = _report_file(tmp_path / "b.json", {"bird_6": ("library", True)})
        merged = merge_reports(
            [a, b], mode="per_db", evidence=False,
            import_report={"options": {"descriptions": True}, "totals": {"excluded_by_reason": {"load_fidelity": 2}}},
        )
        assert (merged.total, merged.passed) == (3, 2)
        assert merged.tag_accuracy["db:shop"] == (1, 2) and merged.tag_accuracy["db:library"] == (1, 1)
        assert merged.benchmark["mode"] == "per_db" and merged.benchmark["excluded"] == {"load_fidelity": 2}
        text = render_text(merged)
        assert text.startswith("BENCHMARK RUN (bird)")
        assert "NOT this deployment's accuracy" in text
        assert "Mode: per-database" in text and "Evidence: none" in text
        assert "column descriptions were added" in text

    def test_a_combined_report_says_it_is_a_separate_measurement(self, tmp_path):
        a = _report_file(tmp_path / "a.json", {"bird_1": ("shop", True)})
        text = render_text(merge_reports([a], mode="combined", evidence=True))
        assert "Mode: COMBINED" in text and "do not compare it with the per-database figure" in text
        assert "annotators' hint is appended" in text

    def test_merge_refuses_a_case_in_two_reports_and_mixed_pipelines(self, tmp_path):
        a = _report_file(tmp_path / "a.json", {"bird_1": ("shop", True)})
        b = _report_file(tmp_path / "b.json", {"bird_1": ("shop", True)})
        with pytest.raises(ValueError, match="more than one report"):
            merge_reports([a, b], mode="per_db", evidence=False)
        c = _report_file(tmp_path / "c.json", {"bird_9": ("shop", True)}, pipeline="single_shot")
        with pytest.raises(ValueError, match="different pipelines"):
            merge_reports([a, c], mode="per_db", evidence=False)

    def test_the_merged_report_compares_with_another_round(self, tmp_path):
        from eval.rounds import compare_runs, load_run

        a = _report_file(tmp_path / "a.json", {"bird_1": ("shop", True), "bird_2": ("shop", False)})
        b = _report_file(tmp_path / "b.json", {"bird_1": ("shop", True), "bird_2": ("shop", True)})
        first = bird_run.write_merged(merge_reports([a], mode="per_db", evidence=False), tmp_path / "r1")[0]
        second = bird_run.write_merged(merge_reports([b], mode="per_db", evidence=True), tmp_path / "r2")[0]
        comparison = compare_runs(load_run(first), load_run(second))
        assert comparison.fixed == ["bird_2"] and comparison.paired["fixed"] == 1


class TestCommands:
    def _url(self, database) -> str:
        return database.url

    def test_bench_import_writes_everything_and_prints_the_report(self, world, tmp_path, capsys):
        package, engine, database = world
        out = tmp_path / "cli_out"
        code = cli.main([
            "bench-import", "bird", "--source", str(package), "--target-url", self._url(database),
            "--out", str(out), "--skip-config", "--max-rows", "100", "--with-evidence",
        ])
        assert code == 0
        printed = capsys.readouterr().out
        assert "BENCHMARK IMPORT (bird)" in printed and "bird_5" in printed
        assert (out / "golden.with_evidence.jsonl").is_file()

    def test_a_second_import_exits_2_with_the_reason(self, world, tmp_path, capsys):
        package, _, database = world
        args = ["bench-import", "bird", "--source", str(package), "--target-url", self._url(database),
                "--skip-config", "--max-rows", "100"]
        assert cli.main([*args, "--out", str(tmp_path / "a")]) == 0
        capsys.readouterr()
        assert cli.main([*args, "--out", str(tmp_path / "b")]) == 2
        assert "--replace" in capsys.readouterr().err
        assert not (tmp_path / "b" / "golden.jsonl").exists()
        assert cli.main([*args, "--out", str(tmp_path / "c"), "--replace"]) == 0

    def test_the_target_url_can_come_from_an_environment_variable(self, world, tmp_path, monkeypatch, capsys):
        package, _, database = world
        monkeypatch.setenv("BIRD_TEST_ADMIN_URL", database.url)
        args = ["bench-import", "bird", "--source", str(package), "--skip-config", "--max-rows", "100",
                "--out", str(tmp_path / "o")]
        assert cli.main([*args, "--target-url", "env:BIRD_TEST_ADMIN_URL"]) == 0
        capsys.readouterr()
        monkeypatch.delenv("BIRD_TEST_ADMIN_URL")
        assert cli.main([*args, "--target-url", "env:BIRD_TEST_ADMIN_URL"]) == 2
        assert "unset variable" in capsys.readouterr().err

    def test_a_target_is_required(self, world, tmp_path, capsys):
        package, _, _ = world
        assert cli.main(["bench-import", "bird", "--source", str(package), "--out", str(tmp_path / "o")]) == 2
        assert "--target-url" in capsys.readouterr().err

    def test_an_unknown_database_exits_2(self, world, tmp_path, capsys):
        package, _, database = world
        code = cli.main([
            "bench-import", "bird", "--source", str(package), "--target-url", self._url(database),
            "--out", str(tmp_path / "o"), "--dbs", "nope", "--skip-config",
        ])
        assert code == 2 and "nope" in capsys.readouterr().err

    def test_a_row_count_mismatch_exits_1(self, world, tmp_path, capsys):
        package, engine, database = world
        args = ["bench-import", "bird", "--source", str(package), "--target-url", self._url(database),
                "--skip-config", "--max-rows", "100"]
        assert cli.main([*args, "--out", str(tmp_path / "a")]) == 0
        with engine.begin() as conn:
            conn.exec_driver_sql('DELETE FROM "library"."Book"')
        assert cli.main([*args, "--out", str(tmp_path / "b"), "--skip-load"]) == 1
        assert "library.Book" in capsys.readouterr().err

    def test_bench_run_merges_the_reports_that_exist(self, tmp_path, capsys):
        for db in ("shop", "library"):
            (tmp_path / db / "project_config").mkdir(parents=True)
            (tmp_path / db / "golden.jsonl").write_text("{}\n", encoding="utf-8")
        runs = tmp_path / "runs" / "per_db"
        _report_file(runs / "shop.report.json", {"bird_1": ("shop", True)})
        _report_file(runs / "library.report.json", {"bird_6": ("library", False)})
        assert cli.main(["bench-run", "bird", "--bench", str(tmp_path), "--merge-only"]) == 0
        printed = capsys.readouterr().out
        assert printed.startswith("BENCHMARK RUN (bird)") and "Execution accuracy: 50.00% (1/2)" in printed
        merged = json.loads((runs / "report.json").read_text(encoding="utf-8"))
        assert merged["benchmark"]["mode"] == "per_db" and merged["total"] == 2

    def test_bench_run_exits_1_when_a_database_has_no_report(self, tmp_path):
        for db in ("shop", "library"):
            (tmp_path / db / "project_config").mkdir(parents=True)
            (tmp_path / db / "golden.jsonl").write_text("{}\n", encoding="utf-8")
        _report_file(tmp_path / "runs" / "per_db" / "shop.report.json", {"bird_1": ("shop", True)})
        assert cli.main(["bench-run", "bird", "--bench", str(tmp_path), "--merge-only"]) == 1

    def test_bench_run_passes_extra_arguments_to_every_run(self, tmp_path):
        (tmp_path / "shop" / "project_config").mkdir(parents=True)
        (tmp_path / "shop" / "golden.jsonl").write_text("{}\n", encoding="utf-8")
        commands = []

        def fake(plan, extra):
            commands.append(list(extra))
            _report_file(plan.report, {"bird_1": ("shop", True)})
            return 0

        with patch.object(bird_run, "run_subprocess", fake):
            code = cli.main(["bench-run", "bird", "--bench", str(tmp_path), "--", "--single-shot"])
        assert commands == [["--single-shot"]] and code == 0

    def test_help_needs_no_project_config(self):
        with pytest.raises(SystemExit) as stopped:
            cli.main(["bench-import", "bird", "--help"])
        assert stopped.value.code == 0
