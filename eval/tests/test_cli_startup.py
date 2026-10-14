# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``python -m eval.cli`` start-up: no traceback, whatever ``project_config/`` holds.

``eval.cli`` used to import the runner at module scope, which imports the
prompt builder, which binds ``knowledge.business_rules`` -- so even
``--help`` ended in an import-time traceback on a machine with no (or a
broken) ``project_config/``. Now ``--help``, ``split`` and ``compare`` need
none, and ``run``, ``verify`` and ``recall`` print one ``cannot load
project_config: ...`` line and exit with status 2, like
``scripts/prompt_budget.py``.

Import-time behaviour cannot be observed in-process once the modules are
cached, so every case runs a fresh interpreter against a temporary
``PROJECT_CONFIG_DIR``.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_cli_startup.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = REPO_ROOT / "project_config.example"


def _run(args: list[str], config_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PROJECT_CONFIG_DIR": str(config_dir), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )


def _cli(*args: str, config_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run(["-m", "eval.cli", *args], config_dir)


@pytest.fixture
def golden(tmp_path) -> Path:
    path = tmp_path / "golden.jsonl"
    path.write_text(
        json.dumps({"id": "a", "question": "how many?", "tags": ["count"],
                    "expected_sql": "SELECT 1"}) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def broken_config(tmp_path) -> Path:
    directory = tmp_path / "project_config"
    shutil.copytree(EXAMPLE_DIR, directory)
    (directory / "schema.yaml").write_text("tables: [not, a, mapping]\n", encoding="utf-8")
    return directory


@pytest.fixture
def empty_config(tmp_path) -> Path:
    directory = tmp_path / "no_config_here"
    directory.mkdir()
    return directory


class TestHelpNeedsNoConfig:
    @pytest.mark.parametrize("config", ["empty_config", "broken_config"])
    def test_help_works_and_prints_no_traceback(self, config, request):
        directory = request.getfixturevalue(config)
        done = _cli("--help", config_dir=directory)
        assert done.returncode == 0, done.stderr
        assert "Traceback" not in done.stderr and done.stderr == ""
        for command in ("run", "verify", "recall", "split", "compare"):
            assert command in done.stdout

    def test_help_of_every_subcommand_works_without_a_config(self, empty_config):
        for command in ("run", "verify", "recall", "split", "compare"):
            done = _cli(command, "--help", config_dir=empty_config)
            assert done.returncode == 0, (command, done.stderr)
            assert "Traceback" not in done.stderr
        assert "--share" in _cli("run", "--help", config_dir=empty_config).stdout

    def test_a_directory_that_does_not_exist_is_fine_for_help(self, tmp_path):
        done = _cli("--help", config_dir=tmp_path / "missing")
        assert done.returncode == 0 and "Traceback" not in done.stderr

    def test_importing_the_module_reads_no_project_config(self, empty_config):
        code = (
            "import sys; import eval.cli; "
            "bad = [m for m in ('eval.runner', 'security.sql_guard', 'prompt_engine.builder', "
            "'knowledge.business_rules') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)"
        )
        done = _run(["-c", code], empty_config)
        assert done.returncode == 0, done.stdout + done.stderr

    def test_usage_errors_are_argparse_usage_errors_not_tracebacks(self, empty_config):
        done = _cli("run", config_dir=empty_config)  # --golden missing
        assert done.returncode == 2
        assert "Traceback" not in done.stderr and "usage:" in done.stderr


class TestCommandsThatNeedTheConfigFailInOneLine:
    @pytest.mark.parametrize("command", ["run", "verify", "recall"])
    @pytest.mark.parametrize("config", ["empty_config", "broken_config"])
    def test_one_line_and_exit_code_two(self, command, config, golden, request):
        directory = request.getfixturevalue(config)
        done = _cli(command, "--golden", str(golden), config_dir=directory)
        assert done.returncode == 2
        assert "Traceback" not in done.stderr
        lines = done.stderr.strip().splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("cannot load project_config: ")
        assert done.stdout == ""

    def test_the_line_names_the_broken_file(self, broken_config, golden):
        done = _cli("run", "--golden", str(golden), config_dir=broken_config)
        assert "[schema.yaml] validation error at 'tables'" in done.stderr

    def test_live_with_share_fails_the_same_way_before_anything_runs(self, broken_config, golden, tmp_path):
        done = _cli("run", "--golden", str(golden), "--live", "--reference", "live",
                    "--share", str(tmp_path / "s.json"), config_dir=broken_config)
        assert done.returncode == 2 and "cannot load project_config" in done.stderr
        assert not (tmp_path / "s.json").exists()


class TestCommandsThatNeedNoConfig:
    def test_split_and_compare_work_with_no_config_at_all(self, empty_config, golden, tmp_path):
        out = tmp_path / "split"
        done = _cli("split", "--golden", str(golden), "--out", str(out), config_dir=empty_config)
        assert done.returncode == 0, done.stderr
        assert (out / "dev_ids.txt").read_text(encoding="utf-8") == "a\n"
        bundle = tmp_path / "b.json"
        bundle.write_text(json.dumps({"format_version": 1, "cases": [
            {"case_id": "a", "tags": [], "passed": True, "status": "pass"}]}), encoding="utf-8")
        done = _cli("compare", str(bundle), str(bundle), config_dir=empty_config)
        assert done.returncode == 0, done.stderr
        assert "Traceback" not in done.stderr and "Fixed (0)" in done.stdout


class TestAValidConfigStillRuns:
    def test_the_offline_smoke_run_of_ci_still_works(self):
        done = _cli("run", "--golden", "eval_data.example/golden.jsonl", config_dir=EXAMPLE_DIR)
        assert done.returncode == 0, done.stderr
        assert "Execution accuracy: 100.00%" in done.stdout
        assert "cannot load project_config" not in done.stderr
