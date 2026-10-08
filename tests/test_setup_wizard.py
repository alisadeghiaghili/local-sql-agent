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

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import config as cfg
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


# ---------------------------------------------------------------------------
# Which LLM endpoint the wizard uses, and whether it may see the schema
# ---------------------------------------------------------------------------

LLM_KEY = "sk-distinctive-llm-key"
_LOCAL = "http://localhost:8000/v1"
_HOSTED = "https://api.openai.com/v1"


def _llm_args(*extra: str) -> argparse.Namespace:
    return sp._build_parser().parse_args(["--non-interactive", *extra])


@pytest.fixture()
def clean_wizard_env(monkeypatch):
    for name in ("WIZARD_LLM_PROVIDER", "WIZARD_LLM_MODEL", "WIZARD_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def _app(**overrides):
    values = dict(
        openai_base_url=_LOCAL, openai_model="app-model", openai_api_key=LLM_KEY,
        llm_allow_remote=False, llm_trusted=None,
    )
    values.update(overrides)
    return cfg.override_settings(**values)


class TestResolveWizardLlm:
    """``.env.example`` ships blank WIZARD_LLM_BASE_URL: it used to mean
    "silently use api.openai.com" rather than "use the application's"."""

    def test_empty_wizard_values_fall_back_to_the_application_settings(self, clean_wizard_env, monkeypatch):
        monkeypatch.setenv("WIZARD_LLM_MODEL", "")
        monkeypatch.setenv("WIZARD_LLM_BASE_URL", "")
        with _app():
            resolved = sp.resolve_wizard_llm(_llm_args())
        assert (resolved.model, resolved.base_url, resolved.api_key) == ("app-model", _LOCAL, LLM_KEY)
        assert resolved.sources["model"] == "OPENAI_MODEL"
        assert resolved.sources["base_url"] == "OPENAI_BASE_URL"

    def test_wizard_variables_override_the_application(self, clean_wizard_env, monkeypatch):
        monkeypatch.setenv("WIZARD_LLM_MODEL", "wizard-model")
        monkeypatch.setenv("WIZARD_LLM_BASE_URL", "http://192.168.1.5:9000/v1")
        with _app():
            resolved = sp.resolve_wizard_llm(_llm_args())
        assert resolved.model == "wizard-model"
        assert resolved.base_url == "http://192.168.1.5:9000/v1"
        assert resolved.sources["model"] == "WIZARD_LLM_MODEL"

    def test_a_flag_beats_the_environment(self, clean_wizard_env, monkeypatch):
        monkeypatch.setenv("WIZARD_LLM_MODEL", "wizard-model")
        with _app():
            resolved = sp.resolve_wizard_llm(_llm_args("--llm-model", "flag-model"))
        assert resolved.model == "flag-model"
        assert resolved.sources["model"] == "--llm-model"

    def test_the_description_names_the_endpoint_and_never_the_key(self, clean_wizard_env):
        with _app():
            line = sp.resolve_wizard_llm(_llm_args()).describe()
        assert "app-model" in line and _LOCAL in line
        assert "OPENAI_MODEL" in line and "OPENAI_BASE_URL" in line
        assert LLM_KEY not in line

    def test_an_unsupported_provider_is_an_error(self, clean_wizard_env, monkeypatch):
        monkeypatch.setenv("WIZARD_LLM_PROVIDER", "ollama")
        with _app(), pytest.raises(ValueError, match="Unsupported"):
            sp.resolve_wizard_llm(_llm_args())


class TestRemoteMeansWhatTheApplicationMeans:
    def test_a_local_address_is_not_remote(self, clean_wizard_env):
        with _app(openai_base_url="http://192.168.1.50:8000/v1"):
            assert sp.resolve_wizard_llm(_llm_args()).remote is False

    def test_a_hosted_address_is_remote(self, clean_wizard_env):
        with _app(openai_base_url=_HOSTED):
            assert sp.resolve_wizard_llm(_llm_args()).remote is True

    def test_the_application_trust_override_applies_to_the_application_endpoint(self, clean_wizard_env):
        with _app(openai_base_url="https://llm.corp.example/v1", llm_trusted=True):
            assert sp.resolve_wizard_llm(_llm_args()).remote is False
        with _app(openai_base_url=_LOCAL, llm_trusted=False):
            assert sp.resolve_wizard_llm(_llm_args()).remote is True

    def test_the_override_does_not_vouch_for_a_different_wizard_endpoint(self, clean_wizard_env):
        with _app(openai_base_url=_LOCAL, llm_trusted=True):
            resolved = sp.resolve_wizard_llm(_llm_args("--llm-base-url", _HOSTED))
        assert resolved.remote is True

    def test_the_mock_provider_is_never_remote(self, clean_wizard_env):
        with _app(openai_base_url=_HOSTED):
            assert sp.resolve_wizard_llm(_llm_args("--llm-provider", "mock")).remote is False


class TestWizardHonoursLlmAllowRemote:
    """The wizard sent sample column values to whatever endpoint it was
    pointed at, with no regard for ``LLM_ALLOW_REMOTE``."""

    def test_a_remote_endpoint_is_refused_without_the_opt_in(self, clean_wizard_env):
        with _app(openai_base_url=_HOSTED, llm_allow_remote=False):
            with pytest.raises(sp.RemoteLLMNotAllowedError, match="LLM_ALLOW_REMOTE"):
                sp.enforce_remote_policy(sp.resolve_wizard_llm(_llm_args()))

    def test_a_remote_endpoint_is_allowed_with_the_opt_in(self, clean_wizard_env):
        with _app(openai_base_url=_HOSTED, llm_allow_remote=True):
            sp.enforce_remote_policy(sp.resolve_wizard_llm(_llm_args()))

    def test_a_local_endpoint_needs_no_opt_in(self, clean_wizard_env):
        with _app(llm_allow_remote=False):
            sp.enforce_remote_policy(sp.resolve_wizard_llm(_llm_args()))

    def test_nothing_is_sent_and_the_database_is_not_touched_when_refused(
        self, clean_wizard_env, tmp_path, capsys
    ):
        with _app(openai_base_url=_HOSTED, llm_allow_remote=False), \
             patch("requests.get") as get, patch("requests.post") as post, \
             patch.object(sp, "step1_connection") as step1:
            code = sp.main([
                "--db-url", "sqlite://", "--language", "en", "--non-interactive",
                "--output", str(tmp_path / "out"),
            ])
        assert code == 2
        assert not get.called and not post.called and not step1.called
        out = capsys.readouterr().out
        assert "LLM_ALLOW_REMOTE" in out
        assert LLM_KEY not in out
        assert not (tmp_path / "out").exists()


class TestSetupWizardLlm:
    def test_a_reachable_local_endpoint_is_used_and_announced(self, clean_wizard_env, capsys):
        ok = MagicMock(status_code=200)
        with _app(openai_api_key=""), patch("requests.get", return_value=ok):
            llm = sp.setup_wizard_llm(_llm_args())
        assert llm.provider == "openai"
        assert llm._backend.endpoint == _LOCAL
        assert llm._backend.trusted is True
        out = capsys.readouterr().out
        assert "app-model" in out and "OPENAI_BASE_URL" in out

    def test_the_key_is_never_printed(self, clean_wizard_env, capsys):
        ok = MagicMock(status_code=200)
        with _app(), patch("requests.get", return_value=ok):
            sp.setup_wizard_llm(_llm_args())
        captured = capsys.readouterr()
        assert LLM_KEY not in captured.out + captured.err

    def test_an_unreachable_endpoint_falls_back_to_mock_loudly(self, clean_wizard_env, capsys):
        with _app(), patch("requests.get", side_effect=ConnectionError(f"refused for {LLM_KEY}")):
            llm = sp.setup_wizard_llm(_llm_args())
        assert llm.provider == "mock"
        out = capsys.readouterr().out
        assert "FALLING BACK TO THE MOCK LLM" in out
        assert "EMPTY" in out
        assert LLM_KEY not in out

    def test_an_allowed_remote_endpoint_says_what_is_sent(self, clean_wizard_env, capsys):
        ok = MagicMock(status_code=200)
        with _app(openai_base_url=_HOSTED, llm_allow_remote=True), patch("requests.get", return_value=ok):
            llm = sp.setup_wizard_llm(_llm_args())
        assert llm._backend.trusted is False
        out = capsys.readouterr().out
        assert "remote" in out and "sample column values" in out

    def test_the_mock_provider_is_announced_as_such(self, clean_wizard_env, capsys):
        with _app():
            llm = sp.setup_wizard_llm(_llm_args("--llm-provider", "mock"))
        assert llm.provider == "mock"
        assert "mock" in capsys.readouterr().out


class TestEnvExampleWizardKeys:
    """The shipped example must make the fallback reachable: a non-empty
    default for the model or endpoint would shadow the application's."""

    def test_the_endpoint_values_are_empty_and_the_keys_remain(self):
        from dotenv import dotenv_values

        values = dotenv_values(Path(__file__).resolve().parent.parent / ".env.example")
        for key in ("WIZARD_LLM_PROVIDER", "WIZARD_LLM_MODEL", "WIZARD_LLM_BASE_URL", "WIZARD_LANGUAGE"):
            assert key in values
        assert values["WIZARD_LLM_MODEL"] == ""
        assert values["WIZARD_LLM_BASE_URL"] == ""


# ---------------------------------------------------------------------------
# Step 7 on a project_config/ that is not complete
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_DIR = _REPO_ROOT / "project_config.example"
_WIZARD_FILES = ("entities.yaml", "aliases.yaml", "business_rules.yaml", "examples.yaml")


def _seed(directory: Path, names: tuple[str, ...]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(_EXAMPLE_DIR / name, directory / name)


class TestStep7WithMissingFiles:
    """On a fresh checkout step 7 died with a ``ConfigNotFoundError``
    traceback: ``import knowledge`` reads five files of the default
    ``project_config/`` at import time, before the wizard could look."""

    def test_a_fresh_checkout_gets_a_report_not_a_traceback(self, tmp_path):
        db = tmp_path / "wizard.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        conn.close()
        out = tmp_path / "out"
        env = {**os.environ, "PROJECT_CONFIG_DIR": str(tmp_path / "no_such_project_config")}
        for name in ("WIZARD_LLM_PROVIDER", "WIZARD_LLM_MODEL", "WIZARD_LLM_BASE_URL"):
            env.pop(name, None)
        done = subprocess.run(
            [sys.executable, "setup_project.py", "--db-url", f"sqlite:///{db}",
             "--llm-provider", "mock", "--language", "en", "--non-interactive",
             "--output", str(out)],
            cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Traceback" not in done.stdout + done.stderr
        text = " ".join(done.stdout.split())
        for name in ("metrics.yaml", "schema.yaml", "system_prompt.md"):
            assert name in text
        assert "project_config.example" in text
        assert "NOT complete" in text
        assert (out / "entities.yaml").is_file()

    def test_the_missing_files_are_returned_and_logged(self, tmp_path):
        _seed(tmp_path, _WIZARD_FILES)
        log: dict = {}
        missing = sp.step7_validate(tmp_path, log)
        assert missing == [
            "metrics.yaml", "schema.yaml", "retrieval_hints.yaml",
            "session_policy.yaml", "memory_policy.yaml", "system_prompt.md",
        ]
        assert log["step7_validate"]["missing_files"] == missing

    def test_it_does_not_claim_completion_while_files_are_missing(self, tmp_path, capsys):
        _seed(tmp_path, _WIZARD_FILES)
        sp.step7_validate(tmp_path, {})
        out = capsys.readouterr().out
        assert "Setup complete" not in out
        assert "NOT complete" in out

    def test_nothing_is_copied_in_for_the_operator(self, tmp_path):
        _seed(tmp_path, _WIZARD_FILES)
        before = sorted(p.name for p in tmp_path.iterdir())
        sp.step7_validate(tmp_path, {})
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_a_complete_directory_validates_and_reports_completion(self, tmp_path, capsys):
        from core.project_config_files import REQUIRED_PROJECT_CONFIG_FILES

        _seed(tmp_path, REQUIRED_PROJECT_CONFIG_FILES)
        log: dict = {}
        assert sp.step7_validate(tmp_path, log) == []
        out = capsys.readouterr().out
        assert "Setup complete" in out
        assert log["step7_validate"]["entity_count"] > 0

    def test_an_invalid_generated_file_is_reported_not_raised(self, tmp_path, capsys):
        from core.project_config_files import REQUIRED_PROJECT_CONFIG_FILES

        _seed(tmp_path, REQUIRED_PROJECT_CONFIG_FILES)
        (tmp_path / "entities.yaml").write_text("entities: [not, a, mapping]\n", encoding="utf-8")
        sp.step7_validate(tmp_path, {})
        out = capsys.readouterr().out
        assert "entities.yaml: FAILED" in out
        assert "Setup complete" not in out
