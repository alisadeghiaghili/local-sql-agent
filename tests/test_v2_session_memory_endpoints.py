# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Endpoint tests for the new §3/§5 routes: the conversation index
(``GET``/``PATCH /v2/sessions*``) and cross-session memory
(``GET``/``PUT``/``DELETE /v2/memory*``).

Uses a REAL ``TestClient(api.server.app)``, a REAL
``session.persistence.SessionPersistence`` on a REAL ``tmp_path`` SQLite
file, and two REAL, distinct API-key principals (mirroring
``tests/test_auth.py``'s ``TWO_PRINCIPALS_JSON`` pattern) -- no mock
stands in for the store, the database file, or the caller's identity.
Only the LLM backend and query execution are stubbed, the same
dependency-injection seam every other v2 test in this suite already uses.
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api.server as server_module
import api.v2_routes as v2_routes
from config import override_settings
from knowledge.memory_policy import get_memory_keys
from llm.providers import MockBackend
from llm.router import LLMRouter
from session.engine import TurnEngine

SIMPLE_SQL = "SELECT TOP 10 c.Name AS CustomerName FROM Customer c"
SIMPLE_DF = pd.DataFrame({"CustomerName": ["A", "B"]})

RAW_KEY_1 = "session-memory-test-principal-one-" + "0" * 10
RAW_KEY_2 = "session-memory-test-principal-two-" + "0" * 10


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


TWO_PRINCIPALS_JSON = json.dumps([
    {"id": "analyst-1", "name": "Analyst One", "key_sha256": _sha256(RAW_KEY_1)},
    {"id": "analyst-2", "name": "Analyst Two", "key_sha256": _sha256(RAW_KEY_2)},
])
HEADERS_1 = {"Authorization": f"Bearer {RAW_KEY_1}"}
HEADERS_2 = {"Authorization": f"Bearer {RAW_KEY_2}"}


def _the_one_declared_key() -> tuple[str, object]:
    keys = get_memory_keys()
    key = next(iter(keys))
    return key, keys[key]


@pytest.fixture()
def client_with_persistence(tmp_path):
    """Two authenticated principals, one real SessionStore backed by a
    real SQLite file at ``tmp_path``, one injected TurnEngine (no live LLM
    or database)."""
    server_module._system_prompt = "stub system prompt"
    v2_routes._system_prompt = "stub system prompt"

    with override_settings(
        auth_required=True,
        api_keys_json=TWO_PRINCIPALS_JSON,
        session_store_path=str(tmp_path / "sessions.db"),
    ):
        v2_routes._reset_for_testing()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response=SIMPLE_SQL)]),
            execute_fn=lambda sql: SIMPLE_DF.copy(),
        )
        v2_routes._turn_engine = engine
        client = TestClient(server_module.app, raise_server_exceptions=False)
        yield client
        v2_routes._reset_for_testing()


# ---------------------------------------------------------------------------
# GET /v2/sessions — the conversation index
# ---------------------------------------------------------------------------


class TestSessionIndex:
    def test_index_lists_only_the_callers_own_sessions(self, client_with_persistence):
        client = client_with_persistence
        sid_1 = client.post("/v2/sessions", headers=HEADERS_1).json()["session_id"]
        client.post("/v2/sessions", headers=HEADERS_2).json()["session_id"]

        resp = client.get("/v2/sessions", headers=HEADERS_1)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["sessions"][0]["session_id"] == sid_1

    def test_index_shape_matches_the_frozen_contract(self, client_with_persistence):
        client = client_with_persistence
        sid = client.post("/v2/sessions", headers=HEADERS_1).json()["session_id"]
        client.post(f"/v2/sessions/{sid}/turns", json={"question": "سوال یک"}, headers=HEADERS_1)

        body = client.get("/v2/sessions", headers=HEADERS_1).json()
        assert body["total"] == 1
        entry = body["sessions"][0]
        assert set(entry) == {
            "session_id", "title", "created_at", "last_active_at", "turn_count", "expires_at",
        }
        assert entry["turn_count"] == 1
        assert entry["title"] is not None

    def test_title_derived_from_first_question(self, client_with_persistence):
        client = client_with_persistence
        sid = client.post("/v2/sessions", headers=HEADERS_1).json()["session_id"]
        distinctive_question = "پرسش‌بسیارمتمایز۷۷۷۷ درباره مشتریان را نشان بده"
        client.post(
            f"/v2/sessions/{sid}/turns", json={"question": distinctive_question}, headers=HEADERS_1,
        )

        entry = client.get("/v2/sessions", headers=HEADERS_1).json()["sessions"][0]
        assert entry["title"].startswith("پرسش‌بسیارمتمایز۷۷۷۷")

    def test_a_session_never_shown_to_a_non_owner(self, client_with_persistence):
        client = client_with_persistence
        client.post("/v2/sessions", headers=HEADERS_1)
        body = client.get("/v2/sessions", headers=HEADERS_2).json()
        assert body["total"] == 0


# ---------------------------------------------------------------------------
# PATCH /v2/sessions/{sid} — rename
# ---------------------------------------------------------------------------


class TestRenameSession:
    def test_rename_updates_the_title(self, client_with_persistence):
        client = client_with_persistence
        sid = client.post("/v2/sessions", headers=HEADERS_1).json()["session_id"]

        resp = client.patch(
            f"/v2/sessions/{sid}", json={"title": "عنوان دلخواه من"}, headers=HEADERS_1,
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "عنوان دلخواه من"

        entry = client.get("/v2/sessions", headers=HEADERS_1).json()["sessions"][0]
        assert entry["title"] == "عنوان دلخواه من"

    def test_rename_of_another_principals_session_is_404(self, client_with_persistence):
        client = client_with_persistence
        sid = client.post("/v2/sessions", headers=HEADERS_1).json()["session_id"]
        resp = client.patch(f"/v2/sessions/{sid}", json={"title": "x"}, headers=HEADERS_2)
        assert resp.status_code == 404

    def test_title_with_newline_is_422(self, client_with_persistence):
        client = client_with_persistence
        sid = client.post("/v2/sessions", headers=HEADERS_1).json()["session_id"]
        resp = client.patch(
            f"/v2/sessions/{sid}", json={"title": "line one\nline two"}, headers=HEADERS_1,
        )
        assert resp.status_code == 422

    def test_title_never_enters_a_prompt(self, client_with_persistence):
        """§3: a title is presentation only and never enters a prompt."""
        client = client_with_persistence
        sid = client.post("/v2/sessions", headers=HEADERS_1).json()["session_id"]
        distinctive_title = "عنوان-کاملا-غیرقابل-حدس-۵۵۵۵"
        client.patch(f"/v2/sessions/{sid}", json={"title": distinctive_title}, headers=HEADERS_1)

        captured: list[str] = []

        class _RecordingBackend:
            name = "recording"

            def generate_with_meta_segments(self, segments):
                captured.append(segments.flatten())
                return SIMPLE_SQL, {"raw": {}, "endpoint_status": 200, "attempts": 1}

        v2_routes._turn_engine = TurnEngine(
            router=LLMRouter(default_chain=[_RecordingBackend()]),
            execute_fn=lambda sql: SIMPLE_DF.copy(),
        )
        resp = client.post(
            f"/v2/sessions/{sid}/turns", json={"question": "معاملات را نشان بده"}, headers=HEADERS_1,
        )
        assert resp.status_code == 200
        assert captured, "the mocked backend was never called"
        assert distinctive_title not in captured[0]


# ---------------------------------------------------------------------------
# GET/PUT/DELETE /v2/memory*
# ---------------------------------------------------------------------------


class TestMemoryEndpoints:
    def test_get_memory_lists_rememberable_keys_even_with_no_entries(self, client_with_persistence):
        client = client_with_persistence
        resp = client.get("/v2/memory", headers=HEADERS_1)
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"] == []
        assert len(body["rememberable"]) >= 1

    def test_put_then_get_round_trips(self, client_with_persistence):
        client = client_with_persistence
        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"

        resp = client.put(f"/v2/memory/{key}", json={"value": value}, headers=HEADERS_1)
        assert resp.status_code == 200
        assert resp.json()["value"] == value

        entries = client.get("/v2/memory", headers=HEADERS_1).json()["entries"]
        assert len(entries) == 1
        assert entries[0]["key"] == key
        assert entries[0]["value"] == value
        assert entries[0]["applicable"] is True

    def test_put_unknown_key_is_422(self, client_with_persistence):
        client = client_with_persistence
        resp = client.put(
            "/v2/memory/not-a-declared-key", json={"value": "x"}, headers=HEADERS_1,
        )
        assert resp.status_code == 422

    def test_put_value_with_newline_is_422_and_stores_nothing(self, client_with_persistence):
        client = client_with_persistence
        key, _ = _the_one_declared_key()
        resp = client.put(f"/v2/memory/{key}", json={"value": "a\nb"}, headers=HEADERS_1)
        assert resp.status_code == 422
        assert client.get("/v2/memory", headers=HEADERS_1).json()["entries"] == []

    def test_put_value_over_cap_is_422_and_stores_nothing(self, client_with_persistence):
        client = client_with_persistence
        key, key_cfg = _the_one_declared_key()
        too_long = "x" * (key_cfg.max_length + 1)
        resp = client.put(f"/v2/memory/{key}", json={"value": too_long}, headers=HEADERS_1)
        assert resp.status_code == 422
        assert client.get("/v2/memory", headers=HEADERS_1).json()["entries"] == []

    def test_delete_one_entry(self, client_with_persistence):
        client = client_with_persistence
        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"
        client.put(f"/v2/memory/{key}", json={"value": value}, headers=HEADERS_1)

        resp = client.delete(f"/v2/memory/{key}", headers=HEADERS_1)
        assert resp.status_code == 204
        assert client.get("/v2/memory", headers=HEADERS_1).json()["entries"] == []

    def test_delete_unknown_key_is_idempotent(self, client_with_persistence):
        client = client_with_persistence
        resp = client.delete("/v2/memory/never-set", headers=HEADERS_1)
        assert resp.status_code == 204

    def test_delete_all(self, client_with_persistence):
        client = client_with_persistence
        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"
        client.put(f"/v2/memory/{key}", json={"value": value}, headers=HEADERS_1)

        resp = client.delete("/v2/memory", headers=HEADERS_1)
        assert resp.status_code == 204
        assert client.get("/v2/memory", headers=HEADERS_1).json()["entries"] == []

    def test_max_entries_per_principal_exceeded_is_explicit_422(self, client_with_persistence):
        client = client_with_persistence
        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"

        with override_settings(memory_max_entries_per_principal=0):
            # memory_max_entries_per_principal=0 means even the FIRST new
            # key is already "exceeded" -- an explicit error, never a
            # silently-discarded write.
            resp = client.put(f"/v2/memory/{key}", json={"value": value}, headers=HEADERS_1)
        assert resp.status_code == 422
        assert client.get("/v2/memory", headers=HEADERS_1).json()["entries"] == []


class TestMemoryCrossPrincipalIsolation:
    def test_principal_b_cannot_read_principal_as_entries(self, client_with_persistence):
        client = client_with_persistence
        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"
        client.put(f"/v2/memory/{key}", json={"value": value}, headers=HEADERS_1)

        entries_b = client.get("/v2/memory", headers=HEADERS_2).json()["entries"]
        assert entries_b == []

    def test_principal_b_cannot_delete_principal_as_entry(self, client_with_persistence):
        client = client_with_persistence
        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"
        client.put(f"/v2/memory/{key}", json={"value": value}, headers=HEADERS_1)

        client.delete(f"/v2/memory/{key}", headers=HEADERS_2)  # 204, but must not touch A's entry

        entries_a = client.get("/v2/memory", headers=HEADERS_1).json()["entries"]
        assert len(entries_a) == 1
        assert entries_a[0]["value"] == value

    def test_principal_b_writing_the_same_key_does_not_overwrite_principal_as(self, client_with_persistence):
        client = client_with_persistence
        key, key_cfg = _the_one_declared_key()
        if not key_cfg.options or len(key_cfg.options) < 2:
            pytest.skip("declared key needs at least two closed options for this test")
        value_a, value_b = key_cfg.options[0], key_cfg.options[1]

        client.put(f"/v2/memory/{key}", json={"value": value_a}, headers=HEADERS_1)
        client.put(f"/v2/memory/{key}", json={"value": value_b}, headers=HEADERS_2)

        entries_a = client.get("/v2/memory", headers=HEADERS_1).json()["entries"]
        entries_b = client.get("/v2/memory", headers=HEADERS_2).json()["entries"]
        assert entries_a[0]["value"] == value_a
        assert entries_b[0]["value"] == value_b
