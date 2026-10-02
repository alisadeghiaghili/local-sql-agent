# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``api/server.py``'s ``lifespan`` -- the two multi-source start-up checks.

Drives the REAL ``lifespan`` (not a mock of it), the same way
``tests/test_startup_logging.py`` does, far enough to prove two things
``docs/deployment-runbook.md``'s "configuring several data sources"
section promises:

1. The application database must not resolve to the same server+database
   as ANY configured data source -- not just the default one.
2. Every ``schema.yaml`` table must name a configured data source, or
   start-up refuses with a clear message, before any query could ever
   fail on it.

Both checks run before ``get_app_engine()`` -- a real SQLite file is never
even opened, and the fake ``mssql+pyodbc`` URLs used below are never
contacted (the checks below parse them, never connect) -- so this needs no
running database, warehouse or otherwise.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import yaml

import api.server as server_module
import schema_data.registry as registry_module
from config import override_settings
from database.datasources import reset_datasources_cache

_MAIN_URL = (
    "mssql+pyodbc://svc@main-warehouse.internal:1433/DB"
    "?driver=ODBC+Driver+17+for+SQL+Server"
)
_ARCHIVE_URL = (
    "mssql+pyodbc://svc@archive-warehouse.internal:1433/DB"
    "?driver=ODBC+Driver+17+for+SQL+Server"
)


@pytest.fixture(autouse=True)
def _reset_process_wide_caches():
    """Both ``schema_data.registry`` and ``database.datasources`` cache on
    first access and never re-read on their own -- clear both around every
    test in this file, mirroring ``tests/test_schema_drift.py``'s own
    ``_reset_registry_cache`` fixture, so a temp ``schema.yaml``/
    ``datasources.yaml`` written by one test can never leak into (or be
    shadowed by) whatever the rest of the suite already cached."""
    registry_module._cache.clear()
    reset_datasources_cache()
    yield
    registry_module._cache.clear()
    reset_datasources_cache()


def _write_datasources_yaml(project_dir, monkeypatch) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "datasources.yaml").write_text(
        yaml.dump({
            "default": "main",
            "datasources": {
                "main": {"url_env": "DB_URL_MAIN"},
                "archive": {"url_env": "DB_URL_ARCHIVE"},
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("DB_URL_MAIN", _MAIN_URL)
    monkeypatch.setenv("DB_URL_ARCHIVE", _ARCHIVE_URL)
    reset_datasources_cache()


async def _run_lifespan() -> None:
    async with server_module.lifespan(server_module.app):
        pass  # pragma: no cover - only reached if start-up fully succeeds


class TestLifespanRefusesAppDbSharedWithAnySource:
    def test_refused_when_the_non_default_source_matches_app_db_url(
        self, tmp_path, monkeypatch,
    ):
        """The FIRST-checked source ("main") looks fine on its own -- the
        conflict is on "archive", the second one. A loop that only checked
        the default source (the single-source-era behaviour) would let
        this start."""
        _write_datasources_yaml(tmp_path, monkeypatch)
        with override_settings(
            project_config_dir=str(tmp_path),
            openai_model="gpt-oss-20b",
            auth_required=False,
            app_db_url=_ARCHIVE_URL,  # collides with "archive", not "main"
        ):
            with pytest.raises(RuntimeError, match="same server and database"):
                asyncio.run(_run_lifespan())

    def test_distinct_app_db_is_unaffected(self, tmp_path, monkeypatch):
        """An APP_DB_URL that collides with neither source must clear this
        check -- proven by patching what comes right after it
        (``check_table_datasources`` and ``get_app_engine``) with a
        sentinel, so reaching the sentinel is proof this check passed,
        without needing a real reachable application database."""
        _write_datasources_yaml(tmp_path, monkeypatch)
        with override_settings(
            project_config_dir=str(tmp_path),
            openai_model="gpt-oss-20b",
            auth_required=False,
            app_db_url="sqlite:///" + str(tmp_path / "app.db"),
        ):
            with patch(
                "database.datasources.check_table_datasources",
            ) as mock_check, patch(
                "appdb.engine.get_app_engine",
                side_effect=RuntimeError("sentinel: reached get_app_engine"),
            ):
                with pytest.raises(RuntimeError, match="sentinel"):
                    asyncio.run(_run_lifespan())
            mock_check.assert_called_once()


class TestLifespanRefusesAnUnknownTableDatasource:
    def test_refused_when_schema_yaml_names_an_unconfigured_source(
        self, tmp_path, monkeypatch,
    ):
        _write_datasources_yaml(tmp_path, monkeypatch)
        (tmp_path / "schema.yaml").write_text(
            yaml.dump({
                "tables": {
                    "Widget": {
                        "description": "d",
                        "datasource": "does-not-exist",
                        "columns": {"ID": "pk"},
                    },
                },
            }),
            encoding="utf-8",
        )
        with override_settings(
            project_config_dir=str(tmp_path),
            openai_model="gpt-oss-20b",
            auth_required=False,
            app_db_url="sqlite:///" + str(tmp_path / "app.db"),
        ):
            with pytest.raises(RuntimeError, match="does-not-exist"):
                asyncio.run(_run_lifespan())

    def test_a_table_mapped_to_a_configured_source_reaches_get_app_engine(
        self, tmp_path, monkeypatch,
    ):
        """The mirror image of the test above: a table naming a source
        that DOES exist must clear this check -- proven the same way,
        by patching ``get_app_engine`` with a sentinel."""
        _write_datasources_yaml(tmp_path, monkeypatch)
        (tmp_path / "schema.yaml").write_text(
            yaml.dump({
                "tables": {
                    "Widget": {
                        "description": "d",
                        "datasource": "archive",
                        "columns": {"ID": "pk"},
                    },
                },
            }),
            encoding="utf-8",
        )
        with override_settings(
            project_config_dir=str(tmp_path),
            openai_model="gpt-oss-20b",
            auth_required=False,
            app_db_url="sqlite:///" + str(tmp_path / "app.db"),
        ):
            with patch(
                "appdb.engine.get_app_engine",
                side_effect=RuntimeError("sentinel: reached get_app_engine"),
            ):
                with pytest.raises(RuntimeError, match="sentinel"):
                    asyncio.run(_run_lifespan())


class TestLifespanWithStructuredSources:
    def test_a_structured_source_colliding_with_app_db_url_is_refused_without_its_password(
        self, tmp_path, monkeypatch,
    ):
        (tmp_path / "datasources.yaml").write_text(
            "datasources:\n"
            "  auction:\n"
            "    host: db1.example.test\n"
            "    database: SalesDW\n"
            "    username: nlq_reader\n"
            "    password_env: DB_PASSWORD_SALES\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("DB_PASSWORD_SALES", "hunter2@%")
        reset_datasources_cache()
        with override_settings(
            project_config_dir=str(tmp_path),
            openai_model="gpt-oss-20b",
            auth_required=False,
            app_db_url="mssql+pyodbc://app:other-secret@db1.example.test:1433/SalesDW",
        ):
            with pytest.raises(RuntimeError, match="same server and database") as info:
                asyncio.run(_run_lifespan())
        assert "hunter2" not in str(info.value)
        assert "other-secret" not in str(info.value)

    def test_a_missing_password_variable_stops_start_up_naming_it(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(
            "datasources:\n"
            "  auction:\n"
            "    host: db1.example.test\n"
            "    database: SalesDW\n"
            "    username: nlq_reader\n"
            "    password_env: DB_PASSWORD_SALES\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("DB_PASSWORD_SALES", raising=False)
        reset_datasources_cache()
        with override_settings(
            project_config_dir=str(tmp_path), openai_model="gpt-oss-20b",
            auth_required=False,
        ):
            with pytest.raises(RuntimeError, match="Invalid configuration.*auction.*DB_PASSWORD_SALES"):
                asyncio.run(_run_lifespan())
