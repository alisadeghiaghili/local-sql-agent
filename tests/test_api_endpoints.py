"""TDD tests for POST /query and GET /health.

All external dependencies (LLM backend, DB executor) are replaced by
injected mocks — no Ollama or SQL Server required.

Key fixture design
------------------
- lifespan is bypassed via monkeypatching _system_prompt before app import
- run_query is patched at its definition site: api.runner.run_query
- The endpoint in server.py calls run_query imported from api.runner,
  so patching api.runner.run_query intercepts all calls correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.errors import (
    OutOfScopeError,
    ForbiddenSQLError,
    ModelTimeoutError,
    ModelUnavailableError,
    QueryTimeoutError,
    QueryExecutionError,
)
from api.models import HealthResponse

SIMPLE_SQL = "SELECT TOP 10 * FROM [Auction_Dim].[Customer]"
SIMPLE_DF  = pd.DataFrame({"Id": [1, 2], "Name": ["علی", "سارا"]})
VALID_Q    = "لیست مشتریان"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_response(**overrides):
    from api.models import QueryResponse
    defaults = dict(
        question=VALID_Q,
        sql=SIMPLE_SQL,
        result=SIMPLE_DF.to_dict(orient="records"),
        interpretation=None,
        row_count=2,
        correction_attempts=1,
        elapsed_seconds=0.1,
        model="openai:gpt-oss-20:F16",
    )
    defaults.update(overrides)
    return QueryResponse(**defaults)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_and_client():
    """App with lifespan skipped and run_query fully mocked.

    Patches
    -------
    - api.server._system_prompt  → stub string (skips file-system load)
    - api.runner.run_query       → MagicMock (controls every response)
    """
    import api.server as server_module
    import api.runner as runner_module

    # Pre-set the module-level prompt so lifespan startup doesn’t fail
    server_module._system_prompt = "stub system prompt"

    with patch.object(runner_module, "run_query") as mock_run:
        # Build a *new* TestClient without triggering lifespan events
        client = TestClient(server_module.app, raise_server_exceptions=False)
        yield server_module.app, client, mock_run


# ---------------------------------------------------------------------------
# POST /query — happy paths
# ---------------------------------------------------------------------------

class TestQueryModes:
    def test_mode_sql_returns_sql_not_result(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response(result=None)
        resp = client.post("/query", json={"question": VALID_Q, "mode": "sql"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["sql"] is not None
        assert body["result"] is None

    def test_mode_result_returns_result_not_sql(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response(sql=None)
        resp = client.post("/query", json={"question": VALID_Q, "mode": "result"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"] is not None
        assert body["sql"] is None

    def test_mode_full_returns_both(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response()
        resp = client.post("/query", json={"question": VALID_Q, "mode": "full"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["sql"] is not None
        assert body["result"] is not None

    def test_interpret_true_adds_interpretation(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response(interpretation="در ماه گذشته دو مشتری ثبت شد.")
        resp = client.post("/query", json={"question": VALID_Q, "mode": "full", "interpret": True})
        assert resp.status_code == 200
        assert resp.json()["interpretation"] is not None

    def test_correction_attempts_echoed(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response(correction_attempts=2)
        resp = client.post("/query", json={"question": VALID_Q})
        assert resp.status_code == 200
        assert resp.json()["correction_attempts"] == 2

    def test_elapsed_seconds_present(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response()
        resp = client.post("/query", json={"question": VALID_Q})
        assert resp.status_code == 200
        assert resp.json()["elapsed_seconds"] >= 0

    def test_model_name_echoed(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response(model="openai:gpt-oss-20:F16")
        resp = client.post("/query", json={"question": VALID_Q})
        assert resp.status_code == 200
        assert resp.json()["model"] == "openai:gpt-oss-20:F16"


# ---------------------------------------------------------------------------
# POST /query — error paths
# ---------------------------------------------------------------------------

class TestQueryErrors:
    @pytest.mark.parametrize("exc,expected_status,expected_code", [
        (OutOfScopeError("oot"),        422, "OUT_OF_SCOPE"),
        (ForbiddenSQLError("forbid"),   400, "FORBIDDEN_SQL"),
        (ModelTimeoutError("slow"),     504, "MODEL_TIMEOUT"),
        (ModelUnavailableError("down"), 503, "MODEL_UNAVAILABLE"),
        (QueryTimeoutError("locked"),   504, "QUERY_TIMEOUT"),
        (QueryExecutionError("dberr"),  502, "QUERY_EXECUTION_ERROR"),
    ])
    def test_error_status_and_code(self, app_and_client, exc, expected_status, expected_code):
        _, client, mock_run = app_and_client
        mock_run.side_effect = exc
        resp = client.post("/query", json={"question": VALID_Q})
        assert resp.status_code == expected_status
        assert resp.json()["error"]["code"] == expected_code

    def test_missing_question_returns_422(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post("/query", json={})
        assert resp.status_code == 422

    def test_empty_question_returns_422(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response()
        resp = client.post("/query", json={"question": "  "})
        assert resp.status_code == 422

    def test_question_too_long_returns_422(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response()
        resp = client.post("/query", json={"question": "x" * 1001})
        assert resp.status_code == 422

    def test_invalid_mode_returns_422(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response()
        resp = client.post("/query", json={"question": VALID_Q, "mode": "bad_mode"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealth:
    def _make_health_resp(self, openai_ok: bool, db_ok: bool) -> object:
        import api.server as server_module
        server_module._system_prompt = "stub"
        status = "ok" if (openai_ok and db_ok) else "degraded" if (openai_ok or db_ok) else "down"
        health_resp = HealthResponse(
            status=status,
            openai=openai_ok,
            database=db_ok,
            model="gpt-oss-20:F16",
        )
        with patch("api.health.check_health", return_value=health_resp):
            client = TestClient(server_module.app, raise_server_exceptions=False)
            return client.get("/health")

    def test_all_ok(self):
        resp = self._make_health_resp(True, True)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["openai"] is True
        assert body["database"] is True

    def test_llm_down_degraded(self):
        resp = self._make_health_resp(False, True)
        assert resp.json()["status"] == "degraded"
        assert resp.json()["openai"] is False

    def test_db_down_degraded(self):
        resp = self._make_health_resp(True, False)
        assert resp.json()["status"] == "degraded"
        assert resp.json()["database"] is False

    def test_both_down(self):
        resp = self._make_health_resp(False, False)
        assert resp.json()["status"] == "down"

    def test_health_never_blocked_by_overload(self, app_and_client):
        _, client, _ = app_and_client
        for _ in range(20):
            assert client.get("/health").status_code == 200
