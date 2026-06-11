"""Core request handler — wires SQLAgent, executor, and interpreter.

All raw exceptions from sub-layers are caught here and re-raised as
typed NLQError subclasses so ``api/errors.py`` handlers emit the correct
HTTP status code.

Query result cache
------------------
Successful ``result`` and ``full`` mode responses are stored in
``api.query_cache.query_cache`` (LRU + TTL, thread-safe).  The cache is
**skipped** for:

* ``mode='sql'`` — generation-only; always hit the LLM for freshness.
* ``interpret=True`` — interpretation may change; treat as uncacheable.
* Any request that raises an exception.
"""

from __future__ import annotations

import logging
from typing import Literal

import requests as _requests
from sqlalchemy.exc import OperationalError, TimeoutError as SATimeout

import config as cfg
from api.errors import (
    OutOfScopeError,
    ForbiddenSQLError,
    InjectionAttemptError,
    InvalidSQLResponseError,
    EmptySQLResponseError,
    ModelUnavailableError,
    ModelTimeoutError,
    QueryExecutionError,
    DatabaseConnectionError,
    QueryTimeoutError,
)
from api.models import QueryResponse
from api.query_cache import query_cache
from llm.sql_agent import SQLAgent

logger = logging.getLogger(__name__)

_agent = SQLAgent()

_INTERPRET_TEMPLATE = """
You are a helpful data analyst. The user asked:

{question}

The database returned these results (up to 20 rows shown):

{rows}

Write a concise one-paragraph summary in the same language as the question.
Do not repeat column names literally — describe findings in plain language.
If the result is empty, say so clearly.
"""


def run_query(
    question: str,
    system_prompt: str,
    mode: Literal["sql", "result", "full"] = "full",
    interpret: bool = False,
) -> QueryResponse:
    """Full pipeline with typed error translation and query-result caching.

    Cache is consulted/populated only for mode='result'|'full' with
    interpret=False.  sql-only and interpreted requests always bypass it.

    Raises only :class:`~api.errors.NLQError` subclasses.
    """
    # ── cache lookup (result / full, no interpret) ────────────────────────
    use_cache = (mode in ("result", "full")) and not interpret
    if use_cache:
        cached = query_cache.get(question, mode)
        if cached is not None:
            logger.debug("Cache HIT  question=%.60s mode=%s", question, mode)
            return cached
        logger.debug("Cache MISS question=%.60s mode=%s", question, mode)

    # ── sql-only mode: generate without executing ─────────────────────────
    if mode == "sql":
        sql = _safe_generate_sql_only(question, system_prompt)
        return QueryResponse(
            question=question,
            sql=sql,
            model=_agent._backend.name,
        )

    # ── result / full mode ────────────────────────────────────────────────
    df, result = _safe_run(question, system_prompt)
    rows: list[dict] = df.to_dict(orient="records")

    interpretation: str | None = None
    if interpret and mode in ("result", "full"):
        interpretation = _interpret(question, rows)

    response = QueryResponse(
        question=question,
        sql=result.sql if mode == "full" else None,
        result=rows,
        interpretation=interpretation,
        row_count=len(rows),
        correction_attempts=result.attempt,
        model=_agent._backend.name,
    )

    # ── cache store ───────────────────────────────────────────────────────
    if use_cache:
        query_cache.set(question, mode, response)
        logger.debug("Cache SET  question=%.60s mode=%s", question, mode)

    return response


# ---------------------------------------------------------------------------
# Private helpers — exception translation
# ---------------------------------------------------------------------------

def _safe_generate_sql_only(question: str, system_prompt: str) -> str:
    from retrieval.context_retriever import ContextRetriever
    from prompt_engine.builder import PromptBuilder
    from security.sql_guard import clean_sql, validate_sql

    context = ContextRetriever.retrieve(question)
    prompt = PromptBuilder.build(
        question=question, system_prompt=system_prompt, context=context
    )
    try:
        raw = _agent._backend.generate(prompt)
    except ValueError as exc:
        if str(exc) == "OUT_OF_SCOPE":
            raise OutOfScopeError("This question is outside the Auction domain.")
        raise
    except _requests.Timeout as exc:
        raise ModelTimeoutError(
            "The LLM took too long to respond. Please try again.",
            detail=str(exc),
        )
    except _requests.ConnectionError as exc:
        raise ModelUnavailableError(
            "Cannot reach the LLM backend. Is Ollama running?",
            detail=str(exc),
        )
    except RuntimeError as exc:
        raise ModelUnavailableError(str(exc))

    if not raw or not raw.strip():
        raise EmptySQLResponseError("LLM returned an empty response.")

    try:
        sql = clean_sql(raw)
        validate_sql(sql)
    except ValueError as exc:
        msg = str(exc)
        if "Forbidden keyword" in msg:
            raise ForbiddenSQLError(msg)
        raise InvalidSQLResponseError(
            f"LLM response could not be parsed into valid SQL: {msg}",
            detail=raw[:500],
        )

    return sql


def _safe_run(question: str, system_prompt: str):
    """Run SQLAgent and translate every exception to a typed NLQError."""
    try:
        return _agent.run(question, system_prompt)

    except ValueError as exc:
        msg = str(exc)
        if msg == "OUT_OF_SCOPE":
            raise OutOfScopeError("This question is outside the Auction domain.")
        if "Forbidden keyword" in msg:
            raise ForbiddenSQLError(msg)
        raise InvalidSQLResponseError(
            f"LLM response could not be parsed into valid SQL: {msg}"
        )

    except _requests.Timeout as exc:
        raise ModelTimeoutError(
            "The LLM took too long to respond. Please try again.",
            detail=str(exc),
        )

    except _requests.ConnectionError as exc:
        raise ModelUnavailableError(
            "Cannot reach the LLM backend. Is Ollama running?",
            detail=str(exc),
        )

    except RuntimeError as exc:
        msg = str(exc)
        if "Ollama" in msg or "unreachable" in msg.lower():
            raise ModelUnavailableError(msg)
        if "LOCK_TIMEOUT" in msg or "lock timeout" in msg.lower():
            raise QueryTimeoutError(
                "Query timed out waiting for database lock.",
                detail=msg,
            )
        if "Cannot connect" in msg or "connection" in msg.lower():
            raise DatabaseConnectionError(
                "Cannot connect to the database.",
                detail=msg,
            )
        raise QueryExecutionError(
            f"Database returned an error: {msg}",
            detail=msg,
        )


def _interpret(question: str, rows: list[dict]) -> str:
    preview_text = "\n".join(str(r) for r in rows[:20]) or "(empty result set)"
    prompt = _INTERPRET_TEMPLATE.format(question=question, rows=preview_text)
    try:
        return _agent._backend.generate(prompt).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Interpretation failed (non-fatal): %s", exc)
        return ""
