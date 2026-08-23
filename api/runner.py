"""Core request handler — wires SQLAgent, executor, and interpreter.

Thread-safety model
-------------------
``SQLAgent`` itself holds no per-request mutable state: after ``__init__``
all attributes (``_backend``, ``_execute``, ``_max_corrections``) are
read-only.  Concurrent calls to ``agent.run()`` or ``backend.generate()``
therefore do not race.

However, a plain module-level singleton (``agent = SQLAgent()``) has two
problems:

1. It is created at *import time*, which means any misconfigured env var
   raises during ``import api.runner`` — before the server can return a
   meaningful error.
2. If a future change adds per-request mutable state to ``SQLAgent`` or
   ``OllamaBackend`` the silent sharing would become a real race.

The fix: the singleton is created **lazily** inside ``_get_agent()`` which
is protected by a ``threading.Lock``.  The agent instance is cached after
the first successful construction and reused across requests.  Tests can
patch the module-level ``agent`` name directly via
``unittest.mock.patch('api.runner.agent', mock)`` or call
``_reset_agent_for_testing()`` to force re-construction.

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
import threading
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

# ---------------------------------------------------------------------------
# Module-level agent singleton
# ---------------------------------------------------------------------------
# Public name ``agent`` is intentional: tests patch it with
#   patch('api.runner.agent', mock_agent)
# The private lock + double-checked locking ensure thread-safe lazy init.

_agent_lock: threading.Lock = threading.Lock()

# Public alias — starts as None; lazily populated by _get_agent().
# Keeping this as a plain module attribute (not a property) is what makes
# unittest.mock.patch work: patch() replaces the name in the module's
# __dict__, so run_query() sees the mock on the very next read.
agent: SQLAgent | None = None

# Per-provider agents, keyed by provider name.  Only used when run_query()
# is called with an explicit provider (e.g. from the webapp dropdown).
_agents_by_provider: dict[str, SQLAgent] = {}


def _get_agent(provider: str | None = None) -> SQLAgent:
    """Return the shared ``SQLAgent``, constructing it once on first call.

    When *provider* is None the module-level ``agent`` singleton is used
    (test patches applied to ``api.runner.agent`` are respected).  An
    explicit provider builds its own cached instance.
    """
    global agent
    if provider is None:
        if agent is None:      # fast path — no lock needed once set
            with _agent_lock:
                if agent is None:  # second check inside lock
                    logger.debug("Constructing SQLAgent singleton")
                    agent = SQLAgent()
        return agent

    cached = _agents_by_provider.get(provider)
    if cached is not None:
        return cached
    with _agent_lock:
        cached = _agents_by_provider.get(provider)
        if cached is None:
            logger.debug("Constructing SQLAgent for provider=%s", provider)
            cached = SQLAgent(provider=provider)
            _agents_by_provider[provider] = cached
    return cached


def _reset_agent_for_testing(new_agent: SQLAgent | None = None) -> None:
    """Replace (or clear) the cached agent.  **Test-only helper.**

    Call with an explicit ``new_agent`` to inject a mock, or with no
    arguments to force re-construction on the next ``_get_agent()`` call.
    Prefer ``unittest.mock.patch('api.runner.agent', mock)`` in fixtures
    when you want automatic teardown.
    """
    global agent
    with _agent_lock:
        agent = new_agent
        _agents_by_provider.clear()


# ---------------------------------------------------------------------------
# Helpers that need the agent
# ---------------------------------------------------------------------------

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
    provider: str | None = None,
) -> QueryResponse:
    """Full pipeline with typed error translation and query-result caching.

    Cache is consulted/populated only for mode='result'|'full' with
    interpret=False.  sql-only and interpreted requests always bypass it.

    *provider* selects the LLM backend: ``None`` uses the default singleton
    (``cfg.settings.llm_provider``), any other value (``auto``, ``ollama``,
    ``openai``, ...) builds/returns a dedicated agent for that provider.

    Raises only :class:`~api.errors.NLQError` subclasses.
    """
    # Read the public ``agent`` name — if a test has patched it via
    # patch('api.runner.agent', mock), _get_agent() is bypassed entirely
    # because the mock is already non-None.
    _agent = _get_agent(provider)

    # ── cache lookup (result / full, no interpret) ─────────────────────────
    use_cache = (mode in ("result", "full")) and not interpret
    if use_cache:
        cached = query_cache.get(question, mode)
        if cached is not None:
            logger.debug("Cache HIT  question=%.60s mode=%s", question, mode)
            return cached
        logger.debug("Cache MISS question=%.60s mode=%s", question, mode)

    # ── sql-only mode: generate without executing ──────────────────────────
    if mode == "sql":
        sql = _safe_generate_sql_only(_agent, question, system_prompt)
        return QueryResponse(
            question=question,
            sql=sql,
            model=_agent._backend.name,
        )

    # ── result / full mode ─────────────────────────────────────────────────
    df, result = _safe_run(_agent, question, system_prompt)
    rows: list[dict] = df.to_dict(orient="records")

    interpretation: str | None = None
    if interpret and mode in ("result", "full"):
        interpretation = _interpret(_agent, question, rows)

    response = QueryResponse(
        question=question,
        sql=result.sql if mode == "full" else None,
        result=rows,
        interpretation=interpretation,
        row_count=len(rows),
        correction_attempts=result.attempt,
        model=_agent._backend.name,
    )

    # ── cache store ────────────────────────────────────────────────────────
    if use_cache:
        query_cache.set(question, mode, response)
        logger.debug("Cache SET  question=%.60s mode=%s", question, mode)

    return response


# ---------------------------------------------------------------------------
# Private helpers — exception translation
# ---------------------------------------------------------------------------

def _safe_generate_sql_only(agent: SQLAgent, question: str, system_prompt: str) -> str:
    from retrieval.context_retriever import ContextRetriever
    from prompt_engine.builder import PromptBuilder
    from security.sql_guard import clean_sql, validate_sql

    context = ContextRetriever.retrieve(question)
    prompt = PromptBuilder.build(
        question=question, system_prompt=system_prompt, context=context
    )
    try:
        raw = agent._backend.generate(prompt)
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


def _safe_run(agent: SQLAgent, question: str, system_prompt: str):
    """Run SQLAgent and translate every exception to a typed NLQError."""
    try:
        return agent.run(question, system_prompt)

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


def _interpret(agent: SQLAgent, question: str, rows: list[dict]) -> str:
    preview_text = "\n".join(str(r) for r in rows[:20]) or "(empty result set)"
    prompt = _INTERPRET_TEMPLATE.format(question=question, rows=preview_text)
    try:
        return agent._backend.generate(prompt).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Interpretation failed (non-fatal): %s", exc)
        return ""
