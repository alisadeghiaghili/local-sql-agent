# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``database.connection.get_engine`` -- what each warehouse engine is
built with: no connection pool (``NullPool``), pyodbc's driver-manager
pooling turned off for ``mssql+pyodbc``, the application name stamped on
the URL (Finding 5, 2026 warehouse-load audit), and one engine per data
source.

``create_engine`` is mocked in most tests, because this suite's root
``_no_real_database`` guard replaces it anyway; those tests check what
``get_engine`` passes to it. One test wires the real ``create_engine``
against a throwaway SQLite file to prove that a connection is actually
closed when the caller's ``with`` block exits.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy
import yaml
from sqlalchemy.pool import NullPool

import config as cfg
from database.connection import dispose_engine, get_engine
from database.datasources import UnknownDataSourceError, reset_datasources_cache


class TestEnginesDoNotPoolConnections:
    """The warehouse engine keeps no idle connection between queries.

    A ``QueuePool`` (the previous configuration, ``pool_size=10,
    max_overflow=20``) holds checked-in connections open indefinitely, which
    DBAs see as sleeping sessions that never close. ``NullPool`` closes the
    DBAPI connection as soon as the caller's ``with engine.connect()`` block
    exits.
    """

    @pytest.fixture(autouse=True)
    def _clean_engine_cache(self):
        get_engine.cache_clear()
        yield
        get_engine.cache_clear()

    def test_engine_is_built_with_null_pool(self):
        with patch("database.connection.create_engine") as mock_create:
            get_engine()
        assert mock_create.call_args.kwargs["poolclass"] is NullPool

    def test_no_queue_pool_sizing_or_recycle_is_passed(self):
        with patch("database.connection.create_engine") as mock_create:
            get_engine()
        kwargs = mock_create.call_args.kwargs
        for key in ("pool_size", "max_overflow", "pool_recycle", "pool_pre_ping"):
            assert key not in kwargs

    @pytest.mark.parametrize("pre_ping", [True, False])
    def test_idle_aware_ping_is_never_installed(self, pre_ping):
        """With no pool, a connection is never reused, so there is nothing
        stale to probe; the ping would only add a round trip."""
        with cfg.override_settings(db_pool_pre_ping=pre_ping), \
             patch("database.connection.create_engine"), \
             patch("database.pool_ping.install_idle_aware_ping") as mock_install:
            get_engine()
        mock_install.assert_not_called()

    def test_connection_is_closed_when_the_with_block_exits(self, tmp_path):
        """Behavioural check against a real engine: after ``connect()``
        returns, the underlying DBAPI connection is closed, not parked."""
        url = f"sqlite:///{tmp_path / 'warehouse.db'}"
        opened: list[sqlite3.Connection] = []

        def _real_create_engine(*args, **kwargs):
            engine = sqlalchemy.create_engine(*args, **kwargs)
            sqlalchemy.event.listen(
                engine, "connect", lambda dbapi_conn, _rec: opened.append(dbapi_conn)
            )
            return engine

        with cfg.override_settings(db_connection_url=url), \
             patch("database.connection.create_engine", side_effect=_real_create_engine):
            engine = get_engine()
            for _ in range(3):
                with engine.connect() as conn:
                    conn.execute(sqlalchemy.text("SELECT 1"))

        assert len(opened) == 3, "each connect() must open its own connection"
        for dbapi_conn in opened:
            with pytest.raises(sqlite3.ProgrammingError):
                dbapi_conn.execute("SELECT 1")
        assert engine.pool.status().startswith("NullPool")


class TestOdbcDriverPoolingIsDisabled:
    """pyodbc's ODBC driver-manager pooling would keep a closed connection
    open below SQLAlchemy, so it is turned off for ``mssql+pyodbc``."""

    _MSSQL_URL = "mssql+pyodbc://svc@host/DB?driver=ODBC+Driver+17+for+SQL+Server"

    @pytest.fixture(autouse=True)
    def _clean_engine_cache(self):
        get_engine.cache_clear()
        yield
        get_engine.cache_clear()

    @pytest.fixture()
    def fake_pyodbc(self, monkeypatch):
        module = types.ModuleType("pyodbc")
        module.pooling = True
        monkeypatch.setitem(sys.modules, "pyodbc", module)
        return module

    def test_mssql_pyodbc_source_disables_pooling_before_engine_build(self, fake_pyodbc):
        seen_at_build: list[bool] = []

        def _record(*_args, **_kwargs):
            seen_at_build.append(fake_pyodbc.pooling)
            return MagicMock()

        with cfg.override_settings(db_connection_url=self._MSSQL_URL), \
             patch("database.connection.create_engine", side_effect=_record):
            get_engine()
        assert seen_at_build == [False]

    def test_non_pyodbc_source_leaves_pooling_alone(self, fake_pyodbc):
        with cfg.override_settings(db_connection_url="sqlite:///:memory:"), \
             patch("database.connection.create_engine"):
            get_engine()
        assert fake_pyodbc.pooling is True

    def test_missing_pyodbc_does_not_raise(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pyodbc", None)  # import -> ImportError
        with cfg.override_settings(db_connection_url=self._MSSQL_URL), \
             patch("database.connection.create_engine") as mock_create:
            get_engine()
        mock_create.assert_called_once()


class TestApplicationNameIsApplied:
    def test_mssql_pyodbc_url_gets_an_application_name(self):
        get_engine.cache_clear()
        url = "mssql+pyodbc://user@myhost/Auction_DM?driver=ODBC+Driver+17+for+SQL+Server"
        with cfg.override_settings(db_connection_url=url, db_application_name="local-sql-agent"), \
             patch("database.connection.create_engine") as mock_create:
            get_engine()
        called_url = mock_create.call_args.args[0]
        assert "APP=local-sql-agent" in called_url
        get_engine.cache_clear()

    def test_sqlite_url_used_by_this_suite_is_untouched(self):
        get_engine.cache_clear()
        url = "sqlite:///:memory:"
        with cfg.override_settings(db_connection_url=url), \
             patch("database.connection.create_engine") as mock_create:
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
             patch("database.connection.create_engine") as mock_create:
            engine_bare = get_engine()
            engine_named = get_engine("main")
        assert engine_bare is engine_named
        mock_create.assert_called_once()

    def test_two_sources_build_two_engines_with_the_right_urls(self, two_sources):
        with cfg.override_settings(project_config_dir=str(two_sources)), \
             patch("database.connection.create_engine") as mock_create:
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
             patch("database.connection.create_engine") as mock_create:
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
             patch("database.connection.create_engine", side_effect=mock_engines):
            get_engine("main")
            get_engine("archive")
            dispose_engine()
        for engine in mock_engines:
            engine.dispose.assert_called_once()


class TestStructuredSourcesBuildTheirEngines:
    """``create_engine`` is handed the URL built from the YAML fields and the
    raw password in the environment -- host, database and password intact,
    whatever characters the password holds."""

    PASSWORD_A = "p@ss%41]:/?#&=+; w"
    PASSWORD_B = "100% sure"

    @pytest.fixture(autouse=True)
    def _clean_engine_cache(self):
        get_engine.cache_clear()
        reset_datasources_cache()
        yield
        get_engine.cache_clear()
        reset_datasources_cache()

    @pytest.fixture()
    def structured(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(yaml.dump({
            "default": "auction",
            "datasources": {
                "auction": {
                    "host": "db1.example.test", "database": "SalesDW",
                    "username": "nlq_reader", "password_env": "DB_PASSWORD_SALES",
                    "options": {"TrustServerCertificate": True},
                    "application_name": "app-auction",
                },
                "futures": {
                    "host": "db1.example.test", "port": 1444, "database": "FuturesDW",
                    "username": "nlq_reader", "password_env": "DB_PASSWORD_FUTURES",
                },
                "windows": {
                    "host": "db2.example.test", "database": "ReportsDW",
                    "trusted_connection": True,
                },
            },
        }), encoding="utf-8")
        monkeypatch.setenv("DB_PASSWORD_SALES", self.PASSWORD_A)
        monkeypatch.setenv("DB_PASSWORD_FUTURES", self.PASSWORD_B)
        return tmp_path

    def _url_passed_for(self, project_dir, source):
        from sqlalchemy.engine import make_url

        with cfg.override_settings(project_config_dir=str(project_dir)), \
             patch("database.connection.create_engine") as mock_create:
            get_engine(source)
        mock_create.assert_called_once()
        return make_url(mock_create.call_args.args[0])

    def test_each_source_gets_its_own_url(self, structured):
        auction = self._url_passed_for(structured, "auction")
        assert (auction.host, auction.port, auction.database) == ("db1.example.test", 1433, "SalesDW")
        assert auction.username == "nlq_reader"
        assert auction.drivername == "mssql+pyodbc"
        assert auction.query["driver"] == "ODBC Driver 18 for SQL Server"
        assert auction.query["TrustServerCertificate"] == "yes"

    def test_two_databases_on_one_server_are_two_pools(self, structured):
        get_engine.cache_clear()
        with cfg.override_settings(project_config_dir=str(structured)), \
             patch("database.connection.create_engine") as mock_create:
            get_engine("auction")
            get_engine("futures")
        from sqlalchemy.engine import make_url

        urls = [make_url(c.args[0]) for c in mock_create.call_args_list]
        assert [u.host for u in urls] == ["db1.example.test"] * 2
        assert [u.database for u in urls] == ["SalesDW", "FuturesDW"]
        assert [u.port for u in urls] == [1433, 1444]

    def test_the_raw_password_reaches_create_engine_intact(self, structured):
        assert self._url_passed_for(structured, "auction").password == self.PASSWORD_A
        get_engine.cache_clear()
        assert self._url_passed_for(structured, "futures").password == self.PASSWORD_B

    def test_application_name_is_stamped_without_disturbing_the_password(self, structured):
        url = self._url_passed_for(structured, "auction")
        assert url.query["APP"] == "app-auction"
        assert url.password == self.PASSWORD_A

    def test_trusted_connection_engine_has_no_login(self, structured):
        url = self._url_passed_for(structured, "windows")
        assert (url.username, url.password) == (None, None)
        assert url.query["trusted_connection"] == "yes"
        assert url.host == "db2.example.test"

    def test_a_missing_password_variable_stops_the_engine_naming_it(
        self, structured, monkeypatch,
    ):
        from database.datasources import DataSourceConfigError

        monkeypatch.delenv("DB_PASSWORD_SALES")
        with cfg.override_settings(project_config_dir=str(structured)), \
             patch("database.connection.create_engine") as mock_create:
            with pytest.raises(DataSourceConfigError, match="auction.*DB_PASSWORD_SALES"):
                get_engine("auction")
        mock_create.assert_not_called()


class TestSingleSourceDbPassword:
    @pytest.fixture(autouse=True)
    def _clean_engine_cache(self):
        get_engine.cache_clear()
        yield
        get_engine.cache_clear()

    def test_db_password_is_set_on_the_url_passed_to_create_engine(self, tmp_path):
        from sqlalchemy.engine import make_url

        raw = "p@ss/w:rd?#%40"
        with cfg.override_settings(
            project_config_dir=str(tmp_path),
            db_connection_url="mssql+pyodbc://nlq@db1:1433/SalesDW?driver=ODBC+Driver+18+for+SQL+Server",
            db_password=raw,
        ), patch("database.connection.create_engine") as mock_create:
            get_engine()
        url = make_url(mock_create.call_args.args[0])
        assert url.password == raw
        assert url.username == "nlq"
        assert url.query["APP"] == cfg.settings.db_application_name

    def test_without_db_password_the_url_is_passed_as_written(self, tmp_path):
        from sqlalchemy.engine import make_url

        with cfg.override_settings(
            project_config_dir=str(tmp_path),
            db_connection_url="mssql+pyodbc://nlq:p%40ss@db1/SalesDW?driver=x",
            db_password="",
        ), patch("database.connection.create_engine") as mock_create:
            get_engine()
        assert make_url(mock_create.call_args.args[0]).password == "p@ss"
