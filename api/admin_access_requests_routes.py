# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``/admin/access-requests/*`` -- the "Request access" triage queue, ADR-004
part 1.

``docs/design/DESIGN-INVARIANTS.md`` §8's failure-anatomy table names
"Request access" as the next action for a denied-column guard rejection;
this module is the admin-facing half of it. Mirrors
``api/admin_feedback_routes.py``'s split exactly: all the actual storage
(submitting, listing, approving, denying) lives in
:mod:`appdb.access_requests`; this module is the thin HTTP surface over it
plus the one HTTP-layer concern -- joining a request to its audit record's
question for display.

Queue visibility: SECURITY only (owner decision)
--------------------------------------------------
Unlike the wrong-answer feedback triage queue (either admin role may
triage that one -- ``api/admin_feedback_routes.py``'s own docstring), both
listing and acting on an access request are gated on
:func:`api.auth.require_security` alone. An access request is, by
definition, about widening what a key can see -- exactly the "changes who
can see what data" line ``docs/admin-panel-architecture.md`` §2 already
draws between the two roles for every other route in this codebase
(``PATCH /admin/keys/{id}/acl``, ``POST /admin/roles/{id}``). An
``operations`` key gets the same 403
(:class:`~api.errors.SecurityRequiredError`) any other security-only admin
route already gives it.

No new ACL-writing code (owner decision)
-------------------------------------------
:func:`admin_approve_access_request` calls
:func:`appdb.access_requests.approve_request`, which in turn calls
:func:`appdb.key_store._write_denied_columns` directly, on the same
connection as the request's own status update -- the identical writer
:func:`appdb.key_store.update_denied_columns` (and so
``PATCH /admin/keys/{id}/acl``) calls beneath its own transaction. There
is no second code path here that builds the ``denied_columns_json`` value;
both routes fall through to the one writer, just on different connections.

Maintenance mode stops these writes (mirrors admin_write_routes.py)
-----------------------------------------------------------------------
Approve and deny both declare :func:`api.maintenance.require_not_in_maintenance`
alongside :func:`api.auth.require_security` -- both are application-database
writes (the request row, and for approval, the key store's ACL), the same
reasoning ``api/admin_write_routes.py``'s own module docstring gives for
its key/role lifecycle routes. The read route (``GET``) does NOT declare
it, so the queue itself stays visible while maintenance is on.

Every mutation is audited (mirrors admin_write_routes.py / admin_feedback_routes.py)
-----------------------------------------------------------------------------------------
Both routes below call :func:`appdb.admin_audit.record_admin_action` after
their mutation succeeds, naming ``"security"`` as the capability that
authorised it -- never omitted, matching every other admin write in this
codebase.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import require_security
from api.maintenance import require_not_in_maintenance
from appdb.access_requests import (
    AlreadyResolvedError,
    RequestNotFoundError,
    approve_request,
    deny_request,
    get_request,
    list_requests,
)
from appdb.admin_audit import record_admin_action
from observability.audit import find_record_by_turn
from security.auth import SECURITY_CAPABILITY, Principal

router = APIRouter(prefix="/admin/access-requests", tags=["admin-access-requests"])


# ---------------------------------------------------------------------------
# The join (mirrors admin_feedback_routes.py's _with_audit_join)
# ---------------------------------------------------------------------------

def _with_audit_join(row: dict[str, Any]) -> dict[str, Any]:
    """*row* (an :func:`appdb.access_requests.get_request` result) plus the
    question its audit record carries, for the admin panel's list -- "the
    question (joined from the audit log)". ``audit`` is ``None`` when no
    record can still be joined (the log has rotated past it); the request
    itself is never hidden for that."""
    audit_record = find_record_by_turn(row["session_id"], row["turn_id"])
    audit_view = None
    if audit_record is not None:
        audit_view = {"question": audit_record.get("question")}
    return {**row, "audit": audit_view}


# ---------------------------------------------------------------------------
# GET /admin/access-requests, /admin/access-requests/{id} -- security only
# ---------------------------------------------------------------------------

@router.get("", summary="The access-request queue, newest first (security only)")
def admin_list_access_requests(
    status: Literal["open", "approved", "denied"] | None = None,
    principal: Principal = Depends(require_security),
) -> dict[str, Any]:
    rows = list_requests(status=status)
    return {"access_requests": [_with_audit_join(row) for row in rows]}


@router.get("/{request_id}", summary="One access request, joined to its audit record (security only)")
def admin_get_access_request(
    request_id: int, principal: Principal = Depends(require_security),
) -> dict[str, Any]:
    try:
        row = get_request(request_id)
    except RequestNotFoundError:
        raise HTTPException(status_code=404, detail=f"no access request {request_id}")
    return _with_audit_join(row)


# ---------------------------------------------------------------------------
# POST /admin/access-requests/{id}/approve -- security only, through the
# existing ACL path (owner decision)
# ---------------------------------------------------------------------------

@router.post(
    "/{request_id}/approve",
    summary="Approve -- widens denied_columns on every live key the requester holds (security only)",
)
def admin_approve_access_request(
    request_id: int,
    principal: Principal = Depends(require_security),
    _maintenance: None = Depends(require_not_in_maintenance),
) -> dict[str, Any]:
    try:
        row, updated_key_count = approve_request(request_id, actor_principal_id=principal.id)
    except RequestNotFoundError:
        raise HTTPException(status_code=404, detail=f"no access request {request_id}")
    except AlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    record_admin_action(
        principal.id,
        SECURITY_CAPABILITY,
        "access_request.approve",
        str(request_id),
        detail={
            "column": row["column_name"],
            "requester_principal_id": row["requester_principal_id"],
            "keys_updated": updated_key_count,
        },
    )
    return row


# ---------------------------------------------------------------------------
# POST /admin/access-requests/{id}/deny -- security only, reason required
# ---------------------------------------------------------------------------

class DenyAccessRequestRequest(BaseModel):
    """``POST /admin/access-requests/{id}/deny`` body. ``reason`` is
    required and non-blank -- appdb.access_requests.deny_request enforces
    this too; the field constraint here just gives a 422 before that call
    ever runs."""

    model_config = {"extra": "forbid"}

    reason: str = Field(..., min_length=1, max_length=2000)


@router.post("/{request_id}/deny", summary="Deny, with a reason the requester can see (security only)")
def admin_deny_access_request(
    request_id: int,
    req: DenyAccessRequestRequest,
    principal: Principal = Depends(require_security),
    _maintenance: None = Depends(require_not_in_maintenance),
) -> dict[str, Any]:
    try:
        row = deny_request(request_id, actor_principal_id=principal.id, reason=req.reason)
    except RequestNotFoundError:
        raise HTTPException(status_code=404, detail=f"no access request {request_id}")
    except AlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    record_admin_action(
        principal.id,
        SECURITY_CAPABILITY,
        "access_request.deny",
        str(request_id),
        detail={"reason": req.reason},
    )
    return row
