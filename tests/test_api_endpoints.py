"""TDD tests for POST /query and GET /health.

All external dependencies (LLM backend, DB executor) are replaced by
injected mocks — no live LLM endpoint or SQL Server required.

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
        model="openai:gpt-oss-20b",
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
        mock_run.return_value = _ok_response(model="openai:gpt-oss-20b")
        resp = client.post("/query", json={"question": VALID_Q})
        assert resp.status_code == 200
        assert resp.json()["model"] == "openai:gpt-oss-20b"


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

    def test_openai_down_degraded(self):
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
        # check_health MUST be patched here. The app_and_client fixture mocks
        # run_query but not the health probes, so without this patch each of
        # the 20 requests below ran the real _ping_db(), which tries to
        # resolve the default connection URL's literal host "server" and
        # blocks on DNS/ODBC login timeout for ~21s. That single test cost
        # ~420s and was, on its own, essentially the entire runtime of the
        # whole suite.
        #
        # The test is about the concurrency limiter never gating /health, so
        # the probe results are irrelevant to what it asserts.
        _, client, _ = app_and_client
        healthy = HealthResponse(
            status="ok", openai=True, database=True, model="test-model"
        )
        with patch("api.health.check_health", return_value=healthy):
            for _ in range(20):
                assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# lifespan() fails fast on invalid configuration (item 2)
# ---------------------------------------------------------------------------

class TestLifespanValidatesConfig:
    """cfg.settings.validate() must run at ASGI startup — mirrors the same
    fail-fast check app.py's REPL entry point performs (item 2). Driven
    directly (not through TestClient) since the other fixtures in this
    file deliberately bypass lifespan."""

    def test_raises_when_db_url_is_placeholder(self):
        import asyncio

        import api.server as server_module
        from config import override_settings

        async def _start():
            async with server_module.lifespan(server_module.app):
                pass  # pragma: no cover - must not be reached

        with override_settings(
            openai_model="llama3",
            db_connection_url=(
                "mssql+pyodbc://username@server:1433/Auction_DM"
                "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
            ),
        ):
            with pytest.raises(RuntimeError, match="[Cc]onfiguration"):
                asyncio.run(_start())


# ---------------------------------------------------------------------------
# _PROMPT_PATH must not depend on CWD; empty _system_prompt must fail loudly
# (item 14)
# ---------------------------------------------------------------------------

class TestPromptPathIsCwdIndependent:
    """_PROMPT_PATH used to be Path("prompts/system_prompt.md") -- relative
    to whatever the current working directory happened to be, not to this
    module's own location. Running uvicorn from any directory other than
    the repo root silently broke it."""

    def test_prompt_path_is_absolute(self):
        import api.server as server_module
        assert server_module._PROMPT_PATH.is_absolute()

    def test_prompt_path_resolves_regardless_of_cwd(self, tmp_path, monkeypatch):
        import api.server as server_module
        monkeypatch.chdir(tmp_path)
        assert server_module._PROMPT_PATH.exists()


class TestEmptySystemPromptFailsLoudly:
    """If lifespan never runs (e.g. this app is served without the ASGI
    lifespan protocol), _system_prompt silently stays "" and every
    request would prompt the model with no system instructions at all --
    a serious behaviour change on our end that would otherwise leave no
    trace beyond quietly worse answers. /query must refuse loudly
    instead."""

    def test_query_fails_loudly_when_system_prompt_never_loaded(self):
        import api.server as server_module
        import api.runner as runner_module

        original = server_module._system_prompt
        server_module._system_prompt = ""  # simulate lifespan never running
        try:
            with patch.object(runner_module, "run_query") as mock_run:
                client = TestClient(server_module.app, raise_server_exceptions=False)
                resp = client.post("/query", json={"question": VALID_Q})
        finally:
            server_module._system_prompt = original

        assert resp.status_code >= 500
        mock_run.assert_not_called()
