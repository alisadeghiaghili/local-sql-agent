"""TDD tests for api/runner.py — exception translation layer.

Contracts
---------
run_query()
  - Translates requests.Timeout   → ModelTimeoutError
  - Translates requests.ConnectionError → ModelUnavailableError
  - Translates ValueError(OUT_OF_SCOPE)  → OutOfScopeError
  - Translates RuntimeError with 'LOCK_TIMEOUT' in msg → QueryTimeoutError
  - Translates RuntimeError with 'connection' in msg → DatabaseConnectionError
  - Translates generic RuntimeError → QueryExecutionError
  - Translates ValueError(ForbiddenKeyword) → ForbiddenSQLError
  - On success, QueryResponse fields are populated correctly
  - interpret=True triggers a second LLM call for interpretation
  - interpret=False (default) makes no extra LLM call
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest
import requests

from api.errors import (
    OutOfScopeError,
    ForbiddenSQLError,
    ModelTimeoutError,
    ModelUnavailableError,
    QueryTimeoutError,
    DatabaseConnectionError,
    QueryExecutionError,
)
from llm.base import SQLGenerationResult

SIMPLE_SQL = "SELECT TOP 5 * FROM [Auction_Dim].[Trade]"
SIMPLE_DF  = pd.DataFrame({"TradeId": [1], "Price": [100]})


@pytest.fixture(autouse=True)
def _clear_cache():
    """Isolate every test: wipe the shared cache singleton before and after.

    Without this, a successful run_query() in an earlier test populates the
    cache so that a later test gets a cache-hit instead of calling agent.run,
    causing correction_attempts / side_effect assertions to fail.
    """
    from api.query_cache import query_cache
    query_cache.clear()
    yield
    query_cache.clear()


@pytest.fixture(autouse=True)
def patch_agent():
    """Replace the module-level agent in runner with a fresh mock."""
    mock_agent = MagicMock()
    mock_agent._backend.name = "ollama:test"
    with patch("api.runner.agent", mock_agent):
        yield mock_agent


def _good_result(attempt: int = 1) -> tuple:
    return (
        SIMPLE_DF.copy(),
        SQLGenerationResult(sql=SIMPLE_SQL, raw_response=SIMPLE_SQL, attempt=attempt),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestRunQuerySuccess:
    def test_full_mode_populates_sql_and_result(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        resp = run_query("test", system_prompt="", mode="full")
        assert resp.sql == SIMPLE_SQL
        assert resp.result is not None
        assert resp.row_count == 1

    def test_result_mode_has_no_sql(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        resp = run_query("test", system_prompt="", mode="result")
        assert resp.sql is None
        assert resp.result is not None

    def test_correction_attempts_propagated(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result(attempt=2)
        resp = run_query("test", system_prompt="", mode="full")
        assert resp.correction_attempts == 2

    def test_interpret_false_no_extra_generate_call(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        run_query("test", system_prompt="", mode="full", interpret=False)
        patch_agent._backend.generate.assert_not_called()

    def test_interpret_true_calls_generate_for_summary(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        patch_agent._backend.generate.return_value = "summary text"
        resp = run_query("test", system_prompt="", mode="full", interpret=True)
        assert resp.interpretation == "summary text"
        patch_agent._backend.generate.assert_called_once()


# ---------------------------------------------------------------------------
# Exception translation
# ---------------------------------------------------------------------------

class TestExceptionTranslation:
    @pytest.mark.parametrize("exc,expected_nlq", [
        (ValueError("OUT_OF_SCOPE"),                   OutOfScopeError),
        (ValueError("Forbidden keyword detected: DROP"), ForbiddenSQLError),
        (requests.Timeout(),                           ModelTimeoutError),
        (requests.ConnectionError(),                   ModelUnavailableError),
        (RuntimeError("Ollama unreachable"),           ModelUnavailableError),
        (RuntimeError("Database error: LOCK_TIMEOUT"), QueryTimeoutError),
        (RuntimeError("Cannot connect to server"),    DatabaseConnectionError),
        (RuntimeError("Syntax error near 'FROM'"),    QueryExecutionError),
    ])
    def test_exception_translated(self, patch_agent, exc, expected_nlq):
        from api.runner import run_query
        patch_agent.run.side_effect = exc
        with pytest.raises(expected_nlq):
            run_query("test", system_prompt="", mode="full")
