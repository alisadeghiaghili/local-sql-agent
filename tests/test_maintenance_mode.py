# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 6, §1 — maintenance mode. Frozen spec.

A switch, not a trap: covers every bullet the spec's §7 "Verification"
section names, against the REAL FastAPI app and a REAL TestClient (the
one thing mocked is ``api.runner.run_query`` itself, at its definition
site — never the maintenance dependency or the middleware stack under
test).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api.maintenance as maintenance
import config as cfg
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RAW_ANALYST_KEY = "1" * 40
RAW_OPS_KEY = "2" * 40

_KEYS_JSON = json.dumps([
    {"id": "analyst-1", "name": "Analyst One", "key_sha256": _sha256(RAW_ANALYST_KEY)},
    {"id": "ops-admin", "name": "Ops Admin", "key_sha256": _sha256(RAW_OPS_KEY), "operations": True},
])


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


@pytest.fixture(autouse=True)
def _reset_maintenance_state():
    maintenance.reset_for_testing()
    yield
    maintenance.reset_for_testing()


@pytest.fixture()
def app_db(tmp_path):
    db_path = tmp_path / "appdb.db"
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}", api_keys_json=_KEYS_JSON, auth_required=True,
    ):
        dispose_app_engine()
        invalidate_cache()
        yield db_path
    dispose_app_engine()
    invalidate_cache()


@pytest.fixture()
def client(app_db):
    import api.server as server_module
    import api.v2_routes as v2_routes
    from api.query_cache import query_cache

    server_module._system_prompt = "stub system prompt"
    v2_routes._reset_for_testing()
    query_cache.clear()
    yield TestClient(server_module.app, raise_server_exceptions=False)
    v2_routes._reset_for_testing()
    query_cache.clear()


def _enable_maintenance(client, note: str | None = "planned window") -> None:
    resp = client.post(
        "/admin/maintenance", json={"active": True, "note": note}, headers=_auth(RAW_OPS_KEY),
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# "Analyst queries are refused with the documented status and a body that
# explains, not a hang."
# ---------------------------------------------------------------------------

class TestAnalystQueryRefused:
    def test_query_gets_503_with_an_explanatory_body(self, client):
        with patch("api.runner.run_query") as mock_run:
            _enable_maintenance(client, note="database migration in progress")
            resp = client.post(
                "/query", json={"question": "چند مشتری فعال داریم؟"},
                headers=_auth(RAW_ANALYST_KEY),
            )
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "MAINTENANCE_MODE"
        assert "database migration in progress" in body["error"]["message"]
        mock_run.assert_not_called()  # never reached the pipeline at all

    def test_query_succeeds_normally_once_off(self, client):
        from api.models import QueryResponse

        with patch("api.runner.run_query") as mock_run:
            mock_run.return_value = QueryResponse(
                question="q", sql="SELECT 1", result=None, row_count=0,
                correction_attempts=0, elapsed_seconds=0.0,
            )
            _enable_maintenance(client)
            refused = client.post(
                "/query", json={"question": "q"}, headers=_auth(RAW_ANALYST_KEY),
            )
            assert refused.status_code == 503

            off = client.post(
                "/admin/maintenance", json={"active": False}, headers=_auth(RAW_OPS_KEY),
            )
            assert off.status_code == 200
            assert off.json()["active"] is False

            resp = client.post(
                "/query", json={"question": "q"}, headers=_auth(RAW_ANALYST_KEY),
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# "The panel itself stays reachable." Tested explicitly.
# ---------------------------------------------------------------------------

class TestPanelStaysReachable:
    def test_admin_reads_still_work_while_maintenance_is_on(self, client):
        _enable_maintenance(client)
        resp = client.get("/admin/keys", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200

    def test_health_stays_open_while_maintenance_is_on(self, client):
        with patch("api.health.check_health") as mock_health:
            from api.models import HealthResponse

            mock_health.return_value = HealthResponse(status="ok", openai=True, database=True)
            _enable_maintenance(client)
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_index_stays_open_while_maintenance_is_on(self, client):
        _enable_maintenance(client)
        resp = client.get("/")
        assert resp.status_code == 200

    def test_turning_maintenance_back_off_is_never_itself_blocked(self, client):
        """The difference between a switch and a trap: the toggle route
        must remain reachable precisely BECAUSE it never depends on its
        own gate."""
        _enable_maintenance(client)
        resp = client.post(
            "/admin/maintenance", json={"active": False}, headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["active"] is False

    def test_maintenance_state_is_readable_regardless_of_auth_capability_mix(self, client):
        """GET /admin/maintenance is a read -- reachable by either admin
        role (mutual visibility), never blocked by maintenance mode
        itself."""
        _enable_maintenance(client)
        resp = client.get("/admin/maintenance", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200
        assert resp.json()["active"] is True


# ---------------------------------------------------------------------------
# "Application-database writes stop."
# ---------------------------------------------------------------------------

class TestApplicationDatabaseWritesStop:
    def test_issuing_a_key_is_refused_with_503(self, client):
        _enable_maintenance(client)
        resp = client.post(
            "/admin/keys", json={"principal_id": "new-analyst", "name": "New"},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "MAINTENANCE_MODE"

    def test_issuing_a_key_works_again_once_maintenance_is_off(self, client):
        _enable_maintenance(client)
        client.post(
            "/admin/maintenance", json={"active": False}, headers=_auth(RAW_OPS_KEY),
        )
        resp = client.post(
            "/admin/keys", json={"principal_id": "new-analyst", "name": "New"},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# "An in-flight request that started before the switch completes."
# ---------------------------------------------------------------------------

class TestDrainNotCut:
    def test_in_flight_request_completes_after_maintenance_switches_on_mid_flight(self, client):
        from api.models import QueryResponse

        release = threading.Event()
        started = threading.Event()

        def slow_run_query(**kwargs):
            started.set()
            release.wait(timeout=5)
            return QueryResponse(
                question="q", sql="SELECT 1", result=None, row_count=0,
                correction_attempts=0, elapsed_seconds=0.0,
            )

        results: dict[str, int] = {}

        def _fire():
            resp = client.post("/query", json={"question": "q"}, headers=_auth(RAW_ANALYST_KEY))
            results["status"] = resp.status_code

        with patch("api.runner.run_query", side_effect=slow_run_query):
            thread = threading.Thread(target=_fire)
            thread.start()
            assert started.wait(timeout=5), "in-flight request never reached run_query"

            # The request is now PAST its admission check -- switch on
            # maintenance while it is still running.
            _enable_maintenance(client)

            # A brand-new request must be refused immediately...
            refused = client.post(
                "/query", json={"question": "another"}, headers=_auth(RAW_ANALYST_KEY),
            )
            assert refused.status_code == 503

            # ...while the one already in flight drains to completion.
            release.set()
            thread.join(timeout=5)
        assert results["status"] == 200


# ---------------------------------------------------------------------------
# "Both transitions appear in the admin-action trail."
# ---------------------------------------------------------------------------

class TestBothTransitionsAreAudited:
    def test_enable_and_disable_both_appear_in_the_trail(self, client):
        _enable_maintenance(client, note="window A")
        client.post("/admin/maintenance", json={"active": False}, headers=_auth(RAW_OPS_KEY))

        resp = client.get("/admin/actions", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200
        actions = [a["action"] for a in resp.json()["actions"]]
        assert "maintenance.enable" in actions
        assert "maintenance.disable" in actions


# ---------------------------------------------------------------------------
# The toggle response states the drain deadline (spec §1).
# ---------------------------------------------------------------------------

class TestDrainDeadlineStated:
    def test_toggle_on_response_states_a_drain_deadline(self, client):
        with cfg.override_settings(maintenance_drain_deadline_seconds=42):
            resp = client.post(
                "/admin/maintenance", json={"active": True, "note": None},
                headers=_auth(RAW_OPS_KEY),
            )
        assert resp.status_code == 200
        assert resp.json()["drain_deadline_seconds"] == 42
        assert "drain_note" in resp.json()
