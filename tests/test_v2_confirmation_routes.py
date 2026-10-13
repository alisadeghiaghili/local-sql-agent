# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The analyst-facing "this answer is correct" endpoints.

``POST``/``DELETE /v2/sessions/{sid}/turns/{tid}/confirmation`` and
``GET /v2/sessions/{sid}/confirmations``.

Mirrors ``tests/test_v2_feedback_routes.py`` exactly: a REAL
``TestClient(api.server.app)``, a REAL session store, a REAL application
database, a REAL audit log file and two REAL, distinct API-key principals
-- no mock at the boundary under test. Only the LLM backend and query
execution are stubbed, the same injected-backend seam the other v2 tests
use.

Covers authorisation (the turn's owner only; 404 for anyone else), the 409
for a turn that did not run successfully, idempotence, withdrawal, and the
flag/confirmation interplay -- the later action wins and the earlier one is
removed.
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api.server as server_module
import api.v2_routes as v2_routes
from appdb.confirmations import list_confirmations
from appdb.engine import dispose_app_engine
from appdb.feedback import list_feedback, resolve_feedback
from config import override_settings
from llm.providers import MockBackend
from llm.router import LLMRouter
from session.engine import TurnEngine

SIMPLE_SQL = "SELECT TOP 10 c.Name AS CustomerName FROM Customer c"
SIMPLE_DF = pd.DataFrame({"CustomerName": ["A", "B"]})

#: ``SIMPLE_SQL`` selects the underlying column ``Name``; denying it for
#: analyst-1 turns every question that principal asks into a genuine
#: denied-column guard rejection -- an unsuccessful turn end to end.
DENIED_COLUMN = "Name"

RAW_KEY_1 = "confirmation-route-test-principal-one-" + "0" * 10
RAW_KEY_2 = "confirmation-route-test-principal-two-" + "0" * 10
RAW_KEY_3 = "confirmation-route-test-principal-three-" + "0" * 10


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


KEYS_JSON = json.dumps([
    {"id": "analyst-1", "name": "Analyst One", "key_sha256": _sha256(RAW_KEY_1)},
    {"id": "analyst-2", "name": "Analyst Two", "key_sha256": _sha256(RAW_KEY_2)},
    {
        "id": "analyst-3", "name": "Analyst Three", "key_sha256": _sha256(RAW_KEY_3),
        "denied_columns": [DENIED_COLUMN],
    },
])
HEADERS_1 = {"Authorization": f"Bearer {RAW_KEY_1}"}
HEADERS_2 = {"Authorization": f"Bearer {RAW_KEY_2}"}
HEADERS_3 = {"Authorization": f"Bearer {RAW_KEY_3}"}


@pytest.fixture()
def client(tmp_path):
    server_module._system_prompt = "stub system prompt"
    v2_routes._system_prompt = "stub system prompt"

    with override_settings(
        auth_required=True,
        api_keys_json=KEYS_JSON,
        session_store_path=str(tmp_path / "sessions.db"),
        app_db_url=f"sqlite:///{tmp_path / 'app.db'}",
        log_dir=str(tmp_path / "logs"),
    ):
        dispose_app_engine()
        v2_routes._reset_for_testing()
        v2_routes._turn_engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response=SIMPLE_SQL)]),
            execute_fn=lambda sql: SIMPLE_DF.copy(),
        )
        test_client = TestClient(server_module.app, raise_server_exceptions=False)
        yield test_client
        v2_routes._reset_for_testing()
    dispose_app_engine()


def _ask(client, headers) -> tuple[str, str]:
    sid = client.post("/v2/sessions", headers=headers).json()["session_id"]
    turn = client.post(
        f"/v2/sessions/{sid}/turns", json={"question": "چند مشتری فعال داریم؟"}, headers=headers,
    ).json()
    return sid, turn["turn_id"]


def _url(sid: str, tid: str) -> str:
    return f"/v2/sessions/{sid}/turns/{tid}/confirmation"


def _flag(client, sid, tid, headers):
    return client.post(
        f"/v2/sessions/{sid}/turns/{tid}/feedback", json={"category": "wrong_number"},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# POST -- confirming
# ---------------------------------------------------------------------------


class TestConfirm:
    def test_confirming_own_successful_turn_returns_201_with_identifiers_only(self, client):
        sid, tid = _ask(client, HEADERS_1)
        resp = client.post(_url(sid, tid), headers=HEADERS_1)

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["confirmed"] is True
        assert body["already_confirmed"] is False
        assert body["session_id"] == sid and body["turn_id"] == tid
        assert body["principal_id"] == "analyst-1", "stamped server-side from the key"
        assert body["request_id"], "resolved from the audit record, not the client"
        assert body["removed_flags"] == 0
        for forbidden in ("question", "generated_sql", "sql", "rows", "result"):
            assert forbidden not in body

    def test_confirming_again_is_idempotent_200(self, client):
        sid, tid = _ask(client, HEADERS_1)
        first = client.post(_url(sid, tid), headers=HEADERS_1).json()
        again = client.post(_url(sid, tid), headers=HEADERS_1)

        assert again.status_code == 200
        assert again.json()["already_confirmed"] is True
        assert again.json()["confirmation_id"] == first["confirmation_id"]
        assert len(list_confirmations()) == 1

    def test_the_client_cannot_supply_the_principal_or_request_id(self, client):
        sid, tid = _ask(client, HEADERS_1)
        resp = client.post(
            _url(sid, tid), headers=HEADERS_1,
            json={"principal_id": "analyst-2", "request_id": "r_forged"},
        )
        assert resp.status_code == 201
        assert resp.json()["principal_id"] == "analyst-1"
        assert resp.json()["request_id"] != "r_forged"

    def test_unknown_turn_is_404(self, client):
        sid, _tid = _ask(client, HEADERS_1)
        assert client.post(_url(sid, "t_bogus"), headers=HEADERS_1).status_code == 404

    def test_unknown_session_is_404(self, client):
        assert client.post(_url("s_bogus", "t_1"), headers=HEADERS_1).status_code == 404

    def test_a_turn_that_was_guard_rejected_is_409_with_a_clear_message(self, client):
        sid = client.post("/v2/sessions", headers=HEADERS_3).json()["session_id"]
        turn = client.post(
            f"/v2/sessions/{sid}/turns", json={"question": "show customer names"},
            headers=HEADERS_3,
        ).json()
        assert turn["guard"]["verdict"] == "rejected", "precondition: an unsuccessful turn"

        resp = client.post(_url(sid, turn["turn_id"]), headers=HEADERS_3)

        assert resp.status_code == 409
        message = json.dumps(resp.json(), ensure_ascii=False)
        assert "ran and returned a result" in message
        assert list_confirmations() == []

    def test_a_turn_without_an_audit_record_is_409(self, client, tmp_path):
        sid, tid = _ask(client, HEADERS_1)
        for log in (tmp_path / "logs").glob("audit_log.jsonl*"):
            log.write_text("", encoding="utf-8")

        resp = client.post(_url(sid, tid), headers=HEADERS_1)

        assert resp.status_code == 409
        assert "audit record" in json.dumps(resp.json(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Authorisation -- the turn's owner only
# ---------------------------------------------------------------------------


class TestOwnershipBoundary:
    def test_cannot_confirm_another_principals_turn(self, client):
        sid, tid = _ask(client, HEADERS_1)
        resp = client.post(_url(sid, tid), headers=HEADERS_2)
        assert resp.status_code == 404, (
            "must fail as 'unknown session', never succeed and never leak a 403"
        )
        assert list_confirmations() == []

    def test_cannot_withdraw_via_another_principals_session(self, client):
        sid, tid = _ask(client, HEADERS_1)
        client.post(_url(sid, tid), headers=HEADERS_1)
        resp = client.delete(_url(sid, tid), headers=HEADERS_2)
        assert resp.status_code == 404
        assert len(list_confirmations()) == 1, "the confirmation must survive"

    def test_cannot_list_another_principals_confirmations(self, client):
        sid, tid = _ask(client, HEADERS_1)
        client.post(_url(sid, tid), headers=HEADERS_1)
        assert client.get(f"/v2/sessions/{sid}/confirmations", headers=HEADERS_2).status_code == 404

    def test_a_missing_key_is_401(self, client):
        sid, tid = _ask(client, HEADERS_1)
        assert client.post(_url(sid, tid)).status_code == 401
        assert client.delete(_url(sid, tid)).status_code == 401


# ---------------------------------------------------------------------------
# DELETE -- withdrawing
# ---------------------------------------------------------------------------


class TestWithdraw:
    def test_withdrawing_removes_the_callers_confirmation(self, client):
        sid, tid = _ask(client, HEADERS_1)
        client.post(_url(sid, tid), headers=HEADERS_1)

        resp = client.delete(_url(sid, tid), headers=HEADERS_1)

        assert resp.status_code == 200
        assert resp.json() == {"confirmed": False, "withdrawn": True}
        assert list_confirmations() == []

    def test_withdrawing_when_nothing_is_confirmed_is_200_withdrawn_false(self, client):
        sid, tid = _ask(client, HEADERS_1)
        resp = client.delete(_url(sid, tid), headers=HEADERS_1)
        assert resp.status_code == 200
        assert resp.json()["withdrawn"] is False

    def test_unknown_turn_is_404(self, client):
        sid, _tid = _ask(client, HEADERS_1)
        assert client.delete(_url(sid, "t_bogus"), headers=HEADERS_1).status_code == 404

    def test_the_list_read_shows_the_turns_the_caller_confirmed(self, client):
        sid, first = _ask(client, HEADERS_1)
        second = client.post(
            f"/v2/sessions/{sid}/turns", json={"question": "چند سفارش داریم؟"}, headers=HEADERS_1,
        ).json()["turn_id"]
        client.post(_url(sid, first), headers=HEADERS_1)

        body = client.get(f"/v2/sessions/{sid}/confirmations", headers=HEADERS_1).json()

        assert body["turn_ids"] == [first]
        assert second not in body["turn_ids"]
        assert len(body["confirmations"]) == 1


# ---------------------------------------------------------------------------
# Flag and confirmation -- the later action wins, the earlier one is removed
# ---------------------------------------------------------------------------


class TestFlagConfirmInterplay:
    def test_confirming_a_turn_the_caller_flagged_removes_the_open_flag(self, client):
        sid, tid = _ask(client, HEADERS_1)
        assert _flag(client, sid, tid, HEADERS_1).status_code == 201

        resp = client.post(_url(sid, tid), headers=HEADERS_1)

        assert resp.status_code == 201
        assert resp.json()["removed_flags"] == 1
        assert list_feedback() == []
        assert len(list_confirmations()) == 1

    def test_flagging_a_turn_the_caller_confirmed_removes_the_confirmation(self, client):
        sid, tid = _ask(client, HEADERS_1)
        client.post(_url(sid, tid), headers=HEADERS_1)

        resp = _flag(client, sid, tid, HEADERS_1)

        assert resp.status_code == 201
        assert resp.json()["withdrew_confirmation"] is True
        assert list_confirmations() == []
        assert len(list_feedback()) == 1

    def test_flagging_without_a_confirmation_reports_nothing_withdrawn(self, client):
        sid, tid = _ask(client, HEADERS_1)
        assert _flag(client, sid, tid, HEADERS_1).json()["withdrew_confirmation"] is False

    def test_the_last_of_several_alternations_wins(self, client):
        sid, tid = _ask(client, HEADERS_1)
        client.post(_url(sid, tid), headers=HEADERS_1)
        _flag(client, sid, tid, HEADERS_1)
        client.post(_url(sid, tid), headers=HEADERS_1)

        assert len(list_confirmations()) == 1
        assert list_feedback() == []

    def test_a_flag_an_admin_already_resolved_is_kept_when_the_reporter_confirms(self, client):
        sid, tid = _ask(client, HEADERS_1)
        flag = _flag(client, sid, tid, HEADERS_1).json()
        resolve_feedback(
            flag["feedback_id"], outcome="not_a_defect", actor_principal_id="admin-1",
            note="the number was right",
        )

        resp = client.post(_url(sid, tid), headers=HEADERS_1)

        assert resp.json()["removed_flags"] == 0
        assert len(list_feedback()) == 1, "triage recorded a decision on it; it must not vanish"
        assert len(list_confirmations()) == 1

    def test_it_is_per_principal_one_analysts_action_never_removes_anothers(self, client):
        """Two principals can both reach the same session only through the
        store directly (a session is owned by one), so this drives the
        storage layer for the second principal and the routes for the
        first."""
        from appdb.confirmations import confirm_turn
        from appdb.feedback import submit_flag

        sid, tid = _ask(client, HEADERS_1)
        confirm_turn(session_id=sid, turn_id=tid, principal_id="analyst-2")
        submit_flag(
            session_id=sid, turn_id=tid, reporter_principal_id="analyst-2", category="other",
        )

        client.post(_url(sid, tid), headers=HEADERS_1)
        _flag(client, sid, tid, HEADERS_1)

        by_principal = {c["principal_id"] for c in list_confirmations()}
        assert by_principal == {"analyst-2"}, "analyst-1's flag removed only analyst-1's confirmation"
        flaggers = {f["reporter_principal_id"] for f in list_feedback()}
        assert flaggers == {"analyst-1", "analyst-2"}
