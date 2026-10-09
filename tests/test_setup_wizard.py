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


@pytest.fixture(autouse=True)
def _unwrapped_console(monkeypatch):
    """Keep the wizard's console from wrapping its lines.

    ``rich`` wraps at the terminal width, which differs between runners (the
    Windows CI legs wrap where the Linux ones do not). A wrapped line splits
    a phrase an assertion looks for, and could split a secret an assertion
    says is absent, making that check pass for the wrong reason.
    """
    if hasattr(sp.console, "_width"):
        monkeypatch.setattr(sp.console, "_width", 10_000)


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


class TestStep2DoesNotEchoThePassword:
    def test_a_failed_inspection_is_scrubbed(self, capsys):
        args = sp._build_parser().parse_args(["--non-interactive"])
        inspector = MagicMock()
        inspector.inspect.side_effect = ConnectionError(f"Cannot connect to {_URL}")
        with patch("database.schema_inspector.SchemaInspector", return_value=inspector):
            with pytest.raises(SystemExit):
                sp.step2_schema(args, _URL, {})
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
    traceback: ``import knowledge`` used to read five files of the default
    ``project_config/`` at import time, before the wizard could look. The
    package now loads lazily; this pins the wizard's behaviour end to end."""

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
            cwd=_REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
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


# ---------------------------------------------------------------------------
# "Regenerate" in the review screen
# ---------------------------------------------------------------------------

class _FakeLLM:
    """A stand-in for ``WizardLLM`` that answers by prompt and counts calls."""

    provider = "fake"
    model = "fake"

    def __init__(self, rule_text: str = "RULE-V1") -> None:
        self.calls = 0
        self.rule_text = rule_text

    def test_connection(self) -> bool:
        return True

    def generate(self, prompt: str, expect_json: bool = True):
        self.calls += 1
        if "business rules expert" in prompt:
            return {"rules": {"value_col": "amount", "volume_col": "qty", "rule_text": self.rule_text}}
        if "NLQ-to-SQL example pairs" in prompt:
            return [{"tags": ["count"], "question": "How many items?", "sql": "SELECT COUNT(*) FROM items"}]
        return {"aliases": ["thing"], "description": "a thing"}


def _col(name: str, sample_values: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(name=name, type="int", sample_values=list(sample_values))


def _fake_snapshot() -> SimpleNamespace:
    dim = SimpleNamespace(
        name="items", schema="", full_name="items", classification="dim",
        columns=[_col("id"), _col("name", ("alpha", "beta"))], foreign_keys=[],
    )
    fact = SimpleNamespace(
        name="sales", schema="", full_name="sales", classification="fact",
        columns=[_col("id"), _col("item_id"), _col("amount")],
        foreign_keys=[SimpleNamespace(referred_table="items")],
    )
    return SimpleNamespace(
        tables=[dim, fact], fact_tables=[fact], dim_tables=[dim],
        relationships=[SimpleNamespace(
            from_table="sales", from_column="item_id", to_table="items", to_column="id",
            join_hint="sales.item_id = items.id",
        )],
    )


class TestReviewFileRegenerate:
    """The "Regenerate" choice did nothing: no caller ever passed
    ``llm_regenerate_fn``, so the loop just showed the same text again."""

    def _review(self, answers, fn):
        choices_seen: list[list[str]] = []

        def choose(question, choices, default, non_interactive=False):
            choices_seen.append(list(choices))
            return answers.pop(0)

        with patch.object(sp, "_choose", side_effect=choose):
            accepted = sp._review_file(
                "business_rules.yaml", "rules: {}\n", non_interactive=False, dry_run=False,
                output_dir=Path("."), llm_regenerate_fn=fn,
            )
        return accepted, choices_seen

    def test_regenerate_replaces_the_content_with_a_new_version(self):
        accepted, seen = self._review(["Regenerate", "Accept"], lambda: "rules: {new: x}\n")
        assert accepted == "rules: {new: x}\n"
        assert "Regenerate" in seen[0]

    def test_it_is_not_offered_when_there_is_nothing_to_regenerate_with(self):
        accepted, seen = self._review(["Accept"], None)
        assert accepted == "rules: {}\n"
        assert "Regenerate" not in seen[0]

    def test_a_failing_regeneration_keeps_the_current_version(self, capsys):
        def boom() -> str:
            raise RuntimeError("endpoint down")

        accepted, _ = self._review(["Regenerate", "Accept"], boom)
        assert accepted == "rules: {}\n"
        assert "endpoint down" in capsys.readouterr().out


class TestStep6Regenerate:
    def _run(self, tmp_path, llm, answers):
        args = sp._build_parser().parse_args(["--language", "en"])
        seen: list[list[str]] = []

        def choose(question, choices, default, non_interactive=False):
            seen.append(list(choices))
            return answers.pop(0) if answers else "Accept"

        with patch.object(sp, "_choose", side_effect=choose):
            sp.step6_review_and_write(
                args, tmp_path, "2026-01-01T00:00:00+00:00", "sqlite://", {}, {},
                {"sales": "RULE-V0"}, [], _fake_snapshot(), {}, llm=llm, language="en",
            )
        return seen

    def test_regenerating_the_rules_asks_the_model_again_and_writes_the_new_text(self, tmp_path):
        llm = _FakeLLM(rule_text="RULE-V1")
        # entities, aliases, then business_rules (Regenerate, Accept), examples, relationships.
        self._run(tmp_path, llm, ["Accept", "Accept", "Regenerate", "Accept"])
        text = (tmp_path / "business_rules.yaml").read_text(encoding="utf-8")
        assert "RULE-V1" in text and "RULE-V0" not in text
        assert llm.calls == 1

    def test_regenerating_the_examples_writes_the_new_examples(self, tmp_path):
        llm = _FakeLLM()
        self._run(tmp_path, llm, ["Accept", "Accept", "Accept", "Regenerate", "Accept"])
        assert "How many items?" in (tmp_path / "examples.yaml").read_text(encoding="utf-8")

    def test_regenerating_the_entities_re_runs_the_aliases_for_every_table(self, tmp_path):
        llm = _FakeLLM()
        self._run(tmp_path, llm, ["Regenerate", "Accept"])
        assert llm.calls == 2  # one per table
        assert "thing" in (tmp_path / "entities.yaml").read_text(encoding="utf-8")

    def test_the_schema_only_file_has_no_regenerate_and_the_rest_do(self, tmp_path):
        seen = self._run(tmp_path, _FakeLLM(), [])
        offered = ["Regenerate" in choices for choices in seen]
        assert offered == [True, True, True, True, False]

    def test_without_an_llm_it_is_never_offered(self, tmp_path):
        seen = self._run(tmp_path, None, [])
        assert not any("Regenerate" in choices for choices in seen)


# ---------------------------------------------------------------------------
# --resume continues from the first incomplete step
# ---------------------------------------------------------------------------

class TestResume:
    """``--resume`` only skipped files that already existed; it re-ran every
    step, asking the model for everything again."""

    @pytest.fixture()
    def db(self, tmp_path):
        path = tmp_path / "wizard.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES ('alpha'), ('beta')")
        conn.commit()
        conn.close()
        return path

    def _argv(self, db, out, *extra):
        return ["--db-url", f"sqlite:///{db}", "--language", "en", "--non-interactive",
                "--output", str(out), *extra]

    def _interrupted_first_run(self, db, out, llm):
        with patch.object(sp, "setup_wizard_llm", return_value=llm), \
             patch.object(sp, "step6_review_and_write", side_effect=RuntimeError("interrupted")):
            with pytest.raises(RuntimeError):
                sp.main(self._argv(db, out))

    def test_the_log_records_the_generated_results(self, tmp_path, db):
        out = tmp_path / "out"
        self._interrupted_first_run(db, out, _FakeLLM())
        log = json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))
        assert sp._step_done(log, "step5_examples") and not sp._step_done(log, "step6_write")
        assert log["step3_aliases"]["result"]["entities"]["items"]["aliases"] == ["thing"]

    def test_completed_llm_steps_are_not_run_again(self, tmp_path, db):
        out = tmp_path / "out"
        self._interrupted_first_run(db, out, _FakeLLM())
        second = _FakeLLM()
        with patch.object(sp, "setup_wizard_llm", return_value=second) as setup_llm:
            assert sp.main(self._argv(db, out, "--resume")) == 0
        assert second.calls == 0
        assert not setup_llm.called  # no model needed, so none is configured or contacted
        entities = (out / "entities.yaml").read_text(encoding="utf-8")
        assert "thing" in entities  # the first run's aliases, reused

    def test_a_run_whose_steps_are_all_done_only_validates(self, tmp_path, db, capsys):
        out = tmp_path / "out"
        with patch.object(sp, "setup_wizard_llm", return_value=_FakeLLM()):
            assert sp.main(self._argv(db, out)) == 0
        capsys.readouterr()
        with patch.object(sp, "step1_connection", side_effect=AssertionError("connected")), \
             patch.object(sp, "setup_wizard_llm", side_effect=AssertionError("llm")):
            assert sp.main(self._argv(db, out, "--resume")) == 0
        assert "Running validation only" in " ".join(capsys.readouterr().out.split())

    def test_an_existing_file_is_still_not_rewritten(self, tmp_path, db):
        out = tmp_path / "out"
        self._interrupted_first_run(db, out, _FakeLLM())
        out.mkdir(exist_ok=True)
        (out / "entities.yaml").write_text("entities: {}  # hand edited\n", encoding="utf-8")
        with patch.object(sp, "setup_wizard_llm", return_value=_FakeLLM()):
            sp.main(self._argv(db, out, "--resume"))
        assert "hand edited" in (out / "entities.yaml").read_text(encoding="utf-8")
        assert (out / "aliases.yaml").is_file()

    def test_a_changed_schema_generates_again(self, tmp_path, db):
        out = tmp_path / "out"
        self._interrupted_first_run(db, out, _FakeLLM())
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE extra (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        second = _FakeLLM()
        with patch.object(sp, "setup_wizard_llm", return_value=second):
            sp.main(self._argv(db, out, "--resume"))
        assert second.calls > 0

    def test_a_changed_language_generates_again(self, tmp_path, db):
        out = tmp_path / "out"
        self._interrupted_first_run(db, out, _FakeLLM())
        second = _FakeLLM()
        argv = self._argv(db, out, "--resume")
        argv[argv.index("en")] = "fa"
        with patch.object(sp, "setup_wizard_llm", return_value=second):
            sp.main(argv)
        assert second.calls > 0

    def test_results_made_by_the_mock_llm_are_not_reused(self, tmp_path, db):
        out = tmp_path / "out"
        mock = SimpleNamespace(provider="mock", model="mock", test_connection=lambda: True,
                               generate=lambda prompt, expect_json=True: {})
        self._interrupted_first_run(db, out, mock)
        second = _FakeLLM()
        with patch.object(sp, "setup_wizard_llm", return_value=second):
            sp.main(self._argv(db, out, "--resume"))
        assert second.calls > 0

    def test_without_resume_the_earlier_log_is_not_trusted(self, tmp_path, db):
        out = tmp_path / "out"
        self._interrupted_first_run(db, out, _FakeLLM())
        second = _FakeLLM()
        with patch.object(sp, "setup_wizard_llm", return_value=second):
            sp.main(self._argv(db, out))
        assert second.calls > 0

    def test_a_dry_run_writes_no_log_and_nothing_resumes_from_it(self, tmp_path, db):
        out = tmp_path / "out"
        with patch.object(sp, "setup_wizard_llm", return_value=_FakeLLM()):
            assert sp.main(self._argv(db, out, "--dry-run")) == 0
        assert not out.exists()
