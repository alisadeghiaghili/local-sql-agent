# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pydantic models for the v2 conversational API — ``docs/api-contract-v2.md`` §4.

Mirrors ``api/models.py``'s conventions (Pydantic ``BaseModel``, explicit
``Field`` descriptions) but lives in its own module because the ``Turn``
shape is the v2-specific contract, not a v1 request/response.

Every sub-model below corresponds to one bracketed block in the contract's
§4 JSON example. Field names and nesting match that example exactly so the
frontend (``web/js/api.js``'s JSDoc typedefs) needs no translation layer.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Ambiguity / assumptions (§5)
# ---------------------------------------------------------------------------


class Assumption(BaseModel):
    """One declared assumption backing the turn's answer (§5).

    ``source`` is what the UI shows next to the assumption chip, and what
    a compliance reviewer reads to distinguish "the user said this"
    (``question``), "inherited from an earlier turn" (``session``), "a
    configured fallback" (``default``), or "a system rule the user cannot
    override" (``policy``, e.g. the §2 scope rule) — see ``editable``.
    """

    field: str
    value: str
    source: Literal["question", "session", "default", "policy", "memory"]
    editable: bool = True


class Clarification(BaseModel):
    """A one-click refinement offer (§5) — never a gate on the answer."""

    field: str
    prompt: str
    options: list[str] = Field(default_factory=list)


class Ambiguity(BaseModel):
    """``Turn.ambiguity`` — presentation only; never withholds a result (§5)."""

    is_ambiguous: bool = False
    assumptions: list[Assumption] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Basis (§2, §4)
# ---------------------------------------------------------------------------


class Basis(BaseModel):
    """How this turn relates to the conversation so far (§2, §4)."""

    kind: Literal["fresh", "refines"] = "fresh"
    refines_turn_id: str | None = None
    composition: Literal["cte", "none"] = "none"
    inherited: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Guard verdict (§4)
# ---------------------------------------------------------------------------


class GuardVerdict(BaseModel):
    """``Turn.guard`` — the security-layer outcome for this turn's SQL."""

    verdict: Literal["allowed", "rejected"] = "allowed"
    rule: str | None = None
    injected_top: int | None = None
    tables_touched: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Result (§4)
# ---------------------------------------------------------------------------


class ResultColumn(BaseModel):
    name: str
    type: str = "string"


class TurnResult(BaseModel):
    columns: list[ResultColumn] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    rows_omitted: bool = False
    """``True`` only for a turn rehydrated from ``session.persistence``
    (§9, §10) — row *values* are never written to disk, so a reopened
    conversation's earlier turns carry their shape (``columns``,
    ``row_count``, ``truncated``) but an empty ``rows``. Additive and
    defaulting ``False`` so every existing response is unchanged; a
    client must read this rather than inferring "0 rows" from an empty
    ``rows`` list, since ``row_count`` stays accurate either way."""


class TurnErrorInfo(BaseModel):
    code: str
    message: str


# ---------------------------------------------------------------------------
# Turn (§4)
# ---------------------------------------------------------------------------


class Turn(BaseModel):
    """The full per-question record — ``docs/api-contract-v2.md`` §4.

    Returned by ``POST /v2/sessions/{sid}/turns``, embedded in the SSE
    ``done`` event, listed by ``GET /v2/sessions/{sid}``, and returned
    (as a *new* turn, never a mutation) by
    ``PATCH /v2/sessions/{sid}/turns/{tid}/assumptions``.
    """

    turn_id: str
    session_id: str
    index: int

    question: str
    resolved_question: str | None = None

    basis: Basis = Field(default_factory=Basis)

    sql: str | None = None
    sql_display: str | None = None

    ambiguity: Ambiguity = Field(default_factory=Ambiguity)

    guard: GuardVerdict | None = None
    result: TurnResult | None = None

    interpretation: str | None = None
    tier: str | None = None
    warnings: list[str] = Field(default_factory=list)

    llm: dict[str, Any] | None = None
    timings: dict[str, int] = Field(default_factory=dict)

    error: TurnErrorInfo | None = None


# ---------------------------------------------------------------------------
# Request / response envelopes (§3)
# ---------------------------------------------------------------------------


class CreateSessionResponse(BaseModel):
    session_id: str
    created_at: str
    expires_at: str | None = None


class SessionTranscriptResponse(BaseModel):
    session_id: str
    created_at: str
    turns: list[Turn] = Field(default_factory=list)


class AskTurnRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be blank")
        return v


class AssumptionEdit(BaseModel):
    """One entry in a ``PATCH .../assumptions`` request body."""

    field: str
    value: str


class PatchAssumptionsRequest(BaseModel):
    assumptions: list[AssumptionEdit] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Conversation index (§3) — GET/PATCH /v2/sessions*
# ---------------------------------------------------------------------------


class SessionIndexEntry(BaseModel):
    """One row of ``GET /v2/sessions`` — the frozen index shape (§3)."""

    session_id: str
    title: str | None = None
    created_at: str
    last_active_at: str
    turn_count: int = 0
    expires_at: str | None = None


class SessionIndexResponse(BaseModel):
    sessions: list[SessionIndexEntry] = Field(default_factory=list)
    total: int = 0


class RenameSessionRequest(BaseModel):
    """``PATCH /v2/sessions/{sid}`` — rename only; a title never enters a
    prompt (§3), so it is validated (length, no control characters) but
    otherwise opaque presentation text."""

    title: str = Field(..., min_length=1)


class RenameSessionResponse(BaseModel):
    session_id: str
    title: str


# ---------------------------------------------------------------------------
# Cross-session memory (§5) — GET/PUT/DELETE /v2/memory*
# ---------------------------------------------------------------------------


class MemoryEntryResponse(BaseModel):
    """One row of ``GET /v2/memory``'s ``entries`` list."""

    key: str
    field: str
    value: str
    updated_at: str
    applicable: bool = True
    """The read-time ACL re-check result (§5): ``False`` means the entry is
    still stored but the caller may no longer see the column it
    constrains, so it was not applied to any turn just now."""


class RememberableKeyResponse(BaseModel):
    """One row of ``GET /v2/memory``'s ``rememberable`` list — the closed
    set of keys an analyst may pin, independent of whether they have."""

    key: str
    field: str
    options: list[str] = Field(default_factory=list)
    max_length: int = 0


class MemoryIndexResponse(BaseModel):
    entries: list[MemoryEntryResponse] = Field(default_factory=list)
    rememberable: list[RememberableKeyResponse] = Field(default_factory=list)


class SetMemoryRequest(BaseModel):
    """``PUT /v2/memory/{key}`` request body."""

    value: str = Field(..., min_length=1)
