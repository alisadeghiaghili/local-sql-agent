"""FastAPI server for the Auction NLQ Engine.

Endpoints
---------
POST /query
    Accept a natural-language question and return SQL, result, or both
    — plus an optional plain-language interpretation.

GET  /health
    Liveness check: confirms Ollama is reachable and DB can be pinged.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

import config as cfg
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
from api.middleware import RequestIDMiddleware, ConcurrencyMiddleware
from api.models import QueryRequest, QueryResponse, HealthResponse
from api.runner import run_query

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
    description="Natural-language → SQL → Results API for Auction_DM.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Middleware (order matters: outer → inner) ---
app.add_middleware(ConcurrencyMiddleware)
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
        500: {"description": "Unexpected server error"},
        502: {"description": "LLM or database returned an unusable response"},
        503: {"description": "LLM or database is unavailable, or server is overloaded"},
        504: {"description": "LLM inference or query execution timed out"},
    },
)
def query(req: QueryRequest) -> QueryResponse:
    import time
    start = time.perf_counter()

    response = run_query(
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
