# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""D3 (2026 hall-filter audit) -- a failed startup vocabulary warm-up must
never stop the server from starting.

``config.Settings.dimension_vocabulary_warm_on_startup`` defaults to
``True`` (see that field's own docstring), so ``api/server.py``'s
``lifespan`` calls ``retrieval.dimension_vocabulary.warm_all`` on every real
startup. This drives that call through a REAL ``TestClient`` context
manager (the only way to actually execute FastAPI's lifespan startup/
shutdown, unlike a bare ``TestClient(app)`` — see
``tests/test_api_endpoints.py``'s ``app_and_client`` fixture, which
deliberately avoids the context-manager form to skip lifespan) with
``warm_all`` forced to raise, and asserts the app still comes up and
``GET /health`` still answers -- the one behaviour this phase's own
try/except around that call exists to guarantee.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from config import override_settings


class TestStartupWarmupFailureIsNonFatal:
    def test_failing_prefetch_still_serves_health(self):
        import api.server as server_module

        with override_settings(
            dimension_vocabulary_warm_on_startup=True,
            # lifespan's own startup checks, unrelated to this test's own
            # concern, otherwise refuse to start first: the factory-default
            # DB_CONNECTION_URL placeholder (same value
            # tests/test_config.py's test_validate_passes uses) and
            # AUTH_REQUIRED with no usable key configured.
            db_connection_url=(
                "mssql+pyodbc://prod-db-host:1433/RealDB"
                "?driver=ODBC+Driver+17+for+SQL+Server"
            ),
            auth_required=False,
        ):
            with patch(
                "retrieval.dimension_vocabulary.warm_all",
                side_effect=RuntimeError("simulated warehouse outage"),
            ) as mock_warm_all:
                with TestClient(server_module.app) as client:
                    resp = client.get("/health")

                assert mock_warm_all.called

        assert resp.status_code == 200
