# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""ADR-004 part 1: "Request access" from a denied-column guard rejection.

``docs/design/DESIGN-INVARIANTS.md`` §8's failure-anatomy table names
"Request access" as the next action beside "Ask without that column" for a
denied-column rejection. This module is the storage layer, mirroring the
split :mod:`appdb.feedback` already keeps from its own callers:
authorisation (only the turn's own principal may request access to it;
only ``security`` may act on the queue) is enforced by the caller
(``api/v2_routes.py`` for submission, ``api/admin_access_requests_routes.py``
for approval/denial), not here.

The join, not a copy -- and never trust the client for the column
--------------------------------------------------------------------
A request row never carries the question or the SQL -- the same "an id,
never the content it names" category ``appdb.feedback``'s own module
docstring already documents for ``turn_feedback``. More load-bearing here
than there: :func:`submit_request` also refuses to take *which column* is
being requested from the caller at all. It re-derives that column itself,
by joining ``session_id``/``turn_id`` to the audit record
(``observability.audit.find_record_by_turn``) and reading
``guard.subject`` -- and only when ``guard.reason`` is genuinely
``"denied_column"``. A client that posts a different column name is
silently overruled, not merely validated against; there is no code path
here that ever reads a column name out of a request body.

Dedup (owner decision)
-----------------------
While an OPEN request already exists for the same
(``requester_principal_id``, ``column_name``) pair, :func:`submit_request`
returns that existing row instead of creating a second one -- an analyst
re-asking the same denied question, or clicking the button twice, must
not pile up duplicate queue entries for a security admin to triage.

Approval goes through the existing ACL path (owner decision)
-------------------------------------------------------------------
:func:`approve_request` never builds the ``denied_columns_json`` update
itself -- it calls :func:`appdb.key_store._write_denied_columns`, the same
writer :func:`appdb.key_store.update_denied_columns` (and so
``PATCH /admin/keys/{id}/acl``) uses, once per live key. Access belongs to
the *person*, not to one key (the owner's own reasoning: a grant scoped to
a single key would be lost the moment that key rotates), so every key the
requesting principal holds whose ``revoked_at`` is still ``NULL`` gets the
column removed from its ``denied_columns`` -- a *disabled* key is included
(disabling is reversible, and a re-enabled key should reflect the access
decision made while it was off), a *revoked* key is never touched
(revocation is a tombstone; there is nothing left to grant access on).

Approval is one transaction. The request is claimed with a conditional
``UPDATE ... WHERE status = 'open'`` and the key writes run on the same
connection, so the claim and every key change commit together or not at
all. A failure partway leaves the request ``open`` and no key widened, and
the admin can simply retry. :func:`deny_request` claims the same way, so a
concurrent approve and deny cannot both succeed: whichever commits second
matches no open row and raises :class:`AlreadyResolvedError`. The key-store
cache is invalidated once, after the commit, so approval takes effect on
the very next request, exactly like any other ACL change.

Denial requires a reason
--------------------------
:func:`deny_request` refuses a blank *reason* -- mirroring
``appdb.feedback.resolve_feedback``'s own "not_a_defect" requirement:
"recorded with a reason, not silently dropped". The reason is stored on
the row and read back only by the requester's own scoped listing
(``GET /v2/access-requests`` -- never by the general triage queue read
alone, and never shown to any other analyst).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

# The key writer is looked up on the module at call time (not imported by
# name) so a test can replace it to inject a failure mid-transaction.
from appdb import key_store
from appdb.engine import get_app_engine
from appdb.models import access_requests, admin_api_keys
from observability.audit import find_record_by_turn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TurnNotAuditedError(LookupError):
    """No audit record names the given ``session_id``/``turn_id`` (see
    :func:`~observability.audit.find_record_by_turn`) -- there is nothing
    to join this request against, so it is refused rather than stored with
    a silently missing column."""


class NotDeniedColumnError(ValueError):
    """The turn's own audit record is not a denied-column guard rejection
    (``guard.reason != "denied_column"``, or it names no ``guard.subject``)
    -- there is no single column here to request access to."""


class RequestNotFoundError(LookupError):
    """No ``access_requests`` row matches the given ``request_id``."""


class AlreadyResolvedError(RuntimeError):
    """:func:`approve_request`/:func:`deny_request` called on a request
    whose ``status`` is no longer ``"open"`` -- resolution is one-way, the
    same reasoning ``appdb.feedback.AlreadyResolvedError`` already
    documents: a second resolution would either silently overwrite the
    first admin's decision or need its own audit trail to avoid doing so."""


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": row["request_id"],
        "session_id": row["session_id"],
        "turn_id": row["turn_id"],
        "requester_principal_id": row["requester_principal_id"],
        "column_name": row["column_name"],
        "created_at": row["created_at"],
        "status": row["status"],
        "resolution_note": row["resolution_note"],
        "resolved_by": row["resolved_by"],
        "resolved_at": row["resolved_at"],
    }


# ---------------------------------------------------------------------------
# Submit (analyst-facing; ownership is the caller's job -- see module
# docstring)
# ---------------------------------------------------------------------------


def submit_request(
    *, session_id: str, turn_id: str, requester_principal_id: str,
) -> tuple[dict[str, Any], bool]:
    """Record one access request for *session_id*/*turn_id*'s denied column.

    Parameters
    ----------
    session_id, turn_id:
        The turn being requested access from. Ownership (only the turn's
        own principal may request against it) is the caller's
        responsibility -- see module docstring.
    requester_principal_id:
        The requesting principal's id -- stamped by the caller from the
        authenticated principal, never read from a request body by this
        function (it has no such parameter).

    Returns
    -------
    (dict, bool)
        The request row's public representation, and ``True`` iff a new
        row was created (``False`` when an existing open request for the
        same principal/column was returned instead -- see module
        docstring's "Dedup").

    Raises
    ------
    TurnNotAuditedError
        No audit record joins to *session_id*/*turn_id*.
    NotDeniedColumnError
        The joined audit record is not a denied-column guard rejection.
    """
    audit_record = find_record_by_turn(session_id, turn_id)
    if audit_record is None:
        raise TurnNotAuditedError(
            f"no audit record names session_id={session_id!r} turn_id={turn_id!r} "
            "-- nothing to join this request against"
        )

    guard = audit_record.get("guard") or {}
    column_name = guard.get("subject")
    if guard.get("reason") != "denied_column" or not column_name:
        raise NotDeniedColumnError(
            f"session_id={session_id!r} turn_id={turn_id!r} was not a "
            "denied-column guard rejection -- there is no single column "
            "here to request access to"
        )

    engine = get_app_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            select(access_requests).where(
                access_requests.c.requester_principal_id == requester_principal_id,
                access_requests.c.column_name == column_name,
                access_requests.c.status == "open",
            )
        ).mappings().first()
        if existing is not None:
            return _public(dict(existing)), False

        result = conn.execute(
            access_requests.insert().values(
                session_id=session_id,
                turn_id=turn_id,
                requester_principal_id=requester_principal_id,
                column_name=column_name,
                created_at=_now_iso(),
                status="open",
                resolution_note=None,
                resolved_by=None,
                resolved_at=None,
            )
        )
        request_id = result.inserted_primary_key[0]
        row = conn.execute(
            select(access_requests).where(access_requests.c.request_id == request_id)
        ).mappings().first()
    return _public(dict(row)), True


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def get_request(request_id: int) -> dict[str, Any]:
    """One request's stored row.

    Raises
    ------
    RequestNotFoundError
    """
    engine = get_app_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(access_requests).where(access_requests.c.request_id == request_id)
        ).mappings().first()
    if row is None:
        raise RequestNotFoundError(f"no access request {request_id!r}")
    return _public(dict(row))


def list_requests(
    *, status: str | None = None, requester_principal_id: str | None = None,
) -> list[dict[str, Any]]:
    """Every request matching the given filters, newest first.

    Parameters
    ----------
    status:
        ``"open"``, ``"approved"`` or ``"denied"`` to filter, ``None``
        (default) for every request regardless of status.
    requester_principal_id:
        Restrict to one principal's own requests -- used by the
        analyst-facing ``GET /v2/access-requests`` (the caller's own
        status read, denial reason included); never by the admin triage
        queue, which deliberately shows every principal's requests.
    """
    stmt = select(access_requests).order_by(access_requests.c.request_id.desc())
    if status is not None:
        stmt = stmt.where(access_requests.c.status == status)
    if requester_principal_id is not None:
        stmt = stmt.where(access_requests.c.requester_principal_id == requester_principal_id)
    engine = get_app_engine()
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [_public(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Triage (security only -- enforced by the caller)
# ---------------------------------------------------------------------------


def approve_request(request_id: int, *, actor_principal_id: str) -> tuple[dict[str, Any], int]:
    """Approve *request_id* -- removes its column from ``denied_columns``
    on every LIVE key the requesting principal holds, in one transaction
    (see module docstring's "Approval goes through the existing ACL path").
    If anything fails, the request stays "open" and no key is changed, so
    the approval can be retried.

    Returns
    -------
    (dict, int)
        The resolved row's public representation, and how many keys were
        updated -- purely informational, for the caller's admin-action
        log entry; never persisted on the row itself.

    Raises
    ------
    RequestNotFoundError
    AlreadyResolvedError
    """
    engine = get_app_engine()
    now = _now_iso()

    with engine.begin() as conn:
        # Claim the request with a conditional update.
        result = conn.execute(
            access_requests.update()
            .where(access_requests.c.request_id == request_id, access_requests.c.status == "open")
            .values(
                status="approved",
                resolution_note=None,
                resolved_by=actor_principal_id,
                resolved_at=now,
            )
        )

        # If the update affected 0 rows, the request was either not found
        # or already resolved by another admin concurrently.
        if result.rowcount == 0:
            row = conn.execute(
                select(access_requests).where(access_requests.c.request_id == request_id)
            ).mappings().first()
            if row is None:
                raise RequestNotFoundError(f"no access request {request_id!r}")
            raise AlreadyResolvedError(
                f"access request {request_id!r} was already resolved as "
                f"{row['status']!r} by {row['resolved_by']!r} at {row['resolved_at']!r}; "
                "no key was changed"
            )

        # Read the request row to get column_name and principal_id.
        row = conn.execute(
            select(access_requests).where(access_requests.c.request_id == request_id)
        ).mappings().first()

        column_name = row["column_name"]
        principal_id = row["requester_principal_id"]

        # Read all keys for this principal and update those that deny the column.
        key_rows = conn.execute(
            select(admin_api_keys).where(admin_api_keys.c.principal_id == principal_id)
        ).mappings().all()

        updated = 0
        for key_row in key_rows:
            if key_row["revoked_at"] is not None:
                continue  # revoked keys are never touched (module docstring)
            denied = list(json.loads(key_row["denied_columns_json"]))
            if column_name not in denied:
                continue
            denied.remove(column_name)
            key_store._write_denied_columns(conn, key_row["key_sha256"], denied, now)
            updated += 1

    # After the transaction commits, invalidate the cache once.
    key_store.invalidate_cache()

    return get_request(request_id), updated


def deny_request(request_id: int, *, actor_principal_id: str, reason: str) -> dict[str, Any]:
    """Deny *request_id* with a required, non-blank *reason* (module
    docstring's "Denial requires a reason").

    The status update is conditional (WHERE status = 'open') so a concurrent
    approval cannot be silently overwritten. If another admin approved the
    request concurrently, this raises AlreadyResolvedError and changes
    nothing.

    Raises
    ------
    ValueError
        *reason* is blank.
    RequestNotFoundError
    AlreadyResolvedError
    """
    if not reason.strip():
        raise ValueError(
            "denying an access request requires a non-blank reason -- "
            "recorded and shown to the requester, never silently dropped"
        )

    engine = get_app_engine()
    now = _now_iso()

    with engine.begin() as conn:
        # Deny with a conditional update.
        result = conn.execute(
            access_requests.update()
            .where(access_requests.c.request_id == request_id, access_requests.c.status == "open")
            .values(
                status="denied",
                resolution_note=reason,
                resolved_by=actor_principal_id,
                resolved_at=now,
            )
        )

        # If the update affected 0 rows, the request was either not found
        # or already resolved by another admin concurrently.
        if result.rowcount == 0:
            row = conn.execute(
                select(access_requests).where(access_requests.c.request_id == request_id)
            ).mappings().first()
            if row is None:
                raise RequestNotFoundError(f"no access request {request_id!r}")
            raise AlreadyResolvedError(
                f"access request {request_id!r} was already resolved as "
                f"{row['status']!r} by {row['resolved_by']!r} at {row['resolved_at']!r}; "
                "no change was made"
            )

    return get_request(request_id)
