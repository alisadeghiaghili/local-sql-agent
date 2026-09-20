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
    """``Turn.guard`` — the security-layer outcome for this turn's SQL.

    ``rule`` is the guard's free-text message (e.g. ``"Forbidden keyword
    detected: denied column 'NationalID'"``) and stays exactly as-is —
    ``tests/test_sql_guard*.py`` and the audit trail both depend on its
    literal wording, so it is never reworded or dropped. ``reason`` and
    ``subject`` are additive structure alongside it, not a replacement:
    the web UI (``web/js/render/turn.js``, per
    ``docs/design/DESIGN-INVARIANTS.md`` §8) needs to offer a *targeted*
    next action for a denied-column refusal ("Ask without that column"),
    and finding the column name by matching patterns in ``rule`` would
    make the client re-derive something the guard already knew for
    certain the moment it decided to reject — exactly the kind of
    string-parsing ``security.sql_guard``'s own module docstring already
    rejects as fragile for itself. ``security.sql_guard.SqlGuardRejection``
    carries the same two fields for the identical reason, one layer down;
    ``session.engine.TurnEngine`` copies them onto this model unchanged
    when it catches that exception, rather than re-deriving them here.
    """

    verdict: Literal["allowed", "rejected"] = "allowed"
    rule: str | None = None
    reason: (
        Literal[
            "denied_column", "forbidden_statement", "unknown_table",
            "system_catalogue", "no_table_reference", "other",
        ]
        | None
    ) = None
    """Machine-readable refusal category, or ``None`` for an allowed verdict
    (or a rejection this contract predates — e.g. one rehydrated from
    ``session.persistence`` storage written before this field existed).
    Mirrors :data:`security.sql_guard._REASONS` exactly; kept as a literal
    copy here rather than an import because ``session`` sits above
    ``security`` in this project's dependency graph and must not reach
    back down into it for a type. The web UI switches on this to pick a
    §8-specified action instead of falling back to a generic one — see
    that document's failure-anatomy table for exactly which reason maps to
    which action."""
    subject: str | None = None
    """The one column/table/keyword *reason* is about, when the rejection
    names exactly one (``None`` when it is about the query's shape rather
    than a single identifier — e.g. a stacked-statement query with no one
    dangerous statement, or a ``*`` that could expose more than one denied
    column at once). Server-supplied text originating from the analyst's
    own question and the schema — never internal infrastructure detail —
    but still untrusted input from the UI's perspective: render it with
    ``textContent``/``dataset``, exactly like any other field on this
    model, never interpolated into an HTML string."""
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
    """``Turn.error`` — populated instead of raising, per §5's "answer, then
    declare — never block" (see ``session.engine.TurnEngine.ask``'s module
    docstring).

    ``request_id`` exists so a user reading a failed turn has something to
    quote back to an operator, per ``docs/design/DESIGN-INVARIANTS.md``
    §8's copy discipline ("Keep the machine-readable code and the
    ``request_id`` visible but subordinate"). It is set by
    ``TurnEngine.ask`` to the *exact* id that call also passes to
    ``TurnEngine._write_audit`` for this same turn — never a second,
    independently-minted id — because an id that does not match what the
    server actually logged against would send an operator chasing a
    request they cannot find; a mismatching id is worse than an absent
    one, not merely useless. ``None`` only for a turn rehydrated from
    ``session.persistence`` storage written before this field existed
    (contract §9/§10's additive-field discipline — an old stored turn
    still deserializes, just without an id to show).
    """

    code: str
    message: str
    request_id: str | None = None


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
    interpret: bool = Field(
        default=False,
        description=(
            "If true, add a plain-language summary of the result rows. Sends "
            "up to 20 real result rows to the interpretation backend, so it "
            "is opt-in per request -- the same default /query has carried "
            "since phase 2. The web UI exposes it as a per-analyst toggle."
        ),
    )

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
