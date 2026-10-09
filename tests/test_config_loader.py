# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for knowledge/config_loader.py's system-prompt helpers.

``resolve_system_prompt_path`` / ``load_system_prompt`` are the shared
loader every system-prompt call site (``api/server.py``, ``app.py``,
``eval/cli.py``, ``webapp/agent.py``) is built on top of -- see
``project_config.example/README.md`` and ``docs/deployment-runbook.md``
for the deployment-facing side of this contract.
"""

from __future__ import annotations


import pytest

from config import override_settings
from knowledge.config_loader import (
    ConfigNotFoundError,
    load_system_prompt,
    resolve_system_prompt_path,
)


class TestResolveSystemPromptPath:
    def test_path_is_project_config_dir_slash_system_prompt_md(self, tmp_path):
        with override_settings(project_config_dir=str(tmp_path)):
            assert resolve_system_prompt_path() == tmp_path / "system_prompt.md"

    def test_does_no_io(self, tmp_path):
        """Resolving the path must not require the file (or even the
        directory) to exist -- callers build their own error message from
        it before checking existence."""
        missing_dir = tmp_path / "does-not-exist"
        with override_settings(project_config_dir=str(missing_dir)):
            path = resolve_system_prompt_path()
        assert path == missing_dir / "system_prompt.md"

    def test_relative_project_config_dir_resolves_against_repo_root(self):
        with override_settings(project_config_dir="project_config.example"):
            path = resolve_system_prompt_path()
        assert path.is_absolute()
        assert path.name == "system_prompt.md"


class TestLoadSystemPrompt:
    def test_reads_file_contents(self, tmp_path):
        (tmp_path / "system_prompt.md").write_text(
            "You are a T-SQL expert.", encoding="utf-8"
        )
        with override_settings(project_config_dir=str(tmp_path)):
            assert load_system_prompt() == "You are a T-SQL expert."

    def test_missing_file_raises_config_not_found_error_naming_the_path(self, tmp_path):
        with override_settings(project_config_dir=str(tmp_path)):
            expected_path = resolve_system_prompt_path()
            with pytest.raises(ConfigNotFoundError) as exc_info:
                load_system_prompt()
        assert str(expected_path) in str(exc_info.value)

    def test_missing_file_message_points_at_the_example_template(self, tmp_path):
        with override_settings(project_config_dir=str(tmp_path)):
            with pytest.raises(ConfigNotFoundError) as exc_info:
                load_system_prompt()
        assert "project_config.example/system_prompt.md" in str(exc_info.value)
