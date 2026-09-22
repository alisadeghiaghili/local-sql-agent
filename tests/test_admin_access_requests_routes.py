# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""ADR-004 part 1 -- ``/admin/access-requests/*`` at the HTTP boundary.

Mirrors ``tests/test_admin_feedback_routes.py``'s own shape: a real
FastAPI app, a real ``TestClient``, a real application database on a real
temp SQLite file, a real (copied) ``project_config.example/``, a real
session store, and a real (injected-backend) ``TurnEngine`` -- no mock at
the boundary under test.

``tests/test_appdb_access_requests.py`` covers :mod:`appdb.access_requests`
itself in depth (dedup, every-live-key approval, revoked/disabled key
handling); this module covers the thin HTTP surface: role gating
(``security`` only, unlike the feedback queue's either-role gate), the
audit join, and the admin-action log.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import config as cfg
from appdb.admin_audit import iter_admin_actions
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache
from llm.providers import MockBackend
from llm.router import LLMRouter
from session.engine import TurnEngine

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"

SIMPLE_SQL = "SELECT TOP 10 c.Name AS CustomerName FROM Customer c"
SIMPLE_DF = pd.DataFrame({"CustomerName": ["A", "B"]})

#: The real, underlying column SIMPLE_SQL selects -- see
#: tests/test_v2_access_request_routes.py's DENIED_COLUMN for why the
#: guard's allowlist check needs this, never the result alias.
DENIED_COLUMN = "Name"


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RAW_OPS_KEY = "1" * 40
RAW_SECURITY_KEY = "2" * 40
RAW_ANALYST_KEY = "3" * 40

_KEYS_JSON = json.dumps([
    {"id": "ops-admin", "name": "Ops Admin", "key_sha256": _sha256(RAW_OPS_KEY), "operations": True},
    {
        "id": "security-admin", "name": "Security Admin",
        "key_sha256": _sha256(RAW_SECURITY_KEY), "security": True,
    },
    {
        "id": "analyst-1", "name": "Analyst One", "key_sha256": _sha256(RAW_ANALYST_KEY),
        "denied_columns": [DENIED_COLUMN],
    },
])


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


@pytest.fixture(autouse=True)
def _reset_shared_state():
    import api.v2_routes as v2_routes
    from api.query_cache import query_cache

    v2_routes._reset_for_testing()
    query_cache.reconfigure(ttl_seconds=300, max_size=256)
    query_cache.clear()
    yield
    v2_routes._reset_for_testing()
    query_cache.clear()


@pytest.fixture()
def app_db(tmp_path):
    project_dir = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE_CONFIG_DIR, project_dir)
    db_path = tmp_path / "appdb.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}",
        api_keys_json=_KEYS_JSON,
        project_config_dir=str(project_dir),
        session_store_path=str(tmp_path / "sessions.db"),
        log_dir=str(log_dir),
    ):
        dispose_app_engine()
        invalidate_cache()
        yield {"project_dir": project_dir, "log_dir": log_dir}
    dispose_app_engine()
    invalidate_cache()


@pytest.fixture()
def client(app_db):
    import api.server as server_module
    import api.v2_routes as v2_routes

    server_module._system_prompt = "stub system prompt"
    v2_routes._system_prompt = "stub system prompt"
    engine = TurnEngine(
        router=LLMRouter(default_chain=[MockBackend(response=SIMPLE_SQL)]),
        execute_fn=lambda sql: SIMPLE_DF.copy(),
    )
    v2_routes._turn_engine = engine
    return TestClient(server_module.app, raise_server_exceptions=False)


def _ask_denied_and_request_access(client) -> dict:
    """Create a session, ask a question that trips the denied-column guard
    (real analyst auth, a real audit record), submit an access request for
    it, and return the stored request."""
    sid = client.post("/v2/sessions", headers=_auth(RAW_ANALYST_KEY)).json()["session_id"]
    turn = client.post(
        f"/v2/sessions/{sid}/turns", json={"question": "show customer names"},
        headers=_auth(RAW_ANALYST_KEY),
    ).json()
    assert turn["guard"]["verdict"] == "rejected", turn
    assert turn["guard"]["reason"] == "denied_column", turn
    resp = client.post(
        f"/v2/sessions/{sid}/turns/{turn['turn_id']}/access-request",
        headers=_auth(RAW_ANALYST_KEY),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Role gating -- SECURITY only (unlike the feedback queue's either-role
# gate), for both listing and acting
# ---------------------------------------------------------------------------


class TestRoleGating:
    def test_analyst_key_gets_403_on_the_queue(self, client):
        _ask_denied_and_request_access(client)
        resp = client.get("/admin/access-requests", headers=_auth(RAW_ANALYST_KEY))
        assert resp.status_code == 403

    def test_operations_key_gets_403_on_the_queue(self, client):
        """Unlike the feedback triage queue (either admin role may
        triage), an access request is a data-visibility change -- the
        same 403 any other security-only admin route already gives
        `operations`."""
        _ask_denied_and_request_access(client)
        resp = client.get("/admin/access-requests", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 403

    def test_security_key_can_read_the_queue(self, client):
        _ask_denied_and_request_access(client)
        resp = client.get("/admin/access-requests", headers=_auth(RAW_SECURITY_KEY))
        assert resp.status_code == 200

    def test_analyst_key_cannot_approve(self, client):
        row = _ask_denied_and_request_access(client)
        resp = client.post(
            f"/admin/access-requests/{row['request_id']}/approve", headers=_auth(RAW_ANALYST_KEY),
        )
        assert resp.status_code == 403

    def test_operations_key_cannot_approve(self, client):
        row = _ask_denied_and_request_access(client)
        resp = client.post(
            f"/admin/access-requests/{row['request_id']}/approve", headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 403

    def test_operations_key_cannot_deny(self, client):
        row = _ask_denied_and_request_access(client)
        resp = client.post(
            f"/admin/access-requests/{row['request_id']}/deny",
            json={"reason": "No."}, headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 403

    def test_security_key_can_approve(self, client):
        row = _ask_denied_and_request_access(client)
        resp = client.post(
            f"/admin/access-requests/{row['request_id']}/approve", headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_security_key_can_deny(self, client):
        row = _ask_denied_and_request_access(client)
        resp = client.post(
            f"/admin/access-requests/{row['request_id']}/deny",
            json={"reason": "Column is under legal hold."}, headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "denied"


# ---------------------------------------------------------------------------
# The queue joins to the audit record's question
# ---------------------------------------------------------------------------


class TestQueueJoinsTheAuditRecord:
    def test_list_shows_the_question(self, client):
        _ask_denied_and_request_access(client)
        resp = client.get("/admin/access-requests", headers=_auth(RAW_SECURITY_KEY))
        assert resp.status_code == 200
        rows = resp.json()["access_requests"]
        assert len(rows) == 1
        assert rows[0]["audit"]["question"] == "show customer names"
        assert rows[0]["column_name"] == DENIED_COLUMN

    def test_get_one_carries_the_same_join(self, client):
        row = _ask_denied_and_request_access(client)
        resp = client.get(
            f"/admin/access-requests/{row['request_id']}", headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["audit"]["question"] == "show customer names"

    def test_unknown_request_id_is_404(self, client):
        resp = client.get("/admin/access-requests/999999", headers=_auth(RAW_SECURITY_KEY))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Deny requires a reason
# ---------------------------------------------------------------------------


class TestDenyRequiresAReason:
    def test_blank_reason_is_rejected(self, client):
        row = _ask_denied_and_request_access(client)
        resp = client.post(
            f"/admin/access-requests/{row['request_id']}/deny",
            json={"reason": ""}, headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 422

    def test_missing_reason_is_rejected(self, client):
        row = _ask_denied_and_request_access(client)
        resp = client.post(
            f"/admin/access-requests/{row['request_id']}/deny",
            json={}, headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Approval actually widens the analyst's key -- end to end through HTTP
# ---------------------------------------------------------------------------


class TestApprovalEndToEnd:
    def test_approved_analyst_can_now_ask_the_previously_denied_question(self, client):
        # appdb.access_requests.approve_request only ever touches DB-tracked
        # keys (appdb.key_store.list_keys) -- an API_KEYS_JSON-only
        # principal (this fixture's default) has no admin_api_keys row at
        # all, the same structural limit PATCH /admin/keys/{id}/acl already
        # has for a purely environment-sourced key. bootstrap_from_env
        # (normally called once at real server start-up,
        # api/server.py's lifespan) imports every API_KEYS_JSON entry into
        # the (empty) key table first, so this end-to-end assertion is
        # actually exercising the widened row, not a no-op.
        from appdb.key_store import bootstrap_from_env

        bootstrap_from_env()

        row = _ask_denied_and_request_access(client)
        approve_resp = client.post(
            f"/admin/access-requests/{row['request_id']}/approve", headers=_auth(RAW_SECURITY_KEY),
        )
        assert approve_resp.status_code == 200

        sid = client.post("/v2/sessions", headers=_auth(RAW_ANALYST_KEY)).json()["session_id"]
        turn = client.post(
            f"/v2/sessions/{sid}/turns", json={"question": "show customer names"},
            headers=_auth(RAW_ANALYST_KEY),
        ).json()
        assert turn["guard"]["verdict"] == "allowed", turn


# ---------------------------------------------------------------------------
# Every mutation is recorded on the admin-action log
# ---------------------------------------------------------------------------


class TestAdminAuditTrail:
    def test_approve_is_recorded(self, client, app_db):
        row = _ask_denied_and_request_access(client)
        client.post(
            f"/admin/access-requests/{row['request_id']}/approve", headers=_auth(RAW_SECURITY_KEY),
        )
        actions = iter_admin_actions()
        matching = [a for a in actions if a["action"] == "access_request.approve"]
        assert len(matching) == 1
        assert matching[0]["actor_principal_id"] == "security-admin"
        assert matching[0]["authorised_by"] == "security"
        assert matching[0]["target"] == str(row["request_id"])
        assert matching[0]["detail"]["column"] == DENIED_COLUMN

    def test_deny_is_recorded_with_the_reason(self, client, app_db):
        row = _ask_denied_and_request_access(client)
        client.post(
            f"/admin/access-requests/{row['request_id']}/deny",
            json={"reason": "Column is under legal hold."}, headers=_auth(RAW_SECURITY_KEY),
        )
        actions = iter_admin_actions()
        matching = [a for a in actions if a["action"] == "access_request.deny"]
        assert len(matching) == 1
        assert matching[0]["detail"]["reason"] == "Column is under legal hold."
