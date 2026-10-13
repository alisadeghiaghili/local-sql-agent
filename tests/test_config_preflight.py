# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A broken ``project_config/`` ends start-up in one clear line, not a traceback.

``security.sql_guard`` binds ``schema_data.columns.TABLE_COLUMNS`` at import
time, so ``import api.server`` (what ``uvicorn api.server:app`` and
``python -m api`` do) and ``scripts/prompt_budget.py`` used to die with a
forty-line traceback when ``schema.yaml`` was bad. Each now prints what is
wrong, naming the file, and exits non-zero.

Import-time behaviour cannot be observed in-process once the modules are
cached, so these tests run fresh interpreters against a temporary
``PROJECT_CONFIG_DIR`` that holds a copy of ``project_config.example/`` with
one file broken.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from config import override_settings
from core.config_preflight import (
    PROMPT_BUDGET_IMPORT_FILES,
    SERVER_IMPORT_FILES,
    exit_if_project_config_unusable,
    project_config_problem,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_DIR = _REPO_ROOT / "project_config.example"

_DUPLICATE_SCHEMA = (
    "tables:\n"
    "  Customer:\n"
    "    columns: {ID: key}\n"
    "  Customer:\n"
    "    columns: {ID: key}\n"
    "  Order:\n"
    "    columns: {ID: key}\n"
    "  Order:\n"
    "    columns: {ID: key}\n"
    "relationships: []\n"
)


def _run(args: list[str], config_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PROJECT_CONFIG_DIR": str(config_dir)}
    return subprocess.run(
        [sys.executable, *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


@pytest.fixture
def broken_config(tmp_path: Path) -> Path:
    """A copy of the example config whose ``schema.yaml`` repeats keys."""
    directory = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE_DIR, directory)
    (directory / "schema.yaml").write_text(_DUPLICATE_SCHEMA, encoding="utf-8")
    return directory


class TestServerImport:
    def test_duplicate_keys_end_the_import_with_three_lines_and_no_traceback(self, broken_config):
        done = _run(["-c", "import api.server"], broken_config)

        assert done.returncode == 2
        assert "Traceback" not in done.stderr
        lines = done.stderr.strip().splitlines()
        assert lines == [
            "cannot load project_config: [schema.yaml] 2 duplicate keys: "
            "'Customer' (lines 2, 4); 'Order' (lines 6, 8)",
            "run python scripts/verify_deployment.py to check every project_config file",
            "nothing was started",
        ]

    def test_a_missing_file_names_it(self, tmp_path):
        directory = tmp_path / "project_config"
        shutil.copytree(_EXAMPLE_DIR, directory)
        (directory / "metrics.yaml").unlink()

        done = _run(["-c", "import api.server"], directory)

        assert done.returncode != 0
        assert "Traceback" not in done.stderr
        assert "metrics.yaml" in done.stderr
        assert "not found" in done.stderr

    def test_an_empty_directory_is_one_line_about_the_first_missing_file(self, tmp_path):
        done = _run(["-c", "import api.server"], tmp_path)

        assert done.returncode != 0
        assert "Traceback" not in done.stderr
        assert done.stderr.startswith("cannot load project_config: ")

    def test_an_invalid_file_that_parses_is_reported_too(self, tmp_path):
        directory = tmp_path / "project_config"
        shutil.copytree(_EXAMPLE_DIR, directory)
        (directory / "schema.yaml").write_text("tables: [not, a, mapping]\n", encoding="utf-8")

        done = _run(["-c", "import api.server"], directory)

        assert done.returncode != 0
        assert "Traceback" not in done.stderr
        assert "[schema.yaml] validation error at 'tables'" in done.stderr

    def test_a_valid_config_imports_cleanly_and_prints_nothing_extra(self):
        done = _run(["-c", "import api.server; print('ok')"], _EXAMPLE_DIR)

        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "ok"
        assert "cannot load project_config" not in done.stderr

    def test_python_dash_m_api_stops_the_same_way(self, broken_config):
        done = _run(["-m", "api"], broken_config)

        assert done.returncode == 2
        assert "Traceback" not in done.stderr
        assert "[schema.yaml] 2 duplicate keys" in done.stderr
        assert "nothing was started" in done.stderr


class TestPromptBudgetScript:
    def test_a_broken_schema_is_one_line_and_exit_code_two(self, broken_config):
        done = _run(["scripts/prompt_budget.py"], broken_config)

        assert done.returncode == 2
        assert "Traceback" not in done.stderr
        assert done.stderr.strip().splitlines() == [
            "cannot load project_config: [schema.yaml] 2 duplicate keys: "
            "'Customer' (lines 2, 4); 'Order' (lines 6, 8)",
        ]

    def test_a_missing_file_is_one_line_and_exit_code_two(self, tmp_path):
        done = _run(["scripts/prompt_budget.py"], tmp_path)

        assert done.returncode == 2
        assert "Traceback" not in done.stderr
        assert done.stderr.startswith("cannot load project_config: ")
        assert len(done.stderr.strip().splitlines()) == 1

    def test_importing_the_module_is_unchanged(self, broken_config):
        # sync_schema.py imports it inside `except Exception`, which a
        # SystemExit would slip past; the loaders' ValueError must still
        # reach an importer.
        done = _run(["-c", "import scripts.prompt_budget"], broken_config)

        assert done.returncode == 1
        assert "ValueError: [schema.yaml] 2 duplicate keys" in done.stderr

    def test_a_valid_config_still_runs(self):
        done = _run(["scripts/prompt_budget.py", "--help"], _EXAMPLE_DIR)

        assert done.returncode == 0, done.stderr


class TestHelper:
    def test_returns_none_for_a_valid_config(self):
        assert project_config_problem(SERVER_IMPORT_FILES) is None

    def test_exits_silently_for_a_valid_config(self, capsys):
        exit_if_project_config_unusable(SERVER_IMPORT_FILES)

        captured = capsys.readouterr()
        assert captured.out == captured.err == ""

    def test_reports_only_the_first_problem_in_the_order_given(self, tmp_path):
        with override_settings(project_config_dir=str(tmp_path)):
            message = project_config_problem(["metrics.yaml", "schema.yaml"])

        assert message is not None
        assert "metrics.yaml" in message
        assert "schema.yaml" not in message

    def test_message_is_a_single_line_naming_the_file(self, tmp_path):
        (tmp_path / "schema.yaml").write_text("a: 1\n\n\na: 2\n", encoding="utf-8")
        with override_settings(project_config_dir=str(tmp_path)):
            message = project_config_problem(["schema.yaml"])

        assert message == "[schema.yaml] duplicate key 'a' (first on line 1, again on line 4)"

    def test_exit_prints_hints_after_the_message(self, tmp_path, capsys):
        with override_settings(project_config_dir=str(tmp_path)):
            with pytest.raises(SystemExit) as excinfo:
                exit_if_project_config_unusable(["schema.yaml"], hints=["second", "third"], status=5)

        assert excinfo.value.code == 5
        lines = capsys.readouterr().err.splitlines()
        assert lines[0].startswith("cannot load project_config: ")
        assert lines[1:] == ["second", "third"]


_TRACE = """
import sys
sys.path.insert(0, ".")
import core.yaml_loading as y
original = y.safe_load_strict
def traced(stream):
    print("READ", Path(stream.name).name)
    return original(stream)
from pathlib import Path
y.safe_load_strict = traced
import importlib
importlib.import_module(sys.argv[1])
"""


class TestFileListsMatchTheImportChain:
    """The lists must name what the import really reads, no more, no fewer."""

    @staticmethod
    def _files_read_by_importing(module: str) -> set[str]:
        done = _run(["-c", _TRACE, module], _EXAMPLE_DIR)
        assert done.returncode == 0, done.stderr
        return {line.split()[1] for line in done.stdout.splitlines() if line.startswith("READ ")}

    def test_server(self):
        assert self._files_read_by_importing("api.server") == set(SERVER_IMPORT_FILES)

    def test_prompt_budget(self):
        assert self._files_read_by_importing("scripts.prompt_budget") == set(
            PROMPT_BUDGET_IMPORT_FILES
        )
