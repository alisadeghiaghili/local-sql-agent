# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``nolock`` in ``datasources.yaml``: parsing, resolution and the dialect check.

Same conventions as ``tests/test_datasources.py``: every test writes its own
``datasources.yaml`` under ``tmp_path`` and passes its own ``Settings``.
"""

from __future__ import annotations

import os
import re

import pytest
import yaml

import config as cfg
from config import Settings
from database.connection_identity import with_application_name
from database.datasources import (
    DataSource,
    DataSourceConfigError,
    DataSourceDefinition,
    build_url,
    datasource_nolock,
    get_datasource,
    get_datasources,
    load_datasources_config,
    reset_datasources_cache,
    validate_datasource_urls,
    validate_datasources_yaml_text,
)
from security.dialects import DIALECT_PROFILES

_URL = "mssql+pyodbc://svc@main-host:1433/DB?driver=ODBC+Driver+17+for+SQL+Server"


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_datasources_cache()
    yield
    reset_datasources_cache()


def _structured(**overrides):
    entry = {
        "host": "db1.example.test",
        "database": "SalesDW",
        "username": "nlq_reader",
        "password_env": "DB_PASSWORD_SALES",
    }
    entry.update(overrides)
    return entry


def _write(tmp_path, sources: dict, default: str | None = None, dialect: str = "tsql"):
    doc: dict = {"datasources": sources}
    if default is not None:
        doc["default"] = default
    (tmp_path / "datasources.yaml").write_text(yaml.dump(doc, sort_keys=False), encoding="utf-8")
    return Settings(project_config_dir=str(tmp_path), sql_dialect=dialect)


def _refused(entry: dict) -> str:
    with pytest.raises(ValueError) as info:
        validate_datasources_yaml_text(yaml.dump({"datasources": {"sales": entry}}))
    return str(info.value)


# ---------------------------------------------------------------------------
# The field
# ---------------------------------------------------------------------------

class TestNolockField:
    def test_defaults_to_false_in_both_forms(self):
        parsed = validate_datasources_yaml_text(yaml.dump({"datasources": {
            "structured": _structured(),
            "legacy": {"url_env": "DB_URL_LEGACY"},
        }, "default": "structured"}, sort_keys=False)).datasources
        assert parsed["structured"].nolock is False
        assert parsed["legacy"].nolock is False

    def test_true_in_the_structured_form(self):
        parsed = validate_datasources_yaml_text(yaml.dump({"datasources": {
            "sales": _structured(nolock=True),
        }}))
        assert parsed.datasources["sales"].nolock is True

    def test_true_in_the_legacy_url_env_form(self):
        parsed = validate_datasources_yaml_text(yaml.dump({"datasources": {
            "sales": {"url_env": "DB_URL_SALES", "nolock": True},
        }}))
        assert parsed.datasources["sales"].nolock is True

    def test_false_written_out_is_accepted(self):
        parsed = validate_datasources_yaml_text(yaml.dump({"datasources": {
            "sales": _structured(nolock=False),
        }}))
        assert parsed.datasources["sales"].nolock is False

    def test_it_does_not_make_a_legacy_source_look_structured(self):
        # nolock is not one of the connection fields, so it never clashes
        # with url_env.
        DataSourceDefinition(url_env="DB_URL_SALES", nolock=True)

    def test_the_yaml_text_true_and_false_spellings(self):
        for text, expected in (("true", True), ("false", False), ("yes", True), ("no", False)):
            parsed = validate_datasources_yaml_text(
                f"datasources:\n  main:\n    url_env: DB_URL_MAIN\n    nolock: {text}\n"
            )
            assert parsed.datasources["main"].nolock is expected

    @pytest.mark.parametrize("value", [
        "true", "yes", "1", "on", 1, 0, 2.5, None, [], ["true"], {"a": 1},
    ])
    def test_anything_but_a_boolean_is_refused_naming_the_source(self, value):
        message = _refused(_structured(nolock=value))
        assert "datasources -> sales -> nolock" in message
        assert "nolock must be true or false" in message

    def test_the_legacy_form_is_checked_the_same_way(self):
        message = _refused({"url_env": "DB_URL_SALES", "nolock": "yes please"})
        assert "datasources -> sales -> nolock" in message
        assert "'yes please'" in message

    def test_an_unquoted_yaml_scalar_that_is_not_a_boolean_is_refused(self):
        with pytest.raises(ValueError, match="datasources -> main -> nolock"):
            validate_datasources_yaml_text(
                "datasources:\n  main:\n    url_env: DB_URL_MAIN\n    nolock: maybe\n"
            )


# ---------------------------------------------------------------------------
# The resolved source
# ---------------------------------------------------------------------------

class TestResolvedSource:
    def test_data_source_defaults_to_no_nolock(self):
        source = DataSource("sales", "mssql+pyodbc://u:secret@h/db", None, "tsql", "app")
        assert source.nolock is False

    def test_get_datasource_carries_the_flag_for_a_structured_source(self, tmp_path, monkeypatch):
        settings = _write(tmp_path, {
            "hinted": _structured(nolock=True),
            "plain": _structured(),
        }, default="plain")
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        assert get_datasource("hinted", settings).nolock is True
        assert get_datasource("plain", settings).nolock is False

    def test_get_datasource_carries_the_flag_for_a_legacy_source(self, tmp_path, monkeypatch):
        settings = _write(tmp_path, {
            "hinted": {"url_env": "DB_URL_HINTED", "nolock": True},
            "plain": {"url_env": "DB_URL_PLAIN"},
        }, default="plain")
        monkeypatch.setenv("DB_URL_HINTED", _URL)
        monkeypatch.setenv("DB_URL_PLAIN", _URL)
        assert [s.nolock for s in get_datasources(settings)] == [False, True]

    def test_the_default_source_is_used_for_none(self, tmp_path, monkeypatch):
        settings = _write(tmp_path, {"only": {"url_env": "DB_URL_ONLY", "nolock": True}})
        monkeypatch.setenv("DB_URL_ONLY", _URL)
        assert get_datasource(None, settings).nolock is True

    def test_the_single_source_fallback_never_hints(self, tmp_path):
        settings = Settings(project_config_dir=str(tmp_path), db_connection_url=_URL)
        assert get_datasource(None, settings).nolock is False

    def test_the_flag_is_not_in_the_repr(self):
        source = DataSource("sales", "mssql+pyodbc://u:secret@h/db", None, "tsql", "app", nolock=True)
        assert "secret" not in repr(source)


class TestDatasourceNolock:
    """The executor's accessor: the same flag, without resolving credentials."""

    def test_reads_the_flag_without_needing_the_secrets(self, tmp_path, monkeypatch):
        settings = _write(tmp_path, {
            "hinted": _structured(nolock=True),
            "plain": _structured(password_env="DB_PASSWORD_OTHER"),
        }, default="plain")
        monkeypatch.delenv("DB_PASSWORD_SALES", raising=False)
        monkeypatch.delenv("DB_PASSWORD_OTHER", raising=False)
        assert datasource_nolock("hinted", settings) is True
        assert datasource_nolock("plain", settings) is False
        assert datasource_nolock(None, settings) is False

    def test_agrees_with_get_datasource(self, tmp_path, monkeypatch):
        settings = _write(tmp_path, {
            "hinted": {"url_env": "DB_URL_HINTED", "nolock": True},
            "plain": _structured(),
        }, default="plain")
        monkeypatch.setenv("DB_URL_HINTED", _URL)
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        for name in ("hinted", "plain"):
            assert datasource_nolock(name, settings) is get_datasource(name, settings).nolock

    def test_no_file_and_unknown_names_are_false(self, tmp_path):
        assert datasource_nolock(None, Settings(project_config_dir=str(tmp_path))) is False
        settings = _write(tmp_path, {"main": {"url_env": "DB_URL_MAIN", "nolock": True}})
        assert datasource_nolock("nope", settings) is False

    def test_follows_the_process_wide_settings_by_default(self, tmp_path):
        _write(tmp_path, {"main": {"url_env": "DB_URL_MAIN", "nolock": True}})
        with cfg.override_settings(project_config_dir=str(tmp_path)):
            assert datasource_nolock("main") is True
            assert datasource_nolock() is True


# ---------------------------------------------------------------------------
# Table hints are T-SQL only
# ---------------------------------------------------------------------------

class TestRefusedOutsideTsql:
    @pytest.mark.parametrize("dialect", ["postgres", "mysql", "sqlite"])
    def test_loading_is_refused_naming_the_source_and_the_dialect(self, tmp_path, dialect):
        settings = _write(tmp_path, {
            "plain": {"url_env": "DB_URL_PLAIN"},
            "hinted": {"url_env": "DB_URL_HINTED", "nolock": True},
        }, default="plain", dialect=dialect)
        with pytest.raises(DataSourceConfigError) as info:
            load_datasources_config(settings)
        message = str(info.value)
        assert "'hinted'" in message
        assert "nolock" in message
        assert repr(dialect) in message
        assert "T-SQL only" in message
        assert "'plain'" not in message

    def test_the_structured_form_is_refused_too(self, tmp_path):
        settings = _write(tmp_path, {"sales": _structured(nolock=True)}, dialect="postgres")
        with pytest.raises(DataSourceConfigError, match="'sales'.*T-SQL only"):
            load_datasources_config(settings)

    def test_every_consumer_of_the_file_refuses_it(self, tmp_path, monkeypatch):
        settings = _write(tmp_path, {"main": {"url_env": "DB_URL_MAIN", "nolock": True}},
                          dialect="postgres")
        monkeypatch.setenv("DB_URL_MAIN", "postgresql://u:pw@h/db")
        with pytest.raises(ValueError, match="T-SQL only"):
            get_datasource("main", settings)
        with pytest.raises(ValueError, match="T-SQL only"):
            datasource_nolock("main", settings)

    def test_start_up_validation_refuses_it_before_checking_urls(self, tmp_path, monkeypatch):
        settings = _write(tmp_path, {"main": {"url_env": "DB_URL_MAIN", "nolock": True}},
                          dialect="postgres")
        monkeypatch.setenv("DB_URL_MAIN", "postgresql://u:pw@h/db")
        seen = []
        with pytest.raises(ValueError, match="nolock.*T-SQL only"):
            validate_datasource_urls(lambda *a: seen.append(a), settings)
        assert seen == []

    def test_settings_validate_refuses_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DB_URL_MAIN", "postgresql://u:pw@h/db")
        settings = _write(tmp_path, {"main": {"url_env": "DB_URL_MAIN", "nolock": True}},
                          dialect="postgres")
        with pytest.raises(ValueError, match="T-SQL only"):
            settings.validate()

    def test_tsql_accepts_it(self, tmp_path, monkeypatch):
        settings = _write(tmp_path, {"main": {"url_env": "DB_URL_MAIN", "nolock": True}})
        monkeypatch.setenv("DB_URL_MAIN", _URL)
        assert load_datasources_config(settings).datasources["main"].nolock is True
        seen = []
        validate_datasource_urls(lambda label, url, dialect: seen.append(label), settings)
        assert seen == ["DB_URL_MAIN"]

    @pytest.mark.parametrize("dialect", ["postgres", "mysql", "sqlite"])
    def test_other_dialects_are_unaffected_while_nolock_is_off(self, tmp_path, dialect):
        settings = _write(tmp_path, {
            "main": {"url_env": "DB_URL_MAIN"},
            "other": {"url_env": "DB_URL_OTHER", "nolock": False},
        }, default="main", dialect=dialect)
        assert load_datasources_config(settings) is not None

    def test_only_tsql_supports_table_hints(self):
        assert {name for name, p in DIALECT_PROFILES.items() if p.supports_table_hints} == {"tsql"}

    def test_an_unknown_dialect_is_refused_too(self, tmp_path):
        settings = _write(tmp_path, {"main": {"url_env": "DB_URL_MAIN", "nolock": True}},
                          dialect="oracle")
        with pytest.raises(DataSourceConfigError, match="T-SQL only"):
            load_datasources_config(settings)


# ---------------------------------------------------------------------------
# The example file, and the APP keyword it documents
# ---------------------------------------------------------------------------

_EXAMPLE = os.path.join(
    os.path.dirname(__file__), "..", "project_config.example", "datasources.example.yaml",
)


def _example_text() -> str:
    with open(_EXAMPLE, encoding="utf-8") as fh:
        return fh.read()


class TestExampleFile:
    def test_nolock_is_documented_and_off_as_shipped(self):
        text = _example_text()
        assert re.search(r"^#\s+nolock\s+: ", text, flags=re.MULTILINE)
        sales = validate_datasources_yaml_text(text).datasources["sales"]
        assert sales.nolock is False

    def test_the_commented_nolock_and_app_lines_are_valid_when_uncommented(self):
        text = _example_text()
        uncommented = re.sub(r"^(\s+)# (nolock: true)", r"\1\2", text, flags=re.MULTILINE)
        uncommented = re.sub(r"^(\s+)# (APP: local-sql-agent)", r"\1\2", uncommented,
                             flags=re.MULTILINE)
        assert "\n    nolock: true" in uncommented and "\n    nolock: true" not in text
        assert "\n      APP: local-sql-agent" in uncommented
        sales = validate_datasources_yaml_text(uncommented).datasources["sales"]
        assert sales.nolock is True
        assert sales.options == {"TrustServerCertificate": "yes", "APP": "local-sql-agent"}

    def test_app_is_an_accepted_option_and_reaches_the_odbc_url(self):
        definition = DataSourceDefinition(**_structured(options={"APP": "local-sql-agent"}))
        assert definition.options == {"APP": "local-sql-agent"}
        url = build_url("sales", definition, "tsql", {"DB_PASSWORD_SALES": "pw"})
        assert dict(url.query)["APP"] == "local-sql-agent"

    def test_an_app_from_options_is_not_replaced_by_the_default_application_name(self):
        definition = DataSourceDefinition(**_structured(options={"APP": "reporting-nlq"}))
        url = build_url("sales", definition, "tsql", {"DB_PASSWORD_SALES": "pw"})
        rendered = url.render_as_string(hide_password=False)
        assert with_application_name(rendered, "local-sql-agent") == rendered
