# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``/admin/maintenance``, ``/admin/schema-drift``, ``/admin/vocabulary``,
``/admin/usage``, ``/admin/cache/*``, ``/admin/security/*`` — the
operational tier, admin panel phase 6.

``docs/admin-panel-architecture.md`` §3 tier 3 is the design contract; the
frozen phase 6 spec is what this module implements: the operator-facing
pieces earlier phases referred to but never built. Every route here
belongs to the ``operations`` capability per the architecture's §2 table
("Maintenance mode, cache controls, ``verify_deployment``" — operations
only) — reads that either admin role legitimately wants for mutual
visibility (§2.4) are gated on :func:`api.auth.require_operations_or_security`
instead, never on :func:`api.auth.require_admin` (phase 1's now-superseded
single capability).

What this module deliberately does NOT do
------------------------------------------
* **Apply a schema change.** ``GET /admin/schema-drift`` only ever reads —
  see :func:`schema_data.drift.check_schema_drift`'s own docstring for why
  there is no corresponding write route, in this module or anywhere else.
* **Invent a second source for anything the audit log already answers.**
  ``GET /admin/usage`` is a thin HTTP surface over
  :func:`scripts.analyze_audit_log.per_principal_usage`; no aggregation
  happens in this module.
* **Touch the feedback loop, config versioning, or the migration tool** —
  phases 3-5 own those (:mod:`api.admin_feedback_routes`,
  :mod:`api.admin_config_routes`, :mod:`appdb.migrate`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import api.maintenance as maintenance
import config as cfg
from api.auth import require_operations, require_operations_or_security
from api.models import CacheInvalidateRequest
from appdb.admin_audit import record_admin_action
from retrieval.dimension_vocabulary import (
    PREFETCH_COLUMNS,
    get_vocabulary_status,
    manual_refresh,
)
from schema_data.drift import check_schema_drift
from security.auth import OPERATIONS_CAPABILITY, Principal

router = APIRouter(prefix="/admin", tags=["admin-ops"])

#: The loaded system prompt, handed across by ``api/server.py``'s
#: ``lifespan`` exactly the way it hands ``api.v2_routes`` its own copy --
#: this module cannot import ``api.server`` at load time (``api/server.py``
#: imports THIS module to mount its router, which would make that a
#: cycle), so the value is assigned onto this module-level name from
#: outside instead. Needed only for ``prompt_engine.static_prefix.prefix_version``,
#: which every cache key (and therefore ``POST /admin/cache/invalidate``)
#: is namespaced by.
_system_prompt: str = ""


# ---------------------------------------------------------------------------
# 1. Maintenance mode (spec §1)
# ---------------------------------------------------------------------------

class MaintenanceToggleRequest(BaseModel):
    """``POST /admin/maintenance`` body."""

    model_config = {"extra": "forbid"}

    active: bool
    note: str | None = Field(default=None, max_length=2000)


@router.get("/maintenance", summary="Current maintenance-mode state")
def admin_maintenance_state(
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    """Always reachable regardless of ``active`` — the panel itself must
    never become part of what maintenance mode refuses (spec §1)."""
    return maintenance.get_state()


@router.post("/maintenance", summary="Switch maintenance mode on or off (operations)")
def admin_maintenance_toggle(
    req: MaintenanceToggleRequest, principal: Principal = Depends(require_operations),
) -> dict[str, Any]:
    """Both transitions are recorded in the admin-action trail (spec §1).
    Turning maintenance ON does not gate this route itself — the toggle
    is never a one-way door, see :mod:`api.maintenance`'s module
    docstring."""
    if req.active:
        state = maintenance.enable(note=req.note, actor_principal_id=principal.id)
        record_admin_action(
            principal.id, OPERATIONS_CAPABILITY, "maintenance.enable", "system",
            detail={"note": req.note},
        )
        return {
            **state,
            "drain_deadline_seconds": cfg.settings.maintenance_drain_deadline_seconds,
            "drain_note": (
                "In-flight requests are never force-cancelled -- they drain to "
                "completion. If one is still running after the drain deadline, "
                "treat it as a hang worth investigating, not as evidence the "
                "switch failed."
            ),
        }
    state = maintenance.disable(actor_principal_id=principal.id)
    record_admin_action(principal.id, OPERATIONS_CAPABILITY, "maintenance.disable", "system")
    return state


# ---------------------------------------------------------------------------
# 2. Schema drift (spec §2) -- read-only, proposes nothing, applies nothing
# ---------------------------------------------------------------------------

@router.get(
    "/schema-drift",
    summary="Read-only comparison of schema.yaml against the live warehouse",
)
def admin_schema_drift(
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    """Never writes ``schema.yaml`` and never applies anything -- see
    :func:`schema_data.drift.check_schema_drift`'s own docstring. A
    warehouse this deployment's read-only login cannot currently reach is
    reported as a clear 503, not a stack trace."""
    try:
        report = check_schema_drift()
    except Exception as exc:  # noqa: BLE001 - the warehouse being unreachable is an operational fact, not a bug
        raise HTTPException(
            status_code=503,
            detail=f"Could not read the live warehouse for a schema drift check: {exc}",
        )
    return report.as_dict()


# ---------------------------------------------------------------------------
# 3. Vocabulary freshness and manual refresh (spec §3)
# ---------------------------------------------------------------------------

@router.get(
    "/vocabulary",
    summary="Per prefetched dimension column: freshness and failure state",
)
def admin_vocabulary_status(
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    """Read straight off ``retrieval.dimension_vocabulary``'s own
    bookkeeping -- see :func:`retrieval.dimension_vocabulary.get_vocabulary_status`."""
    return {"columns": get_vocabulary_status()}


@router.post(
    "/vocabulary/{table}/{column}/refresh",
    summary="Manually refresh one prefetched dimension column (operations)",
)
def admin_vocabulary_refresh(
    table: str, column: str, principal: Principal = Depends(require_operations),
) -> dict[str, Any]:
    """An operations action that touches no data access -- it re-reads
    what the engine already reads (spec §3). Reports a failing refresh as
    a failure (``ok: false``), never as a silent success."""
    if column not in PREFETCH_COLUMNS.get(table, ()):
        raise HTTPException(
            status_code=404,
            detail=f"{table!r}.{column!r} is not a prefetched dimension column",
        )
    result = manual_refresh(table, column)
    record_admin_action(
        principal.id, OPERATIONS_CAPABILITY, "vocabulary.refresh", f"{table}.{column}",
        detail={"ok": result["ok"]},
    )
    return result


# ---------------------------------------------------------------------------
# 4. Per-analyst usage and rate-limit pressure (spec §4)
# ---------------------------------------------------------------------------

@router.get(
    "/usage",
    summary="Per-principal queries/failures/latency/rate-limit hits over a window",
)
def admin_usage(
    since: str | None = Query(
        None, description="ISO-8601 timestamp (inclusive lower bound). Omit for unbounded.",
    ),
    until: str | None = Query(
        None, description="ISO-8601 timestamp (inclusive upper bound). Omit for unbounded.",
    ),
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    """A thin surface over :func:`scripts.analyze_audit_log.per_principal_usage`
    -- the SAME audit log ``GET /admin/summary`` already reads, so these
    per-principal figures can never diverge from that aggregate report for
    the same window (spec §4: "not from a new counter")."""
    from scripts.analyze_audit_log import (
        iter_records,
        per_principal_usage,
        resolve_log_paths,
        resolve_rate_limit_hit_paths,
    )

    audit_paths = resolve_log_paths([f"{cfg.settings.log_dir}/audit_log.jsonl*"])
    audit_records = list(iter_records(audit_paths))
    hit_paths = resolve_rate_limit_hit_paths()
    hit_records = list(iter_records(hit_paths))

    return per_principal_usage(audit_records, hit_records, since=since, until=until)


# ---------------------------------------------------------------------------
# 5. Cache controls (spec §5)
# ---------------------------------------------------------------------------

@router.post(
    "/cache/clear",
    summary="Flush the entire query-result cache (operations, recorded)",
)
def admin_cache_clear(principal: Principal = Depends(require_operations)) -> dict[str, Any]:
    """Safe (the cache is derived state) but not free: the next requests
    after this call pay full cost. ``GET /admin/cache`` (phase 1) is what
    the panel shows *before* this button is offered, so an operator sees
    exactly what will be discarded (size, hit rate) before clicking
    clear -- this route's own response still echoes that same pre-clear
    snapshot, but the panel must not wait until after the click to show
    it (spec §5)."""
    from api.query_cache import query_cache

    snapshot = query_cache.stats()
    query_cache.clear()
    record_admin_action(
        principal.id, OPERATIONS_CAPABILITY, "cache.clear", "query_cache",
        detail={"stats_before_clear": snapshot},
    )
    return {"cleared": True, "stats_before_clear": snapshot}


@router.post(
    "/cache/invalidate",
    summary="Evict a single cache entry (operations, recorded)",
)
def admin_cache_invalidate(
    req: CacheInvalidateRequest, principal: Principal = Depends(require_operations),
) -> dict[str, Any]:
    from prompt_engine.static_prefix import prefix_version
    from api.query_cache import query_cache

    removed = query_cache.invalidate(
        req.question, req.mode, prefix_version=prefix_version(_system_prompt)
    )
    record_admin_action(
        principal.id, OPERATIONS_CAPABILITY, "cache.invalidate", req.question,
        detail={"mode": req.mode, "removed": removed},
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"No cache entry for question={req.question!r} mode={req.mode!r}",
        )
    return {"removed": True, "stats": query_cache.stats()}


# ---------------------------------------------------------------------------
# 6. Failed-authentication visibility (§9's resolved "is IP alone enough?")
# ---------------------------------------------------------------------------

@router.get(
    "/security/auth-failures",
    summary="Count and source-address breakdown of failed authentication attempts",
)
def admin_auth_failures(
    window_seconds: float | None = Query(
        None, description="Only count failures in the last N seconds. Omit for all time.",
    ),
    principal: Principal = Depends(require_operations_or_security),
) -> dict[str, Any]:
    """The real control for a leaked (vs. guessed) admin key: visibility
    into a sudden run of failures, not a tighter rate limit -- see
    ``docs/admin-panel-architecture.md`` §9 and
    :mod:`security.auth_failures`'s own module docstring."""
    from security.auth_failures import iter_auth_failures, summarize_auth_failures

    records = iter_auth_failures()
    return summarize_auth_failures(records, window_seconds=window_seconds)
