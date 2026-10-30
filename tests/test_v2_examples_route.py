# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``GET /v2/examples`` -- questions the analyst page offers to start with.

A REAL ``TestClient(api.server.app)`` with two REAL API-key principals, as
``tests/test_v2_access_request_routes.py`` sets them up; only the content of
``examples.yaml`` is replaced (through :mod:`knowledge.examples`' own cache),
so the guard that decides what is offered is the real one. Pins:

* only question text leaves the server -- never SQL or tags;
* a question whose SQL reads a column the caller may not read is not offered
  to that caller, and is offered to a caller who may;
* SQL the guard refuses for anyone (a write, an unknown table) is never offered;
* order is the file's, duplicates and blank questions are dropped, ``limit``
  is clamped;
* no readable ``examples.yaml`` is an empty list, not an error;
* the route needs a key, like every other v2 route.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import api.server as server_module
import api.v2_routes as v2_routes
import knowledge.examples as examples_module
from appdb.engine import dispose_app_engine
from config import override_settings
from core.start_examples import MAX_LIMIT

RAW_KEY_1 = "examples-route-test-principal-one-" + "0" * 16
RAW_KEY_2 = "examples-route-test-principal-two-" + "0" * 16
HEADERS_1 = {"Authorization": f"Bearer {RAW_KEY_1}"}
HEADERS_2 = {"Authorization": f"Bearer {RAW_KEY_2}"}

#: The template schema's ``Customer`` table and its ``Name`` column -- the
#: same pair ``tests/test_v2_access_request_routes.py`` denies.
DENIED_COLUMN = "Name"


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


KEYS_JSON = json.dumps([
    {"id": "analyst-1", "name": "One", "key_sha256": _sha256(RAW_KEY_1), "denied_columns": [DENIED_COLUMN]},
    {"id": "analyst-2", "name": "Two", "key_sha256": _sha256(RAW_KEY_2), "denied_columns": []},
])

EXAMPLES = [
    {"tags": ["names"], "question": "Customer names", "sql": "SELECT TOP 10 c.Name AS CustomerName FROM Customer c"},
    {"tags": ["count"], "question": "How many customers?", "sql": "SELECT COUNT(c.ID) AS N FROM Customer c"},
    {"tags": ["dup"], "question": "  How many   customers? ", "sql": "SELECT COUNT(c.ID) AS N FROM Customer c"},
    {"tags": ["blank"], "question": "   ", "sql": "SELECT COUNT(c.ID) AS N FROM Customer c"},
    {"tags": ["write"], "question": "Delete customers", "sql": "DELETE FROM Customer"},
    {"tags": ["unknown"], "question": "Unknown table", "sql": "SELECT x.A FROM NoSuchTable x"},
    {"tags": ["count2"], "question": "Count again", "sql": "SELECT COUNT(c.ID) AS M FROM Customer c"},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with override_settings(
        auth_required=True,
        api_keys_json=KEYS_JSON,
        session_store_path=str(tmp_path / "sessions.db"),
        app_db_url=f"sqlite:///{tmp_path / 'app.db'}",
        log_dir=str(tmp_path / "logs"),
    ):
        dispose_app_engine()
        v2_routes._reset_for_testing()
        monkeypatch.setattr(examples_module, "_cache", {"EXAMPLES": EXAMPLES, "_loaded": True})
        yield TestClient(server_module.app, raise_server_exceptions=False)
        v2_routes._reset_for_testing()
    dispose_app_engine()


def _questions(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"examples"}
    for item in body["examples"]:
        assert set(item) == {"question"}, "only question text leaves the server"
    return [item["question"] for item in body["examples"]]


class TestWhatIsOffered:
    def test_a_caller_without_denied_columns_gets_every_valid_question_in_file_order(self, client):
        assert _questions(client.get("/v2/examples", headers=HEADERS_2)) == [
            "Customer names", "How many customers?", "Count again",
        ]

    def test_a_question_over_a_denied_column_is_not_offered_to_that_caller(self, client):
        assert _questions(client.get("/v2/examples", headers=HEADERS_1)) == [
            "How many customers?", "Count again",
        ]

    def test_no_sql_or_tags_in_the_response(self, client):
        text = client.get("/v2/examples", headers=HEADERS_2).text
        assert "SELECT" not in text and "Customer c" not in text
        assert "tags" not in text and "count2" not in text

    def test_limit_is_respected_and_clamped(self, client):
        assert _questions(client.get("/v2/examples?limit=1", headers=HEADERS_2)) == ["Customer names"]
        assert len(_questions(client.get("/v2/examples?limit=0", headers=HEADERS_2))) == 1
        assert len(_questions(client.get(f"/v2/examples?limit={MAX_LIMIT + 50}", headers=HEADERS_2))) == 3


class TestEdges:
    def test_no_readable_examples_file_is_an_empty_list(self, client, monkeypatch):
        def boom(name):
            raise RuntimeError("examples.yaml not found")

        monkeypatch.setattr(examples_module, "_cache", {})
        monkeypatch.setattr(examples_module, "load_examples", lambda: boom("x"))
        assert _questions(client.get("/v2/examples", headers=HEADERS_2)) == []

    def test_the_route_needs_a_key(self, client):
        assert client.get("/v2/examples").status_code == 401
        assert client.get("/v2/examples", headers={"Authorization": "Bearer wrong"}).status_code == 401
