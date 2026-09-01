# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for the v2 conversational endpoints — docs/api-contract-v2.md §3, §7.

Mirrors ``tests/test_api_endpoints.py``'s fixture style: lifespan is
skipped (the system prompt is pre-set directly), and the LLM/DB layer is
replaced by an injected ``TurnEngine`` built with a ``MockBackend`` +
in-memory execute stub — no Ollama or SQL Server required.
"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api.server as server_module
import api.v2_routes as v2_routes
from llm.providers import MockBackend
from llm.router import LLMRouter
from session.engine import TurnEngine

SIMPLE_SQL = "SELECT TOP 10 c.Name AS CustomerName FROM Customer c"
SIMPLE_DF = pd.DataFrame({"CustomerName": ["A", "B"]})


@pytest.fixture()
def client_and_engine(auth_settings):
    server_module._system_prompt = "stub system prompt"
    v2_routes._system_prompt = "stub system prompt"
    v2_routes._reset_for_testing()

    engine = TurnEngine(
        router=LLMRouter(default_chain=[MockBackend(response=SIMPLE_SQL)]),
        execute_fn=lambda sql: SIMPLE_DF.copy(),
    )
    v2_routes._turn_engine = engine  # bypass the lazy singleton for this test

    client = TestClient(
        server_module.app, raise_server_exceptions=False, headers=auth_settings,
    )
    yield client, engine
    v2_routes._reset_for_testing()


class TestCreateSession:
    def test_create_session_returns_id(self, client_and_engine):
        client, _ = client_and_engine
        resp = client.post("/v2/sessions")
        assert resp.status_code == 201
        body = resp.json()
        assert body["session_id"].startswith("s_")
        assert body["created_at"]


class TestGetSession:
    def test_unknown_session_is_404(self, client_and_engine):
        client, _ = client_and_engine
        resp = client.get("/v2/sessions/s_nope")
        assert resp.status_code == 404

    def test_known_session_lists_turns(self, client_and_engine):
        client, _ = client_and_engine
        sid = client.post("/v2/sessions").json()["session_id"]
        client.post(f"/v2/sessions/{sid}/turns", json={"question": "لیست مشتریان"})
        resp = client.get(f"/v2/sessions/{sid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == sid
        assert len(body["turns"]) == 1


class TestDeleteSession:
    def test_delete_then_get_is_404(self, client_and_engine):
        client, _ = client_and_engine
        sid = client.post("/v2/sessions").json()["session_id"]
        resp = client.delete(f"/v2/sessions/{sid}")
        assert resp.status_code == 204
        assert client.get(f"/v2/sessions/{sid}").status_code == 404

    def test_delete_unknown_session_is_idempotent(self, client_and_engine):
        client, _ = client_and_engine
        assert client.delete("/v2/sessions/s_nope").status_code == 204


class TestAskTurn:
    def test_unknown_session_is_404(self, client_and_engine):
        client, _ = client_and_engine
        resp = client.post("/v2/sessions/s_nope/turns", json={"question": "q"})
        assert resp.status_code == 404

    def test_blank_question_is_422(self, client_and_engine):
        client, _ = client_and_engine
        sid = client.post("/v2/sessions").json()["session_id"]
        resp = client.post(f"/v2/sessions/{sid}/turns", json={"question": "   "})
        assert resp.status_code == 422

    def test_happy_path_returns_full_turn_shape(self, client_and_engine):
        client, _ = client_and_engine
        sid = client.post("/v2/sessions").json()["session_id"]
        resp = client.post(f"/v2/sessions/{sid}/turns", json={"question": "لیست مشتریان"})
        assert resp.status_code == 200
        turn = resp.json()
        assert turn["session_id"] == sid
        assert turn["index"] == 1
        assert turn["question"] == "لیست مشتریان"
        assert turn["basis"]["kind"] == "fresh"
        assert turn["sql"].startswith("SELECT")
        assert turn["result"]["row_count"] == 2
        assert turn["error"] is None
        assert turn["llm"] is not None
        assert set(turn["timings"].keys()) == {
            "total_ms", "plan_ms", "prompt_ms", "llm_ms", "guard_ms", "execute_ms", "interpret_ms",
        }

    def test_a_turn_never_surfaces_as_an_http_error_status(self, client_and_engine):
        """§5: a turn-level failure is data in the response body (200 + Turn.error),
        never an HTTP error status -- that would defeat "answer, then declare"."""
        client, engine = client_and_engine

        class _BrokenBackend:
            name = "broken"

            def generate_with_meta_segments(self, segments):
                raise RuntimeError("connection refused")

        v2_routes._turn_engine = TurnEngine(
            router=LLMRouter(default_chain=[_BrokenBackend()]),
            execute_fn=lambda sql: SIMPLE_DF.copy(),
        )
        sid = client.post("/v2/sessions").json()["session_id"]
        resp = client.post(f"/v2/sessions/{sid}/turns", json={"question": "چیزی"})
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == "MODEL_UNAVAILABLE"


class TestAskTurnStreaming:
    def test_stream_emits_contract_events_in_order(self, client_and_engine):
        client, _ = client_and_engine
        sid = client.post("/v2/sessions").json()["session_id"]
        with client.stream(
            "POST", f"/v2/sessions/{sid}/turns?stream=1", json={"question": "لیست مشتریان"},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join(resp.iter_text())

        events = [line[len("event: "):] for line in body.splitlines() if line.startswith("event:")]
        assert events == ["stage", "resolved", "assumptions", "sql", "rows", "llm", "done"]
        assert "done" in body and '"turn"' in body


class TestPatchAssumptions:
    def test_unknown_turn_is_404(self, client_and_engine):
        client, _ = client_and_engine
        sid = client.post("/v2/sessions").json()["session_id"]
        client.post(f"/v2/sessions/{sid}/turns", json={"question": "لیست مشتریان"})
        resp = client.patch(
            f"/v2/sessions/{sid}/turns/t_nope/assumptions",
            json={"assumptions": [{"field": "ring", "value": "x"}]},
        )
        assert resp.status_code == 404

    def test_patch_returns_new_turn_without_mutating_original(self, client_and_engine):
        client, _ = client_and_engine
        sid = client.post("/v2/sessions").json()["session_id"]
        original = client.post(
            f"/v2/sessions/{sid}/turns", json={"question": "۱۰ مشتری برتر را نشان بده"},
        ).json()
        assert original["ambiguity"]["is_ambiguous"] is True

        resp = client.patch(
            f"/v2/sessions/{sid}/turns/{original['turn_id']}/assumptions",
            json={"assumptions": [{"field": "ring", "value": "تالار فلزات"}]},
        )
        assert resp.status_code == 200
        patched = resp.json()
        assert patched["turn_id"] != original["turn_id"]

        transcript = client.get(f"/v2/sessions/{sid}").json()["turns"]
        assert len(transcript) == 2
        assert transcript[0] == original  # original entry in the transcript is untouched
        ring_assumption = next(a for a in patched["ambiguity"]["assumptions"] if a["field"] == "ring")
        assert ring_assumption["value"] == "تالار فلزات"


class TestCORS:
    def test_preflight_is_allowed_for_a_configured_origin(self):
        from config import override_settings

        with override_settings(cors_allowed_origins=("http://localhost:8080",)):
            import importlib
            importlib.reload(server_module)
            server_module._system_prompt = "stub"
            client = TestClient(server_module.app, raise_server_exceptions=False)
            resp = client.options(
                "/v2/sessions",
                headers={
                    "Origin": "http://localhost:8080",
                    "Access-Control-Request-Method": "POST",
                },
            )
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:8080"
        importlib.reload(server_module)  # restore the module-level app for later tests

    def test_default_config_blocks_unlisted_origins(self, client_and_engine):
        client, _ = client_and_engine
        resp = client.options(
            "/v2/sessions",
            headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers.keys()}
