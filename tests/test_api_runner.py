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

from security.sql_guard import PolicyRejection

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
from llm.router import RouteResult

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
        patch_agent._router.generate_text_for_task.assert_not_called()

    def test_interpret_true_calls_generate_for_summary(self, patch_agent):
        from api.runner import run_query

        patch_agent.run.return_value = _good_result()
        patch_agent._router.generate_text_for_task.return_value = RouteResult(
            text="summary text", structured=None, meta={}, provider="mock:stub", fallback_used=False,
        )
        resp = run_query("test", system_prompt="", mode="full", interpret=True)
        assert resp.interpretation == "summary text"
        patch_agent._router.generate_text_for_task.assert_called_once()

    def test_interpret_replaces_toman_with_rial(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        patch_agent._router.generate_text_for_task.return_value = RouteResult(
            text="مجموع خرید ۱۲۰ میلیارد تومان بود.",
            structured=None, meta={}, provider="mock:stub", fallback_used=False,
        )
        resp = run_query("test", system_prompt="", mode="full", interpret=True)
        assert resp.interpretation == "مجموع خرید ۱۲۰ میلیارد ریال بود."
        assert "تومان" not in resp.interpretation

    def test_interpret_replaces_english_toman_with_rial(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        patch_agent._router.generate_text_for_task.return_value = RouteResult(
            text="Total purchase value was 120 billion toman.",
            structured=None, meta={}, provider="mock:stub", fallback_used=False,
        )
        resp = run_query("test", system_prompt="", mode="full", interpret=True)
        assert resp.interpretation == "Total purchase value was 120 billion Rial."
        assert "toman" not in resp.interpretation.lower()

    def test_interpret_replaces_capitalized_toman_with_rial(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        patch_agent._router.generate_text_for_task.return_value = RouteResult(
            text="Total value: 120 billion Toman, up 5% vs last year.",
            structured=None, meta={}, provider="mock:stub", fallback_used=False,
        )
        resp = run_query("test", system_prompt="", mode="full", interpret=True)
        assert resp.interpretation == (
            "Total value: 120 billion Rial, up 5% vs last year."
        )
        assert "toman" not in resp.interpretation.lower()

    def test_interpret_adds_thousands_separators_ascii(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        patch_agent._router.generate_text_for_task.return_value = RouteResult(
            text="مجموع خرید 12000000000 تومان در سال 1402 بود.",
            structured=None, meta={}, provider="mock:stub", fallback_used=False,
        )
        resp = run_query("test", system_prompt="", mode="full", interpret=True)
        assert resp.interpretation == (
            "مجموع خرید 12,000,000,000 ریال در سال 1402 بود."
        )

    def test_interpret_adds_thousands_separators_persian_digits(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        patch_agent._router.generate_text_for_task.return_value = RouteResult(
            text="ارزش کل ۱۲۰۰۰۰۰۰۰۰۰ ریال بود.",
            structured=None, meta={}, provider="mock:stub", fallback_used=False,
        )
        resp = run_query("test", system_prompt="", mode="full", interpret=True)
        assert resp.interpretation == "ارزش کل ۱۲,۰۰۰,۰۰۰,۰۰۰ ریال بود."

    def test_interpret_preserves_4_digit_years_and_formats_counts(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        patch_agent._router.generate_text_for_task.return_value = RouteResult(
            text="در سال 1402 تعداد 32808 معامله ثبت شد.",
            structured=None, meta={}, provider="mock:stub", fallback_used=False,
        )
        resp = run_query("test", system_prompt="", mode="full", interpret=True)
        assert resp.interpretation == (
            "در سال 1402 تعداد 32,808 معامله ثبت شد."
        )
        assert "1,402" not in resp.interpretation

    def test_interpret_normalizes_space_separated_number_to_commas(self, patch_agent):
        from api.runner import run_query
        patch_agent.run.return_value = _good_result()
        patch_agent._router.generate_text_for_task.return_value = RouteResult(
            text="مجموع فروش 143 066 295 130 000 ریال بود.",
            structured=None, meta={}, provider="mock:stub", fallback_used=False,
        )
        resp = run_query("test", system_prompt="", mode="full", interpret=True)
        assert resp.interpretation == (
            "مجموع فروش 143,066,295,130,000 ریال بود."
        )


# ---------------------------------------------------------------------------
# Exception translation
# ---------------------------------------------------------------------------

class TestExceptionTranslation:
    @pytest.mark.parametrize("exc,expected_nlq", [
        (ValueError("OUT_OF_SCOPE"),                   OutOfScopeError),
        # PolicyRejection, not a bare ValueError: since the guard grew its
        # rejection taxonomy, the runner decides 400-vs-502 from the
        # exception's is_refusal attribute rather than by looking for
        # "Forbidden keyword" in the message. A bare ValueError carrying
        # that text is no longer reachable in production -- every raise
        # site in security/sql_guard.py raises a typed subclass -- and a
        # genuinely unexpected ValueError *should* surface as 502 rather
        # than be dressed up as a security refusal on the strength of its
        # wording.
        (PolicyRejection("Forbidden keyword detected: DROP"), ForbiddenSQLError),
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
