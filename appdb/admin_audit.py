# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The admin-action audit trail — a separate, append-only stream (spec §5).

Every write endpoint under ``/admin`` (:mod:`api.admin_write_routes`) calls
:func:`record_admin_action` after a mutation succeeds. Two rules, both
structural rather than merely documented:

1. **A separate stream from the analyst audit log.**
   ``observability/audit.py``'s ``audit_log.jsonl`` exists for latency
   distributions and error taxonomies over analyst query traffic —
   ``scripts/analyze_audit_log.py`` computes those aggregates assuming
   every record is a query. Mixing admin-action records into that file
   would pollute exactly the analysis it exists for. This module writes
   ``admin_action_log.jsonl`` instead, via the same
   :func:`logs.logger.append_jsonl` writer (size-rotated the same way,
   read at call time from ``cfg.settings.log_dir``).
2. **Append-only files, not a database table.** See this package's
   docstring for why: a row in the application database is editable by
   anyone with a connection to it, including a DBA, which defeats the one
   property this record exists for — being the sole supervisory mechanism
   over the security admin (``docs/admin-panel-architecture.md`` §3.1,
   §4.2). There is deliberately no ``delete``/``edit`` function in this
   module, in any phase.

Every record names **which capability authorised the action**
(``authorised_by``) — never just "this principal acted" — because one
principal may legitimately hold both roles (§2.3), and without this field
"acting as operations" and "acting as security" collapse into one
indistinguishable entry, making the separation of duties invisible to
whoever reviews the log later.

Retention: by TIME, never by size (admin panel phase 6, §9)
-------------------------------------------------------------------
``docs/admin-panel-architecture.md`` §9 used to ask "is there a retention
policy?" without one existing. Two facts settle half of that question on
their own, recorded here rather than left implicit: this log is
size-rotated the same way every other JSONL log in this project is
(``log_max_bytes``/``log_backup_count``), and :func:`record_admin_action`
is called only from write routes — a search or a read of this log is
never itself logged here, so the "a record per admin log search" worry
in the old §9 wording described a design that was never built.

The real risk runs the other way. Size-based rotation discards the
OLDEST evidence first, exactly when there is the MOST activity — which
means an admin wanting to bury one specific action could do so on
purpose, by generating enough unrelated admin noise to roll it off the
end of the file before the other role ever reads it. A trail whose whole
stated purpose is "each role can read that the other one acted" cannot
depend on a retention mechanism the party being watched can defeat by
volume.

So this log is exempt from size-based rotation entirely
(``append_jsonl(..., max_bytes=0)`` below) and retained by TIME instead:
:func:`purge_expired_admin_actions`, called once at start-up
(``api/server.py``'s ``lifespan``, mirroring the existing session-retention
purge), discards a record only once it is older than
:attr:`config.Settings.admin_action_log_retention_days` — never because
something noisier was appended after it. ``<= 0`` disables the purge
(keep everything forever), the safest default for a deployment that would
rather manage its own disk space than lose evidence automatically; an
on-prem deployment with an externally imposed retention requirement sets
this explicitly (see ``docs/deployment-runbook.md``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import config as cfg
from logs.logger import append_jsonl

logger = logging.getLogger(__name__)

# Module-level path variable so tests can patch
# "appdb.admin_audit._ADMIN_ACTION_LOG_FILE", mirroring
# observability.audit._AUDIT_LOG_FILE's own test seam.
_ADMIN_ACTION_LOG_FILE: str = ""


def _admin_action_log_file() -> str:
    """Effective admin-action log path — see module docstring."""
    if _ADMIN_ACTION_LOG_FILE:
        return _ADMIN_ACTION_LOG_FILE
    return os.path.join(cfg.settings.log_dir, "admin_action_log.jsonl")


@dataclass(slots=True)
class AdminActionRecord:
    """One admin-action audit entry.

    Parameters
    ----------
    actor_principal_id:
        The principal that performed the action.
    authorised_by:
        Which capability the actor held that made this action permitted —
        ``"operations"`` or ``"security"`` (see :mod:`security.auth`'s
        capability constants). Never omitted: this is the field that
        keeps a dual-capability principal's actions distinguishable (see
        module docstring).
    action:
        A short, stable verb-noun tag, e.g. ``"key.issue"``,
        ``"key.revoke"``, ``"key.acl.update"``, ``"role.grant"``,
        ``"role.revoke"``.
    target:
        What the action acted on — a principal id or key id.
    detail:
        Small, JSON-serialisable extra context (e.g. which capability was
        granted). Never row data, never a raw API key — the same
        "column names/counts, not values" posture
        ``observability/audit.py`` and ``api/admin_routes.py`` already
        hold to.
    """

    actor_principal_id: str
    authorised_by: str
    action: str
    target: str
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "actor_principal_id": self.actor_principal_id,
            "authorised_by": self.authorised_by,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
        }


def record_admin_action(
    actor_principal_id: str,
    authorised_by: str,
    action: str,
    target: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one :class:`AdminActionRecord` to the admin-action log.

    Never raises: an ``OSError`` writing the record is logged and
    swallowed, mirroring ``observability.audit.save_audit_record``'s own
    "writing an audit record must never fail the caller's request"
    contract. A caller that must know the write actually happened (there
    is none, in this phase) would need a different function; every
    ``/admin`` write endpoint calls this only *after* its own mutation has
    already committed, so a failure here never leaves the mutation itself
    unrecorded from the caller's point of view -- only from this log's.
    """
    record = AdminActionRecord(
        actor_principal_id=actor_principal_id,
        authorised_by=authorised_by,
        action=action,
        target=target,
        detail=detail or {},
    )
    try:
        # max_bytes=0 disables size-based rotation for THIS log only --
        # see the module docstring's "Retention: by time, never by size"
        # section for why an admin-action trail must not be discardable
        # by volume. Every other JSONL log in this project keeps its own
        # log_max_bytes/log_backup_count default unchanged.
        append_jsonl(_admin_action_log_file(), record.as_dict(), max_bytes=0)
    except OSError as exc:  # pragma: no cover - defensive, mirrors observability.audit
        logger.error("Failed to write admin-action audit record: %s", exc)


def purge_expired_admin_actions() -> int:
    """Discard admin-action records older than
    :attr:`config.Settings.admin_action_log_retention_days` -- the
    time-based retention mechanism this log relies on INSTEAD of size
    rotation (see module docstring). A no-op, returning ``0``, when the
    setting is ``<= 0`` (retain forever) or the log does not exist yet.

    Rewrites the file in place, keeping only records at or after the
    cutoff, in their original order -- an ordinary file replace, the same
    "read everything, write the survivors, swap" shape
    ``session.persistence.SessionPersistence.purge_expired`` already uses
    for the same reason (time-based retention, called once at start-up).

    Returns
    -------
    int
        How many records were discarded.
    """
    retention_days = cfg.settings.admin_action_log_retention_days
    if retention_days <= 0:
        return 0

    path = _admin_action_log_file()
    if not os.path.exists(path):
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()

    kept: list[str] = []
    discarded = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                kept.append(stripped)  # malformed line -- never silently drop unreadable evidence
                continue
            timestamp = record.get("timestamp")
            if isinstance(timestamp, str) and timestamp < cutoff:
                discarded += 1
                continue
            kept.append(stripped)

    if discarded:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            for line in kept:
                fh.write(line + "\n")
        os.replace(tmp_path, path)
    return discarded


def iter_admin_actions() -> list[dict[str, Any]]:
    """Read every record currently in the admin-action log, oldest first.

    Used by ``GET /admin/actions`` (mutual visibility, §2.4/§5: an
    operations admin can read that a security admin changed something,
    without being able to change it themselves). Returns an empty list if
    the file does not exist yet -- nothing has been recorded.
    """
    import json

    path = _admin_action_log_file()
    if not os.path.exists(path):
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records
