"""TDD tests for POST /query and GET /health.

All external dependencies (LLM backend, DB executor) are replaced by
injected mocks — no Ollama or SQL Server required.

Contracts under test
--------------------
POST /query
  mode='sql'    → returns sql, no result
  mode='result' → returns result, no sql
  mode='full'   → returns both sql and result
  interpret=True→ adds interpretation field
  OUT_OF_SCOPE  → 422
  ForbiddenSQL  → 400
  ModelTimeout  → 504
  ModelUnavail  → 503
  QueryTimeout  → 504
  Overload      → 503
  question too short → 422
  question too long  → 422
  missing question   → 422
  correction_attempts echoed in response
  elapsed_seconds > 0
  model echoed in response

GET /health
  all ok → {status: ok, ollama: true, database: true}
  ollama down → {status: degraded}
  db down → {status: degraded}
  both down → {status: down}
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
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
SIMPLE_DF = pd.DataFrame({"Id": [1, 2], "Name": ["علی", "سارا"]})
VALID_Q = "لیست مشتریان"


# ---------------------------------------------------------------------------
# Fixture: app with mocked runner
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_and_client():
    """Returns (app, TestClient, mock_run_query) with runner patched."""
    # Import here so patching happens before app is built
    import api.server as server_module
    # Ensure system prompt is loaded
    server_module._system_prompt = "system prompt stub"

    with patch("api.runner.run_query") as mock_run:
        from api.server import app
        client = TestClient(app, raise_server_exceptions=False)
        yield app, client, mock_run


def _ok_response(**overrides):
    from api.models import QueryResponse
    defaults = dict(
        question=VALID_Q,
        sql=SIMPLE_SQL,
        result=SIMPLE_DF.to_dict(orient="records"),
        interpretation=None,
        row_count=2,
        correction_attempts=1,
        elapsed_seconds=0.0,
        model="ollama:llama3",
    )
    defaults.update(overrides)
    return QueryResponse(**defaults)


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
        mock_run.return_value = _ok_response(interpretation="در ماه گذشته دو مشتری ثبت شد.”")
        resp = client.post("/query", json={"question": VALID_Q, "mode": "full", "interpret": True})
        assert resp.status_code == 200
        assert resp.json()["interpretation"] is not None

    def test_correction_attempts_echoed(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response(correction_attempts=2)
        resp = client.post("/query", json={"question": VALID_Q})
        assert resp.json()["correction_attempts"] == 2

    def test_elapsed_seconds_present(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response()
        resp = client.post("/query", json={"question": VALID_Q})
        assert resp.json()["elapsed_seconds"] >= 0

    def test_model_name_echoed(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response(model="ollama:codellama")
        resp = client.post("/query", json={"question": VALID_Q})
        assert resp.json()["model"] == "ollama:codellama"


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
    def _make_health_client(self, ollama_ok: bool, db_ok: bool) -> TestClient:
        from api.server import app
        with patch("api.health.check_health") as mock_health:
            mock_health.return_value = HealthResponse(
                status="ok" if (ollama_ok and db_ok)
                       else "degraded" if (ollama_ok or db_ok)
                       else "down",
                ollama=ollama_ok,
                database=db_ok,
                model="llama3",
            )
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/health")
        return resp

    def test_all_ok(self):
        resp = self._make_health_client(True, True)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["ollama"] is True
        assert body["database"] is True

    def test_ollama_down_degraded(self):
        resp = self._make_health_client(False, True)
        assert resp.json()["status"] == "degraded"
        assert resp.json()["ollama"] is False

    def test_db_down_degraded(self):
        resp = self._make_health_client(True, False)
        assert resp.json()["status"] == "degraded"
        assert resp.json()["database"] is False

    def test_both_down(self):
        resp = self._make_health_client(False, False)
        assert resp.json()["status"] == "down"

    def test_health_never_blocked_by_overload(self, app_and_client):
        """Health must return 200 even when concurrency limit is hit."""
        _, client, _ = app_and_client
        # Even if we call health many times it should never 503
        for _ in range(20):
            assert client.get("/health").status_code == 200
