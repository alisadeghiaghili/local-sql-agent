# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression tests for ``setup_project.py``, the one-time setup wizard.

The wizard is interactive and is excluded from the coverage gate, but the
bugs pinned here are not about the prompts: a connection string with its
password left in a file on disk, an LLM call that ignored the data-governance
gate, a validation step that crashed on a fresh checkout. Each class below
names the bug it guards.

Nothing here touches a network or a real database. Where the wizard needs a
connection, ``sqlalchemy.create_engine`` is replaced by a stub, and the
schema snapshot is a small fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import setup_project as sp

#: Appears only as a password in the fixtures below. Any output containing
#: it has leaked a credential.
SECRET = "hunter2-distinctive"

_URL = f"mssql+pyodbc://nlq_reader:{SECRET}@db1.example.test/SalesDW?driver=ODBC+Driver+17+for+SQL+Server"


def _empty_snapshot() -> SimpleNamespace:
    """A schema snapshot with no tables, enough for steps 3 to 7."""
    return SimpleNamespace(tables=[], relationships=[], fact_tables=[], dim_tables=[])


def _tree_text(root: Path) -> str:
    """Every file under *root* concatenated, for a leak search."""
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    )


# ---------------------------------------------------------------------------
# The connection string is never written or printed with its password
# ---------------------------------------------------------------------------

class TestRedactDbUrl:
    def test_masks_the_url_password(self):
        assert sp._redact_db_url(_URL).startswith("mssql+pyodbc://nlq_reader:***@db1.example.test/SalesDW")
        assert SECRET not in sp._redact_db_url(_URL)

    @pytest.mark.parametrize(
        "password",
        ["p@ss", "p:ss", "p/ss", "p%40ss", "a@b@c"],
    )
    def test_masks_passwords_a_simple_pattern_would_miss(self, password):
        url = f"postgresql+psycopg2://u:{password}@h:5432/d"
        redacted = sp._redact_db_url(url)
        assert password not in redacted
        assert "***" in redacted
        assert redacted.endswith("@h:5432/d")

    def test_masks_a_password_in_the_query_string(self):
        redacted = sp._redact_db_url(f"mssql+pyodbc://u@h/d?driver=X&PWD={SECRET}")
        assert SECRET not in redacted

    def test_masks_a_password_inside_odbc_connect(self):
        redacted = sp._redact_db_url(f"mssql+pyodbc:///?odbc_connect=UID%3Da%3BPWD%3D{SECRET}")
        assert SECRET not in redacted

    def test_a_url_without_credentials_is_unchanged(self):
        assert sp._redact_db_url("sqlite:///data/x.db") == "sqlite:///data/x.db"

    def test_an_unparseable_string_is_never_echoed(self):
        assert SECRET not in sp._redact_db_url(f"not a url {SECRET}")
        assert SECRET not in sp._redact_db_url(f"weird://u:{SECRET} x@h")

    def test_scrub_removes_the_url_and_the_password_from_driver_text(self):
        text = f"login failed for {_URL!r} (password {SECRET})"
        scrubbed = sp._scrub_secrets(text, _URL)
        assert SECRET not in scrubbed


class TestStep1DoesNotStoreThePassword:
    """``'re' in dir()`` is always False inside a function, so the URL was
    stored under ``db_url_redacted`` exactly as typed."""

    def _run_step1(self, capsys) -> dict:
        args = sp._build_parser().parse_args(["--db-url", _URL, "--non-interactive"])
        log: dict = {}
        with patch("sqlalchemy.create_engine", return_value=MagicMock()):
            assert sp.step1_connection(args, log) == _URL
        return log

    def test_the_log_entry_is_redacted(self, capsys):
        log = self._run_step1(capsys)
        assert SECRET not in json.dumps(log)
        assert "nlq_reader:***@db1.example.test" in log["step1_connection"]["db_url_redacted"]

    def test_a_failure_message_does_not_echo_the_password(self, capsys):
        args = sp._build_parser().parse_args(["--db-url", _URL, "--non-interactive"])
        with patch("sqlalchemy.create_engine", side_effect=RuntimeError(f"cannot open {_URL}")):
            with pytest.raises(SystemExit):
                sp.step1_connection(args, {})
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err


class TestWizardWritesNoPassword:
    def test_nothing_on_disk_or_on_screen_holds_the_password(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("WIZARD_LLM_BASE_URL", raising=False)
        out = tmp_path / "project_config"
        with patch("sqlalchemy.create_engine", return_value=MagicMock()), \
             patch.object(sp, "step2_schema", return_value=_empty_snapshot()):
            code = sp.main([
                "--db-url", _URL, "--llm-provider", "mock", "--language", "en",
                "--non-interactive", "--output", str(out),
            ])
        assert code == 0
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err
        assert SECRET not in _tree_text(out)
        log = json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))
        assert "***" in log["step1_connection"]["db_url_redacted"]
        assert "***" in (out / "entities.yaml").read_text(encoding="utf-8")
