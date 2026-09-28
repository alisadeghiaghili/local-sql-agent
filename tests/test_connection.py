# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``database.connection.get_engine`` -- pool-shape settings actually
read from ``cfg.settings`` (Findings 1 and 5, 2026 warehouse-load audit,
and the idle-aware-ping follow-up to Finding 1).

``create_engine`` itself is always mocked here (never a real engine --
this suite's root ``_no_real_database`` guard would refuse it anyway):
these tests are about what ``get_engine`` PASSES to ``create_engine`` and
to :func:`database.pool_ping.install_idle_aware_ping`, not about a real
connection. ``install_idle_aware_ping`` is mocked too in this file
(rather than let the real one run against a ``MagicMock`` standing in for
an engine) -- its own idle/threshold/failure behaviour is covered against
a real engine in ``tests/test_idle_aware_ping.py``; this file is only
about the wiring: whether ``get_engine`` calls it at all, and with what
arguments.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml

import config as cfg
from database.connection import dispose_engine, get_engine
from database.datasources import UnknownDataSourceError, reset_datasources_cache


class TestPoolPrePingIsConfigurable:
    def test_defaults_to_installing_idle_aware_ping(self):
        """Default ``db_pool_pre_ping=True`` -- no bare ``pool_pre_ping``
        kwarg goes to ``create_engine`` any more; the idle-aware ping is
        installed on the engine instead, with the default idle threshold."""
        get_engine.cache_clear()
        with patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping") as mock_install:
            get_engine()
        assert "pool_pre_ping" not in mock_create.call_args.kwargs
        mock_install.assert_called_once_with(
            mock_create.return_value, cfg.settings.db_pool_ping_idle_seconds
        )
        get_engine.cache_clear()

    def test_honours_db_pool_pre_ping_false(self):
        """``False`` means never ping -- ``install_idle_aware_ping`` is not
        even called, regardless of the idle-threshold setting."""
        get_engine.cache_clear()
        with cfg.override_settings(db_pool_pre_ping=False), \
             patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping") as mock_install:
            get_engine()
        assert "pool_pre_ping" not in mock_create.call_args.kwargs
        mock_install.assert_not_called()
        get_engine.cache_clear()

    def test_honours_db_pool_pre_ping_true_explicitly(self):
        get_engine.cache_clear()
        with cfg.override_settings(db_pool_pre_ping=True), \
             patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping") as mock_install:
            get_engine()
        mock_install.assert_called_once_with(
            mock_create.return_value, cfg.settings.db_pool_ping_idle_seconds
        )
        get_engine.cache_clear()


class TestPoolPingIdleSecondsIsConfigurable:
    def test_defaults_to_60(self):
        get_engine.cache_clear()
        with patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping") as mock_install:
            get_engine()
        assert mock_install.call_args.args[1] == 60
        get_engine.cache_clear()

    def test_honours_db_pool_ping_idle_seconds_override(self):
        get_engine.cache_clear()
        with cfg.override_settings(db_pool_ping_idle_seconds=0), \
             patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping") as mock_install:
            get_engine()
        assert mock_install.call_args.args[1] == 0
        get_engine.cache_clear()


class TestPoolRecycleIsConfigurable:
    def test_defaults_to_3600(self):
        get_engine.cache_clear()
        with patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping"):
            get_engine()
        assert mock_create.call_args.kwargs["pool_recycle"] == 3600
        get_engine.cache_clear()

    def test_honours_db_pool_recycle_seconds(self):
        get_engine.cache_clear()
        with cfg.override_settings(db_pool_recycle_seconds=120), \
             patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping"):
            get_engine()
        assert mock_create.call_args.kwargs["pool_recycle"] == 120
        get_engine.cache_clear()


class TestApplicationNameIsApplied:
    def test_mssql_pyodbc_url_gets_an_application_name(self):
        get_engine.cache_clear()
        url = "mssql+pyodbc://user@myhost/Auction_DM?driver=ODBC+Driver+17+for+SQL+Server"
        with cfg.override_settings(db_connection_url=url, db_application_name="local-sql-agent"), \
             patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping"):
            get_engine()
        called_url = mock_create.call_args.args[0]
        assert "APP=local-sql-agent" in called_url
        get_engine.cache_clear()

    def test_sqlite_url_used_by_this_suite_is_untouched(self):
        get_engine.cache_clear()
        url = "sqlite:///:memory:"
        with cfg.override_settings(db_connection_url=url), \
             patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping"):
            get_engine()
        called_url = mock_create.call_args.args[0]
        assert called_url == url
        get_engine.cache_clear()


class TestMultipleDataSources:
    """One cached engine per data source (``database.datasources``) --
    ``get_engine()`` with no argument, and with the default source's own
    name, must resolve to the SAME cached engine and never call
    ``create_engine`` twice for it."""

    @pytest.fixture(autouse=True)
    def _clean_engine_cache(self):
        get_engine.cache_clear()
        reset_datasources_cache()
        yield
        get_engine.cache_clear()
        reset_datasources_cache()

    @pytest.fixture()
    def two_sources(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(yaml.dump({
            "default": "main",
            "datasources": {
                "main": {"url_env": "DB_URL_MAIN"},
                "archive": {"url_env": "DB_URL_ARCHIVE"},
            },
        }), encoding="utf-8")
        monkeypatch.setenv(
            "DB_URL_MAIN",
            "mssql+pyodbc://svc@main-host/DB?driver=ODBC+Driver+17+for+SQL+Server",
        )
        monkeypatch.setenv(
            "DB_URL_ARCHIVE",
            "mssql+pyodbc://svc@archive-host/DB?driver=ODBC+Driver+17+for+SQL+Server",
        )
        return tmp_path

    def test_no_argument_and_the_default_name_share_one_engine(self, two_sources):
        with cfg.override_settings(project_config_dir=str(two_sources)), \
             patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping"):
            engine_bare = get_engine()
            engine_named = get_engine("main")
        assert engine_bare is engine_named
        mock_create.assert_called_once()

    def test_two_sources_build_two_engines_with_the_right_urls(self, two_sources):
        with cfg.override_settings(project_config_dir=str(two_sources)), \
             patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping"):
            get_engine("main")
            get_engine("archive")
        assert mock_create.call_count == 2
        called_urls = [c.args[0] for c in mock_create.call_args_list]
        assert any("main-host" in u for u in called_urls)
        assert any("archive-host" in u for u in called_urls)

    def test_each_source_gets_its_own_application_name(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(yaml.dump({
            "default": "main",
            "datasources": {
                "main": {"url_env": "DB_URL_MAIN", "application_name": "app-main"},
                "archive": {"url_env": "DB_URL_ARCHIVE", "application_name": "app-archive"},
            },
        }), encoding="utf-8")
        monkeypatch.setenv(
            "DB_URL_MAIN",
            "mssql+pyodbc://svc@main-host/DB?driver=ODBC+Driver+17+for+SQL+Server",
        )
        monkeypatch.setenv(
            "DB_URL_ARCHIVE",
            "mssql+pyodbc://svc@archive-host/DB?driver=ODBC+Driver+17+for+SQL+Server",
        )
        with cfg.override_settings(project_config_dir=str(tmp_path)), \
             patch("database.connection.create_engine") as mock_create, \
             patch("database.connection.install_idle_aware_ping"):
            get_engine("main")
            get_engine("archive")
        called_urls = {c.args[0] for c in mock_create.call_args_list}
        assert any("APP=app-main" in u for u in called_urls)
        assert any("APP=app-archive" in u for u in called_urls)

    def test_unknown_source_name_raises(self, two_sources):
        with cfg.override_settings(project_config_dir=str(two_sources)):
            with pytest.raises(UnknownDataSourceError):
                get_engine("does-not-exist")

    def test_dispose_engine_disposes_every_source(self, two_sources):
        mock_engines = [MagicMock(name="main-engine"), MagicMock(name="archive-engine")]
        with cfg.override_settings(project_config_dir=str(two_sources)), \
             patch("database.connection.create_engine", side_effect=mock_engines), \
             patch("database.connection.install_idle_aware_ping"):
            get_engine("main")
            get_engine("archive")
            dispose_engine()
        for engine in mock_engines:
            engine.dispose.assert_called_once()
