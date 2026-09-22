# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""ADR-004 part 1 -- the analyst-facing "Request access" endpoints.

Mirrors ``tests/test_v2_feedback_routes.py`` exactly: a REAL
``TestClient(api.server.app)``, a REAL session store, a REAL application
database, a REAL audit log, and two REAL, distinct API-key principals --
no mock at the boundary under test. Only the LLM backend and query
execution are stubbed, the same injected-backend seam every other v2 test
in this suite uses.
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api.server as server_module
import api.v2_routes as v2_routes
from appdb.engine import dispose_app_engine
from config import override_settings
from llm.providers import MockBackend
from llm.router import LLMRouter
from session.engine import TurnEngine

SIMPLE_SQL = "SELECT TOP 10 c.Name AS CustomerName FROM Customer c"
SIMPLE_DF = pd.DataFrame({"CustomerName": ["A", "B"]})

RAW_KEY_1 = "access-request-route-test-principal-one-" + "0" * 10
RAW_KEY_2 = "access-request-route-test-principal-two-" + "0" * 10

#: The real, underlying column ``SIMPLE_SQL`` selects (``c.Name``, aliased
#: to ``CustomerName`` in the result) -- the guard's column allowlist
#: checks the underlying column, not a result alias, so this (never the
#: alias) is what a ``denied_columns`` entry must name to actually trigger
#: a denied-column guard rejection for this fixture's fixed SQL.
DENIED_COLUMN = "Name"


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


HEADERS_1 = {"Authorization": f"Bearer {RAW_KEY_1}"}
HEADERS_2 = {"Authorization": f"Bearer {RAW_KEY_2}"}


def _ask_denied(client, headers) -> tuple[str, dict]:
    """Create a session and ask a question whose fixed stub SQL selects
    :data:`DENIED_COLUMN` -- against the ``denied_client`` fixture below,
    this is a genuine denied-column guard rejection end to end (not a
    contrived ``Turn`` object), the precondition every test in this module
    needs."""
    sid = client.post("/v2/sessions", headers=headers).json()["session_id"]
    turn = client.post(
        f"/v2/sessions/{sid}/turns", json={"question": "show customer names"}, headers=headers,
    ).json()
    return sid, turn


def _ask(client, headers) -> tuple[str, str]:
    sid = client.post("/v2/sessions", headers=headers).json()["session_id"]
    turn = client.post(
        f"/v2/sessions/{sid}/turns", json={"question": "how many active customers"}, headers=headers,
    ).json()
    return sid, turn["turn_id"]


DENIED_KEYS_JSON = json.dumps([
    {
        "id": "analyst-1", "name": "Analyst One", "key_sha256": _sha256(RAW_KEY_1),
        "denied_columns": [DENIED_COLUMN],
    },
    {"id": "analyst-2", "name": "Analyst Two", "key_sha256": _sha256(RAW_KEY_2), "denied_columns": []},
])


@pytest.fixture()
def denied_client(tmp_path):
    """Two authenticated principals; real session persistence, real
    application database, and a real (file-backed) audit log -- mirrors
    ``tests/test_v2_feedback_routes.py``'s own ``client`` fixture, except
    ``analyst-1``'s key denies :data:`DENIED_COLUMN` -- the column the
    fixture's stub SQL selects -- so every turn analyst-1 asks is a
    genuine denied-column guard rejection."""
    server_module._system_prompt = "stub system prompt"
    v2_routes._system_prompt = "stub system prompt"

    with override_settings(
        auth_required=True,
        api_keys_json=DENIED_KEYS_JSON,
        session_store_path=str(tmp_path / "sessions.db"),
        app_db_url=f"sqlite:///{tmp_path / 'app.db'}",
        log_dir=str(tmp_path / "logs"),
    ):
        dispose_app_engine()
        v2_routes._reset_for_testing()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response=SIMPLE_SQL)]),
            execute_fn=lambda sql: SIMPLE_DF.copy(),
        )
        v2_routes._turn_engine = engine
        test_client = TestClient(server_module.app, raise_server_exceptions=False)
        yield test_client
        v2_routes._reset_for_testing()
    dispose_app_engine()


# ---------------------------------------------------------------------------
# Submit -- the column comes from the audit record, never the client
# ---------------------------------------------------------------------------


class TestSubmitAccessRequest:
    def test_requesting_access_on_a_denied_column_turn_succeeds(self, denied_client):
        sid, turn = _ask_denied(denied_client, HEADERS_1)
        assert turn["guard"]["verdict"] == "rejected"
        assert turn["guard"]["reason"] == "denied_column"
        tid = turn["turn_id"]

        resp = denied_client.post(
            f"/v2/sessions/{sid}/turns/{tid}/access-request", headers=HEADERS_1,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["session_id"] == sid
        assert body["turn_id"] == tid
        assert body["status"] == "open"
        assert body["column_name"] == turn["guard"]["subject"]
        assert body["already_pending"] is False
        assert body["requester_principal_id"] == "analyst-1"

    def test_requesting_access_on_an_allowed_turn_is_refused(self, denied_client):
        sid, tid = _ask(denied_client, HEADERS_2)  # analyst-2 has no denied columns
        resp = denied_client.post(
            f"/v2/sessions/{sid}/turns/{tid}/access-request", headers=HEADERS_2,
        )
        assert resp.status_code == 422

    def test_unknown_turn_is_404(self, denied_client):
        sid, _ = _ask_denied(denied_client, HEADERS_1)
        resp = denied_client.post(
            f"/v2/sessions/{sid}/turns/t_bogus/access-request", headers=HEADERS_1,
        )
        assert resp.status_code == 404

    def test_the_column_comes_from_the_audit_record_even_if_the_client_posts_a_different_one(
        self, denied_client,
    ):
        """The submit endpoint takes no request body at all (path
        parameters only) -- posting an arbitrary JSON body must not let a
        client smuggle a column name in; it is silently ignored, and the
        server-resolved column (from the audit record's guard.subject)
        wins."""
        sid, turn = _ask_denied(denied_client, HEADERS_1)
        tid = turn["turn_id"]
        resp = denied_client.post(
            f"/v2/sessions/{sid}/turns/{tid}/access-request",
            headers=HEADERS_1,
            json={"column_name": "TotallyDifferentColumn"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["column_name"] == turn["guard"]["subject"]
        assert resp.json()["column_name"] != "TotallyDifferentColumn"


# ---------------------------------------------------------------------------
# Dedup -- a second submit merges into the existing open request
# ---------------------------------------------------------------------------


class TestDedup:
    def test_second_submit_for_the_same_turns_column_returns_the_existing_request(self, denied_client):
        sid, turn = _ask_denied(denied_client, HEADERS_1)
        tid = turn["turn_id"]

        first = denied_client.post(
            f"/v2/sessions/{sid}/turns/{tid}/access-request", headers=HEADERS_1,
        )
        assert first.status_code == 201
        first_id = first.json()["request_id"]

        second = denied_client.post(
            f"/v2/sessions/{sid}/turns/{tid}/access-request", headers=HEADERS_1,
        )
        assert second.status_code == 200
        body = second.json()
        assert body["already_pending"] is True
        assert body["request_id"] == first_id

        own = denied_client.get("/v2/access-requests", headers=HEADERS_1).json()
        assert len(own["access_requests"]) == 1


# ---------------------------------------------------------------------------
# Ownership -- an analyst may request access only on their OWN turns
# (mirrors TestOwnershipBoundary in test_v2_feedback_routes.py)
# ---------------------------------------------------------------------------


class TestOwnershipBoundary:
    def test_cannot_request_access_on_another_principals_turn(self, denied_client):
        sid, turn = _ask_denied(denied_client, HEADERS_1)
        tid = turn["turn_id"]
        resp = denied_client.post(
            f"/v2/sessions/{sid}/turns/{tid}/access-request", headers=HEADERS_2,
        )
        assert resp.status_code == 404, (
            "requesting access on a turn belonging to another principal's "
            "session must fail as 'unknown session' (404), never succeed "
            "and never leak a 403 that would itself confirm the session exists"
        )
        # And it must not have created a request under analyst-2's name either.
        own = denied_client.get("/v2/access-requests", headers=HEADERS_2).json()
        assert own["access_requests"] == []


# ---------------------------------------------------------------------------
# Analyst status read -- scoped to the caller alone
# ---------------------------------------------------------------------------


class TestOwnStatusRead:
    def test_caller_sees_only_their_own_requests(self, denied_client):
        sid1, turn1 = _ask_denied(denied_client, HEADERS_1)
        denied_client.post(
            f"/v2/sessions/{sid1}/turns/{turn1['turn_id']}/access-request", headers=HEADERS_1,
        )

        resp1 = denied_client.get("/v2/access-requests", headers=HEADERS_1)
        assert resp1.status_code == 200
        rows1 = resp1.json()["access_requests"]
        assert len(rows1) == 1
        assert rows1[0]["requester_principal_id"] == "analyst-1"

        resp2 = denied_client.get("/v2/access-requests", headers=HEADERS_2)
        assert resp2.status_code == 200
        assert resp2.json()["access_requests"] == []

    def test_a_denial_reason_is_visible_to_the_requester_in_their_own_status_read(self, denied_client):
        from appdb.access_requests import deny_request

        sid, turn = _ask_denied(denied_client, HEADERS_1)
        resp = denied_client.post(
            f"/v2/sessions/{sid}/turns/{turn['turn_id']}/access-request", headers=HEADERS_1,
        )
        request_id = resp.json()["request_id"]
        deny_request(request_id, actor_principal_id="security-1", reason="Column is under legal hold.")

        rows = denied_client.get("/v2/access-requests", headers=HEADERS_1).json()["access_requests"]
        assert rows[0]["status"] == "denied"
        assert rows[0]["resolution_note"] == "Column is under legal hold."
