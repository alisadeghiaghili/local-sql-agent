# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""FastAPI server for the Auction NLQ Engine.

Endpoints
---------
POST /query
    Accept a natural-language question and return SQL, result, or both
    — plus an optional plain-language interpretation.

GET  /health
    Liveness check: confirms the LLM endpoint is reachable and DB can be pinged.

GET  /cache/stats
    Return current cache metrics (hits, misses, size, evictions, enabled).

POST /cache/clear
    Flush the entire query-result cache and return a stats snapshot.

POST /cache/invalidate
    Evict a single (question, mode) entry from the cache.

GET  /admin/summary, /admin/health/checks, /admin/cache, /admin/config
    Read-only operator observability, admin-capability-gated — see
    ``api/admin_routes.py`` and ``docs/admin-panel-architecture.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse, StreamingResponse

import config as cfg
import api.admin_routes as admin_routes
import api.runner as runner  # import the MODULE so patch.object(runner, 'run_query') works
import api.v2_routes as v2_routes
# Only register_handlers is needed here: the typed exceptions are raised in
# api/runner.py and translated to responses by the handlers that
# register_handlers() attaches, so this module never names them.
from api.auth import AuthMiddleware, get_principal_if_any, require_principal
from api.errors import register_handlers
from core.provenance import log_startup_notice
from core.version import __version__
from api.middleware import RequestIDMiddleware, RateLimitMiddleware, ConcurrencyMiddleware
from api.models import (
    QueryRequest,
    QueryResponse,
    HealthResponse,
    CacheStatsResponse,
    CacheInvalidateRequest,
)
from api.query_cache import query_cache
from security.auth import ApiKeyConfigError, Principal, load_api_keys

logger = logging.getLogger(__name__)

# Resolved relative to this file (api/server.py), NOT the process's
# current working directory — running uvicorn from anywhere other than
# the repo root used to silently fail to find the prompt.
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.md"
_system_prompt: str = ""

# ---------------------------------------------------------------------------
# Bounded threadpool for blocking pipeline work (Phase 2 task 4)
# ---------------------------------------------------------------------------
# runner.run_query() is fully synchronous (blocking requests to the LLM endpoint,
# blocking pyodbc calls to SQL Server). Since api/server.py's handlers are
# now `async def`, calling it directly would block the whole event loop --
# every other in-flight request (including /health) would stall for the
# duration. asyncio.to_thread() moves it to a worker thread instead, and
# this semaphore bounds how many such threads may run at once, independent
# of ConcurrencyMiddleware's own admission-control counter (api/middleware.py)
# — that middleware decides whether a request is accepted into the server at
# all (503 when over capacity); this semaphore decides how many accepted
# requests may occupy a blocking worker thread simultaneously. Sized
# generously above ConcurrencyMiddleware's default cap (10) so it is not the
# binding constraint under normal load, while still being a real, explicit
# bound rather than "however many threads asyncio.to_thread's default
# executor happens to allow".
_QUERY_THREAD_LIMIT: int = int(os.getenv("QUERY_THREAD_LIMIT", "16"))
_query_semaphore: asyncio.Semaphore = asyncio.Semaphore(_QUERY_THREAD_LIMIT)


async def _run_query_bounded(**kwargs) -> QueryResponse:
    """Run ``runner.run_query`` off the event loop, under a concurrency bound.

    Parameters
    ----------
    **kwargs:
        Forwarded verbatim to :func:`api.runner.run_query`.

    Returns
    -------
    api.models.QueryResponse

    Raises
    ------
    api.errors.NLQError
        Whatever ``runner.run_query`` itself raises, propagated unchanged
        — ``asyncio.to_thread`` re-raises the worker thread's exception in
        the calling coroutine, so the existing exception-handler
        registration (``register_handlers``) still works unmodified.
    """
    async with _query_semaphore:
        return await asyncio.to_thread(runner.run_query, **kwargs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _system_prompt

    # Stated before anything can fail: an operator who never reaches a
    # working config should still have seen whose work this is and on what
    # terms. See core/provenance.py for why this is a log line and not a
    # licence check that could refuse to start.
    log_startup_notice(logger)

    try:
        cfg.settings.validate()
    except ValueError as exc:
        raise RuntimeError(f"Invalid configuration: {exc}") from exc

    # ── Phase 8: fail closed on authentication config ──────────────────────
    # Mirrors the db_connection_url precedent immediately above: a broken
    # or absent auth configuration must stop the server from starting at
    # all, not be discovered later as every caller gets a 401 nobody can
    # fix without a redeploy.
    try:
        configured_keys = load_api_keys()
    except ApiKeyConfigError as exc:
        raise RuntimeError(f"Invalid API_KEYS_JSON: {exc}") from exc

    if cfg.settings.auth_required and not configured_keys:
        raise RuntimeError(
            "AUTH_REQUIRED is true but API_KEYS_JSON has no configured keys "
            "-- refusing to start a server that requires authentication "
            "nobody could ever satisfy. Set API_KEYS_JSON (see "
            "scripts/issue_api_key.py) or explicitly set AUTH_REQUIRED=false."
        )
    if not cfg.settings.auth_required:
        # Logged on EVERY startup (not deduplicated) -- a deliberately
        # disabled front door must never be quiet in the logs. See
        # config.py's Settings.auth_required docstring.
        logger.warning(
            "AUTH_REQUIRED=false -- this server is accepting unauthenticated "
            "requests on every route except the ones that were already open. "
            "This is a deliberate escape hatch and must not be used in a "
            "production deployment."
        )

    if not _PROMPT_PATH.exists():
        raise RuntimeError(f"System prompt not found: {_PROMPT_PATH}")
    _system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    # v2_routes.py cannot import this module at load time (this module
    # imports v2_routes to mount its router -- a cycle), so the loaded
    # prompt is handed across explicitly here instead.
    v2_routes._system_prompt = _system_prompt
    logger.info("System prompt loaded (%d chars)", len(_system_prompt))

    # ── §9/§10: retention purge, once at start-up (no second daemon thread) ─
    # Permanently deletes any persisted session past session_retention_days
    # since its last activity. A no-op when session persistence is disabled
    # (session_store_path=""). Best-effort: a failure here (e.g. a
    # transiently locked SQLite file) must not stop the server from
    # starting -- the next restart tries again, and TTL expiry alone still
    # bounds the in-memory hot set in the meantime.
    try:
        removed = v2_routes.get_session_store().purge_expired()
        if removed:
            logger.info("session retention purge: removed %d expired session(s)", removed)
    except Exception as exc:  # noqa: BLE001 - startup must not fail on this
        logger.warning("session retention purge failed at startup: %s", exc)

    # ── Phase 5b: prefetch the small-dimension value vocabulary ────────────
    # Opt-in (see Settings.dimension_vocabulary_warm_on_startup) so this
    # codebase's DB-less test suite is unaffected by default. This call
    # happens BEFORE `yield` -- under the ASGI lifespan protocol no request
    # is served until this coroutine yields, so "the first request must not
    # block on a database round trip" holds by construction: the round
    # trips happen here, on zero in-flight requests, not during a request.
    # A failure is a warning, not fatal -- an empty vocabulary cache
    # degrades every dimension it covers to "no match" (this phase's
    # universal safe-miss behaviour), which is not a reason to refuse to
    # start the server at all.
    if cfg.settings.dimension_vocabulary_warm_on_startup:
        from retrieval.dimension_vocabulary import warm_all

        try:
            counts = warm_all()
            logger.info("dimension_vocabulary: startup warm-up complete: %s", counts)
        except Exception as exc:  # noqa: BLE001 - a cold cache degrades gracefully, startup must not fail
            logger.warning("dimension_vocabulary: startup warm-up failed: %s", exc)

    yield


# docs_url/redoc_url/openapi_url are disabled here and re-registered by hand
# below (Phase 8) so they can sit behind the same Depends(require_principal)
# as every other non-/health route -- FastAPI's auto-registered docs routes
# have no dependency-injection seam of their own.
app = FastAPI(
    title="Auction NLQ Engine",
    description="Natural-language \u2192 SQL \u2192 Results API for Auction_DM.",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# --- Middleware (order matters: outer → inner) ---
# 1. ConcurrencyMiddleware  — innermost: applied after rate-limit passes
# 2. RateLimitMiddleware    — per-IP (or per-principal) token-bucket (429 on excess)
# 3. AuthMiddleware         — resolves request.state.principal (Phase 8) --
#                              must run before RateLimitMiddleware so it can
#                              bucket on principal id, and after RequestID so
#                              its own logging can carry the request id
# 4. RequestIDMiddleware    — stamps X-Request-ID first so all downstream
#                              middleware can log it
# 5. CORSMiddleware         — outermost: a preflight OPTIONS request must
#                              get CORS headers even when every other
#                              layer would otherwise reject it
#
# FastAPI/Starlette applies add_middleware() in REVERSE order, so the last
# add_middleware() call becomes the outermost layer.
app.add_middleware(ConcurrencyMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(RequestIDMiddleware)

# CORS: default-restrictive (docs/api-contract-v2.md §9 / web/README.md).
# Empty cfg.settings.cors_allowed_origins (the default) means allow_origins=[],
# which blocks every cross-origin request -- same-origin callers are
# unaffected either way. Set CORS_ALLOWED_ORIGINS (comma-separated) to
# unblock the bundled web/ UI when it is served from a different
# origin/port than this API, e.g. CORS_ALLOWED_ORIGINS=http://localhost:8080.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(cfg.settings.cors_allowed_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- v2 conversational session routes (docs/api-contract-v2.md §3) ---
app.include_router(v2_routes.router)

# --- Admin panel, phase 1: read-only observability (docs/admin-panel-architecture.md) ---
app.include_router(admin_routes.router)

# --- Exception handlers ---
register_handlers(app)


# ---------------------------------------------------------------------------
# API documentation — gated the same as every other route (Phase 8)
# ---------------------------------------------------------------------------
# /docs, /redoc, and /openapi.json describe exactly what an authenticated
# caller can do to production data; publishing that to an unauthenticated
# network is itself a disclosure. Registered by hand (docs_url=None etc.
# above) because FastAPI's auto-registered docs routes have no
# dependency-injection seam to attach Depends(require_principal) to.
# APP_DOCS_PUBLIC=true is the explicit opt-out for a deployment that
# already sits behind its own perimeter auth.

def _require_docs_access(request: Request) -> None:
    if cfg.settings.app_docs_public:
        return
    require_principal(request)


@app.get("/openapi.json", include_in_schema=False)
def openapi_json(_: None = Depends(_require_docs_access)) -> JSONResponse:
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
def swagger_docs(_: None = Depends(_require_docs_access)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Docs")


@app.get("/redoc", include_in_schema=False)
def redoc_docs(_: None = Depends(_require_docs_access)):
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")


# ---------------------------------------------------------------------------
# POST /query
# ---------------------------------------------------------------------------

@app.post(
    "/query",
    response_model=QueryResponse,
    summary="Translate a question to SQL and/or execute it",
    responses={
        400: {"description": "Bad request (forbidden SQL, injection attempt, invalid input)"},
        422: {"description": "Out-of-scope question or Pydantic validation error"},
        429: {"description": "Rate limit exceeded"},
        500: {"description": "Unexpected server error"},
        502: {"description": "LLM or database returned an unusable response"},
        503: {"description": "LLM or database is unavailable, or server is overloaded"},
        504: {"description": "LLM inference or query execution timed out"},
    },
)
async def query(
    req: QueryRequest, request: Request, principal: Principal = Depends(require_principal),
) -> QueryResponse:
    import time
    start = time.perf_counter()

    if not _system_prompt:
        # lifespan() is what loads _system_prompt; reaching this point
        # with it still empty means startup ran without the ASGI
        # lifespan protocol (or some other bug bypassed it). Silently
        # continuing would prompt the model with NO system instructions
        # at all for every request — fail loudly instead of guessing.
        raise RuntimeError(
            "System prompt not loaded — lifespan startup did not run "
            "(is this app being served without the ASGI lifespan protocol?)"
        )

    # request.state.request_id is stamped by RequestIDMiddleware before
    # this handler runs (see api/middleware.py). Passing it through to
    # run_query() means the audit record it writes, this response's
    # X-Request-ID header, and any server log line for this request all
    # agree on the same id.
    request_id = getattr(request.state, "request_id", None)

    # Call via module reference so that patch.object(api.runner, 'run_query')
    # in tests intercepts this call correctly. _run_query_bounded() runs it
    # in a worker thread (asyncio.to_thread) under a bounded semaphore, so
    # this fully-synchronous pipeline never blocks the event loop -- see
    # that function's docstring.
    response = await _run_query_bounded(
        question=req.question,
        system_prompt=_system_prompt,
        mode=req.mode,
        interpret=req.interpret,
        request_id=request_id,
        principal=principal,
    )

    response.elapsed_seconds = round(time.perf_counter() - start, 3)
    return response


# ---------------------------------------------------------------------------
# POST /query/stream — SSE, docs/api-contract-v2.md §7 event shape
# ---------------------------------------------------------------------------

async def _query_event_stream(
    req: QueryRequest, request_id: str | None, principal: Principal,
):
    """Yield SSE events for one query, per contract §7's event names.

    Perceived latency matters as much as measured latency for a streaming
    endpoint: the client sees a ``stage`` event the instant the request is
    accepted, then ``sql``, ``rows``, ``llm``, and ``done`` as the
    (still-synchronous, still-corrected-if-needed) pipeline produces them.

    Known limitation
    ----------------
    This is **not** per-token streaming. ``runner.run_query`` still runs
    end-to-end (including SQLAgent's self-correction loop) as one blocking
    call in a worker thread before the ``sql``/``rows`` events are emitted
    — true incremental "SQL forming before rows arrive" typing would
    require SQLAgent itself to stream partial tokens from the endpoint's
    own streaming response, which is a larger change than Task 4
    covers this phase (see the Phase 2 report). What this endpoint DOES
    provide today: an immediate ``stage`` event so the client can render a
    progress indicator instead of a blank wait, and events broken out by
    pipeline phase rather than one opaque JSON blob at the end.
    """
    import json

    def _event(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield _event("stage", {"stage": "plan", "state": "start"})

    try:
        response = await _run_query_bounded(
            question=req.question,
            system_prompt=_system_prompt,
            mode=req.mode,
            interpret=req.interpret,
            request_id=request_id,
            principal=principal,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as an SSE error event
        code = getattr(exc, "error_code", "INTERNAL_ERROR")
        message = getattr(exc, "message", str(exc))
        yield _event("error", {"code": code, "message": message})
        return

    yield _event("sql", {"sql": response.sql, "guard": {}})
    if response.result is not None:
        columns = list(response.result[0].keys()) if response.result else []
        yield _event(
            "rows",
            {"columns": columns, "rows": response.result, "row_count": response.row_count or 0},
        )
    if response.interpretation:
        yield _event("interpretation_delta", {"text": response.interpretation})
    if response.llm is not None:
        yield _event("llm", response.llm)
    yield _event("done", {"turn": response.model_dump()})


@app.post(
    "/query/stream",
    summary="Same as POST /query, streamed as Server-Sent Events (contract §7)",
    tags=["query"],
)
async def query_stream(
    req: QueryRequest, request: Request, principal: Principal = Depends(require_principal),
) -> StreamingResponse:
    request_id = getattr(request.state, "request_id", None)
    return StreamingResponse(
        _query_event_stream(req, request_id, principal),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------

@app.get("/", summary="Service index")
def index() -> dict[str, object]:
    """What this server is, and where the things a caller wants actually are.

    There was no route here, so the first thing anyone does after
    starting the server -- open ``http://localhost:8000`` in a browser --
    returned ``{"detail":"Not Found"}``. With ``APP_DOCS_PUBLIC=false``
    (the default) ``/docs`` is behind auth too, so a correctly running,
    correctly configured server looked completely dead to the one check a
    person actually performs. That cost a real deployment an
    investigation into a server that was working perfectly.

    The most important line here is ``ui``. This is an API server; the
    browser interface is a *separate* static server on a *different*
    port, and the common wrong turn on a first run is expecting it here.

    Deliberately open, and deliberately boring
    -----------------------------------------
    No credentials, for the same reason ``/health`` needs none: someone
    checking whether the process is up should not have to authenticate to
    find out. So it carries nothing an anonymous caller should not have --
    no model name (the disclosure ``/health`` already withholds
    deliberately), no connection string, no configuration, no counts.
    Only the service's identity and a directory of paths that are already
    public in ``docs/api-contract-v2.md``.
    """
    return {
        "service": app.title,
        "version": app.version,
        "status": "ok",
        "ui": (
            "This is the API, not the web interface. The browser UI is a "
            "separate static server: serve web/ (for example "
            "`python -m http.server 8080`, run from inside web/) and open "
            "that port instead. See docs/fa/getting-started.md."
        ),
        "endpoints": {
            "health": "GET /health",
            "docs": "GET /docs",
            "query": "POST /query",
            "sessions": "POST /v2/sessions",
        },
        "auth": (
            "Every route except GET /health and this one requires "
            "'Authorization: Bearer <key>'."
        ),
    }


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    response_model_exclude_none=True,
    summary="Liveness check",
)
def health(request: Request) -> HealthResponse:
    """Always open (no credentials required) -- liveness probes need it.

    Phase 8: a probe hitting this endpoint with no credentials at all
    does not need the model name, and publishing it to an unauthenticated
    caller is a small but real disclosure -- so ``model`` is only included
    when the caller presented a valid API key (``request.state.principal``
    is set by ``AuthMiddleware``, which runs regardless of whether this
    route itself requires auth). ``response_model_exclude_none=True``
    means an unauthenticated caller's response omits the ``model`` key
    entirely rather than sending it as ``null``.
    """
    from api.health import check_health
    response = check_health()
    if get_principal_if_any(request) is None:
        response.model = None
    return response


# ---------------------------------------------------------------------------
# Cache admin endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/cache/stats",
    response_model=CacheStatsResponse,
    summary="Return current cache metrics",
    tags=["cache"],
)
def cache_stats(principal: Principal = Depends(require_principal)) -> CacheStatsResponse:
    """Return hits, misses, size, evictions, and enabled flag."""
    s = query_cache.stats()
    return CacheStatsResponse(**s)


@app.post(
    "/cache/clear",
    response_model=CacheStatsResponse,
    summary="Flush the entire query-result cache",
    tags=["cache"],
)
def cache_clear(principal: Principal = Depends(require_principal)) -> CacheStatsResponse:
    """Evict all cached responses.  Returns a snapshot of stats *before* clearing."""
    snapshot = query_cache.stats()
    query_cache.clear()
    return CacheStatsResponse(**snapshot)


@app.post(
    "/cache/invalidate",
    response_model=CacheStatsResponse,
    summary="Evict a single cache entry",
    tags=["cache"],
    responses={404: {"description": "Entry not found in cache"}},
)
def cache_invalidate(
    req: CacheInvalidateRequest, principal: Principal = Depends(require_principal),
) -> CacheStatsResponse:
    """Remove the cached response for a specific (question, mode) pair.

    Returns 404 if the entry does not exist.
    """
    from prompt_engine.static_prefix import prefix_version

    removed = query_cache.invalidate(
        req.question, req.mode, prefix_version=prefix_version(_system_prompt)
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"No cache entry for question={req.question!r} mode={req.mode!r}",
        )
    return CacheStatsResponse(**query_cache.stats())
