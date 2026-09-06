# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Maintenance mode — a switch, not a trap (admin panel phase 6).

``docs/admin-panel-architecture.md`` leans on this concept twice (§3 tier
3, §5.4 migration safety) without ever defining it. The frozen phase 6
spec (§1) defines it here, minimally and exactly:

* **New analyst queries are refused with a clear 503**, never a hang —
  see :class:`~api.errors.MaintenanceModeError` and
  :func:`require_not_in_maintenance` below.
* **Writes to the application database stop** — enforced by the same
  dependency, declared on every mutating route this phase is free to
  touch (``api/admin_write_routes.py``'s key/role lifecycle). See that
  module's own comment for why ``appdb.config_versions``/``appdb.feedback``
  are NOT gated here: the frozen spec explicitly reserves the feedback
  loop, config versioning, and the migration tool to phases 3-5, and this
  phase must not touch them.
* **The panel itself stays reachable.** Every ``GET /admin/*`` route, and
  this module's own toggle route, declare no dependency on
  :func:`require_not_in_maintenance` at all — turning maintenance on is
  never a one-way door, because nothing about turning it back off is
  itself blocked by it. The state this module keeps is pure in-process
  memory (no application-database write, no file), so the toggle route
  never has anything to refuse.

  **What that costs, stated plainly:** the flag is per *process*. A
  deployment running more than one worker has one flag per worker, and a
  separate process cannot observe it at all -- which means
  ``scripts/migrate_app_db.py`` (phase 5) CANNOT use this to establish
  quiescence, even though ``docs/admin-panel-architecture.md`` names
  migration safety as maintenance mode's second purpose. That tool keeps
  its own recent-write-activity refusal for exactly this reason. Moving
  this state into the application database would fix both, and the
  "toggle would refuse itself" objection above is answerable (the toggle
  write is the one write that stays permitted, the same way every
  ``GET /admin/*`` route stays reachable) -- it is simply not done here.
  Until it is, maintenance mode serves the analyst-facing half of its
  job and not the migration half.
* **``/health`` and ``/`` stay open** — they never gained this dependency
  in the first place.

In-flight requests drain, they are never cut
---------------------------------------------
:func:`require_not_in_maintenance` is a FastAPI dependency resolved once,
before a route handler begins. A request already past that point when
maintenance switches on keeps running, completely unaffected — nothing
in this module (or anywhere else on the request path) re-checks the flag
mid-flight, and this codebase has no mechanism to forcibly cancel a
request that has already reached the warehouse or the LLM endpoint
in the first place (see ``database/executor.py``'s query-timeout comment
for the closest thing that exists, which is a *time bound*, not a kill
switch). "Drain, don't cut" therefore falls out of *where* this check is
placed, not from any draining machinery having to exist. See
:attr:`config.Settings.maintenance_drain_deadline_seconds` for the
informational number this module hands back on toggle-on, and what an
operator should conclude if it passes.

Recorded in the admin-action trail
-------------------------------------
Both transitions (on and off) are operations actions and are recorded via
:func:`appdb.admin_audit.record_admin_action` by the route in
:mod:`api.admin_ops_routes` that calls :func:`enable`/:func:`disable` —
this module itself performs no audit-trail write, mirroring the split
:mod:`appdb.key_store` keeps from :mod:`api.admin_write_routes` (storage
here, the gate and the audit call at the route layer).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from api.errors import MaintenanceModeError

_lock = threading.Lock()


@dataclass
class _MaintenanceState:
    active: bool = False
    note: str | None = None
    since: str | None = None
    actor_principal_id: str | None = None


_state = _MaintenanceState()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_state() -> dict:
    """The current maintenance state -- read by ``GET /admin/maintenance``
    and by :func:`require_not_in_maintenance`. Always reachable regardless
    of ``active``: this is a read, and reads are never gated."""
    with _lock:
        return {
            "active": _state.active,
            "note": _state.note,
            "since": _state.since,
            "actor_principal_id": _state.actor_principal_id,
        }


def enable(*, note: str | None, actor_principal_id: str) -> dict:
    """Switch maintenance mode on. Idempotent -- re-enabling with a new
    note replaces the old one and updates ``since``, since that is a
    distinct operator action worth its own timestamp."""
    with _lock:
        _state.active = True
        _state.note = note
        _state.since = _now_iso()
        _state.actor_principal_id = actor_principal_id
        return {
            "active": _state.active,
            "note": _state.note,
            "since": _state.since,
            "actor_principal_id": _state.actor_principal_id,
        }


def disable(*, actor_principal_id: str) -> dict:
    """Switch maintenance mode off."""
    with _lock:
        _state.active = False
        _state.note = None
        _state.since = _now_iso()
        _state.actor_principal_id = actor_principal_id
        return {
            "active": _state.active,
            "note": _state.note,
            "since": _state.since,
            "actor_principal_id": _state.actor_principal_id,
        }


def reset_for_testing() -> None:
    """Restore the module-level singleton to its off/never-toggled state.
    Test-only escape hatch, mirroring ``api.v2_routes._reset_for_testing``."""
    global _state
    with _lock:
        _state = _MaintenanceState()


def require_not_in_maintenance() -> None:
    """FastAPI dependency: refuse a NEW analyst query while maintenance is
    on. Declare this alongside ``Depends(require_principal)`` on any route
    that submits a query for execution (``POST /query``,
    ``POST /query/stream``, ``POST /v2/sessions/{id}/turns`` and its SSE
    sibling) -- never on ``/health``, ``/``, or any ``/admin/*`` route.

    Raises
    ------
    api.errors.MaintenanceModeError
        (503) with a body naming the operator's note, if one was given.
    """
    state = get_state()
    if not state["active"]:
        return
    note = state.get("note")
    message = "The system is in maintenance mode."
    if note:
        message = f"{message} {note}"
    raise MaintenanceModeError(message)
