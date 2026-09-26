# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for webapp/agent.py's system_prompt() loader.

Regression coverage for the "webapp/agent.py currently has no bespoke
missing-file handling" gap flagged while relocating the system prompt
under PROJECT_CONFIG_DIR: a bare, unhandled FileNotFoundError used to
surface instead of the clear, path-naming RuntimeError every other call
site (api/server.py, app.py, eval/cli.py) already raises.
"""

from __future__ import annotations

import pytest

import webapp.agent as agent


class TestSystemPrompt:
    def test_returns_and_caches_file_contents(self, tmp_path, monkeypatch):
        prompt_file = tmp_path / "system_prompt.md"
        prompt_file.write_text("You are a SQL agent.", encoding="utf-8")
        monkeypatch.setattr(agent, "SYSTEM_PROMPT_PATH", prompt_file)
        monkeypatch.setattr(agent, "_system_prompt", None)

        first = agent.system_prompt()
        assert first == "You are a SQL agent."

        # Cached: deleting the file after the first read must not matter.
        prompt_file.unlink()
        assert agent.system_prompt() == "You are a SQL agent."

    def test_missing_file_raises_runtime_error_naming_the_path(self, tmp_path, monkeypatch):
        missing_path = tmp_path / "does_not_exist.md"
        monkeypatch.setattr(agent, "SYSTEM_PROMPT_PATH", missing_path)
        monkeypatch.setattr(agent, "_system_prompt", None)

        with pytest.raises(RuntimeError) as exc_info:
            agent.system_prompt()
        assert str(missing_path) in str(exc_info.value)
