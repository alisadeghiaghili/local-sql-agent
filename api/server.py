"""FastAPI server for the Auction NLQ Engine.

Endpoints
---------
POST /query
    Accept a natural-language question and return SQL, result, or both
    — plus an optional plain-language interpretation.

GET  /health
    Liveness check: confirms Ollama is reachable and DB can be pinged.

GET  /cache/stats
    Return current cache metrics (hits, misses, size, evictions, enabled).

POST /cache/clear
    Flush the entire query-result cache and return a stats snapshot.

POST /cache/invalidate
    Evict a single (question, mode) entry from the cache.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

import config as cfg
import api.runner as runner  # import the MODULE so patch.object(runner, 'run_query') works
from api.errors import (
    NLQError,
    OutOfScopeError,
    ForbiddenSQLError,
    InvalidSQLResponseError,
    EmptySQLResponseError,
    ModelUnavailableError,
    ModelTimeoutError,
    QueryExecutionError,
    DatabaseConnectionError,
    QueryTimeoutError,
    register_handlers,
)
from api.middleware import RequestIDMiddleware, RateLimitMiddleware, ConcurrencyMiddleware
from api.models import (
    QueryRequest,
    QueryResponse,
    HealthResponse,
    CacheStatsResponse,
    CacheInvalidateRequest,
)
from api.query_cache import query_cache

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path("prompts/system_prompt.md")
_system_prompt: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _system_prompt
    if not _PROMPT_PATH.exists():
        raise RuntimeError(f"System prompt not found: {_PROMPT_PATH}")
    _system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    logger.info("System prompt loaded (%d chars)", len(_system_prompt))
    yield


app = FastAPI(
    title="Auction NLQ Engine",
    description="Natural-language \u2192 SQL \u2192 Results API for Auction_DM.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Middleware (order matters: outer → inner) ---
# 1. ConcurrencyMiddleware  — innermost: applied after rate-limit passes
# 2. RateLimitMiddleware    — per-IP token-bucket (429 on excess)
# 3. RequestIDMiddleware    — outermost: stamps X-Request-ID first so all
#                              downstream middleware can log it
#
# FastAPI/Starlette applies add_middleware() in REVERSE order, so the last
# add_middleware() call becomes the outermost layer.
app.add_middleware(ConcurrencyMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestIDMiddleware)

# --- Exception handlers ---
register_handlers(app)


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
def query(req: QueryRequest) -> QueryResponse:
    import time
    start = time.perf_counter()

    # Call via module reference so that patch.object(api.runner, 'run_query')
    # in tests intercepts this call correctly.
    response = runner.run_query(
        question=req.question,
        system_prompt=_system_prompt,
        mode=req.mode,
        interpret=req.interpret,
    )

    response.elapsed_seconds = round(time.perf_counter() - start, 3)
    return response


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, summary="Liveness check")
def health() -> HealthResponse:
    from api.health import check_health
    return check_health()


# ---------------------------------------------------------------------------
# Cache admin endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/cache/stats",
    response_model=CacheStatsResponse,
    summary="Return current cache metrics",
    tags=["cache"],
)
def cache_stats() -> CacheStatsResponse:
    """Return hits, misses, size, evictions, and enabled flag."""
    s = query_cache.stats()
    return CacheStatsResponse(**s)


@app.post(
    "/cache/clear",
    response_model=CacheStatsResponse,
    summary="Flush the entire query-result cache",
    tags=["cache"],
)
def cache_clear() -> CacheStatsResponse:
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
def cache_invalidate(req: CacheInvalidateRequest) -> CacheStatsResponse:
    """Remove the cached response for a specific (question, mode) pair.

    Returns 404 if the entry does not exist.
    """
    removed = query_cache.invalidate(req.question, req.mode)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"No cache entry for question={req.question!r} mode={req.mode!r}",
        )
    return CacheStatsResponse(**query_cache.stats())
