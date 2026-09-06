# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 4 — the analyst-facing flag endpoints. Frozen spec.

Uses a REAL ``TestClient(api.server.app)``, a REAL
``session.persistence.SessionPersistence`` on a REAL ``tmp_path`` SQLite
file, a REAL application database (``appdb``) on a second REAL
``tmp_path`` SQLite file, a REAL audit log file, and two REAL, distinct
API-key principals (mirroring ``tests/test_v2_session_memory_endpoints.py``'s
own two-principal pattern) -- no mock stands in for the store, the
databases, or the caller's identity. Only the LLM backend and query
execution are stubbed, the same dependency-injection seam every other v2
test in this suite already uses.

Covers the phase 4 spec's §7 requirements that are genuinely HTTP-layer
concerns:

* An analyst may flag only their own turns, and may not read feedback on
  another principal's session (§7's second and third bullets).
* A flag references a real turn, and the join to its audit record returns
  the question and SQL (§7's first bullet) -- proven end to end through
  the real HTTP surface here; ``tests/test_appdb_feedback.py`` covers the
  same join at the storage layer directly, plus the raw-database-bytes
  check.
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

RAW_KEY_1 = "feedback-route-test-principal-one-" + "0" * 10
RAW_KEY_2 = "feedback-route-test-principal-two-" + "0" * 10


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


TWO_PRINCIPALS_JSON = json.dumps([
    {"id": "analyst-1", "name": "Analyst One", "key_sha256": _sha256(RAW_KEY_1)},
    {"id": "analyst-2", "name": "Analyst Two", "key_sha256": _sha256(RAW_KEY_2)},
])
HEADERS_1 = {"Authorization": f"Bearer {RAW_KEY_1}"}
HEADERS_2 = {"Authorization": f"Bearer {RAW_KEY_2}"}


@pytest.fixture()
def client(tmp_path):
    """Two authenticated principals; real session persistence, real
    application database, and a real (file-backed) audit log -- all on
    ``tmp_path``, so :func:`observability.audit.find_record_by_turn` (the
    join every route here depends on) reads genuine files rather than a
    patched module attribute."""
    server_module._system_prompt = "stub system prompt"
    v2_routes._system_prompt = "stub system prompt"

    with override_settings(
        auth_required=True,
        api_keys_json=TWO_PRINCIPALS_JSON,
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


def _ask(client, headers) -> tuple[str, str]:
    sid = client.post("/v2/sessions", headers=headers).json()["session_id"]
    turn = client.post(
        f"/v2/sessions/{sid}/turns", json={"question": "چند مشتری فعال داریم؟"}, headers=headers,
    ).json()
    return sid, turn["turn_id"]


# ---------------------------------------------------------------------------
# POST .../feedback -- submitting a flag, and the join it proves
# ---------------------------------------------------------------------------


class TestSubmitFeedback:
    def test_flagging_own_turn_succeeds_and_the_join_works(self, client):
        sid, tid = _ask(client, HEADERS_1)
        resp = client.post(
            f"/v2/sessions/{sid}/turns/{tid}/feedback",
            json={"category": "wrong_number", "note": "چهار رقم آخر عجیب است"},
            headers=HEADERS_1,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["session_id"] == sid
        assert body["turn_id"] == tid
        assert body["category"] == "wrong_number"
        assert body["status"] == "open"
        # The audit join succeeded (phase 4.2.0's session_id/turn_id exist
        # precisely for this): request_id was resolved from the real audit
        # log, not left null.
        assert body["request_id"], "feedback row must carry the joined request_id"
        # Never duplicated onto the feedback row (spec §2.2):
        assert "question" not in body
        assert "generated_sql" not in body
        assert "sql" not in body

    def test_invalid_category_is_rejected(self, client):
        sid, tid = _ask(client, HEADERS_1)
        resp = client.post(
            f"/v2/sessions/{sid}/turns/{tid}/feedback",
            json={"category": "not_a_real_category"},
            headers=HEADERS_1,
        )
        assert resp.status_code == 422

    def test_unknown_turn_is_404(self, client):
        sid, _tid = _ask(client, HEADERS_1)
        resp = client.post(
            f"/v2/sessions/{sid}/turns/t_bogus/feedback",
            json={"category": "other"},
            headers=HEADERS_1,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# §7: an analyst may flag only their OWN turns
# ---------------------------------------------------------------------------


class TestOwnershipBoundary:
    def test_cannot_flag_another_principals_turn(self, client):
        sid, tid = _ask(client, HEADERS_1)
        resp = client.post(
            f"/v2/sessions/{sid}/turns/{tid}/feedback",
            json={"category": "wrong_number"},
            headers=HEADERS_2,
        )
        assert resp.status_code == 404, (
            "flagging a turn on another principal's session must fail as "
            "'unknown session' (404), never succeed and never leak a 403 "
            "that would itself confirm the session exists"
        )

    def test_cannot_read_feedback_on_another_principals_session(self, client):
        sid, tid = _ask(client, HEADERS_1)
        client.post(
            f"/v2/sessions/{sid}/turns/{tid}/feedback",
            json={"category": "wrong_number"},
            headers=HEADERS_1,
        )
        resp = client.get(f"/v2/sessions/{sid}/turns/{tid}/feedback", headers=HEADERS_2)
        assert resp.status_code == 404, (
            "reading feedback on a turn that belongs to another principal's "
            "session must fail the same way as reading the session itself"
        )

    def test_owner_can_read_their_own_flag_back(self, client):
        sid, tid = _ask(client, HEADERS_1)
        client.post(
            f"/v2/sessions/{sid}/turns/{tid}/feedback",
            json={"category": "other", "note": "توضیح"},
            headers=HEADERS_1,
        )
        resp = client.get(f"/v2/sessions/{sid}/turns/{tid}/feedback", headers=HEADERS_1)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["feedback"]) == 1
        assert body["feedback"][0]["category"] == "other"
