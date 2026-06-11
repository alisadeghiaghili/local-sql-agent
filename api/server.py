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
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

import config as cfg
from api.models import QueryRequest, QueryResponse, HealthResponse
from api.runner import run_query

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path("prompts/system_prompt.md")
_system_prompt: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load heavy resources once at startup."""
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


# ---------------------------------------------------------------------------
# POST /query
# ---------------------------------------------------------------------------

@app.post(
    "/query",
    response_model=QueryResponse,
    summary="Translate a question to SQL and/or execute it",
)
def query(req: QueryRequest) -> QueryResponse:
    start = time.perf_counter()
    try:
        response = run_query(
            question=req.question,
            system_prompt=_system_prompt,
            mode=req.mode,
            interpret=req.interpret,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "OUT_OF_SCOPE":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="OUT_OF_SCOPE: question is outside the Auction domain.",
            )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
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
