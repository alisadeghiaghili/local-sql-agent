"""TDD tests for llm/sql_agent.py — SQLAgent + self-correction loop.

All LLM and DB calls are replaced by injected stubs so tests are:
  - Deterministic (no Ollama, no SQL Server required)
  - Fast (< 1 ms per test)
  - Focused on the agent’s own logic

Contracts under test
--------------------
SQLGenerationResult
  - Immutable dataclass with sql, raw_response, attempt, correction_prompts.

SQLAgent.run()
  - Returns (DataFrame, SQLGenerationResult) on success.
  - attempt=1 when first execution succeeds.
  - Retries up to max_corrections times on RuntimeError from execute_fn.
  - attempt == number of retries on eventual success.
  - Re-raises RuntimeError when all correction attempts are exhausted.
  - Passes the error message and failed SQL into the correction prompt.
  - Passes OUT_OF_SCOPE ValueError through unchanged.
  - Each correction prompt is appended to SQLGenerationResult.correction_prompts.

LLMBackend contract
  - Any object implementing generate(prompt) → str can be used as backend.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pandas as pd
import pytest

from llm.base import LLMBackend, SQLGenerationResult
from llm.sql_agent import SQLAgent, MAX_CORRECTION_ATTEMPTS


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

SIMPLE_SQL = "SELECT TOP 10 * FROM [Auction_Dim].[Customer]"
SIMPLE_DF = pd.DataFrame({"Id": [1, 2], "Name": ["A", "B"]})


class FixedBackend(LLMBackend):
    """Backend that returns a pre-set sequence of responses."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)

    def generate(self, prompt: str) -> str:
        return next(self._responses)


def _ok_execute(sql: str) -> pd.DataFrame:
    return SIMPLE_DF.copy()


def _fail_execute(sql: str) -> pd.DataFrame:
    raise RuntimeError("Database error: Invalid column name 'Foo'")


def _agent(
    responses: list[str],
    execute_fn=_ok_execute,
    max_corrections: int = MAX_CORRECTION_ATTEMPTS,
) -> SQLAgent:
    return SQLAgent(
        backend=FixedBackend(responses),
        execute_fn=execute_fn,
        max_corrections=max_corrections,
    )


# ---------------------------------------------------------------------------
# SQLGenerationResult
# ---------------------------------------------------------------------------

class TestSQLGenerationResult:
    def test_is_frozen(self):
        r = SQLGenerationResult(sql="SELECT 1", raw_response="SELECT 1")
        with pytest.raises((AttributeError, TypeError)):
            r.sql = "mutated"  # type: ignore[misc]

    def test_default_attempt_is_one(self):
        r = SQLGenerationResult(sql="SELECT 1", raw_response="SELECT 1")
        assert r.attempt == 1

    def test_default_correction_prompts_is_empty_list(self):
        r = SQLGenerationResult(sql="SELECT 1", raw_response="SELECT 1")
        assert r.correction_prompts == []


# ---------------------------------------------------------------------------
# LLMBackend contract
# ---------------------------------------------------------------------------

class TestLLMBackendContract:
    def test_any_callable_implementing_generate_works(self):
        class MinimalBackend(LLMBackend):
            def generate(self, prompt: str) -> str:
                return SIMPLE_SQL

        ag = SQLAgent(
            backend=MinimalBackend(),
            execute_fn=_ok_execute,
        )
        df, result = ag.run("anything", system_prompt="")
        assert isinstance(df, pd.DataFrame)
        assert isinstance(result, SQLGenerationResult)

    def test_backend_name_defaults_to_class_name(self):
        class MyBackend(LLMBackend):
            def generate(self, prompt: str) -> str:
                return SIMPLE_SQL

        assert MyBackend().name == "MyBackend"


# ---------------------------------------------------------------------------
# Successful first attempt
# ---------------------------------------------------------------------------

class TestFirstAttemptSuccess:
    def test_returns_dataframe_and_result(self):
        ag = _agent([SIMPLE_SQL])
        df, result = ag.run("test", system_prompt="")
        assert isinstance(df, pd.DataFrame)
        assert isinstance(result, SQLGenerationResult)

    def test_attempt_is_one(self):
        ag = _agent([SIMPLE_SQL])
        _, result = ag.run("test", system_prompt="")
        assert result.attempt == 1

    def test_sql_is_cleaned(self):
        ag = _agent(["```sql\n" + SIMPLE_SQL + "\n```"])
        _, result = ag.run("test", system_prompt="")
        assert "```" not in result.sql

    def test_no_correction_prompts_on_first_success(self):
        ag = _agent([SIMPLE_SQL])
        _, result = ag.run("test", system_prompt="")
        assert result.correction_prompts == []


# ---------------------------------------------------------------------------
# Self-correction loop
# ---------------------------------------------------------------------------

class TestSelfCorrection:
    def test_retries_on_execution_error(self):
        """Agent tries a second time when first execution fails."""
        call_count = 0

        def execute_fail_once(sql: str) -> pd.DataFrame:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("deadlock")
            return SIMPLE_DF.copy()

        ag = _agent([SIMPLE_SQL, SIMPLE_SQL], execute_fn=execute_fail_once)
        df, result = ag.run("test", system_prompt="")
        assert result.attempt == 2
        assert call_count == 2

    def test_correction_prompt_contains_error_message(self):
        """The error text must appear in the correction prompt."""
        error_msg = "Invalid column name 'XYZ'"

        call_count = 0

        def execute_fail_once(sql: str) -> pd.DataFrame:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError(f"Database error: {error_msg}")
            return SIMPLE_DF.copy()

        ag = _agent([SIMPLE_SQL, SIMPLE_SQL], execute_fn=execute_fail_once)
        _, result = ag.run("test", system_prompt="")
        assert result.correction_prompts
        assert error_msg in result.correction_prompts[0]

    def test_correction_prompt_contains_failed_sql(self):
        bad_sql = "SELECT TOP 10 BadCol FROM [Auction_Dim].[Trade]"

        call_count = 0

        def fail_once(sql: str) -> pd.DataFrame:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("some db error")
            return SIMPLE_DF.copy()

        ag = _agent([bad_sql, SIMPLE_SQL], execute_fn=fail_once)
        _, result = ag.run("test", system_prompt="")
        assert bad_sql in result.correction_prompts[0]

    def test_raises_after_max_corrections_exhausted(self):
        ag = _agent(
            [SIMPLE_SQL] * (MAX_CORRECTION_ATTEMPTS + 1),
            execute_fn=_fail_execute,
            max_corrections=MAX_CORRECTION_ATTEMPTS,
        )
        with pytest.raises(RuntimeError):
            ag.run("test", system_prompt="")

    def test_exactly_max_corrections_attempts_made(self):
        execute_calls: list[str] = []

        def counting_fail(sql: str) -> pd.DataFrame:
            execute_calls.append(sql)
            raise RuntimeError("always fails")

        ag = _agent(
            [SIMPLE_SQL] * (MAX_CORRECTION_ATTEMPTS + 1),
            execute_fn=counting_fail,
            max_corrections=MAX_CORRECTION_ATTEMPTS,
        )
        with pytest.raises(RuntimeError):
            ag.run("test", system_prompt="")

        # initial attempt + max_corrections correction rounds
        assert len(execute_calls) == MAX_CORRECTION_ATTEMPTS + 1

    def test_max_corrections_zero_means_no_retry(self):
        ag = _agent([SIMPLE_SQL], execute_fn=_fail_execute, max_corrections=0)
        with pytest.raises(RuntimeError):
            ag.run("test", system_prompt="")


# ---------------------------------------------------------------------------
# OUT_OF_SCOPE passthrough
# ---------------------------------------------------------------------------

class TestOutOfScope:
    def test_out_of_scope_raises_value_error(self):
        ag = _agent(["OUT_OF_SCOPE"])
        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            ag.run("random question", system_prompt="")

    def test_out_of_scope_is_not_retried(self):
        """OUT_OF_SCOPE must propagate immediately without correction attempts."""
        generate_calls: list[str] = []

        class TrackingBackend(LLMBackend):
            def generate(self, prompt: str) -> str:
                generate_calls.append(prompt)
                return "OUT_OF_SCOPE"

        ag = SQLAgent(
            backend=TrackingBackend(),
            execute_fn=_ok_execute,
        )
        with pytest.raises(ValueError):
            ag.run("irrelevant", system_prompt="")

        assert len(generate_calls) == 1
