# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``API_KEYS_FILE``: the API key array read from a file instead of ``.env``.

A pretty-printed ``API_KEYS_JSON`` only survives ``.env`` wrapped in single
quotes with no apostrophe inside, so the same array can instead live in a
file named by ``API_KEYS_FILE``. These tests pin that the file goes through
the one parser ``API_KEYS_JSON`` uses, that it is read once per process, that
every refusal names the source and never quotes file content, and that
``verify_deployment``, the server and ``issue_api_key`` all point at it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import pytest

import config
import security.auth as auth
from appdb import key_store
from config import override_settings
from scripts.issue_api_key import main as issue_main
from scripts.verify_deployment import check_api_key_authenticates
from security.auth import (
    ApiKeyConfigError,
    _parse_api_keys,
    _reset_api_keys_file_cache,
    api_keys_source,
    load_api_keys,
)

#: Appears only inside fixture files; a message containing it quoted the file.
SECRET = "never-quote-this-distinctive-text"


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _entry(principal_id: str, raw_key: str, **extra) -> dict:
    return {
        "id": principal_id,
        "name": principal_id.title(),
        "key_sha256": _sha256(raw_key),
        "denied_columns": [],
        **extra,
    }


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


@pytest.fixture
def keys_file(tmp_path) -> Path:
    return _write(
        tmp_path / "api_keys.json",
        json.dumps([_entry("analyst-1", "a" * 40), _entry("admin-1", "b" * 40, admin=True)]),
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

class TestLoadsKeysFromTheFile:
    def test_file_only_keys_load(self, keys_file):
        with override_settings(api_keys_file=str(keys_file), api_keys_json=""):
            keys = load_api_keys()
        assert {p.id for p in keys.values()} == {"analyst-1", "admin-1"}
        assert keys[_sha256("b" * 40)].capabilities

    def test_a_pretty_printed_file_with_apostrophes_loads(self, tmp_path):
        # The exact shape that python-dotenv drops from .env.
        path = _write(tmp_path / "keys.json", f"""
        [
          {{
            "id": "analyst-1",
            "name": "Ali's key",
            "key_sha256": "{_sha256("a" * 40)}",
            "denied_columns": []
          }},
          {{
            "id": "admin-1",
            "name": "Admin",
            "key_sha256": "{_sha256("b" * 40)}",
            "denied_columns": []
          }}
        ]
        """)
        with override_settings(api_keys_file=str(path)):
            keys = load_api_keys()
        assert keys[_sha256("a" * 40)].name == "Ali's key"
        assert len(keys) == 2

    def test_a_utf8_bom_is_tolerated(self, tmp_path):
        # PowerShell's Out-File and many Windows editors write one.
        path = _write(
            tmp_path / "keys.json", json.dumps([_entry("a", "a" * 40)]), encoding="utf-8-sig"
        )
        with override_settings(api_keys_file=str(path)):
            assert len(load_api_keys()) == 1

    def test_non_ascii_names_load(self, tmp_path):
        path = _write(
            tmp_path / "keys.json",
            json.dumps([{**_entry("a", "a" * 40), "name": "تحلیل‌گر"}], ensure_ascii=False),
        )
        with override_settings(api_keys_file=str(path)):
            assert next(iter(load_api_keys().values())).name == "تحلیل‌گر"

    def test_an_empty_file_means_no_keys(self, tmp_path):
        path = _write(tmp_path / "keys.json", "  \n")
        with override_settings(api_keys_file=str(path)):
            assert load_api_keys() == {}

    def test_unset_file_uses_api_keys_json_as_before(self):
        raw = json.dumps([_entry("a", "a" * 40)])
        with override_settings(api_keys_file="", api_keys_json=raw):
            assert len(load_api_keys()) == 1

    def test_whitespace_only_api_keys_json_does_not_conflict_with_the_file(self, keys_file):
        with override_settings(api_keys_file=str(keys_file), api_keys_json="  "):
            assert len(load_api_keys()) == 2

    def test_the_setting_reads_the_environment_variable(self, monkeypatch):
        monkeypatch.setenv("API_KEYS_FILE", "project_config/api_keys.json")
        assert config.Settings().api_keys_file == "project_config/api_keys.json"
        monkeypatch.delenv("API_KEYS_FILE")
        assert config.Settings().api_keys_file == ""


class TestRelativePathsResolveAgainstTheRepositoryRoot:
    def test_the_root_is_the_directory_holding_config_py(self):
        assert auth._REPO_ROOT == Path(config.__file__).resolve().parent

    def test_a_relative_path_is_not_taken_from_the_working_directory(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "repo"
        _write(root / "project_config" / "api_keys.json", json.dumps([_entry("a", "a" * 40)]))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        # A decoy at the same relative path under the working directory.
        _write(elsewhere / "project_config" / "api_keys.json", "[]")
        monkeypatch.chdir(elsewhere)
        monkeypatch.setattr(auth, "_REPO_ROOT", root)
        with override_settings(api_keys_file="project_config/api_keys.json"):
            assert [p.id for p in load_api_keys().values()] == ["a"]

    def test_an_absolute_path_is_used_as_is(self, keys_file, monkeypatch, tmp_path):
        monkeypatch.setattr(auth, "_REPO_ROOT", tmp_path / "somewhere-else")
        with override_settings(api_keys_file=str(keys_file)):
            assert len(load_api_keys()) == 2


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

class TestRefusals:
    def test_both_sources_set_is_refused_saying_to_use_one(self, keys_file):
        raw = json.dumps([_entry("a", "c" * 40)])
        with override_settings(api_keys_file=str(keys_file), api_keys_json=raw):
            with pytest.raises(ApiKeyConfigError) as excinfo:
                load_api_keys()
        text = str(excinfo.value)
        assert "API_KEYS_JSON" in text and "API_KEYS_FILE" in text
        assert "use one" in text
        assert _sha256("c" * 40) not in text

    def test_a_missing_file_names_the_resolved_path(self, tmp_path):
        missing = tmp_path / "nope" / "keys.json"
        with override_settings(api_keys_file=str(missing)):
            with pytest.raises(ApiKeyConfigError) as excinfo:
                load_api_keys()
        assert str(missing) in str(excinfo.value)
        assert "could not be read" in str(excinfo.value)

    def test_a_relative_missing_file_names_the_path_it_resolved_to(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auth, "_REPO_ROOT", tmp_path)
        with override_settings(api_keys_file="project_config/api_keys.json"):
            with pytest.raises(ApiKeyConfigError) as excinfo:
                load_api_keys()
        assert str(tmp_path / "project_config" / "api_keys.json") in str(excinfo.value)

    def test_a_directory_is_refused(self, tmp_path):
        with override_settings(api_keys_file=str(tmp_path)):
            with pytest.raises(ApiKeyConfigError, match="could not be read"):
                load_api_keys()

    def test_a_file_that_is_not_utf8_is_refused_without_its_bytes(self, tmp_path):
        path = tmp_path / "keys.json"
        path.write_bytes(b"[\xff\xfe]")
        with override_settings(api_keys_file=str(path)):
            with pytest.raises(ApiKeyConfigError) as excinfo:
                load_api_keys()
        assert "not valid UTF-8" in str(excinfo.value)
        assert str(path) in str(excinfo.value)
        assert "0xff" not in str(excinfo.value)

    def test_invalid_json_gives_line_and_column_but_not_content(self, tmp_path):
        path = _write(tmp_path / "keys.json", f'[\n  {{"id": "{SECRET}",\n  "name" "x"}}\n]\n')
        with override_settings(api_keys_file=str(path)):
            with pytest.raises(ApiKeyConfigError) as excinfo:
                load_api_keys()
        text = str(excinfo.value)
        assert "is not valid JSON" in text
        assert "line 3, column 10" in text
        assert SECRET not in text

    def test_a_repeated_key_inside_an_object_is_refused_naming_it(self, tmp_path):
        path = _write(
            tmp_path / "keys.json",
            '[{"id": "a", "name": "A", "key_sha256": "%s", '
            '"denied_columns": ["X"], "denied_columns": []}]' % _sha256("a" * 40),
        )
        with override_settings(api_keys_file=str(path)):
            with pytest.raises(ApiKeyConfigError) as excinfo:
                load_api_keys()
        assert "'denied_columns'" in str(excinfo.value)
        assert "repeats the key" in str(excinfo.value)

    def test_the_same_field_in_two_entries_is_fine(self, keys_file):
        with override_settings(api_keys_file=str(keys_file)):
            assert len(load_api_keys()) == 2

    def test_a_repeated_key_is_refused_for_api_keys_json_too(self):
        with pytest.raises(ApiKeyConfigError, match="repeats the key 'id'"):
            _parse_api_keys('[{"id": "a", "id": "b"}]')

    def test_a_non_array_is_refused(self, tmp_path):
        path = _write(tmp_path / "keys.json", json.dumps(_entry("a", "a" * 40)))
        with override_settings(api_keys_file=str(path)):
            with pytest.raises(ApiKeyConfigError) as excinfo:
                load_api_keys()
        assert "must be a JSON array of key objects" in str(excinfo.value)
        assert "API_KEYS_FILE" in str(excinfo.value)

    def test_a_file_error_never_quotes_the_file(self, tmp_path):
        path = _write(tmp_path / "keys.json", f'[{{"id": 1, "name": "{SECRET}"}}]')
        with override_settings(api_keys_file=str(path)):
            with pytest.raises(ApiKeyConfigError) as excinfo:
                load_api_keys()
        assert SECRET not in str(excinfo.value)


# ---------------------------------------------------------------------------
# The committed template
# ---------------------------------------------------------------------------

#: Every field ``security.auth._parse_api_keys`` reads from an entry.
_PARSED_FIELDS = {
    "id", "name", "key_sha256", "denied_columns", "admin", "operations", "security",
}

_TEMPLATE = Path(__file__).resolve().parent.parent / "project_config.example" / "api_keys.example.json"


class TestTemplateFile:
    """``project_config.example/api_keys.example.json`` is copied, then edited."""

    def test_it_is_a_json_array_of_entries_using_only_parsed_fields(self):
        entries = json.loads(_TEMPLATE.read_text(encoding="utf-8"))
        assert isinstance(entries, list) and len(entries) >= 2
        for entry in entries:
            assert set(entry) <= _PARSED_FIELDS, set(entry) - _PARSED_FIELDS
            assert {"id", "name", "key_sha256"} <= set(entry)

    def test_the_two_entries_together_show_every_parsed_field(self):
        entries = json.loads(_TEMPLATE.read_text(encoding="utf-8"))
        assert set().union(*entries) == _PARSED_FIELDS

    def test_it_is_refused_as_it_stands(self, tmp_path):
        # A copy with no digest replaced must never start a server.
        copy = _write(tmp_path / "api_keys.json", _TEMPLATE.read_text(encoding="utf-8"))
        with override_settings(api_keys_file=str(copy), api_keys_json=""):
            with pytest.raises(ApiKeyConfigError) as excinfo:
                load_api_keys()
        text = str(excinfo.value)
        assert f"API_KEYS_FILE ({copy})[0].key_sha256 must be a 64-character SHA-256" in text

    def test_it_loads_once_each_digest_is_replaced(self, tmp_path):
        entries = json.loads(_TEMPLATE.read_text(encoding="utf-8"))
        for entry, raw_key in zip(entries, ("a" * 40, "b" * 40)):
            entry["key_sha256"] = _sha256(raw_key)
        copy = _write(tmp_path / "api_keys.json", json.dumps(entries))
        with override_settings(api_keys_file=str(copy), api_keys_json=""):
            keys = load_api_keys()
        analyst, admin = keys[_sha256("a" * 40)], keys[_sha256("b" * 40)]
        assert analyst.denied_columns and not analyst.capabilities
        assert admin.is_admin and admin.is_operations and admin.is_security

    def test_the_template_is_not_where_the_server_reads_keys(self, monkeypatch):
        # The recommended path is project_config/api_keys.json, and no path is
        # configured by default, so the template is only ever read when
        # someone points API_KEYS_FILE at a copy of it.
        monkeypatch.delenv("API_KEYS_FILE", raising=False)
        assert config.Settings().api_keys_file == ""
        assert _TEMPLATE.parent.name == "project_config.example"
        assert _TEMPLATE.name != "api_keys.json"


# ---------------------------------------------------------------------------
# Messages name their source
# ---------------------------------------------------------------------------

class TestMessagesNameTheSource:
    def test_source_label(self):
        with override_settings(api_keys_file=""):
            assert api_keys_source() == "API_KEYS_JSON"
        with override_settings(api_keys_file=" project_config/api_keys.json "):
            assert api_keys_source() == "API_KEYS_FILE (project_config/api_keys.json)"

    def test_an_invalid_entry_names_the_file_and_index(self, tmp_path):
        path = _write(tmp_path / "keys.json", json.dumps([{"id": "a", "name": "A"}]))
        with override_settings(api_keys_file=str(path)):
            with pytest.raises(ApiKeyConfigError) as excinfo:
                load_api_keys()
        assert f"API_KEYS_FILE ({path})[0] is missing required field" in str(excinfo.value)

    def test_an_invalid_api_keys_json_entry_still_says_api_keys_json(self):
        with pytest.raises(ApiKeyConfigError, match=r"API_KEYS_JSON\[0\] is missing required field"):
            _parse_api_keys(json.dumps([{"id": "a", "name": "A"}]))

    def test_the_missing_denied_columns_warning_names_the_file(self, tmp_path, caplog):
        entry = {k: v for k, v in _entry("x", "a" * 40).items() if k != "denied_columns"}
        path = _write(tmp_path / "keys.json", json.dumps([entry]))
        with override_settings(api_keys_file=str(path)):
            with caplog.at_level(logging.WARNING, logger="security.auth"):
                load_api_keys()
        [message] = [r.getMessage() for r in caplog.records if "denied_columns" in r.getMessage()]
        assert message.startswith(f"API_KEYS_FILE ({path})[0] (id='x') has no denied_columns")

    def test_the_warning_is_still_logged_once_per_key(self, tmp_path, caplog):
        entry = {k: v for k, v in _entry("x", "a" * 40).items() if k != "denied_columns"}
        path = _write(tmp_path / "keys.json", json.dumps([entry]))
        with override_settings(api_keys_file=str(path)):
            with caplog.at_level(logging.WARNING, logger="security.auth"):
                load_api_keys()
                _reset_api_keys_file_cache()
                load_api_keys()
                load_api_keys()
        assert sum("has no denied_columns" in r.getMessage() for r in caplog.records) == 1

    def test_the_api_keys_json_warning_is_unchanged(self, caplog):
        entry = {k: v for k, v in _entry("x", "a" * 40).items() if k != "denied_columns"}
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            _parse_api_keys(json.dumps([entry]))
        assert any(
            r.getMessage().startswith("API_KEYS_JSON[0] (id='x') has no denied_columns")
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Read once per process
# ---------------------------------------------------------------------------

class TestReadOncePerProcess:
    def test_editing_the_file_changes_nothing_until_a_restart(self, keys_file):
        with override_settings(api_keys_file=str(keys_file)):
            first = load_api_keys()
            keys_file.write_text("[]", encoding="utf-8")
            assert load_api_keys() == first
            _reset_api_keys_file_cache()  # what a restart is
            assert load_api_keys() == {}

    def test_a_half_written_file_is_never_seen_after_the_first_read(self, keys_file):
        with override_settings(api_keys_file=str(keys_file)):
            first = load_api_keys()
            keys_file.write_text('[{"id": "a", "na', encoding="utf-8")
            assert load_api_keys() == first

    def test_a_different_path_is_read_afresh(self, keys_file, tmp_path):
        other = _write(tmp_path / "other.json", json.dumps([_entry("only", "c" * 40)]))
        with override_settings(api_keys_file=str(keys_file)):
            assert len(load_api_keys()) == 2
        with override_settings(api_keys_file=str(other)):
            assert [p.id for p in load_api_keys().values()] == ["only"]

    def test_a_failed_read_is_not_remembered(self, tmp_path):
        path = tmp_path / "keys.json"
        with override_settings(api_keys_file=str(path)):
            with pytest.raises(ApiKeyConfigError):
                load_api_keys()
            _write(path, json.dumps([_entry("a", "a" * 40)]))
            assert len(load_api_keys()) == 1

    def test_the_file_is_opened_once_across_many_loads(self, keys_file, monkeypatch):
        reads = []
        real = Path.read_text

        def counting(self, *args, **kwargs):
            reads.append(self)
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting)
        with override_settings(api_keys_file=str(keys_file)):
            for _ in range(5):
                load_api_keys()
        assert reads == [keys_file]


# ---------------------------------------------------------------------------
# Consumers
# ---------------------------------------------------------------------------

class TestKeyStoreSeesFileKeys:
    def test_file_keys_are_active_and_imported_as_environment_keys(self, keys_file):
        with override_settings(api_keys_file=str(keys_file)):
            key_store.bootstrap_from_env()
            key_store.invalidate_cache()
            assert _sha256("a" * 40) in key_store.get_active_principals()
            rows = {r["principal_id"]: r for r in key_store.list_keys()}
        assert rows["analyst-1"]["source"] == "imported_from_env"


class TestVerifyDeployment:
    def test_the_check_passes_with_file_keys_and_names_the_file(self, keys_file, monkeypatch):
        monkeypatch.delenv("VERIFY_API_KEY", raising=False)
        with override_settings(auth_required=True, api_keys_file=str(keys_file), api_keys_json=""):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert f"2 from API_KEYS_FILE ({keys_file})" in result.detail

    def test_a_raw_key_from_the_file_authenticates(self, tmp_path, monkeypatch):
        raw = "r" * 40
        path = _write(tmp_path / "keys.json", json.dumps([_entry("analyst-1", raw)]))
        monkeypatch.setenv("VERIFY_API_KEY", raw)
        with override_settings(auth_required=True, api_keys_file=str(path)):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert "analyst-1" in result.detail

    def test_a_bad_file_fails_naming_the_file(self, tmp_path):
        missing = tmp_path / "missing.json"
        with override_settings(auth_required=True, api_keys_file=str(missing)):
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert str(missing) in result.detail
        assert "the server would refuse to start" in result.detail

    def test_both_sources_set_fails(self, keys_file):
        raw = json.dumps([_entry("a", "c" * 40)])
        with override_settings(auth_required=True, api_keys_file=str(keys_file), api_keys_json=raw):
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert "use one" in result.detail

    def test_no_keys_message_names_both_sources(self):
        with override_settings(auth_required=True, api_keys_file="", api_keys_json=""):
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert "API_KEYS_FILE" in result.detail and "API_KEYS_JSON" in result.detail

    def test_a_file_with_no_keys_fails_naming_the_file_when_all_revoked(self, keys_file):
        with override_settings(auth_required=True, api_keys_file=str(keys_file)):
            key_store.bootstrap_from_env()
            for key_hash in (_sha256("a" * 40), _sha256("b" * 40)):
                key_store.revoke_key(key_hash)
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert f"every key in API_KEYS_FILE ({keys_file}) is revoked" in result.detail

    def test_unreadable_application_database_says_which_source_was_counted(
        self, keys_file, monkeypatch
    ):
        def _boom():
            raise RuntimeError("connection refused")

        monkeypatch.setattr(key_store, "get_active_principals", _boom)
        monkeypatch.delenv("VERIFY_API_KEY", raising=False)
        with override_settings(auth_required=True, api_keys_file=str(keys_file)):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert f"counted API_KEYS_FILE ({keys_file}) only)" in result.detail


class TestServerStartup:
    @staticmethod
    def _start_error(**settings) -> str:
        import api.server as server_module

        async def _start():
            async with server_module.lifespan(server_module.app):
                pass  # pragma: no cover - must not be reached

        with override_settings(
            openai_model="llama3",
            db_connection_url=(
                "mssql+pyodbc://prod-db-host:1433/RealDB"
                "?driver=ODBC+Driver+17+for+SQL+Server"
            ),
            auth_required=True,
            **settings,
        ):
            with pytest.raises(RuntimeError) as excinfo:
                asyncio.run(_start())
        return str(excinfo.value)

    def test_no_usable_key_message_names_both_sources(self):
        text = self._start_error(api_keys_json="", api_keys_file="")
        assert "API_KEYS_FILE, API_KEYS_JSON or the application database" in text
        assert "project_config/api_keys.json" in text

    def test_both_sources_set_refuses_to_start(self, keys_file):
        raw = json.dumps([_entry("a", "c" * 40)])
        text = self._start_error(api_keys_json=raw, api_keys_file=str(keys_file))
        assert text.startswith("Invalid API key configuration: Both API_KEYS_JSON and API_KEYS_FILE")

    def test_a_missing_file_refuses_to_start_naming_the_path(self, tmp_path):
        missing = tmp_path / "missing.json"
        text = self._start_error(api_keys_json="", api_keys_file=str(missing))
        assert text.startswith("Invalid API key configuration: ")
        assert str(missing) in text


class TestIssueApiKeyOutput:
    def test_step_two_points_at_the_file_and_the_json_variable(self, capsys):
        assert issue_main(["--id", "analyst-1", "--name", "Analyst One"]) == 0
        out = capsys.readouterr().out
        step_two = out[out.index("STEP 2 OF 2"):]
        assert "API_KEYS_FILE" in step_two
        assert "project_config/api_keys.json" in step_two
        assert "API_KEYS_JSON" in step_two
