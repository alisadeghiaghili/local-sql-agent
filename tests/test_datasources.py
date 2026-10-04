# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for ``database.datasources``.

Every test either builds a ``Settings`` instance directly (mirroring
``tests/test_config.py``'s own style) or uses ``config.override_settings``
+ ``monkeypatch.setenv``, so nothing here ever touches the real,
git-ignored ``project_config/`` or a real environment variable outside the
test. ``reset_datasources_cache()`` is called around every test that
writes its own ``datasources.yaml`` -- the loader caches on
``(path, mtime)`` and this suite reuses paths across tmp_path fixtures
often enough that a stale cache entry would otherwise leak between tests.
"""

from __future__ import annotations

import os
import time

import pytest
import yaml
from sqlalchemy.engine import make_url

import config as cfg
from config import Settings, _check_warehouse_url, override_settings
from database.datasources import (
    DEFAULT_DATASOURCE,
    DataSource,
    DataSourceConfigError,
    DataSourceDefinition,
    DataSourcesConfig,
    UnknownDataSourceError,
    apply_db_password,
    build_url,
    check_table_datasources,
    datasource_names,
    datasources_path,
    default_datasource_name,
    get_datasource,
    get_datasources,
    load_datasources_config,
    reset_datasources_cache,
    table_datasources,
    validate_datasource_urls,
    validate_datasources_yaml_text,
)

_MAIN_URL = "mssql+pyodbc://svc@main-host:1433/DB?driver=ODBC+Driver+17+for+SQL+Server"
_ARCHIVE_URL = "mssql+pyodbc://svc@archive-host:1433/DB?driver=ODBC+Driver+17+for+SQL+Server"


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_datasources_cache()
    yield
    reset_datasources_cache()


def _dump(doc: dict) -> str:
    return yaml.dump(doc, sort_keys=False)


# ---------------------------------------------------------------------------
# YAML validation
# ---------------------------------------------------------------------------

class TestValidateDatasourcesYamlText:
    def test_minimal_single_source_needs_no_explicit_default(self):
        cfg_parsed = validate_datasources_yaml_text(
            "datasources:\n  main:\n    url_env: DB_URL_MAIN\n"
        )
        assert cfg_parsed.default_name == "main"

    def test_url_env_rejects_a_connection_string(self):
        with pytest.raises(ValueError, match="url_env"):
            validate_datasources_yaml_text(_dump({
                "datasources": {"main": {"url_env": "mssql://user:pw@host/db"}},
            }))

    def test_url_env_rejects_a_non_identifier(self):
        with pytest.raises(ValueError, match="datasources.yaml"):
            validate_datasources_yaml_text(_dump({
                "datasources": {"main": {"url_env": "not a valid env name"}},
            }))

    def test_default_required_when_more_than_one_source(self):
        with pytest.raises(ValueError, match="default"):
            validate_datasources_yaml_text(_dump({
                "datasources": {
                    "main": {"url_env": "DB_URL_MAIN"},
                    "archive": {"url_env": "DB_URL_ARCHIVE"},
                },
            }))

    def test_unknown_default_is_rejected(self):
        with pytest.raises(ValueError, match="not one of the listed"):
            validate_datasources_yaml_text(_dump({
                "default": "nope",
                "datasources": {"main": {"url_env": "DB_URL_MAIN"}},
            }))

    def test_bad_source_name_is_rejected(self):
        with pytest.raises(ValueError, match="must start with a letter"):
            validate_datasources_yaml_text(_dump({
                "datasources": {"1bad": {"url_env": "DB_URL_MAIN"}},
            }))

    def test_source_name_with_a_dot_is_rejected(self):
        """Dots would collide with the ``source/schema`` prefix
        ``schema_data.drift`` builds for its per-source scan report."""
        with pytest.raises(ValueError, match="must start with a letter"):
            validate_datasources_yaml_text(_dump({
                "datasources": {"main.db": {"url_env": "DB_URL_MAIN"}},
            }))

    def test_extra_key_on_the_datasource_entry_is_rejected(self):
        with pytest.raises(ValueError):
            validate_datasources_yaml_text(_dump({
                "datasources": {
                    "main": {"url_env": "DB_URL_MAIN", "not_a_real_field": 1},
                },
            }))

    def test_extra_top_level_key_is_rejected(self):
        with pytest.raises(ValueError):
            validate_datasources_yaml_text(_dump({
                "datasources": {"main": {"url_env": "DB_URL_MAIN"}},
                "not_a_real_top_level_key": 1,
            }))

    def test_empty_datasources_mapping_is_rejected(self):
        with pytest.raises(ValueError):
            validate_datasources_yaml_text("datasources: {}\n")

    def test_not_valid_yaml_is_rejected(self):
        with pytest.raises(ValueError, match="not valid YAML"):
            validate_datasources_yaml_text("datasources: [this is not, a mapping")

    def test_description_and_dialect_and_application_name_round_trip(self):
        parsed = validate_datasources_yaml_text(_dump({
            "datasources": {
                "main": {
                    "url_env": "DB_URL_MAIN",
                    "description": "Main warehouse",
                    "dialect": "tsql",
                    "application_name": "custom-app",
                },
            },
        }))
        definition = parsed.datasources["main"]
        assert definition.description == "Main warehouse"
        assert definition.dialect == "tsql"
        assert definition.application_name == "custom-app"


# ---------------------------------------------------------------------------
# DataSource -- repr never prints the connection string
# ---------------------------------------------------------------------------

class TestDataSourceRepr:
    def test_repr_never_prints_the_url(self):
        source = DataSource(
            name="main", url="mssql+pyodbc://user:hunter2@host/db",
            url_env="DB_URL_MAIN", dialect="tsql", application_name="app",
        )
        rendered = repr(source)
        assert "hunter2" not in rendered
        assert "user" not in rendered
        assert "DB_URL_MAIN" in rendered
        assert "main" in rendered


# ---------------------------------------------------------------------------
# Loading and the single-source fallback
# ---------------------------------------------------------------------------

class TestLoadDatasourcesConfig:
    def test_none_when_the_file_does_not_exist(self, tmp_path):
        settings = Settings(project_config_dir=str(tmp_path))
        assert load_datasources_config(settings) is None

    def test_datasources_path_is_under_project_config_dir(self, tmp_path):
        settings = Settings(project_config_dir=str(tmp_path))
        assert datasources_path(settings) == tmp_path / "datasources.yaml"

    def test_invalid_file_on_disk_raises(self, tmp_path):
        (tmp_path / "datasources.yaml").write_text("datasources: {}\n", encoding="utf-8")
        settings = Settings(project_config_dir=str(tmp_path))
        with pytest.raises(ValueError):
            load_datasources_config(settings)

    def test_mtime_change_is_picked_up_without_an_explicit_reset(self, tmp_path):
        """The loader is cached on ``(path, mtime_ns)`` -- an edited file
        must be re-read on the very next call, with no call to
        :func:`reset_datasources_cache` in between (that function exists
        for tests that swap *paths*, not for picking up an on-disk edit)."""
        path = tmp_path / "datasources.yaml"
        path.write_text(_dump({
            "datasources": {"main": {"url_env": "DB_URL_MAIN"}},
        }), encoding="utf-8")
        settings = Settings(project_config_dir=str(tmp_path))
        first = load_datasources_config(settings)
        assert first.default_name == "main"

        # Force a distinct mtime -- two writes in quick succession can
        # otherwise land on the same nanosecond-resolution timestamp on a
        # coarser filesystem.
        os.utime(path, ns=(path.stat().st_mtime_ns + 10_000_000,) * 2)
        path.write_text(_dump({
            "default": "archive",
            "datasources": {
                "main": {"url_env": "DB_URL_MAIN"},
                "archive": {"url_env": "DB_URL_ARCHIVE"},
            },
        }), encoding="utf-8")
        os.utime(path, ns=(path.stat().st_mtime_ns + 20_000_000,) * 2)

        second = load_datasources_config(settings)
        assert second.default_name == "archive"


# ---------------------------------------------------------------------------
# default_datasource_name / datasource_names
# ---------------------------------------------------------------------------

class TestDefaultAndNames:
    def test_default_name_and_names_without_a_file(self, tmp_path):
        settings = Settings(project_config_dir=str(tmp_path))
        assert default_datasource_name(settings) == DEFAULT_DATASOURCE
        assert datasource_names(settings) == (DEFAULT_DATASOURCE,)

    def test_names_default_first(self, tmp_path):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "default": "archive",
            "datasources": {
                "main": {"url_env": "DB_URL_MAIN"},
                "archive": {"url_env": "DB_URL_ARCHIVE"},
            },
        }), encoding="utf-8")
        settings = Settings(project_config_dir=str(tmp_path))
        assert datasource_names(settings) == ("archive", "main")


# ---------------------------------------------------------------------------
# get_datasource / get_datasources -- env resolution
# ---------------------------------------------------------------------------

class TestGetDatasource:
    def test_fallback_uses_db_connection_url(self, tmp_path):
        settings = Settings(
            project_config_dir=str(tmp_path), db_connection_url=_MAIN_URL,
            sql_dialect="tsql", db_application_name="local-sql-agent",
        )
        source = get_datasource(None, settings)
        assert source.name == DEFAULT_DATASOURCE
        assert source.url == _MAIN_URL
        assert source.url_env == "DB_CONNECTION_URL"
        assert source.dialect == "tsql"

    def test_fallback_rejects_a_named_source(self, tmp_path):
        settings = Settings(project_config_dir=str(tmp_path))
        with pytest.raises(UnknownDataSourceError):
            get_datasource("archive", settings)

    def test_env_var_is_read_fresh_on_every_call(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {"main": {"url_env": "DB_URL_MAIN"}},
        }), encoding="utf-8")
        settings = Settings(project_config_dir=str(tmp_path), sql_dialect="tsql")

        monkeypatch.setenv("DB_URL_MAIN", _MAIN_URL)
        assert get_datasource("main", settings).url == _MAIN_URL

        monkeypatch.setenv("DB_URL_MAIN", _ARCHIVE_URL)
        assert get_datasource("main", settings).url == _ARCHIVE_URL

    def test_unset_env_var_resolves_to_an_empty_url(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {"main": {"url_env": "DB_URL_DOES_NOT_EXIST"}},
        }), encoding="utf-8")
        monkeypatch.delenv("DB_URL_DOES_NOT_EXIST", raising=False)
        settings = Settings(project_config_dir=str(tmp_path))
        assert get_datasource("main", settings).url == ""

    def test_unknown_named_source_raises(self, tmp_path):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {"main": {"url_env": "DB_URL_MAIN"}},
        }), encoding="utf-8")
        settings = Settings(project_config_dir=str(tmp_path))
        with pytest.raises(UnknownDataSourceError, match="unknown"):
            get_datasource("does-not-exist", settings)

    def test_per_source_dialect_and_application_name_override(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {
                "main": {
                    "url_env": "DB_URL_MAIN",
                    "dialect": "tsql",
                    "application_name": "custom-app",
                },
            },
        }), encoding="utf-8")
        monkeypatch.setenv("DB_URL_MAIN", _MAIN_URL)
        settings = Settings(
            project_config_dir=str(tmp_path), sql_dialect="tsql",
            db_application_name="local-sql-agent",
        )
        source = get_datasource("main", settings)
        assert source.dialect == "tsql"
        assert source.application_name == "custom-app"

    def test_missing_per_source_settings_fall_back_to_deployment_defaults(
        self, tmp_path, monkeypatch,
    ):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {"main": {"url_env": "DB_URL_MAIN"}},
        }), encoding="utf-8")
        monkeypatch.setenv("DB_URL_MAIN", _MAIN_URL)
        settings = Settings(
            project_config_dir=str(tmp_path), sql_dialect="tsql",
            db_application_name="local-sql-agent",
        )
        source = get_datasource("main", settings)
        assert source.dialect == "tsql"
        assert source.application_name == "local-sql-agent"

    def test_get_datasources_resolves_every_source_default_first(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "default": "archive",
            "datasources": {
                "main": {"url_env": "DB_URL_MAIN"},
                "archive": {"url_env": "DB_URL_ARCHIVE"},
            },
        }), encoding="utf-8")
        monkeypatch.setenv("DB_URL_MAIN", _MAIN_URL)
        monkeypatch.setenv("DB_URL_ARCHIVE", _ARCHIVE_URL)
        settings = Settings(project_config_dir=str(tmp_path), sql_dialect="tsql")
        sources = get_datasources(settings)
        assert [s.name for s in sources] == ["archive", "main"]
        assert sources[0].url == _ARCHIVE_URL
        assert sources[1].url == _MAIN_URL


# ---------------------------------------------------------------------------
# table_datasources / check_table_datasources
# ---------------------------------------------------------------------------

class TestCheckTableDatasources:
    def test_passes_when_every_assignment_is_known(self, tmp_path):
        settings = Settings(project_config_dir=str(tmp_path))
        with override_settings(project_config_dir=str(tmp_path)):
            check_table_datasources({"Order": "", "Customer": DEFAULT_DATASOURCE})

    def test_raises_naming_the_offending_table_and_source(self, tmp_path):
        with override_settings(project_config_dir=str(tmp_path)):
            with pytest.raises(ValueError, match="archive"):
                check_table_datasources({"Order": "archive"})

    def test_empty_string_resolves_to_the_default_source(self, tmp_path):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "default": "main",
            "datasources": {
                "main": {"url_env": "DB_URL_MAIN"},
                "archive": {"url_env": "DB_URL_ARCHIVE"},
            },
        }), encoding="utf-8")
        with override_settings(project_config_dir=str(tmp_path)):
            check_table_datasources({"Order": ""})  # -> "main", configured

    def test_default_argument_reads_schema_yaml_via_registry(self, tmp_path):
        """With no ``assignments`` given, this reads
        ``schema_data.registry.table_datasources()`` -- which is exactly
        why every fixture that changes ``schema.yaml`` must also clear
        ``schema_data.registry._cache`` (see this module's own fixture in
        the multi-source test files, and this test's use of the real,
        already-loaded example schema instead)."""
        with override_settings(project_config_dir="project_config.example"):
            check_table_datasources()  # every table there has no datasource key


class TestTableDatasources:
    def test_uses_schema_data_registry(self, tmp_path):
        import schema_data.registry as registry_module

        registry_module._cache.clear()
        try:
            with override_settings(project_config_dir="project_config.example"):
                assignments = table_datasources()
        finally:
            registry_module._cache.clear()
        assert assignments  # non-empty
        assert all(v == DEFAULT_DATASOURCE for v in assignments.values())


# ---------------------------------------------------------------------------
# validate_datasource_urls
# ---------------------------------------------------------------------------

def _real_check_url(label, url, dialect):
    _check_warehouse_url(label, url, dialect, {"change_me", "your_password_here"})


class TestValidateDatasourceUrls:
    def test_calls_check_url_for_every_source(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "default": "main",
            "datasources": {
                "main": {"url_env": "DB_URL_MAIN"},
                "archive": {"url_env": "DB_URL_ARCHIVE"},
            },
        }), encoding="utf-8")
        monkeypatch.setenv("DB_URL_MAIN", _MAIN_URL)
        monkeypatch.setenv("DB_URL_ARCHIVE", _ARCHIVE_URL)
        settings = Settings(project_config_dir=str(tmp_path), sql_dialect="tsql")

        seen = []
        validate_datasource_urls(lambda label, url, dialect: seen.append(label), settings)
        assert seen == ["DB_URL_MAIN", "DB_URL_ARCHIVE"]

    def test_unset_env_var_is_refused(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {"main": {"url_env": "DB_URL_MISSING"}},
        }), encoding="utf-8")
        monkeypatch.delenv("DB_URL_MISSING", raising=False)
        settings = Settings(project_config_dir=str(tmp_path), sql_dialect="tsql")
        with pytest.raises(ValueError, match="DB_URL_MISSING"):
            validate_datasource_urls(_real_check_url, settings)

    def test_placeholder_value_is_refused(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {"main": {"url_env": "DB_URL_MAIN"}},
        }), encoding="utf-8")
        monkeypatch.setenv("DB_URL_MAIN", "change_me")
        settings = Settings(project_config_dir=str(tmp_path), sql_dialect="tsql")
        with pytest.raises(ValueError, match="DB_URL_MAIN"):
            validate_datasource_urls(_real_check_url, settings)

    def test_url_backend_dialect_mismatch_is_refused(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {"main": {"url_env": "DB_URL_MAIN"}},
        }), encoding="utf-8")
        monkeypatch.setenv("DB_URL_MAIN", "postgresql://user:pw@host/db")
        settings = Settings(project_config_dir=str(tmp_path), sql_dialect="tsql")
        with pytest.raises(ValueError, match="does not match"):
            validate_datasource_urls(_real_check_url, settings)

    def test_per_source_dialect_field_mismatching_sql_dialect_is_refused(
        self, tmp_path, monkeypatch,
    ):
        """The ``dialect:`` key on a source entry itself, not the URL's own
        backend -- refused before ``check_url`` is even called (mixed
        dialects are not supported yet; see the module docstring's
        "Scope" section)."""
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {
                "main": {"url_env": "DB_URL_MAIN", "dialect": "postgres"},
            },
        }), encoding="utf-8")
        monkeypatch.setenv("DB_URL_MAIN", _MAIN_URL)
        settings = Settings(project_config_dir=str(tmp_path), sql_dialect="tsql")

        called = []
        with pytest.raises(ValueError, match="declares dialect"):
            validate_datasource_urls(
                lambda *a: called.append(a), settings,
            )
        assert called == []

    def test_single_source_fallback_still_runs_the_check(self, tmp_path):
        settings = Settings(
            project_config_dir=str(tmp_path), db_connection_url="",
            sql_dialect="tsql",
        )
        with pytest.raises(ValueError, match="DB_CONNECTION_URL"):
            validate_datasource_urls(_real_check_url, settings)


# ---------------------------------------------------------------------------
# Settings.validate() integration
# ---------------------------------------------------------------------------

class TestSettingsValidateWithDatasourcesYaml:
    def test_db_connection_url_is_not_required_once_datasources_yaml_exists(
        self, tmp_path, monkeypatch,
    ):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {"main": {"url_env": "DB_URL_MAIN"}},
        }), encoding="utf-8")
        monkeypatch.setenv("DB_URL_MAIN", _MAIN_URL)
        settings = Settings(
            openai_model="gpt-oss-20b",
            db_connection_url="",  # would fail Settings.validate() alone
            project_config_dir=str(tmp_path),
            sql_dialect="tsql",
        )
        settings.validate()  # must not raise

    def test_a_missing_named_source_env_var_still_fails_validate(
        self, tmp_path, monkeypatch,
    ):
        (tmp_path / "datasources.yaml").write_text(_dump({
            "datasources": {"main": {"url_env": "DB_URL_MAIN"}},
        }), encoding="utf-8")
        monkeypatch.delenv("DB_URL_MAIN", raising=False)
        settings = Settings(
            openai_model="gpt-oss-20b",
            db_connection_url="",
            project_config_dir=str(tmp_path),
            sql_dialect="tsql",
        )
        with pytest.raises(ValueError, match="DB_URL_MAIN"):
            settings.validate()


# ===========================================================================
# Structured sources: host / database in the YAML, the secret in the environment
# ===========================================================================

#: Every character that has a meaning in a URL or an ODBC connection
#: string, plus spaces and a lone percent sign.
_NASTY_PASSWORDS = [
    "plain",
    "p@ss",
    "100%",
    "%40",
    "a]b",
    "a:b",
    "a/b",
    "a?b",
    "a#b",
    "a&b",
    "a=b",
    "a+b",
    "a;b",
    "a b",
    " leading and trailing ",
    "@ % ] : / ? # & = + ; and spaces",
    "p@ss%41]:/?#&=+; w d%",
    "{braces}",
    "back\\slash",
    "quo'te\"d",
    "ünï-çödé",
]


def _odbc_value(connection_string: str, keyword: str) -> str:
    """Read *keyword*'s value the way an ODBC driver does: a value in braces
    runs to the first ``}`` that is not doubled; any other ends at ``;``."""
    start = connection_string.index(f"{keyword}=") + len(keyword) + 1
    rest = connection_string[start:]
    if not rest.startswith("{"):
        return rest.split(";", 1)[0]
    value, i = "", 1
    while i < len(rest):
        if rest[i] == "}":
            if rest[i + 1:i + 2] != "}":
                return value
            i += 1
        value += rest[i]
        i += 1
    raise AssertionError("unterminated braced value")


def _structured(**overrides):
    entry = {
        "host": "db1.example.test",
        "database": "SalesDW",
        "username": "nlq_reader",
        "password_env": "DB_PASSWORD_SALES",
    }
    entry.update(overrides)
    return {k: v for k, v in entry.items() if v is not None}


def _write_sources(tmp_path, sources: dict, default: str | None = None):
    doc = {"datasources": sources}
    if default is not None:
        doc["default"] = default
    (tmp_path / "datasources.yaml").write_text(_dump(doc), encoding="utf-8")
    return Settings(project_config_dir=str(tmp_path), sql_dialect="tsql")


def _definition(**overrides) -> DataSourceDefinition:
    return DataSourceDefinition(**_structured(**overrides))


def _refused(entry: dict) -> str:
    with pytest.raises(ValueError) as info:
        validate_datasources_yaml_text(_dump({"datasources": {"auction": entry}}))
    return str(info.value)


class TestStructuredParsing:
    def test_minimal_structured_source(self):
        parsed = validate_datasources_yaml_text(_dump({
            "datasources": {"auction": _structured()},
        }))
        definition = parsed.datasources["auction"]
        assert (definition.host, definition.database) == ("db1.example.test", "SalesDW")
        assert definition.username == "nlq_reader"
        assert definition.password_env == "DB_PASSWORD_SALES"
        assert definition.url_env is None
        assert definition.port is None
        assert definition.driver is None
        assert definition.trusted_connection is False
        assert definition.options == {}

    def test_every_field_round_trips(self):
        definition = _definition(
            description="Auction warehouse", port=1444, driver="ODBC Driver 17 for SQL Server",
            dialect="tsql", application_name="app", options={"Encrypt": "yes"},
        )
        assert definition.port == 1444
        assert definition.driver == "ODBC Driver 17 for SQL Server"
        assert definition.description == "Auction warehouse"
        assert definition.options == {"Encrypt": "yes"}

    def test_surrounding_whitespace_is_stripped_from_text_fields(self):
        definition = _definition(host=" db1 ", database=" Sales ", username=" nlq ")
        assert (definition.host, definition.database, definition.username) == ("db1", "Sales", "nlq")

    def test_trusted_connection_needs_no_login(self):
        definition = DataSourceDefinition(
            host="db1", database="FuturesDW", trusted_connection=True,
        )
        assert definition.trusted_connection is True

    def test_username_env_in_place_of_username(self):
        definition = DataSourceDefinition(
            host="db1", database="Sales", username_env="DB_USER_SALES",
            password_env="DB_PASSWORD_SALES",
        )
        assert definition.username is None
        assert definition.username_env == "DB_USER_SALES"

    def test_a_null_variable_name_is_treated_as_unset(self):
        definition = DataSourceDefinition(
            host="db1", database="Sales", username="u", password_env="PW",
            username_env=None,
        )
        assert definition.username_env is None

    def test_a_null_driver_means_the_default(self):
        assert _definition(driver=None).driver is None
        assert DataSourceDefinition(
            host="db1", database="Sales", username="u", password_env="PW", driver=None,
        ).driver is None

    def test_both_forms_in_one_file(self):
        parsed = validate_datasources_yaml_text(_dump({
            "default": "auction",
            "datasources": {
                "auction": _structured(),
                "legacy": {"url_env": "DB_URL_LEGACY"},
            },
        }))
        assert parsed.datasources["auction"].url_env is None
        assert parsed.datasources["legacy"].url_env == "DB_URL_LEGACY"

    def test_unquoted_yaml_booleans_in_options_become_yes_and_no(self):
        parsed = validate_datasources_yaml_text(
            "datasources:\n"
            "  auction:\n"
            "    host: db1\n"
            "    database: Sales\n"
            "    trusted_connection: true\n"
            "    options:\n"
            "      TrustServerCertificate: yes\n"
            "      Encrypt: false\n"
            "      LoginTimeout: 30\n"
            "      Application Name: nlq agent\n"
        )
        assert parsed.datasources["auction"].options == {
            "TrustServerCertificate": "yes",
            "Encrypt": "no",
            "LoginTimeout": "30",
            "Application Name": "nlq agent",
        }


class TestStructuredValidationErrors:
    def test_mixing_url_env_and_structured_fields_names_the_source(self):
        message = _refused({"url_env": "DB_URL_X", "host": "db1", "database": "Sales"})
        assert "auction" in message
        assert "url_env" in message and "host" in message and "database" in message
        assert "one or the other" in message

    def test_url_env_with_options_is_mixed_too(self):
        message = _refused({"url_env": "DB_URL_X", "options": {}})
        assert "options" in message

    def test_neither_form_names_the_source(self):
        message = _refused({"description": "nothing to connect to"})
        assert "auction" in message
        assert "url_env" in message and "host" in message

    @pytest.mark.parametrize("missing", ["host", "database"])
    def test_host_and_database_are_both_required(self, missing):
        entry = _structured()
        del entry[missing]
        message = _refused(entry)
        assert "auction" in message
        assert f"missing {missing}" in message

    def test_trusted_connection_forbids_username_username_env_and_password_env(self):
        for clash in ({"username": "u"}, {"username_env": "U"}, {"password_env": "P"}):
            message = _refused({
                "host": "db1", "database": "Sales", "trusted_connection": True, **clash,
            })
            assert "auction" in message
            assert next(iter(clash)) in message
            assert "Windows authentication" in message

    def test_username_and_username_env_together_are_refused(self):
        message = _refused(_structured(username_env="DB_USER_SALES"))
        assert "not both" in message
        assert "auction" in message

    def test_a_login_is_required_without_trusted_connection(self):
        message = _refused(_structured(username=None))
        assert "needs a login" in message
        assert "trusted_connection" in message

    def test_password_env_is_required_with_a_username(self):
        message = _refused(_structured(password_env=None))
        assert "password_env is required" in message

    @pytest.mark.parametrize("field", ["url_env", "password_env", "username_env"])
    @pytest.mark.parametrize("value", [
        "mssql+pyodbc://u:pw@host/db", "has space", "1STARTS_WITH_DIGIT", "hun-ter2", "",
    ])
    def test_env_fields_must_be_variable_names(self, field, value):
        if field == "url_env":
            entry = {"url_env": value}
        elif field == "password_env":
            entry = _structured(password_env=value)
        else:
            entry = _structured(username=None, username_env=value)
        message = _refused(entry)
        assert field in message
        assert "NAME of an environment variable" in message

    def test_a_connection_string_in_password_env_is_never_echoed(self):
        message = _refused(_structured(password_env="mssql://u:hunter2@h/db"))
        assert "hunter2" not in message

    @pytest.mark.parametrize("key", ["password", "pwd", "url"])
    def test_a_secret_written_in_the_file_is_refused(self, key):
        message = _refused({**_structured(), key: "hunter2"})
        assert "not accepted" in message
        assert "hunter2" not in message

    @pytest.mark.parametrize("host", [
        "mssql://db1", "user@db1", "db1/db", "db1,1433", "db 1", "  ",
    ])
    def test_host_must_be_a_bare_server_name(self, host):
        assert _refused(_structured(host=host))

    @pytest.mark.parametrize("port", [0, 65536, -1, "not-a-port"])
    def test_port_must_be_a_valid_tcp_port(self, port):
        assert "port" in _refused(_structured(port=port))

    def test_a_non_mapping_source_is_refused(self):
        with pytest.raises(ValueError):
            validate_datasources_yaml_text(_dump({"datasources": {"auction": "db1"}}))

    def test_extra_keys_on_a_structured_source_are_refused(self):
        assert _refused(_structured(colour="blue"))


class TestOptions:
    @pytest.mark.parametrize("key", [
        "pwd", "PWD", "Password", "password", "uid", "UID", "user", "User",
        "User ID", "user id", "USER_ID", "UserID", "username",
        "driver", "Driver", "trusted_connection", "Trusted_Connection",
        "TrustedConnection", "server", "Database", "odbc_connect",
    ])
    def test_credential_and_reserved_keys_are_refused(self, key):
        message = _refused(_structured(options={key: "x"}))
        assert "not allowed" in message
        assert "auction" in message

    @pytest.mark.parametrize("key", ["Encrypt", "TrustServerCertificate", "Application Name", "APP"])
    def test_ordinary_odbc_keywords_are_accepted(self, key):
        assert _definition(options={key: "x"}).options == {key: "x"}

    @pytest.mark.parametrize("key", ["", "1abc", "a=b", "a;b", "a-b"])
    def test_malformed_keys_are_refused(self, key):
        assert "ODBC keyword" in _refused(_structured(options={key: "x"}))

    def test_a_non_string_key_is_refused(self):
        assert "ODBC keyword" in _refused(_structured(options={1: "x"}))

    @pytest.mark.parametrize("value,expected", [
        (True, "yes"), (False, "no"), (30, "30"), (0, "0"), (1.5, "1.5"),
        ("Strict", "Strict"), ("  padded  ", "padded"), ("yes", "yes"),
    ])
    def test_values_become_strings_in_a_defined_way(self, value, expected):
        assert _definition(options={"K": value}).options == {"K": expected}

    @pytest.mark.parametrize("value", [None, [], ["a"], {"a": 1}, "", "   ", "a;PWD=x", "a\nb"])
    def test_unusable_values_are_refused(self, value):
        assert "options value" in _refused(_structured(options={"K": value}))

    def test_the_same_keyword_twice_is_refused(self):
        assert "twice" in _refused(_structured(options={"Encrypt": "yes", "encrypt": "no"}))

    def test_options_must_be_a_mapping(self):
        assert "options" in _refused(_structured(options=["Encrypt"]))


class TestBuildUrl:
    def test_url_has_every_part(self):
        url = build_url("auction", _definition(port=1444), "tsql", {"DB_PASSWORD_SALES": "pw"})
        assert url.drivername == "mssql+pyodbc"
        assert (url.username, url.password) == ("nlq_reader", "pw")
        assert (url.host, url.port, url.database) == ("db1.example.test", 1444, "SalesDW")
        assert dict(url.query) == {"driver": "ODBC Driver 18 for SQL Server"}

    def test_port_and_driver_have_defaults(self):
        url = build_url("auction", _definition(), "tsql", {"DB_PASSWORD_SALES": "pw"})
        assert url.port == 1433
        assert url.query["driver"] == "ODBC Driver 18 for SQL Server"

    def test_driver_can_be_overridden(self):
        url = build_url(
            "auction", _definition(driver="ODBC Driver 17 for SQL Server"), "tsql",
            {"DB_PASSWORD_SALES": "pw"},
        )
        assert url.query["driver"] == "ODBC Driver 17 for SQL Server"

    def test_a_named_instance_gets_no_default_port(self):
        url = build_url(
            "auction", _definition(host="db1\\SQLEXPRESS"), "tsql", {"DB_PASSWORD_SALES": "pw"},
        )
        assert url.port is None
        assert make_url(url.render_as_string(hide_password=False)).host == "db1\\SQLEXPRESS"

    def test_a_named_instance_keeps_an_explicit_port(self):
        url = build_url(
            "auction", _definition(host="db1\\SQLEXPRESS", port=1500), "tsql",
            {"DB_PASSWORD_SALES": "pw"},
        )
        assert url.port == 1500

    def test_options_become_query_parameters(self):
        url = build_url(
            "auction",
            _definition(options={"TrustServerCertificate": True, "LoginTimeout": 15}),
            "tsql", {"DB_PASSWORD_SALES": "pw"},
        )
        assert dict(url.query) == {
            "driver": "ODBC Driver 18 for SQL Server",
            "TrustServerCertificate": "yes",
            "LoginTimeout": "15",
        }

    def test_trusted_connection_has_no_login_and_says_so(self):
        definition = DataSourceDefinition(host="db1", database="FuturesDW", trusted_connection=True)
        url = build_url("futures", definition, "tsql", {})
        assert (url.username, url.password) == (None, None)
        assert url.query["trusted_connection"] == "yes"
        assert url.render_as_string(hide_password=False) == (
            "mssql+pyodbc://db1:1433/FuturesDW"
            "?driver=ODBC+Driver+18+for+SQL+Server&trusted_connection=yes"
        )

    def test_trusted_connection_ignores_credentials_in_the_environment(self):
        definition = DataSourceDefinition(host="db1", database="Sales", trusted_connection=True)
        url = build_url("futures", definition, "tsql", {"DB_PASSWORD_SALES": "pw"})
        assert url.password is None

    def test_username_comes_from_username_env(self):
        definition = DataSourceDefinition(
            host="db1", database="Sales", username_env="DB_USER_SALES",
            password_env="DB_PASSWORD_SALES",
        )
        url = build_url(
            "auction", definition, "tsql",
            {"DB_USER_SALES": "svc@corp", "DB_PASSWORD_SALES": "pw"},
        )
        assert url.username == "svc@corp"

    def test_a_plain_username_never_reads_the_environment(self):
        # username_env is None here: only password_env is consulted.
        url = build_url("auction", _definition(), "tsql", {"DB_PASSWORD_SALES": "pw"})
        assert url.username == "nlq_reader"

    def test_an_unsupported_dialect_is_refused_with_the_alternative(self):
        with pytest.raises(DataSourceConfigError, match="auction.*url_env"):
            build_url("auction", _definition(), "postgres", {"DB_PASSWORD_SALES": "pw"})

    @pytest.mark.parametrize("environ", [{}, {"DB_PASSWORD_SALES": ""}])
    def test_a_missing_or_empty_password_names_source_and_variable(self, environ):
        with pytest.raises(DataSourceConfigError) as info:
            build_url("auction", _definition(), "tsql", environ)
        message = str(info.value)
        assert "auction" in message
        assert "DB_PASSWORD_SALES" in message
        assert "password_env" in message

    @pytest.mark.parametrize("environ", [{"DB_PASSWORD_SALES": "pw"}, {"DB_USER_SALES": ""}])
    def test_a_missing_or_empty_username_names_source_and_variable(self, environ):
        definition = DataSourceDefinition(
            host="db1", database="Sales", username_env="DB_USER_SALES",
            password_env="DB_PASSWORD_SALES",
        )
        with pytest.raises(DataSourceConfigError, match="auction.*DB_USER_SALES.*username_env"):
            build_url("auction", definition, "tsql", environ)


class TestPasswordsNeedNoEncoding:
    """The operator writes the raw password; the URL is built correctly."""

    @pytest.mark.parametrize("raw", _NASTY_PASSWORDS)
    def test_password_round_trips_through_the_rendered_url(self, raw):
        url = build_url("auction", _definition(), "tsql", {"DB_PASSWORD_SALES": raw})
        assert url.password == raw
        rendered = url.render_as_string(hide_password=False)
        assert make_url(rendered).password == raw
        assert make_url(rendered).username == "nlq_reader"
        assert make_url(rendered).host == "db1.example.test"

    @pytest.mark.parametrize("raw", _NASTY_PASSWORDS)
    def test_password_survives_the_data_source_and_the_application_name(self, raw):
        from database.connection_identity import with_application_name

        source = DataSource(
            "auction",
            build_url(
                "auction", _definition(), "tsql", {"DB_PASSWORD_SALES": raw},
            ).render_as_string(hide_password=False),
            None, "tsql", "app",
        )
        passed_to_create_engine = with_application_name(source.url, source.application_name)
        parsed = make_url(passed_to_create_engine)
        assert parsed.password == raw
        assert parsed.query["APP"] == "app"

    @pytest.mark.parametrize("raw", _NASTY_PASSWORDS)
    def test_password_reaches_the_odbc_connection_string_intact(self, raw):
        """What the pyodbc dialect hands the driver reads back, as an ODBC
        driver parses it, as exactly the raw password."""
        from sqlalchemy.dialects.mssql.pyodbc import MSDialect_pyodbc

        url = make_url(
            build_url(
                "auction", _definition(), "tsql", {"DB_PASSWORD_SALES": raw},
            ).render_as_string(hide_password=False)
        )
        (odbc,), _ = MSDialect_pyodbc().create_connect_args(url)
        assert _odbc_value(odbc, "PWD") == raw
        assert _odbc_value(odbc, "UID") == "nlq_reader"

    def test_rendered_string_for_a_password_with_every_special_character(self):
        raw = "@ % ] : / ? # & = + ;"
        url = build_url(
            "auction", _definition(options={"TrustServerCertificate": True}), "tsql",
            {"DB_PASSWORD_SALES": raw},
        )
        assert url.render_as_string(hide_password=False) == (
            "mssql+pyodbc://nlq_reader:%40 %25 %5D %3A %2F %3F %23 %26 %3D + %3B"
            "@db1.example.test:1433/SalesDW"
            "?TrustServerCertificate=yes&driver=ODBC+Driver+18+for+SQL+Server"
        )

    def test_the_password_is_masked_when_the_url_is_displayed(self):
        url = build_url("auction", _definition(), "tsql", {"DB_PASSWORD_SALES": "hunter2"})
        assert "hunter2" not in url.render_as_string()
        assert "hunter2" not in repr(url)
        assert "hunter2" not in str(url)


class TestGetDatasourceStructured:
    def test_resolves_a_structured_source_from_the_environment(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured()})
        monkeypatch.setenv("DB_PASSWORD_SALES", "p@ss/w:rd")
        source = get_datasource("auction", settings)
        assert source.name == "auction"
        assert source.url_env is None
        assert source.is_structured
        assert source.dialect == "tsql"
        assert make_url(source.url).password == "p@ss/w:rd"
        assert make_url(source.url).host == "db1.example.test"

    def test_the_password_is_read_fresh_on_every_call(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured()})
        monkeypatch.setenv("DB_PASSWORD_SALES", "one")
        assert make_url(get_datasource("auction", settings).url).password == "one"
        monkeypatch.setenv("DB_PASSWORD_SALES", "two")
        assert make_url(get_datasource("auction", settings).url).password == "two"

    def test_the_password_is_not_stripped(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured()})
        monkeypatch.setenv("DB_PASSWORD_SALES", " spaced ")
        assert make_url(get_datasource("auction", settings).url).password == " spaced "

    def test_a_missing_password_variable_is_refused_at_resolution(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured()})
        monkeypatch.delenv("DB_PASSWORD_SALES", raising=False)
        with pytest.raises(DataSourceConfigError, match="auction.*DB_PASSWORD_SALES"):
            get_datasource("auction", settings)

    def test_per_source_application_name_and_description(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured(
            description="Auction warehouse", application_name="auction-app",
        )})
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        source = get_datasource("auction", settings)
        assert source.application_name == "auction-app"
        assert source.description == "Auction warehouse"

    def test_two_databases_on_one_server_are_two_sources(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {
            "auction": _structured(database="SalesDW"),
            "futures": _structured(database="FuturesDW", password_env="DB_PASSWORD_FUTURES"),
        }, default="auction")
        monkeypatch.setenv("DB_PASSWORD_SALES", "one")
        monkeypatch.setenv("DB_PASSWORD_FUTURES", "two")
        sources = get_datasources(settings)
        assert [s.name for s in sources] == ["auction", "futures"]
        urls = [make_url(s.url) for s in sources]
        assert {u.host for u in urls} == {"db1.example.test"}
        assert [u.database for u in urls] == ["SalesDW", "FuturesDW"]
        assert [u.password for u in urls] == ["one", "two"]

    def test_a_structured_and_a_legacy_source_in_one_file(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {
            "auction": _structured(),
            "legacy": {"url_env": "DB_URL_LEGACY"},
        }, default="auction")
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        monkeypatch.setenv("DB_URL_LEGACY", _ARCHIVE_URL)
        auction, legacy = get_datasources(settings)
        assert auction.is_structured and not legacy.is_structured
        assert legacy.url == _ARCHIVE_URL
        assert legacy.url_env == "DB_URL_LEGACY"

    def test_a_structured_source_on_an_unsupported_dialect_is_refused(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured()})
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        postgres = Settings(project_config_dir=str(tmp_path), sql_dialect="postgres")
        with pytest.raises(DataSourceConfigError, match="url_env"):
            get_datasource("auction", postgres)
        assert settings.sql_dialect == "tsql"


class TestNeverPrintsThePassword:
    SECRET = "hunter2-@%:/"

    def _source(self) -> DataSource:
        return DataSource(
            "auction",
            build_url(
                "auction", _definition(), "tsql", {"DB_PASSWORD_SALES": self.SECRET},
            ).render_as_string(hide_password=False),
            None, "tsql", "app",
        )

    def test_repr_hides_the_url_and_the_login(self):
        rendered = repr(self._source())
        assert "hunter2" not in rendered
        assert "nlq_reader" not in rendered
        assert "auction" in rendered

    def test_redacted_url_masks_the_password_but_keeps_the_target(self):
        redacted = self._source().redacted_url
        assert "hunter2" not in redacted
        assert "%40" not in redacted  # no fragment of the encoded password either
        assert redacted.startswith("mssql+pyodbc://nlq_reader:***@db1.example.test:1433/SalesDW")

    def test_redacted_url_of_an_unset_variable(self):
        assert DataSource("m", "", "DB_URL_MAIN", "tsql", "app").redacted_url == "<not set>"

    def test_redacted_url_of_something_that_is_not_a_url(self):
        source = DataSource("m", "not a url hunter2", "DB_URL_MAIN", "tsql", "app")
        assert source.redacted_url == "<unparsable connection URL>"

    def test_label_names_the_variable_or_the_source(self):
        assert self._source().label == "data source 'auction'"
        assert DataSource("m", "", "DB_URL_MAIN", "tsql", "app").label == "DB_URL_MAIN"

    def test_errors_for_a_missing_variable_do_not_carry_any_value(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured(
            username=None, username_env="DB_USER_SALES",
        )})
        monkeypatch.setenv("DB_PASSWORD_SALES", self.SECRET)
        monkeypatch.delenv("DB_USER_SALES", raising=False)
        with pytest.raises(DataSourceConfigError) as info:
            get_datasource("auction", settings)
        assert "hunter2" not in str(info.value)
        assert "DB_USER_SALES" in str(info.value)


class TestValidateStructuredSources:
    PLACEHOLDERS = {"your_password_here", "your_server_here", "your_db_here", "change_me", ""}

    def _validate(self, settings):
        validate_datasource_urls(_real_check_url, settings, self.PLACEHOLDERS)

    def test_a_structured_source_passes(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured()})
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        self._validate(settings)

    def test_check_url_is_called_with_the_source_label(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {
            "auction": _structured(), "legacy": {"url_env": "DB_URL_LEGACY"},
        }, default="auction")
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        monkeypatch.setenv("DB_URL_LEGACY", _ARCHIVE_URL)
        seen = []
        validate_datasource_urls(lambda label, url, dialect: seen.append(label), settings)
        assert seen == ["data source 'auction'", "DB_URL_LEGACY"]

    @pytest.mark.parametrize("value", [None, ""])
    def test_a_missing_or_empty_password_variable_is_refused(self, tmp_path, monkeypatch, value):
        settings = _write_sources(tmp_path, {"auction": _structured()})
        if value is None:
            monkeypatch.delenv("DB_PASSWORD_SALES", raising=False)
        else:
            monkeypatch.setenv("DB_PASSWORD_SALES", value)
        with pytest.raises(ValueError, match="auction.*DB_PASSWORD_SALES"):
            self._validate(settings)

    def test_a_missing_username_variable_is_refused(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured(
            username=None, username_env="DB_USER_SALES",
        )})
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        monkeypatch.delenv("DB_USER_SALES", raising=False)
        with pytest.raises(ValueError, match="auction.*DB_USER_SALES"):
            self._validate(settings)

    def test_the_second_sources_missing_variable_is_refused_too(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {
            "auction": _structured(),
            "futures": _structured(database="FuturesDW", password_env="DB_PASSWORD_FUTURES"),
        }, default="auction")
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        monkeypatch.delenv("DB_PASSWORD_FUTURES", raising=False)
        with pytest.raises(ValueError, match="futures.*DB_PASSWORD_FUTURES"):
            self._validate(settings)

    @pytest.mark.parametrize("overrides,part", [
        ({"host": "your_server_here"}, "host"),
        ({"database": "your_db_here"}, "database"),
        ({"username": "change_me"}, "user name"),
        ({"host": "YOUR_SERVER_HERE"}, "host"),
    ])
    def test_a_placeholder_part_is_refused(self, tmp_path, monkeypatch, overrides, part):
        settings = _write_sources(tmp_path, {"auction": _structured(**overrides)})
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        with pytest.raises(ValueError, match=f"data source 'auction' still has a placeholder {part}"):
            self._validate(settings)

    def test_a_placeholder_password_is_refused_without_echoing_it(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured()})
        monkeypatch.setenv("DB_PASSWORD_SALES", "your_password_here")
        with pytest.raises(ValueError, match="placeholder password") as info:
            self._validate(settings)
        assert "your_password_here" not in str(info.value)

    def test_a_trusted_connection_source_has_nothing_to_look_up(self, tmp_path):
        settings = _write_sources(tmp_path, {"futures": {
            "host": "db1", "database": "FuturesDW", "trusted_connection": True,
        }})
        self._validate(settings)

    def test_the_dialect_check_runs_before_credentials_are_read(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"auction": _structured(dialect="postgres")})
        monkeypatch.delenv("DB_PASSWORD_SALES", raising=False)
        with pytest.raises(ValueError, match="declares dialect"):
            self._validate(settings)

    def test_a_legacy_source_is_not_checked_for_placeholder_parts(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"legacy": {"url_env": "DB_URL_LEGACY"}})
        monkeypatch.setenv(
            "DB_URL_LEGACY", "mssql+pyodbc://svc:change_me@your_server_here/db",
        )
        self._validate(settings)  # unchanged 6.1/6.2 behaviour


class TestSettingsValidateWithStructuredSources:
    def _settings(self, tmp_path, **overrides):
        return Settings(
            openai_model="gpt-oss-20b", db_connection_url="",
            project_config_dir=str(tmp_path), sql_dialect="tsql", **overrides,
        )

    def test_a_structured_file_validates(self, tmp_path, monkeypatch):
        _write_sources(tmp_path, {"auction": _structured()})
        monkeypatch.setenv("DB_PASSWORD_SALES", "p@ss%word")
        self._settings(tmp_path).validate()

    def test_a_missing_password_variable_fails_naming_source_and_variable(
        self, tmp_path, monkeypatch,
    ):
        _write_sources(tmp_path, {"auction": _structured()})
        monkeypatch.delenv("DB_PASSWORD_SALES", raising=False)
        with pytest.raises(ValueError, match="auction.*DB_PASSWORD_SALES"):
            self._settings(tmp_path).validate()

    def test_a_placeholder_host_fails(self, tmp_path, monkeypatch):
        _write_sources(tmp_path, {"auction": _structured(host="your_server_here")})
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        with pytest.raises(ValueError, match="placeholder host"):
            self._settings(tmp_path).validate()

    def test_a_mixed_file_validates(self, tmp_path, monkeypatch):
        _write_sources(tmp_path, {
            "auction": _structured(),
            "futures": {"host": "db1", "database": "FuturesDW", "trusted_connection": True},
            "legacy": {"url_env": "DB_URL_LEGACY"},
        }, default="auction")
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        monkeypatch.setenv("DB_URL_LEGACY", _ARCHIVE_URL)
        self._settings(tmp_path).validate()

    def test_a_mixed_form_source_fails_to_load_naming_it(self, tmp_path, monkeypatch):
        _write_sources(tmp_path, {"auction": {"url_env": "DB_URL_X", **_structured()}})
        with pytest.raises(ValueError, match="auction"):
            self._settings(tmp_path).validate()

    def test_db_connection_url_is_not_needed_with_a_structured_file(self, tmp_path, monkeypatch):
        _write_sources(tmp_path, {"auction": _structured()})
        monkeypatch.setenv("DB_PASSWORD_SALES", "pw")
        self._settings(tmp_path, db_password="ignored").validate()


class TestLegacyUrlEnvStillWorks:
    def test_url_env_source_resolves_as_before(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"main": {"url_env": "DB_URL_MAIN"}})
        monkeypatch.setenv("DB_URL_MAIN", f"  {_MAIN_URL}  ")
        source = get_datasource("main", settings)
        assert source.url == _MAIN_URL
        assert source.url_env == "DB_URL_MAIN"
        assert not source.is_structured

    def test_a_percent_encoded_password_in_a_legacy_url_is_decoded_by_sqlalchemy(
        self, tmp_path, monkeypatch,
    ):
        settings = _write_sources(tmp_path, {"main": {"url_env": "DB_URL_MAIN"}})
        monkeypatch.setenv("DB_URL_MAIN", "mssql+pyodbc://svc:p%40ss@main-host/DB")
        assert make_url(get_datasource("main", settings).url).password == "p@ss"

    def test_validation_message_still_names_the_variable(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"main": {"url_env": "DB_URL_MISSING"}})
        monkeypatch.delenv("DB_URL_MISSING", raising=False)
        with pytest.raises(ValueError, match="DB_URL_MISSING is not configured"):
            validate_datasource_urls(_real_check_url, settings)


# ---------------------------------------------------------------------------
# DB_PASSWORD on the single-source path
# ---------------------------------------------------------------------------

_NO_PASSWORD_URL = "mssql+pyodbc://nlq@db1:1433/SalesDW?driver=ODBC+Driver+18+for+SQL+Server"


class TestApplyDbPassword:
    @pytest.mark.parametrize("raw", _NASTY_PASSWORDS)
    def test_a_raw_password_round_trips_through_set(self, raw):
        result = apply_db_password(_NO_PASSWORD_URL, raw)
        parsed = make_url(result)
        assert parsed.password == raw
        assert parsed.username == "nlq"
        assert (parsed.host, parsed.port, parsed.database) == ("db1", 1433, "SalesDW")
        assert parsed.query["driver"] == "ODBC Driver 18 for SQL Server"

    def test_the_slash_question_hash_colon_and_bracket_case(self):
        raw = "a/b?c#d:e]f@g%40"
        assert make_url(apply_db_password(_NO_PASSWORD_URL, raw)).password == raw

    def test_without_a_password_the_url_is_returned_untouched(self):
        encoded = "mssql+pyodbc://nlq:p%40ss@db1/SalesDW"
        assert apply_db_password(encoded, "") == encoded
        assert apply_db_password(_NO_PASSWORD_URL, "") == _NO_PASSWORD_URL

    def test_an_empty_url_stays_empty_so_validation_reports_it_unset(self):
        assert apply_db_password("", "secret") == ""

    def test_both_a_url_password_and_db_password_is_ambiguous(self):
        with pytest.raises(DataSourceConfigError, match="ambiguous") as info:
            apply_db_password("mssql+pyodbc://nlq:p%40ss@db1/SalesDW", "secret")
        assert "secret" not in str(info.value)
        assert "p%40ss" not in str(info.value)

    def test_an_empty_password_in_the_url_is_not_a_password(self):
        result = apply_db_password("mssql+pyodbc://nlq:@db1/SalesDW", "secret")
        assert make_url(result).password == "secret"

    def test_a_url_with_no_login_cannot_take_a_password(self):
        with pytest.raises(DataSourceConfigError, match="no user name"):
            apply_db_password("mssql+pyodbc://db1/SalesDW", "secret")

    def test_an_unparsable_url_is_refused_without_echoing_it(self):
        with pytest.raises(DataSourceConfigError, match="not a valid SQLAlchemy URL") as info:
            apply_db_password("this is not a url", "secret")
        assert "this is not a url" not in str(info.value)

    def test_a_non_mssql_url_gets_the_password_too(self):
        result = apply_db_password("postgresql://nlq@db1/Sales", "a b@c")
        assert make_url(result).password == "a b@c"


class TestDbPasswordSetting:
    def test_defaults_to_empty(self, monkeypatch):
        monkeypatch.delenv("DB_PASSWORD", raising=False)
        assert Settings().db_password == ""

    def test_is_read_from_the_environment_raw(self, monkeypatch):
        monkeypatch.setenv("DB_PASSWORD", "p@ss%40 /?#")
        assert Settings().db_password == "p@ss%40 /?#"

    def test_never_appears_in_the_settings_repr(self):
        assert "hunter2" not in repr(Settings(db_password="hunter2"))

    def test_survives_override_settings(self):
        with override_settings(db_password="hunter2") as patched:
            assert patched.db_password == "hunter2"

    def test_the_fallback_source_applies_it_to_the_url(self, tmp_path):
        settings = Settings(
            project_config_dir=str(tmp_path), db_connection_url=_NO_PASSWORD_URL,
            db_password="p@ss/w:rd", sql_dialect="tsql",
        )
        source = get_datasource(None, settings)
        assert source.url_env == "DB_CONNECTION_URL"
        assert make_url(source.url).password == "p@ss/w:rd"
        assert "p@ss" not in source.redacted_url

    def test_unset_leaves_the_url_exactly_as_written(self, tmp_path):
        encoded = "mssql+pyodbc://nlq:p%40ss@db1:1433/SalesDW"
        settings = Settings(
            project_config_dir=str(tmp_path), db_connection_url=encoded,
            db_password="", sql_dialect="tsql",
        )
        source = get_datasource(None, settings)
        assert source.url == encoded
        assert make_url(source.url).password == "p@ss"

    def test_a_password_in_both_places_is_refused_at_startup(self, tmp_path):
        settings = Settings(
            openai_model="gpt-oss-20b", project_config_dir=str(tmp_path),
            db_connection_url="mssql+pyodbc://nlq:p%40ss@db1:1433/SalesDW",
            db_password="secret", sql_dialect="tsql",
        )
        with pytest.raises(ValueError, match="ambiguous"):
            settings.validate()

    def test_validate_accepts_a_passwordless_url_with_db_password(self, tmp_path):
        Settings(
            openai_model="gpt-oss-20b", project_config_dir=str(tmp_path),
            db_connection_url=_NO_PASSWORD_URL, db_password="p@ss/w:rd",
            sql_dialect="tsql",
        ).validate()

    def test_the_factory_default_placeholder_is_still_caught_with_db_password(self, tmp_path):
        """With a password set the URL reads ``username:<pw>@server``, which
        the literal ``username@server`` text no longer matches."""
        settings = Settings(
            openai_model="gpt-oss-20b", project_config_dir=str(tmp_path),
            db_connection_url="mssql+pyodbc://username@server:1433/DB?driver=x",
            db_password="secret", sql_dialect="tsql",
        )
        with pytest.raises(ValueError, match="placeholder"):
            settings.validate()

    def test_db_password_is_ignored_when_datasources_yaml_exists(self, tmp_path, monkeypatch):
        settings = _write_sources(tmp_path, {"main": {"url_env": "DB_URL_MAIN"}})
        monkeypatch.setenv("DB_URL_MAIN", "mssql+pyodbc://nlq:p%40ss@db1/Sales")
        with override_settings(project_config_dir=str(tmp_path), db_password="ignored"):
            assert make_url(get_datasource("main").url).password == "p@ss"
        assert settings.sql_dialect == "tsql"


class TestHasPlaceholderLoginHost:
    @pytest.mark.parametrize("url,expected", [
        ("mssql+pyodbc://username@server:1433/db", True),
        ("mssql+pyodbc://USERNAME:pw@SERVER/db", True),
        ("mssql+pyodbc://nlq:pw@server/db", False),
        ("mssql+pyodbc://username:pw@db1/db", False),
        ("not a url", False),
    ])
    def test_cases(self, url, expected):
        from config import _has_placeholder_login_host

        assert _has_placeholder_login_host(url) is expected


class TestExampleFile:
    """``project_config.example/datasources.example.yaml`` is documentation an
    operator copies; it must stay valid."""

    _PATH = os.path.join(
        os.path.dirname(__file__), "..", "project_config.example", "datasources.example.yaml",
    )

    def _text(self) -> str:
        with open(self._PATH, encoding="utf-8") as fh:
            return fh.read()

    def test_the_example_as_shipped_is_valid(self):
        parsed = validate_datasources_yaml_text(self._text())
        assert parsed.default_name == "sales"
        assert list(parsed.datasources) == ["sales", "inventory"]
        sales, inventory = parsed.datasources.values()
        assert sales.host == inventory.host  # two databases, one server
        assert sales.database != inventory.database
        assert sales.options == {"TrustServerCertificate": "yes"}

    def test_every_commented_alternative_is_valid_when_uncommented(self):
        import re

        uncommented = re.sub(
            r"^  # ((?:reports|finance|warehouse2):|  \S)", r"  \1", self._text(),
            flags=re.MULTILINE,
        )
        parsed = validate_datasources_yaml_text(uncommented)
        assert set(parsed.datasources) == {
            "sales", "inventory", "reports", "finance", "warehouse2",
        }
        assert parsed.datasources["reports"].trusted_connection is True
        assert parsed.datasources["finance"].username_env == "DB_USER_FINANCE"
        assert parsed.datasources["warehouse2"].url_env == "DB_URL_WAREHOUSE2"


# ---------------------------------------------------------------------------
# Tables that live in several sources
# ---------------------------------------------------------------------------

_TWO_SOURCES = {
    "default": "sales",
    "datasources": {
        "inventory": {"url_env": "DB_URL_INVENTORY"},
        "sales": {"url_env": "DB_URL_SALES"},
        "archive": {"url_env": "DB_URL_COLD"},
    },
}


@pytest.fixture()
def shared_project(tmp_path):
    """A project dir with three sources (default ``sales``, then
    ``inventory``, then ``archive`` in file order after the default) and a
    schema whose tables use a name, a list and nothing."""
    import schema_data.registry as registry_module

    (tmp_path / "datasources.yaml").write_text(_dump(_TWO_SOURCES), encoding="utf-8")
    (tmp_path / "schema.yaml").write_text(
        "tables:\n"
        "  Date:\n    datasource: [archive, inventory, sales]\n"
        "  Broker:\n    datasource: inventory\n"
        "  Replica:\n    datasource: [archive, inventory]\n"
        "  Trade: {}\n",
        encoding="utf-8",
    )
    registry_module._cache.clear()
    try:
        with override_settings(project_config_dir=str(tmp_path)):
            yield tmp_path
    finally:
        registry_module._cache.clear()


class TestTableDatasourceSets:
    def test_every_source_of_a_table_in_datasources_yaml_order(self, shared_project):
        from database.datasources import table_datasource_sets

        assert table_datasource_sets() == {
            # default first, then the file's order -- not the order written
            "Date": ("sales", "inventory", "archive"),
            "Broker": ("inventory",),
            "Replica": ("inventory", "archive"),
            "Trade": ("sales",),
        }

    def test_table_datasources_keeps_returning_one_name_per_table(self, shared_project):
        assert table_datasources() == {
            "Date": "sales",      # the default, because the table lives there
            "Broker": "inventory",
            "Replica": "inventory",    # no default: the first source in file order
            "Trade": "sales",
        }

    def test_single_source_deployment_is_unchanged(self):
        import schema_data.registry as registry_module
        from database.datasources import table_datasource_sets

        registry_module._cache.clear()
        try:
            with override_settings(project_config_dir="project_config.example"):
                sets = table_datasource_sets()
        finally:
            registry_module._cache.clear()
        assert sets and all(v == (DEFAULT_DATASOURCE,) for v in sets.values())


class TestPickDatasource:
    def test_the_default_wins_when_it_is_a_candidate(self, shared_project):
        from database.datasources import pick_datasource

        assert pick_datasource(["archive", "sales"]) == "sales"

    def test_otherwise_the_first_in_datasources_yaml_order(self, shared_project):
        from database.datasources import pick_datasource

        assert pick_datasource(["archive", "inventory"]) == "inventory"
        assert pick_datasource({"inventory", "archive"}) == "inventory"

    def test_nothing_to_pick_from_is_an_error(self, shared_project):
        from database.datasources import pick_datasource

        with pytest.raises(ValueError):
            pick_datasource([])


class TestCheckTableDatasourcesWithLists:
    def test_a_list_of_configured_sources_passes(self, shared_project):
        check_table_datasources({"Date": ("sales", "inventory"), "Order": ""})

    def test_every_unknown_name_in_a_list_is_named(self, shared_project):
        with pytest.raises(ValueError) as exc_info:
            check_table_datasources({"Date": ["sales", "Nope"], "Ring": ["Gone", "Nope"]})
        text = str(exc_info.value)
        assert "Date -> Nope" in text
        assert "Ring -> Gone" in text
        assert "Ring -> Nope" in text
        assert "sales" in text  # the configured sources are listed

    def test_an_empty_tuple_means_the_default_source(self, shared_project):
        check_table_datasources({"Order": ()})

    def test_the_default_argument_checks_the_schema_yaml_lists(self, shared_project):
        check_table_datasources()
