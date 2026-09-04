# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
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
   ``OpenAIBackend`` the silent sharing would become a real race.

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

Audit trail
-----------
Every call to :func:`run_query` writes exactly one
:class:`~observability.audit.AuditRecord` — success, guard rejection, LLM
failure, database error, out-of-scope, or an unexpected bug — via a single
``try/except/finally`` that always reaches the write, regardless of which
path the request took (see :func:`_write_audit`, called only from the
``finally`` block below). Per-stage timings come from one
``observability.timing.StageTimer`` created at the top of ``run_query``
and threaded into ``SQLAgent.run()``; the LLM status block comes from
whatever metadata the router's
:class:`~llm.router.RouteResult` carried back from the backend that
actually served the call (``provider`` and ``fallback_used`` included), or
that a backend attached to the exception which ended the request. Auditing never raises: :func:`_write_audit` catches
everything, on top of :func:`~observability.audit.save_audit_record`
already swallowing its own I/O errors — a broken audit trail must never
fail a user's query.

``request_id`` is always the id ``api/middleware.py``'s
``RequestIDMiddleware`` stamped on ``request.state`` (see
``api/server.py``), so the audit record, the ``X-Request-ID`` response
header, and any server log line for the same request all agree.

``tier`` is ``"T0"`` when this specific call was served from the query
cache, else ``"T2"`` (single-shot LLM pipeline) — the only two tiers
reachable today. ``T1`` (template) and ``T3`` (agent) are introduced in a
later phase, per ``docs/api-contract-v2.md``.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Literal

import pandas as pd
import requests as _requests
from sqlalchemy.exc import OperationalError, TimeoutError as SATimeout

import config as cfg
from api.errors import (
    NLQError,
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
from llm.base import LLMBackend
from llm.router import RemoteProviderNotAllowedError, TaskType, build_prompt_segments
from llm.sql_agent import SQLAgent
from observability.audit import AuditRecord, save_audit_record
from observability.llm_status import build_llm_status, finish_reason_from_meta
from observability.timing import StageTimer
from prompt_engine.static_prefix import prefix_version as _prefix_version_of
from prompt_engine.static_prefix import static_prefix_token_estimate
from security.auth import Principal
from security.auth import scope_key as _scope_key_of
from security.sql_guard import extract_touched_tables

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


def _get_agent() -> SQLAgent:
    """Return the shared ``SQLAgent``, constructing it once on first call.

    Test patches applied to ``api.runner.agent`` are respected.
    """
    global agent
    if agent is None:      # fast path — no lock needed once set
        with _agent_lock:
            if agent is None:  # second check inside lock
                logger.debug("Constructing SQLAgent singleton")
                agent = SQLAgent()
    return agent


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
All monetary values are in Iranian Rials (ریال): always express amounts with
the unit Rial/ریال and never use toman/تومان.
If the result is empty, say so clearly.
"""

# NLQErrors that mean the SQL guard itself rejected the (cleaned) SQL the
# model produced — as opposed to a transport failure, an out-of-scope
# decline, or a downstream database error. Drives AuditRecord.guard's
# "verdict"/"rule" fields.
_GUARD_REJECTED_ERRORS: frozenset[type[NLQError]] = frozenset({
    ForbiddenSQLError,
    InjectionAttemptError,
    InvalidSQLResponseError,
})

# NLQErrors reached only after the model produced a genuine (if unusable)
# response — i.e. the LLM call itself completed, so the audit "llm" block's
# finish_reason is derived from that real response (see
# finish_reason_from_meta) instead of "error" -- typically "stop", but a
# response cut off by the token limit correctly reads "length" here too,
# not just a re-purposed "stop". Anything NOT in this set
# (ModelUnavailableError, ModelTimeoutError, and any future addition) is
# treated as a transport-level failure -- finish_reason "error", regardless
# of meta (there is no real response to read it from).
_LLM_COMPLETED_ERRORS: frozenset[type[NLQError]] = frozenset({
    OutOfScopeError,
    ForbiddenSQLError,
    InjectionAttemptError,
    InvalidSQLResponseError,
    EmptySQLResponseError,
    DatabaseConnectionError,
    QueryTimeoutError,
    QueryExecutionError,
})


def run_query(
    question: str,
    system_prompt: str,
    mode: Literal["sql", "result", "full"] = "full",
    interpret: bool = False,
    request_id: str | None = None,
    principal: Principal | None = None,
) -> QueryResponse:
    """Full pipeline with typed error translation and query-result caching.

    Cache is consulted/populated only for mode='result'|'full' with
    interpret=False.  sql-only and interpreted requests always bypass it.

    Exactly one :class:`~observability.audit.AuditRecord` is written per
    call — on success, on a cache hit, and on every error path — via the
    ``finally`` block below; see the module docstring's "Audit trail"
    section.

    Parameters
    ----------
    question, system_prompt, mode, interpret:
        As before.
    request_id:
        The id ``api/middleware.py``'s ``RequestIDMiddleware`` stamped on
        this HTTP request (``request.state.request_id``), so the audit
        record matches the ``X-Request-ID`` response header and server
        log lines for the same request. ``None`` (e.g. a caller outside an
        HTTP request, such as a direct unit test) falls back to a freshly
        minted id, so the audit record is still well-formed.
    principal:
        The authenticated caller (Phase 8), or ``None`` for a caller with
        no principal at all (the CLI/REPL path in ``webapp/agent.py``, or
        any pre-Phase-8 direct test call). Feeds three things: the query
        cache's partition key (see ``security.auth.scope_key`` and
        ``api/query_cache.py``'s module docstring) — ``None`` uses the
        cache's own default (unscoped) partition, exactly this
        function's pre-Phase-8 behaviour, so existing non-HTTP callers
        are unaffected — ``principal_id`` on the audit record, and
        ``principal.denied_columns``, threaded through to every
        :func:`~security.sql_guard.validate_sql` call this request makes
        (via :func:`_safe_run`/:func:`_safe_generate_sql_only`) so a
        column-restricted principal's SQL is actually rejected for
        touching a denied column, not just partitioned into its own
        cache scope.

    Raises
    ------
    NLQError
        Only :class:`~api.errors.NLQError` subclasses — see individual
        ``_safe_*`` helpers for the translation rules.
    """
    # Read the public ``agent`` name — if a test has patched it via
    # patch('api.runner.agent', mock), _get_agent() is bypassed entirely
    # because the mock is already non-None.
    _agent = _get_agent()
    timer = StageTimer()
    req_id = request_id or uuid.uuid4().hex[:12]
    # Measured once per call (cheap: build_static_prefix/estimate_tokens are
    # lru_cache-backed on system_prompt, which is loaded once at startup) so
    # the audit ``llm`` block's prefix_cache_hit ratio is meaningful instead
    # of comparing against a hardcoded 0 (see docs/api-contract-v2.md §6).
    static_prefix_tokens = static_prefix_token_estimate(system_prompt)
    # Embedded in every query_cache key (question- and SQL-keyed alike) so a
    # knowledge-base change invalidates stale entries by construction -- see
    # api/query_cache.py's module docstring and Phase 2 task 6.
    cache_prefix_version = _prefix_version_of(system_prompt)
    # Cache-partition key (Phase 8): "" (the cache's own unscoped default)
    # when no principal is known at all, so every pre-Phase-8 caller keeps
    # sharing the single partition it always has. A real principal always
    # gets a real scope key -- including an all-access one, which still
    # partitions away from the unscoped "" default used by non-HTTP callers.
    cache_scope = _scope_key_of(principal) if principal is not None else ""
    # Column-level ACL (Phase 8): the seam security.sql_guard.validate_sql
    # has always accepted but that, before this phase, nothing populated.
    # None (no principal) applies no restriction -- pre-Phase-8 behaviour.
    denied_columns = principal.denied_columns if principal is not None else None

    # Mutable state accumulated for the audit record regardless of which
    # path this request takes (success, cache hit, or any error below).
    audit_sql: str = ""
    audit_guard: dict[str, Any] = {
        "verdict": "allowed", "rule": None, "injected_top": None, "tables_touched": None,
    }
    audit_row_count: int = 0
    audit_columns: list[str] | None = None
    audit_llm: dict[str, Any] | None = None
    audit_error_code: str | None = None
    audit_error_message: str | None = None
    # T0 only when this exact call was served from cache; every other
    # path today is the single-shot LLM pipeline, T2. T1 (template) and
    # T3 (agent) are not reachable yet -- they land in a later phase, per
    # docs/api-contract-v2.md.
    audit_tier: str = "T2"

    try:
        # ── cache lookup (result / full, no interpret) ─────────────────────
        use_cache = (mode in ("result", "full")) and not interpret
        if use_cache:
            cached = query_cache.get(
                question, mode, prefix_version=cache_prefix_version, scope_key=cache_scope,
            )
            if cached is not None:
                logger.debug("Cache HIT  question=%.60s mode=%s", question, mode)
                audit_tier = "T0"
                audit_sql = cached.sql or ""
                audit_row_count = cached.row_count or 0
                audit_columns = _columns_from_rows(cached.result)
                if audit_sql:
                    audit_guard["tables_touched"] = _touched_or_none(audit_sql)
                # injected_top is not recoverable from a cached QueryResponse
                # (it isn't part of that model) -- left None, a known gap.
                return cached
            logger.debug("Cache MISS question=%.60s mode=%s", question, mode)

        # ── sql-only mode: generate without executing ──────────────────────
        if mode == "sql":
            sql, llm_meta = _safe_generate_sql_only(
                _agent, question, system_prompt, timer, denied_columns=denied_columns,
            )
            audit_sql = sql
            audit_guard["tables_touched"] = _touched_or_none(sql)
            # ensure_top is never applied in this mode (no execution, no
            # row cap to enforce) -- injected_top correctly stays None.
            audit_llm = _llm_status_block(
                llm_meta, _agent._backend,
                finish_reason=finish_reason_from_meta(llm_meta),
                static_prefix_tokens=static_prefix_tokens,
            )
            return QueryResponse(
                question=question,
                sql=sql,
                model=_agent._backend.name,
                llm=audit_llm,
            )

        # ── result / full mode ─────────────────────────────────────────────
        sql_cache_lookup = None
        if use_cache:
            sql_cache_lookup = functools.partial(
                _rows_from_sql_cache,
                mode=mode, prefix_version=cache_prefix_version, scope_key=cache_scope,
            )

        df, result = _safe_run(
            _agent, question, system_prompt, timer,
            sql_cache_lookup=sql_cache_lookup, denied_columns=denied_columns,
        )
        rows: list[dict] = df.to_dict(orient="records")

        audit_sql = result.sql
        audit_row_count = len(rows)
        audit_columns = [str(c) for c in df.columns]
        audit_guard["tables_touched"] = _touched_or_none(result.sql)
        audit_guard["injected_top"] = result.injected_top
        audit_llm = _llm_status_block(
            result.llm_meta, _agent._backend,
            finish_reason=finish_reason_from_meta(result.llm_meta),
            corrections=max(result.attempt - 1, 0),
            static_prefix_tokens=static_prefix_tokens,
        )

        interpretation: str | None = None
        if interpret and mode in ("result", "full"):
            with timer.stage("interpret"):
                interpretation = _interpret(_agent, question, rows)

        response = QueryResponse(
            question=question,
            sql=result.sql if mode == "full" else None,
            result=rows,
            interpretation=interpretation,
            row_count=len(rows),
            correction_attempts=result.attempt,
            model=_agent._backend.name,
            llm=audit_llm,
        )

        # ── cache store ────────────────────────────────────────────────────
        if use_cache:
            query_cache.set(
                question, mode, response,
                prefix_version=cache_prefix_version, sql=result.sql, scope_key=cache_scope,
            )
            logger.debug("Cache SET  question=%.60s mode=%s", question, mode)

        return response

    except NLQError as exc:
        audit_error_code = exc.error_code
        audit_error_message = exc.message
        error_type = type(exc)
        if error_type in _GUARD_REJECTED_ERRORS:
            audit_guard["verdict"] = "rejected"
            audit_guard["rule"] = exc.error_code
        elif isinstance(exc, OutOfScopeError):
            audit_guard["rule"] = "OUT_OF_SCOPE"
        # A genuine model response completed the LLM stage (the model DID
        # answer -- the SQL just turned out to be unusable, guard-rejected,
        # or the downstream execution/out-of-scope path failed afterward),
        # so its own finish_reason is real and worth reading: a response
        # that got cut off by the token limit must read "length", not
        # "stop", even though it went on to fail for an unrelated reason --
        # that distinction is exactly what tells an operator "raise
        # llm_num_predict" apart from "the model is bad at SQL". A total
        # transport failure (NOT in _LLM_COMPLETED_ERRORS) never reached
        # the model at all, so there is no real finish_reason to read --
        # "error" stays hardcoded for that case, deliberately not run
        # through finish_reason_from_meta.
        if error_type in _LLM_COMPLETED_ERRORS:
            finish_reason = finish_reason_from_meta(getattr(exc, "llm_meta", None))
        else:
            finish_reason = "error"
        audit_llm = _llm_status_block_for_error(
            exc, _agent._backend, finish_reason, static_prefix_tokens=static_prefix_tokens,
        )

        # The guard (or, for an execution failure, validate_sql earlier in
        # the same request) may have left a candidate SQL string on the
        # exception -- see llm/sql_agent.py::SQLAgent._clean_validate_cap
        # and api/runner.py's _carry_exception_meta. Recover it so a
        # rejected/failed request's audit record still says what was
        # attempted, not just that it failed.
        candidate_sql = getattr(exc, "candidate_sql", None)
        if candidate_sql:
            audit_sql = candidate_sql
            audit_guard["tables_touched"] = _touched_or_none(candidate_sql)
        exc_injected_top = getattr(exc, "injected_top", None)
        if exc_injected_top is not None:
            audit_guard["injected_top"] = exc_injected_top
        raise

    except Exception as exc:  # noqa: BLE001 — still write exactly one record
        audit_error_code = "INTERNAL_ERROR"
        audit_error_message = str(exc)
        raise

    finally:
        _write_audit(
            request_id=req_id,
            question=question,
            sql=audit_sql,
            guard=audit_guard,
            row_count=audit_row_count,
            tier=audit_tier,
            error_code=audit_error_code,
            error_message=audit_error_message,
            timings=timer.snapshot(),
            llm=audit_llm,
            columns=audit_columns,
            principal_id=principal.id if principal is not None else None,
        )


# ---------------------------------------------------------------------------
# Private helpers — audit trail
# ---------------------------------------------------------------------------

def _columns_from_rows(rows: list[dict] | None) -> list[str] | None:
    """Column names from a cached response's row dicts, or ``None`` if empty.

    A cache hit does not re-run the query, so the only source of column
    names is the shape of the rows the cache already stored — never their
    values (see :mod:`observability.audit`'s hard rule against writing row
    data to the audit trail).
    """
    if not rows:
        return None
    return [str(c) for c in rows[0].keys()]


def _touched_or_none(sql: str) -> list[str] | None:
    """``extract_touched_tables(sql)``, or ``None`` if it found nothing.

    ``docs/api-contract-v2.md`` §4's ``guard.tables_touched`` is the field
    that answers "which tables did this query read" — the AST guard
    (``security.sql_guard.validate_sql``) already resolves every table
    reference to reject an unknown one; :func:`~security.sql_guard.extract_touched_tables`
    reuses that same resolution to report it instead. Safe to call on a
    *rejected* candidate too (see that function's docstring) — some
    references may simply not resolve, which is exactly when this
    returns ``None`` rather than an empty list, so "we don't know" and
    "we know it touched nothing" don't look the same in the audit log.
    """
    tables = extract_touched_tables(sql, dialect=cfg.settings.sql_dialect)
    return tables or None


def _model_name(backend: LLMBackend) -> str:
    """Best-effort bare model tag for the audit ``llm`` block.

    Prefers the backend's private ``_model`` attribute (e.g. ``"llama3"``)
    when it is a real string, falling back to ``backend.name`` (e.g.
    ``"openai:gpt-oss-20b"``) for backends that don't expose one — including
    test doubles, where an unset attribute on a ``MagicMock`` would
    otherwise be a truthy-but-not-a-string object.
    """
    model = getattr(backend, "_model", None)
    return model if isinstance(model, str) else backend.name


def _llm_status_block(
    meta: dict[str, Any] | None,
    backend: LLMBackend,
    *,
    finish_reason: str,
    default_endpoint_status: int = 200,
    default_attempts: int = 1,
    corrections: int = 0,
    static_prefix_tokens: int = 0,
) -> dict[str, Any]:
    """Build the audit ``llm`` block from best-effort call metadata.

    *meta* is whatever :meth:`~llm.base.LLMBackend.generate_with_meta` (or
    an exception's ``llm_meta`` attribute — see :func:`_llm_status_block_for_error`)
    supplied; it may be empty, e.g. for a backend that doesn't override
    ``generate_with_meta``.

    ``static_prefix_tokens`` is the measured (heuristic) size of the
    current static prompt prefix — see
    ``prompt_engine.static_prefix.static_prefix_token_estimate`` — passed
    through to :func:`~observability.llm_status.build_llm_status` so its
    ``prefix_cache_hit`` ratio (``prompt_tokens < static_prefix_tokens *
    0.5``) is meaningful instead of always ``False`` (the previous
    hardcoded ``0``).
    """
    meta = meta or {}
    return build_llm_status(
        meta.get("raw"),
        model=_model_name(backend),
        endpoint=meta.get("endpoint"),
        trusted=bool(meta.get("trusted", False)),
        endpoint_status=meta.get("endpoint_status", default_endpoint_status),
        attempts=meta.get("attempts", default_attempts),
        finish_reason=finish_reason,
        structured_output=bool(meta.get("structured_output", False)),
        static_prefix_tokens=static_prefix_tokens,
        temperature=cfg.settings.llm_temperature,
        seed=cfg.settings.llm_seed,
        corrections=corrections,
        provider=meta.get("provider"),
        fallback_used=bool(meta.get("fallback_used", False)),
        total_ms=meta.get("total_ms"),
        reasoning_detected=bool(meta.get("reasoning_detected", False)),
    )


def _reasoning_pollution_note(llm_meta: dict[str, Any] | None) -> str:
    """A short prefix for a parse-failure message when the response looks
    like the model answered on its reasoning channel instead of with SQL.

    Reads ``llm_meta["reasoning_detected"]`` -- set by
    :meth:`~llm.providers.OpenAIBackend.generate_with_meta` (see its
    ``reasoning_detected`` meta field) -- and, when set, turns a bare
    "No SELECT / CTE found in model response" (``security.sql_guard.clean_sql``)
    from reading as the model being bad at SQL into an accurate diagnosis:
    a reasoning-capable endpoint (the deployment target, gpt-oss, emits one)
    likely reasoned instead of answering, which is a protocol/prompt issue,
    not a competence one. Returns ``""`` (a no-op prefix) otherwise, so an
    ordinary parse failure is unaffected.
    """
    if llm_meta and llm_meta.get("reasoning_detected"):
        return (
            "Model response appears to carry reasoning/chain-of-thought text "
            "instead of a final SQL answer -- "
        )
    return ""


def _llm_status_block_for_error(
    exc: Exception, backend: LLMBackend, finish_reason: str, *, static_prefix_tokens: int = 0,
) -> dict[str, Any]:
    """Build the audit ``llm`` block after a request failed.

    ``finish_reason == "error"`` means the call never got a usable
    response (transport failure) — in that case default to
    ``endpoint_status=0`` and ``attempts=<configured retries>`` rather
    than pretending a 200 happened. Otherwise (the model did respond, just
    with something the pipeline couldn't use) default to a single
    successful attempt, same as the success path.
    """
    meta = getattr(exc, "llm_meta", None)
    if finish_reason == "error":
        default_status, default_attempts = 0, getattr(backend, "_retries", 1)
    else:
        default_status, default_attempts = 200, 1
    return _llm_status_block(
        meta, backend, finish_reason=finish_reason,
        default_endpoint_status=default_status, default_attempts=default_attempts,
        static_prefix_tokens=static_prefix_tokens,
    )


def _carry_exception_meta(src: Exception, dst: NLQError) -> None:
    """Propagate best-effort audit metadata from *src* onto *dst*.

    Three independent, individually-optional attributes may have been
    attached to *src* by the time it reaches here, and are copied across
    if present (each is a no-op when absent):

    ``llm_meta``
        Set by ``OpenAIBackend.generate_with_meta`` on a genuine model
        response (OUT_OF_SCOPE, or the last attempt before giving up) —
        lets the caller still build an accurate audit ``llm`` block after
        translating *src* into a typed :class:`~api.errors.NLQError`.
    ``candidate_sql``
        Set by ``SQLAgent._clean_validate_cap`` (guard rejection) or by
        ``SQLAgent.run``'s execution-failure branch (the query passed the
        guard but failed at the database) — the SQL text that was
        attempted, so a failed request's audit record can still say what
        was tried and which known tables it touched
        (:func:`_touched_or_none`), not just that it failed.
    ``injected_top``
        Set alongside ``candidate_sql`` on an execution failure — the row
        cap that was actually applied to the query that failed.
    ``attempt``
        Set by ``SQLAgent.run``'s guard except-clauses — how many
        generation calls this request actually cost before giving up: 1
        for a :class:`~security.sql_guard.PolicyRejection` (never
        retried), or ``max_corrections + 1`` for a
        :class:`~security.sql_guard.CorrectableRejection` that never
        passed the guard. Lets a caller report the honest count instead
        of assuming the full correction budget was always spent.
    """
    for attr in ("llm_meta", "candidate_sql", "injected_top", "attempt"):
        value = getattr(src, attr, None)
        if value is not None:
            setattr(dst, attr, value)


def _write_audit(
    *,
    request_id: str,
    question: str,
    sql: str,
    guard: dict[str, Any],
    row_count: int,
    tier: str,
    error_code: str | None,
    error_message: str | None,
    timings: dict[str, int],
    llm: dict[str, Any] | None,
    columns: list[str] | None,
    principal_id: str | None = None,
) -> None:
    """Build and persist one :class:`~observability.audit.AuditRecord`.

    Never raises: wraps both the record's construction (which can raise
    ``TypeError`` if ``columns`` were ever malformed — see
    ``AuditRecord.__post_init__``) and
    :func:`~observability.audit.save_audit_record` (which already
    swallows its own ``OSError``) in one broad ``except``. A broken audit
    trail must never fail a user's query.

    Parameters
    ----------
    tier:
        ``"T0"`` (cache hit) or ``"T2"`` (single-shot LLM pipeline) —
        whichever the caller determined this specific call actually was.
        ``"T1"`` (template) and ``"T3"`` (agent) are not reachable yet;
        they're introduced in a later phase, per
        ``docs/api-contract-v2.md``.
    """
    try:
        record = AuditRecord(
            timestamp=datetime.now(),
            request_id=request_id,
            question=question,
            generated_sql=sql,
            guard=guard,
            row_count=row_count,
            tier=tier,
            error_code=error_code,
            error_message=error_message,
            timings=timings,
            llm=llm,
            columns=columns,
            principal_id=principal_id,
        )
        save_audit_record(record)
    except Exception:  # noqa: BLE001 — auditing must never fail a user's query
        logger.exception("Failed to build/save audit record (request_id=%s)", request_id)


# ---------------------------------------------------------------------------
# Private helpers — exception translation
# ---------------------------------------------------------------------------

def _safe_generate_sql_only(
    agent: SQLAgent, question: str, system_prompt: str, timer: StageTimer,
    *, denied_columns: tuple[str, ...] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Generate SQL without executing it (``mode="sql"``), translating errors.

    Routed via ``agent._router.generate_for_task(TaskType.SQL_GENERATION,
    ...)`` — the same task-based chain, fallback, and governance machinery
    :meth:`~llm.sql_agent.SQLAgent.run` uses for the result/full-mode hot
    path, and :func:`_interpret` uses for interpretation. This mode is a
    third request path into the same task, so it assembles its prompt with
    :func:`~llm.router.build_prompt_segments` (never
    ``PromptBuilder.build`` directly) and hands the segments to the router
    rather than calling ``agent._backend`` — otherwise it would silently
    drift from the other two: a flat prompt string with no static-prefix
    invariance, no fallback chain, and no remote-provider governance.

    Exception-translation contract
    ------------------------------
    Every clause below switches on the ORIGINAL exception type
    (``ValueError("OUT_OF_SCOPE")``, ``requests.Timeout``,
    ``requests.ConnectionError``, plain ``RuntimeError``), and keeping
    them matching :func:`_safe_run`'s is load-bearing: ``run_query``
    documents that it raises only :class:`~api.errors.NLQError`
    subclasses, so a clause that lets the original type escape breaks that
    contract for this mode alone. The ``ValueError`` clause therefore
    mirrors :func:`_safe_run`'s exactly — ``OUT_OF_SCOPE`` is the decline
    sentinel, and *any other* ``ValueError`` becomes an
    :class:`~api.errors.InvalidSQLResponseError` (502) rather than
    propagating bare. It used to ``raise`` bare, which meant a real
    non-sentinel ``ValueError`` — most concretely a
    ``requests.exceptions.JSONDecodeError`` (it subclasses ``ValueError``)
    from a remote provider's unretried ``resp.json()`` on a truncated or
    non-JSON 200 body — reached the caller with no ``error_code`` and no
    ``http_status``, was audited as ``INTERNAL_ERROR``, and was served as
    a generic 500 that hid a model/transport problem behind a server bug.
    ``result``/``full`` mode always mapped that same case to 502; the two
    paths now agree. (``OllamaBackend`` never reaches here that way — it
    catches ``JSONDecodeError`` as the ``RequestException`` it also is,
    retries it, and exhausts into a ``RuntimeError``.)

    The forbidden-keyword branch :func:`_safe_run` has in its own
    ``ValueError`` clause is deliberately absent here: this function runs
    the guard itself, in a separate ``try`` further down, so a guard
    rejection can never surface at the llm stage.

    But
    :meth:`~llm.router.LLMRouter._call_chain` wraps a chain-exhausted
    failure in one ``RuntimeError`` carrying the original exception as
    ``__cause__``, so it is unwrapped (``exc.__cause__ or exc``) and
    re-raised before those clauses see it — exactly what
    :meth:`~llm.sql_agent.SQLAgent.run` does. Without the unwrap, a
    transport timeout or a connection failure would arrive here as an
    opaque ``RuntimeError`` and those branches would stop matching.
    A :class:`~llm.router.RemoteProviderNotAllowedError` (a
    ``RuntimeError`` subclass, raised by the governance check *before* the
    backend it guards is called, hence never wrapped) lands in the
    ``RuntimeError`` branch as a ``ModelUnavailableError`` — this mode
    returns SQL text only, so a refusal is a plain failure, not the
    degrade-and-continue that :func:`_interpret` can afford.

    What routing does change, on this path as on
    :meth:`~llm.sql_agent.SQLAgent.run`'s: a non-decline exception raised
    by a non-final backend in the chain is no longer necessarily terminal
    (a later backend may still answer), and a per-task latency-budget
    breach surfaces as a builtin ``TimeoutError`` — an ``OSError``,
    matching none of the four clauses. Both are router semantics shared
    with the result/full path, not properties of this mode; neither is
    reachable with the default single-entry chain and no configured
    budget.

    ``ValueError("OUT_OF_SCOPE")`` is the deliberate exception to that
    first sentence: :meth:`~llm.router.LLMRouter._call_chain` re-raises a
    decline bare the moment any backend in the chain signals it, so a
    later backend can never override it, and — being the same object, with
    ``__cause__`` untouched — it resolves through the unwrap above to
    itself. The transport types are what exercise the wrapper now.
    """
    from retrieval.context_retriever import ContextRetriever
    from security.sql_guard import clean_sql, validate_sql

    with timer.stage("plan"):
        context = ContextRetriever.retrieve(question)
    with timer.stage("prompt"):
        segments = build_prompt_segments(question, system_prompt, context)

    try:
        # The "llm" stage still records its elapsed time when the call
        # raises (StageTimer.stage times the block either way -- see its
        # docstring), and any ``llm_meta`` a backend attached to the
        # exception survives the unwrap below, since that is the very same
        # exception object the backend raised.
        with timer.stage("llm"):
            try:
                route_result = agent._router.generate_for_task(
                    TaskType.SQL_GENERATION, segments
                )
            except Exception as exc:  # noqa: BLE001 - unwrapped, then translated below
                raise exc.__cause__ or exc
        raw, llm_meta = route_result.text or "", route_result.meta
    except ValueError as exc:
        if str(exc) == "OUT_OF_SCOPE":
            err: NLQError = OutOfScopeError("This question is outside the Auction domain.")
        else:
            err = InvalidSQLResponseError(
                f"LLM response could not be parsed into valid SQL: {exc}"
            )
        _carry_exception_meta(exc, err)
        raise err
    except _requests.Timeout as exc:
        raise ModelTimeoutError(
            "The LLM took too long to respond. Please try again.",
            detail=str(exc),
        )
    except TimeoutError as exc:
        # The BUILTIN TimeoutError -- an OSError subclass, unrelated to
        # requests.Timeout above -- as raised by a socket-level timeout or
        # by LLMRouter._call_chain when a backend breaches
        # `llm_task_budget_seconds`. Without this clause it would fall
        # through every translation below and escape run_query as a
        # non-NLQError, audited as INTERNAL_ERROR. Kept in lockstep with
        # _safe_run's identical clause (see this module's "Private helpers"
        # section and llm/sql_agent.py's module docstring).
        raise ModelTimeoutError(
            "The LLM took too long to respond. Please try again.",
            detail=str(exc),
        )
    except _requests.ConnectionError as exc:
        raise ModelUnavailableError(
            "Cannot reach the LLM backend. Is the configured endpoint running?",
            detail=str(exc),
        )
    except RuntimeError as exc:
        raise ModelUnavailableError(str(exc))

    if not raw or not raw.strip():
        err = EmptySQLResponseError("LLM returned an empty response.")
        err.llm_meta = llm_meta  # type: ignore[attr-defined]
        raise err

    # Pre-declared (rather than only assigned inside the try) so that if
    # clean_sql itself raises, `sql` stays a well-defined None -- there
    # never was a candidate to report -- instead of an UnboundLocalError.
    sql: str | None = None
    try:
        with timer.stage("guard"):
            sql = clean_sql(raw)
            validate_sql(sql, denied_columns=denied_columns)
    except ValueError as exc:
        msg = str(exc)
        # Whether this is an outright refusal (400) or merely unusable
        # output (502) is ``security.sql_guard.SqlGuardRejection``'s own
        # ``is_refusal`` attribute -- a SEPARATE axis from the
        # Correctable/PolicyRejection split (see its docstring): every
        # PolicyRejection is a refusal, but so is an unknown-table
        # CorrectableRejection, so neither the exception's class nor a
        # substring of its message ("Forbidden keyword" happened to open
        # most, but not all, refusal messages -- e.g. "System catalogue
        # forbidden: ...") is the right thing to switch on.
        if getattr(exc, "is_refusal", False):
            err = ForbiddenSQLError(msg)
        else:
            err = InvalidSQLResponseError(
                f"{_reasoning_pollution_note(llm_meta)}"
                f"LLM response could not be parsed into valid SQL: {msg}",
                detail=raw[:500],
            )
        err.llm_meta = llm_meta  # type: ignore[attr-defined]
        # `sql` is the cleaned-but-rejected candidate when validate_sql is
        # what failed, or still None if clean_sql itself failed first.
        err.candidate_sql = sql  # type: ignore[attr-defined]
        raise err

    return sql, llm_meta


def _rows_from_sql_cache(
    sql: str, mode: str, prefix_version: str, scope_key: str = "",
) -> pd.DataFrame | None:
    """SQLAgent's ``sql_cache_lookup`` hook: reuse a result cached under *sql*.

    Consulted by :meth:`~llm.sql_agent.SQLAgent.run` right before it would
    otherwise execute the guard-approved SQL — a cache hit here means a
    (possibly different) earlier question already generated and ran this
    exact SQL, so the database round-trip is skipped entirely (Phase 2
    task 6: "cache by generated SQL as well, so different questions
    producing the same SQL share a result").

    Returns
    -------
    pandas.DataFrame | None
        The cached rows as a DataFrame, or ``None`` on a cache miss — in
        which case :meth:`SQLAgent.run` falls through to a real execution,
        exactly as if this hook were never supplied.
    """
    cached = query_cache.get_by_sql(sql, mode, prefix_version=prefix_version, scope_key=scope_key)
    if cached is None or cached.result is None:
        return None
    return pd.DataFrame(cached.result)


def _safe_run(
    agent: SQLAgent,
    question: str,
    system_prompt: str,
    timer: StageTimer,
    *,
    sql_cache_lookup=None,
    denied_columns: tuple[str, ...] | None = None,
):
    """Run SQLAgent and translate every exception to a typed NLQError."""
    try:
        return agent.run(
            question, system_prompt, timer=timer, sql_cache_lookup=sql_cache_lookup,
            denied_columns=denied_columns,
        )

    except ValueError as exc:
        msg = str(exc)
        if msg == "OUT_OF_SCOPE":
            err: NLQError = OutOfScopeError("This question is outside the Auction domain.")
        elif getattr(exc, "is_refusal", False):
            # See the identical branch in _safe_generate_sql_only for why
            # this switches on SqlGuardRejection.is_refusal and not a
            # "Forbidden keyword" substring test.
            err = ForbiddenSQLError(msg)
        else:
            err = InvalidSQLResponseError(
                f"{_reasoning_pollution_note(getattr(exc, 'llm_meta', None))}"
                f"LLM response could not be parsed into valid SQL: {msg}"
            )
        _carry_exception_meta(exc, err)
        raise err

    except _requests.Timeout as exc:
        raise ModelTimeoutError(
            "The LLM took too long to respond. Please try again.",
            detail=str(exc),
        )

    except TimeoutError as exc:
        # The BUILTIN TimeoutError -- an OSError subclass, NOT a subclass
        # of requests.Timeout above, so the two clauses never overlap.
        # LLMRouter._call_chain raises this when a backend breaches
        # `llm_task_budget_seconds`, and SQLAgent.run re-raises it
        # unwrapped from the chain-exhausted RuntimeError's __cause__ --
        # so it arrives here as a bare TimeoutError and, without this
        # clause, escapes untranslated (audited as INTERNAL_ERROR,
        # breaking run_query's NLQError-only contract). An operator's
        # latency budget is a timeout, so it maps to the same typed error
        # requests.Timeout does. Kept in lockstep with
        # _safe_generate_sql_only's identical clause.
        raise ModelTimeoutError(
            "The LLM took too long to respond. Please try again.",
            detail=str(exc),
        )

    except _requests.ConnectionError as exc:
        raise ModelUnavailableError(
            "Cannot reach the LLM backend. Is the configured endpoint running?",
            detail=str(exc),
        )

    except RuntimeError as exc:
        msg = str(exc)
        if "unreachable" in msg.lower():
            err = ModelUnavailableError(msg)
        elif "LOCK_TIMEOUT" in msg or "lock timeout" in msg.lower():
            err = QueryTimeoutError(
                "Query timed out waiting for database lock.",
                detail=msg,
            )
        elif "Cannot connect" in msg or "connection" in msg.lower():
            err = DatabaseConnectionError(
                "Cannot connect to the database.",
                detail=msg,
            )
        else:
            err = QueryExecutionError(
                f"Database returned an error: {msg}",
                detail=msg,
            )
        _carry_exception_meta(exc, err)
        raise err


_THOUSAND_SEP = r"[ \u00A0\u202F\u2009\u066C]"  # space, NBSP, NNBSP, thin space, ٬

# Numbers already written with thousands separators (e.g. "143 066 295 000").
_SEPARATED_NUMBER_RE = re.compile(
    rf"(?<!\d)\d{{1,3}}(?:{_THOUSAND_SEP}\d{{3}})+(?!\d)"
)


def _thousands_separate(number: str) -> str:
    """Insert comma thousands separators into a digit string (ASCII or Persian)."""
    digits = list(number)
    for i in range(len(digits) - 3, 0, -3):
        digits.insert(i, ",")
    return "".join(digits)


def _format_numbers(text: str) -> str:
    """Normalize large numbers to comma thousands-separators.

    Handles both bare runs (``12000000000``) and numbers already separated
    with spaces / NBSP / thin space (``143 066 295 000``).  4-digit Persian
    years like ``1402`` are left alone.
    """
    def _to_commas(match: re.Match) -> str:
        digits = "".join(ch for ch in match.group(0) if ch.isdigit())
        return _thousands_separate(digits)

    text = _SEPARATED_NUMBER_RE.sub(_to_commas, text)
    text = re.sub(r"\d{5,}", lambda m: _thousands_separate(m.group(0)), text)
    return text


def _interpret(agent: SQLAgent, question: str, rows: list[dict]) -> str:
    """Ask the router to summarise *rows* in natural language.

    Routed via ``agent._router.generate_text_for_task(TaskType.INTERPRETATION,
    ...)`` — the same task-based chain, fallback, and governance machinery
    ``SQLAgent.run`` uses for SQL generation, applied here to the
    interpretation task. ``generate_text_for_task`` (rather than
    ``generate_for_task``) is used deliberately: this prompt is entirely
    per-request row data and a question, with no static prefix worth
    segmenting for provider-side caching, so the plain
    ``LLMBackend.generate`` call is the right primitive, not
    ``generate_with_meta_segments``.

    Data-governance gate (Phase 2 task 5)
    --------------------------------------
    This function sends up to 20 rows of REAL query results to whichever
    backend the interpretation task routes to. As long as that backend is
    local/trusted (a self-hosted endpoint, or the ``mock`` stub), that is exactly this product's
    premise ("runs on your infrastructure"). The moment it is a hosted
    provider (:class:`~llm.providers.OpenAIBackend` pointed at a hosted
    API), sending row data there is a
    genuine data-exfiltration path, and it must not happen silently just
    because ``LLM_PROVIDER`` was set — see ``llm.router``'s module
    docstring. ``LLMRouter._governance_check`` (refuse unless
    ``cfg.settings.llm_allow_remote`` is explicitly ``True``) already
    raises :class:`~llm.router.RemoteProviderNotAllowedError` *before* any
    backend method is called; this function catches that and is loud on
    its own terms: an ``ERROR``-level log naming the backend and the
    row count that was about to be sent, distinct from the generic
    "Interpretation failed (non-fatal)" warning below so the refusal is
    never mistaken for an ordinary transport hiccup.
    """
    preview_text = "\n".join(str(r) for r in rows[:20]) or "(empty result set)"
    prompt = _INTERPRET_TEMPLATE.format(question=question, rows=preview_text)
    try:
        route_result = agent._router.generate_text_for_task(TaskType.INTERPRETATION, prompt)
    except RemoteProviderNotAllowedError as exc:
        logger.error(
            "REFUSED interpretation: %d result row(s) would be sent to a remote LLM "
            "provider without LLM_ALLOW_REMOTE=true (%s). Set LLM_ALLOW_REMOTE=true to "
            "explicitly opt this deployment into sending query-result data to a hosted "
            "provider. Interpretation skipped for this request.",
            len(rows), exc,
        )
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Interpretation failed (non-fatal): %s", exc)
        return ""
    summary = (route_result.text or "").strip()
    # Belt-and-suspenders: the model may still write toman despite the prompt rule.
    summary = re.sub(r"toman", "Rial", summary.replace("تومان", "ریال"), flags=re.IGNORECASE)
    # Normalize price numbers to comma thousands-separators.
    summary = _format_numbers(summary)
    return summary
