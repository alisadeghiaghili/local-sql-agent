# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``database.connection.get_engine`` -- pool-shape settings actually
read from ``cfg.settings`` (Findings 1 and 5, 2026 warehouse-load audit).

``create_engine`` itself is always mocked here (never a real engine --
this suite's root ``_no_real_database`` guard would refuse it anyway):
these tests are about what ``get_engine`` PASSES to ``create_engine``,
not about a real connection.
"""

from __future__ import annotations

from unittest.mock import patch

import config as cfg
from database.connection import get_engine


class TestPoolPrePingIsConfigurable:
    def test_defaults_to_true(self):
        get_engine.cache_clear()
        with patch("database.connection.create_engine") as mock_create:
            get_engine()
        assert mock_create.call_args.kwargs["pool_pre_ping"] is True
        get_engine.cache_clear()

    def test_honours_db_pool_pre_ping_false(self):
        get_engine.cache_clear()
        with cfg.override_settings(db_pool_pre_ping=False), \
             patch("database.connection.create_engine") as mock_create:
            get_engine()
        assert mock_create.call_args.kwargs["pool_pre_ping"] is False
        get_engine.cache_clear()

    def test_honours_db_pool_pre_ping_true_explicitly(self):
        get_engine.cache_clear()
        with cfg.override_settings(db_pool_pre_ping=True), \
             patch("database.connection.create_engine") as mock_create:
            get_engine()
        assert mock_create.call_args.kwargs["pool_pre_ping"] is True
        get_engine.cache_clear()


class TestPoolRecycleIsConfigurable:
    def test_defaults_to_3600(self):
        get_engine.cache_clear()
        with patch("database.connection.create_engine") as mock_create:
            get_engine()
        assert mock_create.call_args.kwargs["pool_recycle"] == 3600
        get_engine.cache_clear()

    def test_honours_db_pool_recycle_seconds(self):
        get_engine.cache_clear()
        with cfg.override_settings(db_pool_recycle_seconds=120), \
             patch("database.connection.create_engine") as mock_create:
            get_engine()
        assert mock_create.call_args.kwargs["pool_recycle"] == 120
        get_engine.cache_clear()


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
