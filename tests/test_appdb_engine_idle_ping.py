# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``appdb.engine.build_engine``'s non-SQLite branch -- the idle-aware-ping
follow-up to Finding 1 (2026 warehouse-load audit) applies to the
application database's own engine too, whenever ``APP_DB_URL`` points at a
real server (PostgreSQL, SQL Server, ...) rather than the SQLite fallback.

``create_engine`` and ``install_idle_aware_ping`` are both mocked here --
this file is only about whether ``build_engine`` calls the latter at all
and with what arguments for a non-SQLite URL, exactly like
``tests/test_connection.py`` does for the warehouse engine.
``install_idle_aware_ping``'s own idle/threshold/failure behaviour is
covered once, against a real pool, in ``tests/test_idle_aware_ping.py`` --
it is not re-tested per call site.
"""

from __future__ import annotations

from unittest.mock import patch

import config as cfg
from appdb.engine import build_engine

_NON_SQLITE_URL = "postgresql://appuser:pw@localhost:5432/appdb"


class TestNonSqliteAppDbUsesIdleAwarePing:
    def test_defaults_to_installing_idle_aware_ping(self):
        with patch("appdb.engine.create_engine") as mock_create, \
             patch("appdb.engine.install_idle_aware_ping") as mock_install:
            build_engine(_NON_SQLITE_URL)
        mock_create.assert_called_once_with(_NON_SQLITE_URL)
        mock_install.assert_called_once_with(
            mock_create.return_value, cfg.settings.db_pool_ping_idle_seconds
        )

    def test_honours_db_pool_pre_ping_false(self):
        with cfg.override_settings(db_pool_pre_ping=False), \
             patch("appdb.engine.create_engine") as mock_create, \
             patch("appdb.engine.install_idle_aware_ping") as mock_install:
            build_engine(_NON_SQLITE_URL)
        mock_install.assert_not_called()

    def test_honours_db_pool_ping_idle_seconds_override(self):
        with cfg.override_settings(db_pool_ping_idle_seconds=0), \
             patch("appdb.engine.create_engine") as mock_create, \
             patch("appdb.engine.install_idle_aware_ping") as mock_install:
            build_engine(_NON_SQLITE_URL)
        assert mock_install.call_args.args[1] == 0

    def test_sqlite_branch_is_unaffected(self):
        """A SQLite URL never reaches the idle-aware-ping install at all --
        it keeps its own pragma-setting ``"connect"`` listener instead (see
        ``build_engine``'s docstring)."""
        with patch("appdb.engine.install_idle_aware_ping") as mock_install:
            engine = build_engine("sqlite:///:memory:")
        mock_install.assert_not_called()
        engine.dispose()
