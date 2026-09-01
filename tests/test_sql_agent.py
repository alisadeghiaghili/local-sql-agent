# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
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
        # A real table (bad_sql is meant to fail at *execution*, not at
        # validate_sql's table allowlist -- Phase 1's table allowlist
        # rejects an unrecognised table like the former [Auction_Dim].[Trade]
        # outright, which would turn this into a validation failure instead
        # and never reach fail_once() below).
        bad_sql = "SELECT TOP 10 BadCol FROM [Auction_Dim].[Customer]"

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

class _OutOfScopeBackend(LLMBackend):
    """Mirrors the REAL contract (see llm/providers.py::OpenAIBackend.generate):
    the backend itself raises ValueError("OUT_OF_SCOPE") — it never
    returns that string as if it were ordinary model output. A stub that
    merely *returned* the literal string "OUT_OF_SCOPE" would instead
    exercise clean_sql()'s unrelated "no SELECT found" failure, which
    happens to also match `pytest.raises(..., match="OUT_OF_SCOPE")`
    because clean_sql's error message echoes back the input via repr —
    a coincidence, not a test of the real sentinel path."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        raise ValueError("OUT_OF_SCOPE")


class TestOutOfScope:
    def test_out_of_scope_raises_value_error(self):
        ag = SQLAgent(backend=_OutOfScopeBackend(), execute_fn=_ok_execute)
        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            ag.run("random question", system_prompt="")

    def test_out_of_scope_is_not_retried(self):
        """OUT_OF_SCOPE must propagate immediately without correction attempts."""
        backend = _OutOfScopeBackend()
        ag = SQLAgent(backend=backend, execute_fn=_ok_execute)

        with pytest.raises(ValueError):
            ag.run("irrelevant", system_prompt="")

        assert backend.calls == 1


# ---------------------------------------------------------------------------
# Validation failures must also trigger self-correction (item 13)
# ---------------------------------------------------------------------------

class TestValidationFailureTriggersCorrection:
    """Previously, ValueError from clean_sql/validate_sql aborted run()
    immediately -- the correction loop only ever caught RuntimeError from
    execute_fn. The most common small-model failure (bad syntax, an
    unknown table, a forbidden keyword -- everything the guard itself
    catches) was therefore NEVER self-corrected, even though execution
    failures were."""

    def test_bad_syntax_triggers_a_correction_round(self):
        """clean_sql raises 'No SELECT / CTE found...' for non-SQL
        output. That must now trigger a correction round instead of
        aborting run() outright."""
        ag = _agent(["this is not sql at all", SIMPLE_SQL])
        df, result = ag.run("test", system_prompt="")
        assert result.attempt == 2
        assert result.sql == SIMPLE_SQL

    def test_forbidden_keyword_triggers_a_correction_round(self):
        """validate_sql raises 'Forbidden keyword detected' -- also a
        ValueError, also previously unretried."""
        ag = _agent(["DELETE FROM Contract", SIMPLE_SQL])
        df, result = ag.run("test", system_prompt="")
        assert result.attempt == 2

    def test_validation_error_message_reaches_the_correction_prompt(self):
        ag = _agent(["not sql at all", SIMPLE_SQL])
        _, result = ag.run("test", system_prompt="")
        assert result.correction_prompts
        assert "No SELECT" in result.correction_prompts[0]

    def test_validation_failures_are_bounded_by_max_corrections(self):
        """All attempts fail validation -> the loop must still give up
        after max_corrections rounds, raising ValueError, not loop
        forever or exceed the budget."""
        ag = _agent(
            ["still not sql"] * (MAX_CORRECTION_ATTEMPTS + 1),
            max_corrections=MAX_CORRECTION_ATTEMPTS,
        )
        with pytest.raises(ValueError):
            ag.run("test", system_prompt="")

    def test_worst_case_llm_call_count_is_unchanged_by_this_fix(self):
        """Bringing validation failures into the loop must NOT raise the
        worst-case number of LLMBackend.generate() calls above what
        execution-failure retries already budgeted for:
        max_corrections + 1 total."""
        generate_calls: list[str] = []

        class CountingAlwaysBadBackend(LLMBackend):
            def generate(self, prompt: str) -> str:
                generate_calls.append(prompt)
                return "still not sql"

        ag = SQLAgent(
            backend=CountingAlwaysBadBackend(),
            execute_fn=_ok_execute,
            max_corrections=MAX_CORRECTION_ATTEMPTS,
        )
        with pytest.raises(ValueError):
            ag.run("test", system_prompt="")

        assert len(generate_calls) == MAX_CORRECTION_ATTEMPTS + 1

    def test_correction_prompt_accumulates_every_prior_attempt(self):
        """Each round's prompt must include EVERY prior correction
        prompt, not just the most recent -- previously
        `initial_prompt + correction_prompt` discarded all earlier
        rounds, letting the model repeat a mistake it was already told
        about."""
        prompts_seen: list[str] = []
        responses = iter(["not sql at all", SIMPLE_SQL, SIMPLE_SQL])

        class TrackingBackend(LLMBackend):
            def generate(self, prompt: str) -> str:
                prompts_seen.append(prompt)
                return next(responses)

        call_count = 0

        def fail_once_then_succeed(sql: str) -> pd.DataFrame:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Database error: some execution problem")
            return SIMPLE_DF.copy()

        ag = SQLAgent(
            backend=TrackingBackend(),
            execute_fn=fail_once_then_succeed,
            max_corrections=2,
        )
        ag.run("test", system_prompt="")

        assert len(prompts_seen) == 3
        # The 3rd prompt (2nd correction round) must still carry evidence
        # of the 1st correction round's (validation) failure, not just
        # the 2nd's (execution) failure.
        assert "not sql at all" in prompts_seen[2]

    def test_correction_prompts_field_is_actually_used_in_the_prompt(self):
        """SQLGenerationResult.correction_prompts must reflect exactly
        what was appended to the prompt sent to the backend -- it must
        not be an accumulated-but-unused list."""
        prompts_seen: list[str] = []
        responses = iter(["not sql at all", SIMPLE_SQL])

        class TrackingBackend(LLMBackend):
            def generate(self, prompt: str) -> str:
                prompts_seen.append(prompt)
                return next(responses)

        ag = SQLAgent(backend=TrackingBackend(), execute_fn=_ok_execute)
        _, result = ag.run("test", system_prompt="")

        assert len(result.correction_prompts) == 1
        assert result.correction_prompts[0] in prompts_seen[1]


# ---------------------------------------------------------------------------
# ensure_top() wiring (item 11)
# ---------------------------------------------------------------------------

class TestEnsureTopWiring:
    """ensure_top() used to be unreachable from any production code path
    (grep-confirmed: only tests and docstrings called it) -- the only cap
    on a result set was client-side fetchmany() *after* SQL Server had
    already computed the full, unbounded result. SQLAgent.run() must now
    apply ensure_top() to the generated SQL before ever calling
    execute_fn."""

    def test_missing_top_is_injected_before_execution(self):
        from config import override_settings

        captured_sql: list[str] = []

        def _capture_execute(sql: str) -> pd.DataFrame:
            captured_sql.append(sql)
            return SIMPLE_DF.copy()

        ag = _agent(
            ["SELECT Name FROM [Auction_Dim].[Customer]"],
            execute_fn=_capture_execute,
        )
        with override_settings(default_top_n=17):
            ag.run("test", system_prompt="")

        assert captured_sql, "execute_fn was never called"
        assert "TOP 17" in captured_sql[0].upper()

    def test_existing_top_is_left_untouched(self):
        """A model that already emits its own TOP must not be overridden."""
        from config import override_settings

        captured_sql: list[str] = []

        def _capture_execute(sql: str) -> pd.DataFrame:
            captured_sql.append(sql)
            return SIMPLE_DF.copy()

        ag = _agent([SIMPLE_SQL], execute_fn=_capture_execute)  # SIMPLE_SQL has TOP 10
        with override_settings(default_top_n=17):
            ag.run("test", system_prompt="")

        assert captured_sql[0] == SIMPLE_SQL
        assert "TOP 17" not in captured_sql[0].upper()

    def test_capped_sql_is_reflected_in_the_result(self):
        """The capped SQL, not the raw model output, must be what
        SQLGenerationResult.sql (and therefore the API response) reports."""
        from config import override_settings

        ag = _agent(["SELECT Name FROM [Auction_Dim].[Customer]"])
        with override_settings(default_top_n=17):
            _, result = ag.run("test", system_prompt="")

        assert "TOP 17" in result.sql.upper()
