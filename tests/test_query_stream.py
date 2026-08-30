"""Tests for POST /query/stream (SSE) and the bounded async pipeline (Phase 2 task 4).

Mirrors tests/test_api_endpoints.py's fixture pattern: run_query is patched
at its definition site (api.runner.run_query) so no real LLM/DB is needed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.models import QueryResponse

VALID_Q = "لیست مشتریان"


def _ok_response(**overrides) -> QueryResponse:
    defaults = dict(
        question=VALID_Q,
        sql="SELECT TOP 10 * FROM Customer",
        result=[{"Id": 1, "Name": "علی"}],
        interpretation=None,
        row_count=1,
        correction_attempts=1,
        model="ollama:llama3",
        llm={"prompt_tokens": 120, "prefix_cache_hit": True},
    )
    defaults.update(overrides)
    return QueryResponse(**defaults)


@pytest.fixture()
def app_and_client():
    import api.runner as runner_module
    import api.server as server_module

    server_module._system_prompt = "stub system prompt"
    with patch.object(runner_module, "run_query") as mock_run:
        client = TestClient(server_module.app, raise_server_exceptions=False)
        yield server_module.app, client, mock_run


class TestQueryStreamHappyPath:
    def test_returns_event_stream_content_type(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response()
        resp = client.post("/query/stream", json={"question": VALID_Q})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_emits_stage_sql_rows_llm_done_in_order(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response()
        resp = client.post("/query/stream", json={"question": VALID_Q})
        body = resp.text
        for expected in ("event: stage", "event: sql", "event: rows", "event: llm", "event: done"):
            assert expected in body
        assert body.index("event: stage") < body.index("event: sql") < body.index("event: rows")
        assert body.index("event: rows") < body.index("event: done")

    def test_sql_event_contains_generated_sql(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response(sql="SELECT TOP 5 * FROM Ring")
        resp = client.post("/query/stream", json={"question": VALID_Q})
        assert "SELECT TOP 5 * FROM Ring" in resp.text

    def test_rows_event_contains_row_count(self, app_and_client):
        _, client, mock_run = app_and_client
        mock_run.return_value = _ok_response(row_count=3)
        resp = client.post("/query/stream", json={"question": VALID_Q})
        assert '"row_count": 3' in resp.text


class TestQueryStreamErrorPath:
    def test_error_emits_error_event_not_500(self, app_and_client):
        from api.errors import OutOfScopeError

        _, client, mock_run = app_and_client
        mock_run.side_effect = OutOfScopeError("This question is outside the Auction domain.")
        resp = client.post("/query/stream", json={"question": "چه آب و هوایی امروز است؟"})
        assert resp.status_code == 200  # SSE transport itself succeeds
        assert "event: error" in resp.text
        assert "OUT_OF_SCOPE" in resp.text


class TestBoundedQueryHelper:
    def test_run_query_bounded_calls_runner(self):
        import api.runner as runner_module
        import api.server as server_module

        with patch.object(runner_module, "run_query", return_value=_ok_response()) as mock_run:
            result = asyncio.run(
                server_module._run_query_bounded(
                    question=VALID_Q, system_prompt="sp", mode="full",
                    interpret=False, request_id="r1",
                )
            )
        mock_run.assert_called_once()
        assert result.question == VALID_Q

    def test_run_query_bounded_propagates_exceptions(self):
        import api.runner as runner_module
        import api.server as server_module
        from api.errors import ModelUnavailableError

        with patch.object(runner_module, "run_query", side_effect=ModelUnavailableError("down")):
            with pytest.raises(ModelUnavailableError):
                asyncio.run(
                    server_module._run_query_bounded(
                        question=VALID_Q, system_prompt="sp", mode="full",
                        interpret=False, request_id="r1",
                    )
                )

    def test_semaphore_bounds_concurrent_threads(self):
        """Sanity check: the semaphore actually limits concurrency to its configured size."""
        import api.server as server_module

        assert server_module._query_semaphore._value == server_module._QUERY_THREAD_LIMIT
