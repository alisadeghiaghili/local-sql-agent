# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The audit record of a session turn names the key that asked.

``/query`` has always written ``principal_id`` (``api.runner``); the turns of
``/v2/sessions`` wrote ``null`` until 6.15.1, so the audit log of a web UI
could not say which key asked what. Every route that runs a turn passes the
caller's id to the engine: a first turn, a follow-up, the streamed variant and
the re-run of ``PATCH .../assumptions``.
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
from tests.conftest import TEST_PRINCIPAL_ID

SIMPLE_SQL = "SELECT TOP 10 c.Name AS CustomerName FROM Customer c"
SIMPLE_DF = pd.DataFrame({"CustomerName": ["A", "B"]})


@pytest.fixture()
def client_and_records(auth_settings, monkeypatch):
    """A test client on a real engine, and the list of audit records it writes."""
    server_module._system_prompt = "stub system prompt"
    v2_routes._system_prompt = "stub system prompt"
    v2_routes._reset_for_testing()
    v2_routes._turn_engine = TurnEngine(
        router=LLMRouter(default_chain=[MockBackend(response=SIMPLE_SQL)]),
        execute_fn=lambda sql: SIMPLE_DF.copy(),
    )
    records: list = []
    monkeypatch.setattr("session.engine.save_audit_record", records.append)
    client = TestClient(server_module.app, raise_server_exceptions=False, headers=auth_settings)
    yield client, records
    v2_routes._reset_for_testing()


class TestWebPathAuditNamesTheCaller:
    def test_a_first_turn(self, client_and_records):
        client, records = client_and_records
        sid = client.post("/v2/sessions").json()["session_id"]
        assert client.post(f"/v2/sessions/{sid}/turns", json={"question": "لیست مشتریان"}).status_code == 200
        assert [r.principal_id for r in records] == [TEST_PRINCIPAL_ID]

    def test_a_follow_up_turn(self, client_and_records):
        client, records = client_and_records
        sid = client.post("/v2/sessions").json()["session_id"]
        client.post(f"/v2/sessions/{sid}/turns", json={"question": "لیست مشتریان"})
        client.post(f"/v2/sessions/{sid}/turns", json={"question": "فقط ده تا"})
        assert [r.principal_id for r in records] == [TEST_PRINCIPAL_ID, TEST_PRINCIPAL_ID]
        assert records[0].turn_id != records[1].turn_id

    def test_a_streamed_turn(self, client_and_records):
        client, records = client_and_records
        sid = client.post("/v2/sessions").json()["session_id"]
        with client.stream(
            "POST", f"/v2/sessions/{sid}/turns?stream=1", json={"question": "لیست مشتریان"},
        ) as resp:
            "".join(resp.iter_text())
        assert [r.principal_id for r in records] == [TEST_PRINCIPAL_ID]

    def test_the_rerun_of_a_patched_assumption(self, client_and_records):
        client, records = client_and_records
        sid = client.post("/v2/sessions").json()["session_id"]
        original = client.post(
            f"/v2/sessions/{sid}/turns", json={"question": "۱۰ مشتری برتر را نشان بده"},
        ).json()
        resp = client.patch(
            f"/v2/sessions/{sid}/turns/{original['turn_id']}/assumptions",
            json={"assumptions": [{"field": "ring", "value": "تالار فلزات"}]},
        )
        assert resp.status_code == 200
        assert [r.principal_id for r in records] == [TEST_PRINCIPAL_ID, TEST_PRINCIPAL_ID]

    def test_a_turn_that_fails_is_audited_with_the_caller_too(self, client_and_records):
        client, records = client_and_records

        class _Broken:
            name = "broken"

            def generate_with_meta_segments(self, segments):
                raise RuntimeError("connection refused")

        v2_routes._turn_engine = TurnEngine(
            router=LLMRouter(default_chain=[_Broken()]), execute_fn=lambda sql: SIMPLE_DF.copy(),
        )
        sid = client.post("/v2/sessions").json()["session_id"]
        client.post(f"/v2/sessions/{sid}/turns", json={"question": "چیزی"})
        assert [r.principal_id for r in records] == [TEST_PRINCIPAL_ID]
        assert records[0].error_code == "MODEL_UNAVAILABLE"


class TestEngineDefault:
    def test_without_a_principal_the_field_stays_empty(self):
        from session.store import SessionStore

        records: list = []
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response=SIMPLE_SQL)]),
            execute_fn=lambda sql: SIMPLE_DF.copy(),
        )
        record = SessionStore().create()
        from unittest.mock import patch

        with patch("session.engine.save_audit_record", records.append):
            engine.ask(record, "لیست مشتریان", "stub system prompt")
            engine.ask(record, "لیست مشتریان", "stub system prompt", principal_id="analyst-7")
        assert [r.principal_id for r in records] == [None, "analyst-7"]

    def test_every_other_audit_field_is_as_before(self):
        from unittest.mock import patch

        from session.store import SessionStore

        records: list = []
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response=SIMPLE_SQL)]),
            execute_fn=lambda sql: SIMPLE_DF.copy(),
        )
        record = SessionStore().create()
        with patch("session.engine.save_audit_record", records.append):
            turn = engine.ask(record, "لیست مشتریان", "stub system prompt", principal_id="analyst-7")
        (audit,) = records
        assert audit.session_id == turn.session_id and audit.turn_id == turn.turn_id
        assert audit.question == "لیست مشتریان"
        assert audit.generated_sql == turn.sql
        assert audit.row_count == turn.result.row_count
        assert set(audit.as_dict()) >= {"timestamp", "request_id", "question", "principal_id", "turn_id"}


class TestRecordsWithoutTheFieldStillLoad:
    def test_an_old_line_with_no_principal_id_is_read_back(self, tmp_path):
        import json
        from unittest.mock import patch

        from observability.audit import find_record_by_turn

        line = {
            "timestamp": "2026-01-01T00:00:00", "request_id": "r1", "question": "q",
            "generated_sql": "SELECT 1", "guard": {"verdict": "allowed"}, "row_count": 0,
            "tier": "T2", "session_id": "s_1", "turn_id": "t_1",
        }
        log = tmp_path / "audit_log.jsonl"
        log.write_text(json.dumps(line) + "\n", encoding="utf-8")
        with patch("observability.audit._AUDIT_LOG_FILE", str(log)):
            found = find_record_by_turn("s_1", "t_1")
        assert found is not None and found.get("principal_id") is None
