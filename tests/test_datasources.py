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

import config as cfg
from config import Settings, _check_warehouse_url, override_settings
from database.datasources import (
    DEFAULT_DATASOURCE,
    DataSource,
    DataSourcesConfig,
    UnknownDataSourceError,
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
