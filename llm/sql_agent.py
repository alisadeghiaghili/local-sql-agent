"""LLM-agnostic SQL agent with self-correction.

Pipeline (first attempt)
------------------------
    question
        → ContextRetriever
        → PromptBuilder          (initial prompt)
        → LLMBackend.generate
        → clean_sql
        → validate_sql
        → execute_sql  ──────────────────────────────────────┐
                                                              │ RuntimeError
Correction loop (up to MAX_CORRECTION_ATTEMPTS)              │
────────────────────────────────────────────────             │
    error message                                            ↓
        → _build_correction_prompt   ←───────────────────────┘
        → LLMBackend.generate
        → clean_sql
        → validate_sql
        → execute_sql

The loop aborts as soon as execution succeeds or the attempt cap is hit.
On the final failed attempt the last RuntimeError is re-raised so the
caller (app.py) can log it normally.

Design notes
------------
* ``SQLAgent`` depends on ``LLMBackend`` (abstract), not on Ollama.  Swap
  the backend at construction time to switch models without touching
  application code.
* ``execute_fn`` is injected so the agent can be unit-tested without a
  real database (pass a mock / stub).
* When *execute_fn* is **not** supplied, the agent calls
  ``database.executor.execute_query`` through its module reference every
  time — this ensures that ``monkeypatch.setattr(database.executor,
  'execute_query', ...)`` in tests is visible at call time.
* The correction prompt is appended *after* the failed SQL, preserving the
  full schema+rules context from the original prompt so the model has all
  the information it needs to fix the query.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pandas as pd

from llm.base import LLMBackend, SQLGenerationResult
from retrieval.context_retriever import ContextRetriever
from prompt_engine.builder import PromptBuilder
from security.sql_guard import clean_sql, validate_sql

logger = logging.getLogger(__name__)

MAX_CORRECTION_ATTEMPTS: int = 2

_CORRECTION_TEMPLATE = """
The SQL query you generated failed to execute.

--- FAILED SQL ---
{sql}

--- DATABASE ERROR ---
{error}

--- INSTRUCTIONS ---
Fix ONLY the SQL error above. Do not change the intent of the query.
Return only the corrected SQL statement with no explanation.

SQL:
"""


def _default_execute(sql: str) -> pd.DataFrame:
    """Call ``database.executor.execute_query`` via module lookup.

    Looking up through the module (rather than capturing the function at
    import time) means ``monkeypatch.setattr(database.executor,
    'execute_query', stub)`` is always visible at call time.
    """
    import database.executor as _executor_mod
    return _executor_mod.execute_query(sql)


class SQLAgent:
    """Orchestrates retrieval → prompt → LLM → execute with self-correction.

    Parameters
    ----------
    backend:
        Any :class:`LLMBackend` implementation.  Defaults to
        :func:`~llm.wizard_llm.build_backend` built from *provider*.
    provider:
        Provider name (``openai``/``mock``).  Used only when *backend* is
        None; defaults to the OpenAI-compatible backend.
    execute_fn:
        Callable ``(sql: str) -> pd.DataFrame``.  Defaults to
        :func:`database.executor.execute_query` (looked up at call time so
        monkeypatching works).  Override in tests for explicit injection.
    max_corrections:
        How many correction rounds to attempt before giving up.
    """

    def __init__(
        self,
        backend: LLMBackend | None = None,
        execute_fn: Callable[[str], pd.DataFrame] | None = None,
        max_corrections: int = MAX_CORRECTION_ATTEMPTS,
        provider: str | None = None,
    ) -> None:
        if backend is None:
            from llm.wizard_llm import build_backend
            backend = build_backend(provider)

        self._backend = backend
        self._execute = execute_fn if execute_fn is not None else _default_execute
        self._max_corrections = max_corrections

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, question: str, system_prompt: str) -> tuple[pd.DataFrame, SQLGenerationResult]:
        """Answer *question* and return ``(DataFrame, SQLGenerationResult)``.

        Raises
        ------
        ValueError("OUT_OF_SCOPE")
            When the model signals the question is out of scope.
        ValueError
            When SQL validation fails and cannot be corrected.
        RuntimeError
            When execution still fails after all correction attempts.
        """
        context = ContextRetriever.retrieve(question)
        initial_prompt = PromptBuilder.build(
            question=question,
            system_prompt=system_prompt,
            context=context,
        )

        sql, raw, correction_prompts = self._generate_and_clean(initial_prompt)

        last_error: str | None = None
        attempt = 1

        for correction_round in range(self._max_corrections + 1):
            if correction_round > 0:
                correction_prompt = _CORRECTION_TEMPLATE.format(
                    sql=sql,
                    error=last_error,
                )
                correction_prompts.append(correction_prompt)
                logger.info(
                    "Self-correction attempt %d/%d for question: %.80s",
                    correction_round,
                    self._max_corrections,
                    question,
                )
                sql, raw, _ = self._generate_and_clean(
                    initial_prompt + correction_prompt
                )
                attempt = correction_round + 1

            try:
                df = self._execute(sql)
                result = SQLGenerationResult(
                    sql=sql,
                    raw_response=raw,
                    attempt=attempt,
                    correction_prompts=correction_prompts,
                )
                if attempt > 1:
                    logger.info(
                        "Self-correction succeeded on attempt %d: %.120s",
                        attempt,
                        sql,
                    )
                return df, result

            except RuntimeError as exc:
                last_error = str(exc)
                logger.warning(
                    "Execution failed (attempt %d): %s",
                    attempt,
                    last_error,
                )
                if correction_round == self._max_corrections:
                    raise

        # unreachable
        raise RuntimeError("SQLAgent loop exited unexpectedly")  # pragma: no cover

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _generate_and_clean(self, prompt: str) -> tuple[str, str, list[str]]:
        """Call backend, clean output, validate.  Returns (sql, raw, [])."""
        raw = self._backend.generate(prompt)
        sql = clean_sql(raw)
        validate_sql(sql)
        return sql, raw, []
