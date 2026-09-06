# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``/admin/config/*`` -- versioned ``project_config/`` editing, phase 3.

``docs/admin-panel-architecture.md`` §6 is the design contract; the frozen
phase 3 spec is what this module implements. All the actual logic --
bootstrap, validation, diffing, the offline dry-run, optimistic locking,
revert-never-reset restore, and the propose-and-approve split for
``schema.yaml`` -- lives in :mod:`appdb.config_versions`; this module is
the thin HTTP surface over it, the same split
:mod:`api.admin_write_routes` keeps from :mod:`appdb.key_store` /
:mod:`appdb.roles`.

Role split (spec §3)
----------------------
============================================  ======================================
Route                                          Role
============================================  ======================================
``GET   /admin/config/active``                 operations OR security (read)
``GET   /admin/config/versions``               operations OR security (read)
``GET   /admin/config/versions/{id}``          operations OR security (read)
``POST  /admin/config/versions``               operations OR security (see below)
``POST  /admin/config/restore``                operations OR security (see below)
``POST  /admin/config/versions/{id}/approve``  security only
``POST  /admin/config/versions/{id}/reject``   security only
``POST  /admin/config/export``                 operations OR security
``POST  /admin/config/import``                 operations OR security
============================================  ======================================

The two "operations OR security (see below)" routes are the one
deliberate exception to this codebase's usual "a mutating route declares
exactly one of require_operations/require_security" rule (see
``tests/test_admin.py``'s ``TestEveryMutatingAdminRouteDeclaresARoleDependency``
for where that rule was widened to admit it). Both routes must be
reachable by an operations principal -- editing the eight operations files
directly, and *proposing* a ``schema.yaml`` change as an unapplied draft,
are both legitimate operations actions (spec §3.1) -- so the route itself
cannot gate on ``require_security`` alone. The actual restriction --
operations cannot make a ``schema.yaml`` change live -- is enforced one
layer down, structurally, in
:func:`appdb.config_versions.propose_or_apply`: a ``schema.yaml`` change
from a caller lacking the security capability is saved as an unapplied
``"draft"``, never as an ``"applied"`` version, regardless of what the
request claims. §3's test proving this split is safe --
``tests/test_config_version_role_split.py`` -- exercises exactly this
path.

Every mutation is audited (spec §5), naming the capability that actually
authorised it -- for a save, that is
:func:`appdb.config_versions.propose_or_apply`'s own decision
(``created_by_capability`` on the returned version), never assumed from
which of the two capabilities the caller happened to present.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import config as cfg
from api.auth import require_operations_or_security, require_security
from appdb.admin_audit import record_admin_action
from appdb.config_versions import (
    ConfigVersionValidationError,
    NotADraftError,
    StaleVersionError,
    VersionNotFoundError,
    approve_draft,
    export_active_version,
    get_active_version,
    get_version,
    import_bundle_from_directory,
    list_versions,
    propose_or_apply,
    reject_draft,
    restore,
)
from security.auth import OPERATIONS_CAPABILITY, SECURITY_CAPABILITY, Principal

router = APIRouter(prefix="/admin/config", tags=["admin-config"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SaveConfigVersionRequest(BaseModel):
    """``POST /admin/config/versions`` body."""

    model_config = {"extra": "forbid"}

    based_on_version: int
    #: ``{filename: yaml_text}`` for only the file(s) being changed --
    #: unlisted files are carried forward unchanged from
    #: ``based_on_version``. Filenames outside
    #: :data:`appdb.config_versions.CONFIG_FILENAMES` are rejected (422) by
    #: :func:`appdb.config_versions.propose_or_apply` itself.
    files: dict[str, str] = Field(default_factory=dict)


class RestoreConfigRequest(BaseModel):
    """``POST /admin/config/restore`` body."""

    model_config = {"extra": "forbid"}

    from_version_id: int
    #: ``None`` restores the whole bundle; a specific filename restores
    #: only that one file (spec §1's "a single file may be restored from
    #: any version").
    filename: str | None = None


class RejectDraftRequest(BaseModel):
    """``POST /admin/config/versions/{id}/reject`` body."""

    model_config = {"extra": "forbid"}

    reason: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Reads -- either role (spec §2.4 mutual visibility)
# ---------------------------------------------------------------------------

@router.get("/active", summary="The current active configuration version")
def admin_config_active(
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    return get_active_version()


@router.get("/versions", summary="Every configuration version's metadata, newest first")
def admin_config_list_versions(
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    return {"versions": list_versions()}


@router.get("/versions/{version_id}", summary="One configuration version, with its content")
def admin_config_get_version(
    version_id: int, principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    try:
        return get_version(version_id)
    except VersionNotFoundError:
        raise HTTPException(status_code=404, detail=f"no config version {version_id}")


# ---------------------------------------------------------------------------
# POST /admin/config/versions -- see module docstring for the role split
# ---------------------------------------------------------------------------

@router.post("/versions", summary="Save a new configuration version (or propose a draft)")
def admin_config_save_version(
    req: SaveConfigVersionRequest,
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    if not req.files:
        raise HTTPException(status_code=422, detail="files must not be empty")
    try:
        result = propose_or_apply(
            req.files,
            based_on_version=req.based_on_version,
            actor_principal_id=principal.id,
            actor_capabilities=principal.capabilities,
        )
    except StaleVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ConfigVersionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    action = "config.version.propose" if result["status"] == "draft" else "config.version.apply"
    files_changed = result["diff"]["files_changed"] if result["diff"] else []
    record_admin_action(
        principal.id, result["created_by_capability"], action, str(result["version_id"]),
        detail={"files_changed": files_changed, "status": result["status"]},
    )
    return result


@router.post("/restore", summary="Restore a file (or the whole bundle) from an earlier version")
def admin_config_restore(
    req: RestoreConfigRequest,
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    try:
        result = restore(
            from_version_id=req.from_version_id,
            filename=req.filename,
            actor_principal_id=principal.id,
            actor_capabilities=principal.capabilities,
        )
    except VersionNotFoundError:
        raise HTTPException(status_code=404, detail=f"no config version {req.from_version_id}")
    except StaleVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ConfigVersionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    action = (
        "config.version.restore_propose" if result["status"] == "draft" else "config.version.restore"
    )
    record_admin_action(
        principal.id, result["created_by_capability"], action, str(result["version_id"]),
        detail={
            "from_version_id": req.from_version_id, "filename": req.filename,
            "status": result["status"],
        },
    )
    return result


# ---------------------------------------------------------------------------
# Approve / reject a draft -- SECURITY only (spec §3.1)
# ---------------------------------------------------------------------------

@router.post(
    "/versions/{version_id}/approve",
    summary="Approve a pending schema.yaml draft, making it the active version (security)",
)
def admin_config_approve(
    version_id: int, principal: Principal = Depends(require_security),
) -> dict[str, Any]:
    try:
        result = approve_draft(version_id, actor_principal_id=principal.id)
    except VersionNotFoundError:
        raise HTTPException(status_code=404, detail=f"no config version {version_id}")
    except NotADraftError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except StaleVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    record_admin_action(
        principal.id, SECURITY_CAPABILITY, "config.version.approve", str(version_id),
    )
    return result


@router.post(
    "/versions/{version_id}/reject",
    summary="Reject a pending draft -- it never becomes active (security)",
)
def admin_config_reject(
    version_id: int, req: RejectDraftRequest, principal: Principal = Depends(require_security),
) -> dict[str, Any]:
    try:
        result = reject_draft(version_id, actor_principal_id=principal.id, reason=req.reason)
    except VersionNotFoundError:
        raise HTTPException(status_code=404, detail=f"no config version {version_id}")
    except NotADraftError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    record_admin_action(
        principal.id, SECURITY_CAPABILITY, "config.version.reject", str(version_id),
        detail={"reason": req.reason},
    )
    return result


# ---------------------------------------------------------------------------
# Export / import (spec §7) -- always via cfg.settings.config_export_dir,
# never an arbitrary path in the request body: these round-trip a
# deployment's own configured export location (setting up a second
# deployment, or an off-box backup), not a general file-read/write primitive.
# ---------------------------------------------------------------------------

@router.post("/export", summary="Write the active version's bundle to CONFIG_EXPORT_DIR")
def admin_config_export(
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    export_dir = cfg.settings.config_export_dir
    if not export_dir:
        raise HTTPException(status_code=400, detail="CONFIG_EXPORT_DIR is not configured")
    manifest = export_active_version(export_dir)
    record_admin_action(
        principal.id, OPERATIONS_CAPABILITY, "config.export", str(manifest["version_id"]),
    )
    return manifest


@router.post("/import", summary="Import a bundle previously written to CONFIG_EXPORT_DIR")
def admin_config_import(
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    source_dir = cfg.settings.config_export_dir
    if not source_dir:
        raise HTTPException(status_code=400, detail="CONFIG_EXPORT_DIR is not configured")
    try:
        result = import_bundle_from_directory(
            source_dir, actor_principal_id=principal.id, actor_capabilities=principal.capabilities,
        )
    except StaleVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ConfigVersionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    record_admin_action(
        principal.id, result["created_by_capability"], "config.version.import",
        str(result["version_id"]),
    )
    return result
