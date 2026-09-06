# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``/admin/keys``, ``/admin/roles`` — the write foundation, phase 2.

``docs/admin-panel-architecture.md`` is the full design contract; the
frozen phase 2 spec (section 4) is what this module implements. Every
route here mutates state, and every one declares exactly one role
dependency (:func:`api.auth.require_operations` or
:func:`api.auth.require_security`) — never :func:`api.auth.require_admin`,
phase 1's single read-only capability, which continues to gate only the
routes it already gated in :mod:`api.admin_routes`.

Role split (spec §2, §4)
-------------------------
============================================  =========  ================
Route                                          Action     Role
============================================  =========  ================
``POST   /admin/keys``                         issue      operations
``POST   /admin/keys/{key_sha256}/disable``    disable    operations
``POST   /admin/keys/{key_sha256}/enable``     re-enable  operations
``POST   /admin/keys/{key_sha256}/revoke``     revoke     operations
``PATCH  /admin/keys/{key_sha256}/acl``         ACL change security
``POST   /admin/roles/{principal_id}``         grant/revoke security
============================================  =========  ================

``{key_sha256}`` is the identifier this path uses for one key — the
SHA-256 digest already stored (never the raw key), unambiguous because it
is the key store's own primary key. Never a synthetic id: there is
already exactly one identifier for a key row, and inventing a second would
be one more thing that could drift out of sync with it.

The restrictive default, structurally (spec §2.1 escalation path #1)
-----------------------------------------------------------------------
:class:`IssueKeyRequest` has no ``denied_columns`` field, and is built
with ``model_config = {"extra": "forbid"}`` — a request body that tries to
smuggle one in gets a 422 from Pydantic before this route's body even
runs, not a silently-ignored field. Combined with
:func:`appdb.key_store.issue_key`'s own signature (also no such
parameter) and its restrictive default (every column
``schema.yaml`` currently describes), there is no path — request shape,
function signature, or default value — through which an operations admin
can choose a new key's ACL.

Every mutation is audited (spec §5)
-------------------------------------
Every route below calls :func:`appdb.admin_audit.record_admin_action`
after its mutation succeeds, naming the capability that authorised it
(``"operations"`` or ``"security"``) — never omitted, because one
principal may hold both roles at once (spec §2.3) and without this field
their two kinds of action would be indistinguishable in the log.

Maintenance mode stops these writes (admin panel phase 6, §1)
-------------------------------------------------------------------
Every mutating route below also declares
:func:`api.maintenance.require_not_in_maintenance` alongside its role
dependency — a key/role write is an application-database write, and
"writes to the application database stop" while maintenance mode is on is
exactly what phase 5's migration safety depends on
(``docs/admin-panel-architecture.md`` §5.4). The read routes in this
module (``GET /admin/keys``, ``GET /admin/roles/{capability}``,
``GET /admin/actions``) do NOT declare it — the panel itself must stay
reachable while maintenance is on. ``api/admin_config_routes.py`` and
``api/admin_feedback_routes.py`` are NOT similarly gated in this phase —
phases 3 and 4 own those modules, and the frozen phase 6 spec explicitly
reserves them; see this codebase's phase 6 report for that scope
decision.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import require_operations, require_operations_or_security, require_security
from api.maintenance import require_not_in_maintenance
from appdb.admin_audit import iter_admin_actions, record_admin_action
from appdb.key_store import (
    KeyNotFoundError,
    issue_key,
    list_keys,
    revoke_key,
    set_disabled,
    update_denied_columns,
)
from appdb.roles import LastAdminError, grant, holders, revoke
from security.auth import OPERATIONS_CAPABILITY, SECURITY_CAPABILITY, Principal

router = APIRouter(prefix="/admin", tags=["admin-write"])

_VALID_CAPABILITIES = (OPERATIONS_CAPABILITY, SECURITY_CAPABILITY)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class IssueKeyRequest(BaseModel):
    """``POST /admin/keys`` body. Deliberately has no ``denied_columns``
    field -- see module docstring's "restrictive default, structurally"."""

    model_config = {"extra": "forbid"}

    principal_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=255)


class UpdateAclRequest(BaseModel):
    """``PATCH /admin/keys/{key_sha256}/acl`` body -- security-gated only."""

    model_config = {"extra": "forbid"}

    denied_columns: list[str] = Field(default_factory=list)


class RoleChangeRequest(BaseModel):
    """``POST /admin/roles/{principal_id}`` body."""

    model_config = {"extra": "forbid"}

    capability: Literal["operations", "security"]
    grant: bool


# ---------------------------------------------------------------------------
# POST /admin/keys
# ---------------------------------------------------------------------------

@router.post("/keys", summary="Issue a new API key (operations)")
def admin_issue_key(
    req: IssueKeyRequest,
    principal: Principal = Depends(require_operations),
    _maintenance: None = Depends(require_not_in_maintenance),
) -> dict[str, Any]:
    """Mint a new key for ``req.principal_id``, with the restrictive
    default ACL. The raw key is returned **once**, in this response, and
    never again -- only its SHA-256 digest is stored (matching
    ``scripts/issue_api_key.py``'s existing behaviour)."""
    raw_key, entry = issue_key(req.principal_id, req.name)
    record_admin_action(
        actor_principal_id=principal.id,
        authorised_by=OPERATIONS_CAPABILITY,
        action="key.issue",
        target=entry["key_sha256"],
        detail={"principal_id": req.principal_id, "name": req.name},
    )
    return {"raw_key": raw_key, **entry}


@router.get("/keys", summary="List issued keys, never the raw key (operations)")
def admin_list_keys(principal: Principal = Depends(require_operations)) -> dict[str, Any]:
    return {"keys": list_keys()}


# ---------------------------------------------------------------------------
# POST /admin/keys/{key_sha256}/disable, /enable, /revoke
# ---------------------------------------------------------------------------

def _handle_not_found(key_sha256: str, fn, *args) -> None:
    try:
        fn(key_sha256, *args)
    except KeyNotFoundError:
        raise HTTPException(status_code=404, detail=f"No key {key_sha256!r}")


@router.post("/keys/{key_sha256}/disable", summary="Disable a key (operations)")
def admin_disable_key(
    key_sha256: str,
    principal: Principal = Depends(require_operations),
    _maintenance: None = Depends(require_not_in_maintenance),
) -> dict[str, Any]:
    _handle_not_found(key_sha256, set_disabled, True)
    record_admin_action(principal.id, OPERATIONS_CAPABILITY, "key.disable", key_sha256)
    return {"key_sha256": key_sha256, "disabled": True}


@router.post("/keys/{key_sha256}/enable", summary="Re-enable a disabled key (operations)")
def admin_enable_key(
    key_sha256: str,
    principal: Principal = Depends(require_operations),
    _maintenance: None = Depends(require_not_in_maintenance),
) -> dict[str, Any]:
    _handle_not_found(key_sha256, set_disabled, False)
    record_admin_action(principal.id, OPERATIONS_CAPABILITY, "key.enable", key_sha256)
    return {"key_sha256": key_sha256, "disabled": False}


@router.post("/keys/{key_sha256}/revoke", summary="Revoke a key permanently (operations)")
def admin_revoke_key(
    key_sha256: str,
    principal: Principal = Depends(require_operations),
    _maintenance: None = Depends(require_not_in_maintenance),
) -> dict[str, Any]:
    """Tombstones the key -- never deletes the row (spec §3.4): a restore
    of the application database to a point before this call must not
    silently un-revoke a key that leaked."""
    _handle_not_found(key_sha256, revoke_key)
    record_admin_action(principal.id, OPERATIONS_CAPABILITY, "key.revoke", key_sha256)
    return {"key_sha256": key_sha256, "revoked": True}


# ---------------------------------------------------------------------------
# PATCH /admin/keys/{key_sha256}/acl -- SECURITY only
# ---------------------------------------------------------------------------

@router.patch("/keys/{key_sha256}/acl", summary="Change a key's denied_columns (security)")
def admin_update_acl(
    key_sha256: str,
    req: UpdateAclRequest,
    principal: Principal = Depends(require_security),
    _maintenance: None = Depends(require_not_in_maintenance),
) -> dict[str, Any]:
    _handle_not_found(key_sha256, update_denied_columns, req.denied_columns)
    record_admin_action(
        principal.id, SECURITY_CAPABILITY, "key.acl.update", key_sha256,
        detail={"denied_columns": req.denied_columns},
    )
    return {"key_sha256": key_sha256, "denied_columns": req.denied_columns}


# ---------------------------------------------------------------------------
# POST /admin/roles/{principal_id} -- SECURITY only
# ---------------------------------------------------------------------------

@router.post("/roles/{principal_id}", summary="Grant or revoke a role (security)")
def admin_change_role(
    principal_id: str,
    req: RoleChangeRequest,
    principal: Principal = Depends(require_security),
    _maintenance: None = Depends(require_not_in_maintenance),
) -> dict[str, Any]:
    """Grant or revoke ``req.capability`` for *principal_id*.

    Refuses (409) to revoke the last remaining holder of a capability
    (spec §2.2) -- counting both environment-bootstrapped and
    database-granted holders (:func:`appdb.roles.holders`), since an
    environment-bootstrapped holder can never be demoted through this
    route at all.
    """
    if req.grant:
        grant(principal_id, req.capability, granted_by=principal.id)
        action = "role.grant"
    else:
        try:
            revoke(principal_id, req.capability)
        except LastAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        action = "role.revoke"

    record_admin_action(
        principal.id, SECURITY_CAPABILITY, action, principal_id,
        detail={"capability": req.capability},
    )
    return {
        "principal_id": principal_id,
        "capability": req.capability,
        "granted": req.grant,
    }


@router.get(
    "/roles/{capability}",
    summary="Every principal id currently holding a capability (either role)",
)
def admin_list_role_holders(
    capability: Literal["operations", "security"],
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    """Read-only -- gated on :func:`api.auth.require_operations_or_security`
    since reading who holds a role is not itself a data-visibility change
    the way *granting* one is, and mutual visibility (spec §2.4/§5) means
    both admin kinds legitimately want to see it."""
    return {"capability": capability, "principal_ids": sorted(holders(capability))}


# ---------------------------------------------------------------------------
# GET /admin/actions -- mutual visibility (spec §5, architecture §2.4)
# ---------------------------------------------------------------------------

@router.get(
    "/actions",
    summary="The admin-action audit trail -- read-only, either role",
)
def admin_list_actions(
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    """Every recorded admin action, oldest first -- readable by either
    admin role (spec §2.4/§5's mutual visibility) and mutable by neither:
    there is no edit/delete route over this log, in this phase or any
    other (see ``appdb.admin_audit``'s module docstring)."""
    return {"actions": iter_admin_actions()}
