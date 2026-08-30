"""v2 conversational session endpoints — ``docs/api-contract-v2.md`` §3, §7.

Mounted onto ``api/server.py``'s ``app`` via ``app.include_router(router)``.
``POST /query`` (v1) is untouched and keeps working exactly as before — this
module is purely additive, per the contract's own preamble.

Unlike v1, a turn-processing failure (LLM down, guard rejection, DB error)
is **not** surfaced as an HTTP error status here: per §5 ("answer, then
declare — never block"), ``session.engine.TurnEngine.ask`` already turns
every such failure into a ``Turn`` with ``error`` populated, and this
module returns that ``Turn`` with a normal ``200`` — the failure is data in
the response body, not a transport-level error. The exceptions are request
shape problems (empty question — 422, handled by Pydantic) and session/turn
lookup failures (404), which genuinely are HTTP-level concerns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

import config as cfg
from session.engine import TurnEngine
from session.models import (
    AskTurnRequest,
    CreateSessionResponse,
    PatchAssumptionsRequest,
    SessionTranscriptResponse,
    Turn,
)
from session.store import SessionNotFoundError, SessionStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v2", tags=["v2"])

#: Set by ``api/server.py``'s lifespan handler, mirroring that module's own
#: ``_system_prompt`` — kept as a separate module attribute (rather than
#: importing ``api.server`` here) so this module has no import-time
#: dependency on ``api.server`` (which imports this module to mount it).
_system_prompt: str = ""

# ---------------------------------------------------------------------------
# Lazily-constructed singletons — mirrors api.runner's agent singleton and
# api.query_cache's module-level query_cache instance.
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_session_store: SessionStore | None = None

_engine_lock = threading.Lock()
_turn_engine: TurnEngine | None = None


def get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        with _store_lock:
            if _session_store is None:
                _session_store = SessionStore(
                    ttl_seconds=cfg.settings.session_ttl_seconds,
                    max_size=cfg.settings.session_max_count,
                    max_turns=cfg.settings.session_max_turns,
                )
    return _session_store


def get_turn_engine() -> TurnEngine:
    global _turn_engine
    if _turn_engine is None:
        with _engine_lock:
            if _turn_engine is None:
                _turn_engine = TurnEngine()
    return _turn_engine


def _reset_for_testing() -> None:
    """Clear both singletons. **Test-only helper.**"""
    global _session_store, _turn_engine
    with _store_lock:
        _session_store = None
    with _engine_lock:
        _turn_engine = None


def _require_system_prompt() -> str:
    if not _system_prompt:
        raise RuntimeError(
            "System prompt not loaded -- lifespan startup did not run "
            "(is this app being served without the ASGI lifespan protocol?)"
        )
    return _system_prompt


# ---------------------------------------------------------------------------
# POST /v2/sessions
# ---------------------------------------------------------------------------


@router.post("/sessions", response_model=CreateSessionResponse, status_code=201)
def create_session() -> CreateSessionResponse:
    record = get_session_store().create()
    expires_at = None
    if cfg.settings.session_ttl_seconds > 0:
        # Best-effort wall-clock estimate for the client; TTL bookkeeping
        # itself is tracked in monotonic time (see session.store).
        from datetime import timedelta

        expires_at = (
            record.created_at + timedelta(seconds=cfg.settings.session_ttl_seconds)
        ).isoformat()
    return CreateSessionResponse(
        session_id=record.session_id,
        created_at=record.created_at.isoformat(),
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# GET /v2/sessions/{sid}
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}", response_model=SessionTranscriptResponse)
def get_session(session_id: str) -> SessionTranscriptResponse:
    try:
        record = get_session_store().require(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SessionTranscriptResponse(
        session_id=record.session_id,
        created_at=record.created_at.isoformat(),
        turns=record.turns,
    )


# ---------------------------------------------------------------------------
# DELETE /v2/sessions/{sid}
# ---------------------------------------------------------------------------


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str) -> None:
    get_session_store().delete(session_id)  # idempotent -- deleting an unknown id is not an error


# ---------------------------------------------------------------------------
# POST /v2/sessions/{sid}/turns  (+ ?stream=1)
# ---------------------------------------------------------------------------


async def _ask_turn_bounded(session_id: str, question: str) -> Turn:
    """Run the (blocking) turn engine off the event loop.

    Mirrors ``api/server.py``'s ``_run_query_bounded`` — ``TurnEngine.ask``
    makes blocking HTTP/DB calls, so it must not run directly on the async
    event loop.
    """
    try:
        record = get_session_store().require(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    system_prompt = _require_system_prompt()
    return await asyncio.to_thread(get_turn_engine().ask, record, question, system_prompt)


@router.post("/sessions/{session_id}/turns", response_model=None)
async def ask_turn(session_id: str, req: AskTurnRequest, request: Request):
    if request.query_params.get("stream") in ("1", "true"):
        return StreamingResponse(
            _turn_event_stream(session_id, req.question),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _ask_turn_bounded(session_id, req.question)


def _sse_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _turn_event_stream(session_id: str, question: str):
    """Yield SSE events per contract §7.

    Known limitation (mirrors ``api/server.py::_query_event_stream``):
    this is not per-token streaming. ``TurnEngine.ask`` still runs
    end-to-end as one blocking call before any event beyond the initial
    ``stage`` frame is emitted; what this DOES provide is the contract's
    exact event vocabulary and ordering, plus an immediate first byte so
    a client doesn't sit on a blank connection.
    """
    yield _sse_event("stage", {"stage": "plan", "state": "start"})
    try:
        turn = await _ask_turn_bounded(session_id, question)
    except HTTPException as exc:
        yield _sse_event("error", {"code": "SESSION_NOT_FOUND", "message": str(exc.detail)})
        return
    except Exception as exc:  # noqa: BLE001 - surfaced as an SSE error event
        logger.exception("Unexpected error while streaming turn for session=%s", session_id)
        yield _sse_event("error", {"code": "INTERNAL_ERROR", "message": str(exc)})
        return

    yield _sse_event(
        "resolved",
        {"resolved_question": turn.resolved_question, "basis": turn.basis.model_dump()},
    )
    yield _sse_event("assumptions", turn.ambiguity.model_dump())
    if turn.sql is not None:
        yield _sse_event(
            "sql",
            {"sql": turn.sql, "guard": turn.guard.model_dump() if turn.guard else None},
        )
    if turn.result is not None:
        yield _sse_event(
            "rows",
            {
                "columns": [c.model_dump() for c in turn.result.columns],
                "rows": turn.result.rows,
                "row_count": turn.result.row_count,
            },
        )
    if turn.interpretation:
        yield _sse_event("interpretation_delta", {"text": turn.interpretation})
    if turn.llm is not None:
        yield _sse_event("llm", turn.llm)
    if turn.error is not None:
        yield _sse_event("error", {"code": turn.error.code, "message": turn.error.message})
    yield _sse_event("done", {"turn": turn.model_dump()})


# ---------------------------------------------------------------------------
# PATCH /v2/sessions/{sid}/turns/{tid}/assumptions
# ---------------------------------------------------------------------------


@router.patch("/sessions/{session_id}/turns/{turn_id}/assumptions", response_model=Turn)
async def patch_assumptions(session_id: str, turn_id: str, req: PatchAssumptionsRequest) -> Turn:
    try:
        record = get_session_store().require(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    target = get_session_store().find_turn(record, turn_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Unknown turn_id: {turn_id!r}")

    system_prompt = _require_system_prompt()
    overrides = {e.field: e.value for e in req.assumptions}
    return await asyncio.to_thread(
        get_turn_engine().ask,
        record,
        target.question,
        system_prompt,
        assumption_overrides=overrides,
    )
