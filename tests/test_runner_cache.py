# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""TDD tests — cache policy inside api/runner.run_query.

Contracts
---------
run_query() cache policy:
  1. mode='result'|'full' + interpret=False  →  consulted & populated
  2. mode='sql'                              →  always bypassed (never read or written)
  3. interpret=True                          →  always bypassed
  4. exception during _safe_run             →  cache NOT written
  5. first call (cache miss)                →  _safe_run called, response stored
  6. second identical call (cache hit)      →  _safe_run NOT called, cached resp returned
  7. reconfigure() changes TTL live         →  entries expire under new TTL

Integration:
  8. Two HTTP POST /query with same payload  →  _agent.run called exactly once
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest
import requests

from api.models import QueryResponse
from llm.base import SQLGenerationResult

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

SIMPLE_SQL = "SELECT TOP 5 * FROM [Auction_Dim].[Trade]"
SIMPLE_DF  = pd.DataFrame({"TradeId": [1], "Price": [100]})


def _good_result(attempt: int = 1):
    return (
        SIMPLE_DF.copy(),
        SQLGenerationResult(sql=SIMPLE_SQL, raw_response=SIMPLE_SQL, attempt=attempt),
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    """Guarantee a clean cache before and after every test."""
    from api.query_cache import query_cache
    query_cache.reconfigure(ttl_seconds=300, max_size=256)
    query_cache.clear()
    yield
    query_cache.clear()


@pytest.fixture()
def mock_agent():
    """Patch the module-level agent so no real LLM / DB is needed."""
    agent = MagicMock()
    agent._backend.name = "ollama:test"
    with patch("api.runner.agent", agent):
        yield agent


# ---------------------------------------------------------------------------
# 1 & 5 — first call populates cache
# ---------------------------------------------------------------------------

class TestFirstCallPopulatesCache:
    @pytest.mark.parametrize("mode", ["result", "full"])
    def test_cache_entry_created_after_success(self, mock_agent, mode):
        from api.runner import run_query
        from api.query_cache import query_cache
        from api.runner import cache_prefix_version_for

        mock_agent.run.return_value = _good_result()
        run_query("سوال", system_prompt="sp", mode=mode, interpret=False)

        assert query_cache.get("سوال", mode, prefix_version=cache_prefix_version_for("sp")) is not None

    @pytest.mark.parametrize("mode", ["result", "full"])
    def test_agent_run_called_exactly_once_on_first_call(self, mock_agent, mode):
        from api.runner import run_query

        mock_agent.run.return_value = _good_result()
        run_query("سوال", system_prompt="sp", mode=mode, interpret=False)

        mock_agent.run.assert_called_once()


# ---------------------------------------------------------------------------
# 6 — second identical call hits cache; _safe_run NOT called again
# ---------------------------------------------------------------------------

class TestSecondCallHitsCache:
    @pytest.mark.parametrize("mode", ["result", "full"])
    def test_second_call_returns_equal_but_independent_object(self, mock_agent, mode):
        """Equal in value (same cached data) but NOT the same object --
        the cache hands back a copy on every get() so that api/server.py
        mutating response.elapsed_seconds on one call's result can never
        leak into another call's cached entry."""
        from api.runner import run_query

        mock_agent.run.return_value = _good_result()
        r1 = run_query("سوال", system_prompt="sp", mode=mode, interpret=False)
        r2 = run_query("سوال", system_prompt="sp", mode=mode, interpret=False)

        assert r1 == r2
        assert r1 is not r2

    @pytest.mark.parametrize("mode", ["result", "full"])
    def test_agent_run_not_called_on_second_request(self, mock_agent, mode):
        from api.runner import run_query

        mock_agent.run.return_value = _good_result()
        run_query("سوال", system_prompt="sp", mode=mode, interpret=False)
        mock_agent.run.reset_mock()
        run_query("سوال", system_prompt="sp", mode=mode, interpret=False)

        mock_agent.run.assert_not_called()

    def test_cache_hit_count_increments(self, mock_agent):
        from api.runner import run_query
        from api.query_cache import query_cache

        mock_agent.run.return_value = _good_result()
        run_query("سوال", system_prompt="sp", mode="full", interpret=False)
        run_query("سوال", system_prompt="sp", mode="full", interpret=False)

        assert query_cache.stats()["hits"] >= 1


# ---------------------------------------------------------------------------
# 2 — mode='sql' always bypasses cache
# ---------------------------------------------------------------------------

class TestSqlModeBypassesCache:
    def test_cache_not_written_for_sql_mode(self, mock_agent):
        from api.runner import run_query
        from api.query_cache import query_cache

        mock_agent._backend.generate.return_value = SIMPLE_SQL
        with patch("api.runner._safe_generate_sql_only", return_value=(SIMPLE_SQL, {})):
            run_query("سوال", system_prompt="sp", mode="sql", interpret=False)

        assert query_cache.get("سوال", "sql") is None
        assert query_cache.stats()["size"] == 0

    def test_sql_mode_always_calls_generate(self, mock_agent):
        """Even on second identical SQL request, LLM is always called."""
        from api.runner import run_query

        with patch("api.runner._safe_generate_sql_only", return_value=(SIMPLE_SQL, {})) as gen:
            run_query("سوال", system_prompt="sp", mode="sql")
            run_query("سوال", system_prompt="sp", mode="sql")
            assert gen.call_count == 2


# ---------------------------------------------------------------------------
# 3 — interpret=True bypasses cache
# ---------------------------------------------------------------------------

class TestInterpretTrueBypassesCache:
    def test_interpret_true_does_not_write_cache(self, mock_agent):
        from llm.router import RouteResult
        from api.runner import run_query
        from api.query_cache import query_cache

        mock_agent.run.return_value = _good_result()
        mock_agent._router.generate_text_for_task.return_value = RouteResult(
            text="summary", structured=None, meta={}, provider="mock:stub", fallback_used=False,
        )
        run_query("سوال", system_prompt="sp", mode="full", interpret=True)

        assert query_cache.get("سوال", "full") is None

    def test_interpret_true_calls_agent_on_every_request(self, mock_agent):
        from llm.router import RouteResult
        from api.runner import run_query

        mock_agent.run.return_value = _good_result()
        mock_agent._router.generate_text_for_task.return_value = RouteResult(
            text="summary", structured=None, meta={}, provider="mock:stub", fallback_used=False,
        )
        run_query("سوال", system_prompt="sp", mode="full", interpret=True)
        run_query("سوال", system_prompt="sp", mode="full", interpret=True)

        assert mock_agent.run.call_count == 2


# ---------------------------------------------------------------------------
# 4 — exceptions do NOT populate cache
# ---------------------------------------------------------------------------

class TestExceptionDoesNotPopulateCache:
    @pytest.mark.parametrize("exc", [
        ValueError("OUT_OF_SCOPE"),
        requests.Timeout(),
        requests.ConnectionError(),
        RuntimeError("Syntax error"),
    ])
    def test_cache_empty_after_failed_run(self, mock_agent, exc):
        from api.runner import run_query
        from api.query_cache import query_cache
        from api.errors import NLQError

        mock_agent.run.side_effect = exc
        with pytest.raises(NLQError):
            run_query("سوال", system_prompt="sp", mode="full")

        assert query_cache.get("سوال", "full") is None
        assert query_cache.stats()["size"] == 0


# ---------------------------------------------------------------------------
# 7 — reconfigure() applies new TTL live
# ---------------------------------------------------------------------------

class TestReconfigure:
    def test_reconfigure_lower_ttl_causes_immediate_expiry(self, mock_agent):
        """Entry set with TTL=300, then cache reconfigured to TTL=0 (disabled).
        get() must return None."""
        from api.query_cache import query_cache
        from api.models import QueryResponse

        r = QueryResponse(question="سوال", sql=SIMPLE_SQL, result=[],
                          row_count=0, model="test")
        query_cache.reconfigure(ttl_seconds=300, max_size=256)
        query_cache.set("سوال", "full", r)
        assert query_cache.get("سوال", "full") == r  # sanity check

        # Disable cache — reconfigure must also clear stale entries
        query_cache.reconfigure(ttl_seconds=0, max_size=256)
        assert query_cache.get("سوال", "full") is None

    def test_reconfigure_clears_existing_entries(self, mock_agent):
        """reconfigure() must wipe stale entries so old TTL can't be used."""
        from api.query_cache import query_cache
        from api.models import QueryResponse

        r = QueryResponse(question="سوال", sql=SIMPLE_SQL, result=[],
                          row_count=0, model="test")
        query_cache.reconfigure(ttl_seconds=300, max_size=256)
        query_cache.set("سوال", "full", r)
        query_cache.reconfigure(ttl_seconds=60, max_size=256)

        # reconfigure() must have cleared the store
        assert query_cache.stats()["size"] == 0


# ---------------------------------------------------------------------------
# 8 — Integration: HTTP POST /query, two identical requests, _agent.run once
# ---------------------------------------------------------------------------

class TestHttpIntegration:
    """End-to-end through FastAPI TestClient → runner → cache.

    The SQLAgent is fully mocked so no Ollama or DB required.

    NOTE: The FastAPI application object lives in ``api.server``, NOT in
    ``app.py``.  ``app.py`` is the interactive CLI entry-point (REPL) and
    does NOT export a ``app`` symbol.
    """

    @pytest.fixture()
    def client(self, mock_agent, auth_settings):
        """Build a fresh TestClient with cache enabled (TTL 300s)."""
        from fastapi.testclient import TestClient
        from api.query_cache import query_cache
        import api.server as server_module

        query_cache.reconfigure(ttl_seconds=300, max_size=256)
        mock_agent.run.return_value = _good_result()
        server_module._system_prompt = "stub system prompt"

        # api.server.app is the FastAPI instance — app.py is the CLI REPL
        return TestClient(
            server_module.app, raise_server_exceptions=False, headers=auth_settings,
        )

    def test_second_http_request_does_not_call_agent_again(self, client, mock_agent):
        payload = {"question": "چند معامله امروز؟", "mode": "full"}

        r1 = client.post("/query", json=payload)
        r2 = client.post("/query", json=payload)

        assert r1.status_code == 200
        assert r2.status_code == 200
        # agent.run must have been called exactly once across both requests
        mock_agent.run.assert_called_once()

    def test_different_questions_each_call_agent(self, client, mock_agent):
        mock_agent.run.return_value = _good_result()

        client.post("/query", json={"question": "سوال الف", "mode": "full"})
        client.post("/query", json={"question": "سوال ب", "mode": "full"})

        assert mock_agent.run.call_count == 2

    def test_sql_mode_requests_never_cached_via_http(self, client, mock_agent):
        """Two identical sql-mode requests → _safe_generate_sql_only called twice."""
        with patch("api.runner._safe_generate_sql_only",
                   return_value=(SIMPLE_SQL, {})) as gen:
            client.post("/query", json={"question": "سوال", "mode": "sql"})
            client.post("/query", json={"question": "سوال", "mode": "sql"})
            assert gen.call_count == 2

    def test_error_response_not_served_from_cache(self, client, mock_agent):
        """A failed request must not poison the cache for the same question."""
        from api.query_cache import query_cache

        mock_agent.run.side_effect = RuntimeError("DB down")
        client.post("/query", json={"question": "سوال", "mode": "full"})
        assert query_cache.stats()["size"] == 0

        # Recovery: next call succeeds and populates cache
        mock_agent.run.side_effect = None
        mock_agent.run.return_value = _good_result()
        r = client.post("/query", json={"question": "سوال", "mode": "full"})
        assert r.status_code == 200
        assert query_cache.stats()["size"] == 1
