# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import config as cfg
from api.auth import require_principal
from knowledge.memory_policy import get_memory_keys
from security.auth import Principal
from session.engine import TurnEngine
from session.memory import (
    MemoryEntry,
    MemoryValidationError,
    has_disallowed_chars,
    validate_memory_value,
)
from session.models import (
    AskTurnRequest,
    CreateSessionResponse,
    MemoryEntryResponse,
    MemoryIndexResponse,
    PatchAssumptionsRequest,
    RememberableKeyResponse,
    RenameSessionRequest,
    RenameSessionResponse,
    SessionIndexEntry,
    SessionIndexResponse,
    SessionTranscriptResponse,
    SetMemoryRequest,
    Turn,
)
from session.persistence import SessionPersistence
from session.store import SessionNotFoundError, SessionRecord, SessionStore

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

#: Cross-session memory's own backend, used ONLY when session persistence
#: itself is disabled (``session_store_path=""``) -- see
#: ``_get_memory_persistence`` for why memory still needs somewhere to
#: live even then.
_memory_persistence_lock = threading.Lock()
_memory_only_persistence: SessionPersistence | None = None


def get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        with _store_lock:
            if _session_store is None:
                persistence = None
                if cfg.settings.session_store_path:
                    persistence = SessionPersistence(cfg.settings.session_store_path)
                _session_store = SessionStore(
                    ttl_seconds=cfg.settings.session_ttl_seconds,
                    max_size=cfg.settings.session_max_count,
                    max_turns=cfg.settings.session_max_turns,
                    persistence=persistence,
                    retention_days=cfg.settings.session_retention_days,
                )
    return _session_store


def get_turn_engine() -> TurnEngine:
    global _turn_engine
    if _turn_engine is None:
        with _engine_lock:
            if _turn_engine is None:
                _turn_engine = TurnEngine()
    return _turn_engine


def _get_memory_persistence() -> SessionPersistence:
    """The backend ``GET``/``PUT``/``DELETE /v2/memory*`` read and write.

    Reuses the session store's own attached backend when session
    persistence is enabled (the common case — memory entries then live in
    the very same SQLite file as everything else, and survive a restart).

    When an operator has disabled session persistence entirely
    (``session_store_path=""``), memory entries still need *somewhere*
    durable enough to survive across requests within this one running
    process — a private, process-lifetime, in-memory SQLite database (the
    same :class:`~session.persistence.SessionPersistence`, pointed at
    ``":memory:"``) rather than inventing a second storage mechanism.
    Nothing in that configuration claims memory survives a restart —
    ``session_store_path=""`` disables persistence for the whole feature
    set, memory included; this only keeps memory *usable* within one
    process's lifetime instead of failing outright.
    """
    store_persistence = get_session_store().persistence
    if store_persistence is not None:
        return store_persistence
    global _memory_only_persistence
    if _memory_only_persistence is None:
        with _memory_persistence_lock:
            if _memory_only_persistence is None:
                _memory_only_persistence = SessionPersistence(":memory:")
    return _memory_only_persistence


def _load_memory_entries_for(principal: Principal) -> dict[str, MemoryEntry]:
    """The caller's stored memory (§5), or ``{}`` when the master switch
    (``cfg.settings.memory_enabled``) is off."""
    if not cfg.settings.memory_enabled:
        return {}
    raw = _get_memory_persistence().get_memory_entries(principal.id)
    return {
        key: MemoryEntry(key=key, field=row["field"], value=row["value"], updated_at=row["updated_at"])
        for key, row in raw.items()
    }


def _reset_for_testing() -> None:
    """Clear both singletons. **Test-only helper.**"""
    global _session_store, _turn_engine, _memory_only_persistence
    with _store_lock:
        if _session_store is not None and _session_store.persistence is not None:
            _session_store.persistence.close()
        _session_store = None
    with _engine_lock:
        _turn_engine = None
    with _memory_persistence_lock:
        if _memory_only_persistence is not None:
            _memory_only_persistence.close()
        _memory_only_persistence = None


def _require_owned_session(record: SessionRecord, principal: Principal) -> None:
    """Raise 404 (never 403) if *record* is owned by someone else (Phase 8).

    ``record.owner_id is None`` covers a session created with no
    principal at all (``AUTH_REQUIRED=false``) — treated as unrestricted
    rather than "owned by nobody", so it stays reachable. A 403 would
    itself confirm the session's existence to a caller who has no
    business knowing that; 404 is indistinguishable from "never
    existed"/"expired", exactly what a cross-principal probe should see.
    """
    if record.owner_id is not None and record.owner_id != principal.id:
        raise HTTPException(
            status_code=404, detail=f"Unknown or expired session: {record.session_id!r}",
        )


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
def create_session(principal: Principal = Depends(require_principal)) -> CreateSessionResponse:
    record = get_session_store().create(owner_id=principal.id)
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
# GET /v2/sessions — the conversation index (§3)
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=SessionIndexResponse)
def list_sessions(principal: Principal = Depends(require_principal)) -> SessionIndexResponse:
    rows = get_session_store().list_sessions(principal.id)
    entries = [SessionIndexEntry(**row) for row in rows]
    return SessionIndexResponse(sessions=entries, total=len(entries))


# ---------------------------------------------------------------------------
# PATCH /v2/sessions/{sid} — rename (§3)
# ---------------------------------------------------------------------------


@router.patch("/sessions/{session_id}", response_model=RenameSessionResponse)
def rename_session(
    session_id: str, req: RenameSessionRequest, principal: Principal = Depends(require_principal),
) -> RenameSessionResponse:
    try:
        record = get_session_store().require(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _require_owned_session(record, principal)

    title = req.title
    if has_disallowed_chars(title):
        raise HTTPException(
            status_code=422, detail="title must not contain a newline or control character",
        )
    max_length = cfg.settings.session_title_max_length
    if len(title) > max_length:
        raise HTTPException(status_code=422, detail=f"title exceeds max_length={max_length}")

    get_session_store().rename(record, title)
    return RenameSessionResponse(session_id=record.session_id, title=record.title)


# ---------------------------------------------------------------------------
# GET /v2/sessions/{sid}
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}", response_model=SessionTranscriptResponse)
def get_session(
    session_id: str, principal: Principal = Depends(require_principal),
) -> SessionTranscriptResponse:
    try:
        record = get_session_store().require(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _require_owned_session(record, principal)
    return SessionTranscriptResponse(
        session_id=record.session_id,
        created_at=record.created_at.isoformat(),
        turns=record.turns,
    )


# ---------------------------------------------------------------------------
# DELETE /v2/sessions/{sid}
# ---------------------------------------------------------------------------


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, principal: Principal = Depends(require_principal)) -> None:
    # Idempotent -- deleting an unknown id is not an error, and stays that
    # way here: a session owned by someone else behaves exactly like an
    # unknown one (204, not deleted, not a 403 confirming it exists).
    record = get_session_store().get(session_id)
    if record is None:
        return
    _require_owned_session(record, principal)
    get_session_store().delete(session_id)


# ---------------------------------------------------------------------------
# POST /v2/sessions/{sid}/turns  (+ ?stream=1)
# ---------------------------------------------------------------------------


async def _ask_turn_bounded(session_id: str, question: str, principal: Principal) -> Turn:
    """Run the (blocking) turn engine off the event loop.

    Mirrors ``api/server.py``'s ``_run_query_bounded`` — ``TurnEngine.ask``
    makes blocking HTTP/DB calls, so it must not run directly on the async
    event loop.
    """
    try:
        record = get_session_store().require(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _require_owned_session(record, principal)
    system_prompt = _require_system_prompt()
    memory_entries = _load_memory_entries_for(principal)
    turn = await asyncio.to_thread(
        get_turn_engine().ask, record, question, system_prompt,
        denied_columns=principal.denied_columns, memory_entries=memory_entries,
    )
    get_session_store().sync_turn(record, turn)
    return turn


@router.post("/sessions/{session_id}/turns", response_model=None)
async def ask_turn(
    session_id: str,
    req: AskTurnRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    if request.query_params.get("stream") in ("1", "true"):
        return StreamingResponse(
            _turn_event_stream(session_id, req.question, principal),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _ask_turn_bounded(session_id, req.question, principal)


def _sse_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _turn_event_stream(session_id: str, question: str, principal: Principal):
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
        turn = await _ask_turn_bounded(session_id, question, principal)
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
async def patch_assumptions(
    session_id: str,
    turn_id: str,
    req: PatchAssumptionsRequest,
    principal: Principal = Depends(require_principal),
) -> Turn:
    try:
        record = get_session_store().require(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _require_owned_session(record, principal)

    target = get_session_store().find_turn(record, turn_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Unknown turn_id: {turn_id!r}")

    system_prompt = _require_system_prompt()
    overrides = {e.field: e.value for e in req.assumptions}
    memory_entries = _load_memory_entries_for(principal)
    turn = await asyncio.to_thread(
        get_turn_engine().ask,
        record,
        target.question,
        system_prompt,
        assumption_overrides=overrides,
        denied_columns=principal.denied_columns,
        memory_entries=memory_entries,
    )
    get_session_store().sync_turn(record, turn)
    return turn


# ---------------------------------------------------------------------------
# GET/PUT/DELETE /v2/memory* — cross-session memory (§5)
# ---------------------------------------------------------------------------


@router.get("/memory", response_model=MemoryIndexResponse)
def get_memory(principal: Principal = Depends(require_principal)) -> MemoryIndexResponse:
    keys = get_memory_keys()
    stored = _get_memory_persistence().get_memory_entries(principal.id)
    denied = set(principal.denied_columns)

    entries = [
        MemoryEntryResponse(
            key=key,
            field=row["field"],
            value=row["value"],
            updated_at=row["updated_at"],
            # A stored entry for a key this deployment no longer declares
            # (config edited after the entry was written) is reported as
            # not applicable -- there is no column left to re-check.
            applicable=(key in keys and keys[key].column not in denied),
        )
        for key, row in stored.items()
    ]
    rememberable = [
        RememberableKeyResponse(
            key=key, field=key_cfg.field_name,
            options=list(key_cfg.options), max_length=key_cfg.max_length,
        )
        for key, key_cfg in keys.items()
    ]
    return MemoryIndexResponse(entries=entries, rememberable=rememberable)


@router.put("/memory/{key}", response_model=MemoryEntryResponse)
def put_memory(
    key: str, req: SetMemoryRequest, principal: Principal = Depends(require_principal),
) -> MemoryEntryResponse:
    try:
        validate_memory_value(key, req.value)
    except MemoryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    persistence = _get_memory_persistence()
    existing = persistence.get_memory_entries(principal.id)
    if key not in existing and len(existing) >= cfg.settings.memory_max_entries_per_principal:
        raise HTTPException(
            status_code=422,
            detail=(
                "memory_max_entries_per_principal "
                f"({cfg.settings.memory_max_entries_per_principal}) would be exceeded by "
                "pinning a new key -- forget an existing entry first"
            ),
        )

    key_cfg = get_memory_keys()[key]  # validate_memory_value already proved this key is declared
    updated_at = datetime.now(timezone.utc).isoformat()
    persistence.set_memory_entry(principal.id, key, key_cfg.field_name, req.value, updated_at)
    return MemoryEntryResponse(
        key=key, field=key_cfg.field_name, value=req.value, updated_at=updated_at, applicable=True,
    )


@router.delete("/memory/{key}", status_code=204)
def delete_memory_key(key: str, principal: Principal = Depends(require_principal)) -> None:
    # Idempotent -- forgetting a never-set (or another principal's) key is
    # not an error.
    _get_memory_persistence().delete_memory_entry(principal.id, key)


@router.delete("/memory", status_code=204)
def delete_all_memory(principal: Principal = Depends(require_principal)) -> None:
    _get_memory_persistence().delete_all_memory_entries(principal.id)
