# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``/admin/feedback/*`` -- the triage queue, admin panel phase 4.

``docs/admin-panel-architecture.md`` §3 Tier 1 is the design contract; the
frozen phase 4 spec is what this module implements. All the actual
storage -- submitting a flag, listing it, resolving it, promoting one to a
golden case -- lives in :mod:`appdb.feedback`; this module is the thin
HTTP surface over it plus the one thing that genuinely is an HTTP-layer
concern: joining a flag to its audit record for display (spec §3's
"showing each flagged turn joined to its audit record").

Role split (spec §3, architecture §2 table)
--------------------------------------------
Every route here is gated on :func:`api.auth.require_operations_or_security`
-- both admin roles may triage (the architecture's §2 table lists a
checkmark for both under "Feedback triage"), unlike most of this codebase's
write routes, which gate on exactly one role. Reading the audit log here
is not a data-visibility change (the audit log deliberately contains no
data -- ``observability/audit.py``'s structural rule), and resolving a
flag changes no ACL and no ``schema.yaml`` allowlist, so neither action
needs the security-only gate the rest of this codebase reserves for
"changes who can see what data" (architecture §2).

Nothing here auto-applies (spec §3.2)
----------------------------------------
:func:`~api.admin_feedback_routes.admin_resolve_feedback` never calls
:mod:`appdb.config_versions` at all. An ``"alias_fix"``/``"rule_fix"``
outcome's actual domain-knowledge edit goes through the existing,
unchanged ``POST /admin/config/versions`` -- this route only accepts an
*already-created* version id as provenance. This is not a policy this
route has to remember to enforce; it is a fact about which functions it
imports.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import config as cfg
from api.auth import require_operations_or_security
from appdb.admin_audit import record_admin_action
from appdb.feedback import (
    AlreadyResolvedError,
    FeedbackNotFoundError,
    TurnNotAuditedError,
    feedback_stats,
    get_feedback,
    list_feedback,
    promote_to_golden_case,
    resolve_feedback,
)
from observability.audit import find_record_by_turn
from security.auth import OPERATIONS_CAPABILITY, SECURITY_CAPABILITY, Principal

router = APIRouter(prefix="/admin/feedback", tags=["admin-feedback"])


def _authorised_by(principal: Principal) -> str:
    """Which capability authorised this action (spec §5; architecture
    §2.3) -- recorded on the admin-action log, never just "this principal
    acted", since one principal may legitimately hold both roles at once.

    Every route in this module accepts either capability
    (:func:`api.auth.require_operations_or_security` -- triage is
    explicitly a both-roles action per the architecture's §2 table), so
    unlike most of this codebase's write routes there is no single
    dependency that already answers "which one let this through". A
    principal holding only one capability is unambiguous; a principal
    holding both is recorded as ``"security"`` -- an arbitrary but stable
    tie-break (the same "which one" reviewer question this field exists to
    answer is at least always answered the same way for the same
    principal, rather than depending on which of two equally-valid
    capabilities happened to be checked first).
    """
    return SECURITY_CAPABILITY if principal.is_security else OPERATIONS_CAPABILITY


# ---------------------------------------------------------------------------
# Small helpers -- the join (spec §3)
# ---------------------------------------------------------------------------

def _with_audit_join(row: dict[str, Any]) -> dict[str, Any]:
    """*row* (an :func:`appdb.feedback.get_feedback` result) plus the
    question/SQL/guard-verdict/assumptions its audit record carries.

    ``audit`` is ``None`` when no record can still be joined (the log has
    rotated past it) -- the flag itself is never hidden for that, only its
    joined detail is missing, which the triage UI must show honestly
    rather than silently omitting the row.
    """
    audit_record = find_record_by_turn(row["session_id"], row["turn_id"])
    audit_view = None
    if audit_record is not None:
        audit_view = {
            "question": audit_record.get("question"),
            "generated_sql": audit_record.get("generated_sql"),
            "guard": audit_record.get("guard"),
            "assumptions": audit_record.get("assumptions"),
            "tier": audit_record.get("tier"),
            "error_code": audit_record.get("error_code"),
            "error_message": audit_record.get("error_message"),
        }
    return {**row, "audit": audit_view}


# ---------------------------------------------------------------------------
# GET /admin/feedback, /admin/feedback/{id}
# ---------------------------------------------------------------------------

@router.get("", summary="The triage queue, newest first (either admin role)")
def admin_list_feedback(
    status: Literal["open", "resolved"] | None = None,
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    rows = list_feedback(status=status)
    return {"feedback": [_with_audit_join(row) for row in rows]}


@router.get("/stats", summary="Flag volume, outcomes and golden-set size (either admin role)")
def admin_feedback_stats(
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    """Spec §5's "closing the loop visibly": flag/outcome counts (from the
    application database) plus the golden set's size and, when one has
    been recorded, its last known accuracy (from
    ``cfg.settings.eval_baseline_path`` -- never a live re-run of the
    harness against a real endpoint from inside a GET route)."""
    stats = feedback_stats()

    baseline_path = cfg.settings.eval_baseline_path
    baseline_info: dict[str, Any] | None = None
    if baseline_path:
        from pathlib import Path

        if Path(baseline_path).exists():
            from eval.baseline import load_baseline

            try:
                baseline = load_baseline(baseline_path)
                baseline_info = {
                    "mode": baseline.mode,
                    "accuracy_pct": baseline.accuracy_pct,
                    "total": baseline.total,
                    "generated_at": baseline.generated_at,
                }
            except (ValueError, FileNotFoundError):
                baseline_info = None

    stats["baseline"] = baseline_info
    return stats


@router.get("/{feedback_id}", summary="One flagged turn, joined to its audit record")
def admin_get_feedback(
    feedback_id: int, principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    try:
        row = get_feedback(feedback_id)
    except FeedbackNotFoundError:
        raise HTTPException(status_code=404, detail=f"no feedback {feedback_id}")
    return _with_audit_join(row)


# ---------------------------------------------------------------------------
# POST /admin/feedback/{id}/resolve -- spec §3.1
# ---------------------------------------------------------------------------

class ResolveFeedbackRequest(BaseModel):
    """``POST /admin/feedback/{id}/resolve`` body.

    ``config_version_id`` is accepted only as provenance for
    ``"alias_fix"``/``"rule_fix"`` -- an id of a version the admin already
    created through ``POST /admin/config/versions``. ``tags`` is accepted
    only for ``"golden_case"``, passed straight through to
    :func:`appdb.feedback.promote_to_golden_case`. Neither field is
    validated against the outcome here (a mismatched combination is simply
    ignored by :mod:`appdb.feedback`, which only ever *reads* the field
    relevant to the outcome it was given) -- keeping this one request body
    shape for all four outcomes is simpler than four separate endpoints
    for what is, underneath, one state transition.
    """

    model_config = {"extra": "forbid"}

    outcome: Literal["alias_fix", "rule_fix", "golden_case", "not_a_defect"]
    note: str = Field(default="", max_length=4000)
    config_version_id: int | None = None
    tags: list[str] = Field(default_factory=list)


@router.post(
    "/{feedback_id}/resolve",
    summary="Resolve a flag into exactly one outcome (either admin role)",
)
def admin_resolve_feedback(
    feedback_id: int,
    req: ResolveFeedbackRequest,
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    golden_case_id: str | None = None
    if req.outcome == "golden_case":
        try:
            promoted = promote_to_golden_case(feedback_id, tags=req.tags)
        except FeedbackNotFoundError:
            raise HTTPException(status_code=404, detail=f"no feedback {feedback_id}")
        except TurnNotAuditedError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        golden_case_id = promoted["case_id"]

    try:
        result = resolve_feedback(
            feedback_id,
            outcome=req.outcome,
            actor_principal_id=principal.id,
            note=req.note,
            config_version_id=req.config_version_id,
            golden_case_id=golden_case_id,
        )
    except FeedbackNotFoundError:
        raise HTTPException(status_code=404, detail=f"no feedback {feedback_id}")
    except AlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    record_admin_action(
        principal.id,
        _authorised_by(principal),
        "feedback.resolve",
        str(feedback_id),
        detail={"outcome": req.outcome, "golden_case_id": golden_case_id},
    )
    return result
