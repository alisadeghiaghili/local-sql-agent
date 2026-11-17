# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``api/server.py``'s ``lifespan`` releases database engines on shutdown.

Drives the real lifespan through ``TestClient`` used as a context manager.
The dispose functions are patched with recorders so the test proves the
shutdown path calls them, and that a failure in one does not stop the
other or break shutdown.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api.server as server_module
from config import override_settings

_SETTINGS = dict(
    db_connection_url=(
        "mssql+pyodbc://prod-db-host:1433/RealDB?driver=ODBC+Driver+17+for+SQL+Server"
    ),
    auth_required=False,
    llm_prefix_warmup_on_startup=False,
    llm_prefix_keepwarm_seconds=0,
)


@pytest.fixture()
def running_app():
    with override_settings(**_SETTINGS), \
         patch("retrieval.dimension_vocabulary.warm_all", return_value={}):
        yield server_module.app


def test_shutdown_disposes_warehouse_and_app_engines(running_app):
    with patch("database.connection.dispose_engine") as warehouse, \
         patch("appdb.engine.dispose_app_engine") as app_db:
        with TestClient(running_app):
            warehouse.assert_not_called()
            app_db.assert_not_called()
        warehouse.assert_called_once_with()
        app_db.assert_called_once_with()


def test_a_failing_warehouse_dispose_still_disposes_the_app_engine(running_app, caplog):
    with patch("database.connection.dispose_engine", side_effect=RuntimeError("boom")), \
         patch("appdb.engine.dispose_app_engine") as app_db:
        with TestClient(running_app):
            pass
        app_db.assert_called_once_with()
    assert "Warehouse engines could not be disposed: RuntimeError" in caplog.text
    assert "boom" not in caplog.text
