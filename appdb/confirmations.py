# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Analyst confirmations: "this answer is correct" (پاسخ درست است).

The positive twin of :mod:`appdb.feedback`. An analyst who has just seen a
correct answer confirms it with one click; each confirmation becomes
evidence in two places: a reviewed golden-set candidate
(``scripts/harvest_golden.py``) and the real-use quality report
(:mod:`observability.quality`). This module is the storage layer, split
from its callers the same way :mod:`appdb.feedback` is: authorisation (only
the turn's own principal may confirm it) is enforced by ``api/v2_routes.py``,
not here.

Identifiers only
-----------------
A row never carries the question, the generated SQL or any result data --
the rule :mod:`appdb.feedback` states in its "The join, not a copy"
section. Those already exist, keyed by the same ``session_id``/``turn_id``,
in the analyst audit log. :func:`confirm_turn` reads that record to decide
whether the turn may be confirmed at all, resolves ``request_id`` from it
(never from the client), and stores only the identifiers: ``session_id``,
``turn_id``, ``principal_id``, ``request_id`` and ``created_at``.

Rules
-----
* **Only a successful answer can be confirmed.** The turn's audit record
  must show SQL that ran and returned a result
  (:func:`observability.audit.turn_executed_successfully`). A rejected,
  declined or failed turn, and a turn with no audit record to check,
  raise :class:`TurnNotConfirmableError`.
* **At most one confirmation per (turn, principal).** Enforced by a unique
  constraint on the table and handled here, so two concurrent clicks store
  one row. Confirming again is idempotent: the existing row is returned and
  nothing changes (``created_at`` keeps the first click's time).
* **A principal withdraws only their own.** :func:`withdraw_confirmation`
  is keyed by the caller's principal id; another principal's confirmation
  of the same turn is never touched. The row is deleted -- nothing
  downstream needs the history of a click that was taken back.
* **Flag and confirmation exclude each other, per principal.** A
  principal who confirms a turn they had flagged, or flags one they had
  confirmed, has changed their mind: the later action wins and the
  earlier one is removed. The routes in ``api/v2_routes.py`` apply this
  with :func:`withdraw_confirmation` and
  :func:`appdb.feedback.withdraw_open_flags`; it is per principal, so two
  analysts disagreeing about one turn leave one confirmation and one flag.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from appdb.engine import get_app_engine
from appdb.models import turn_confirmations
from observability.audit import find_record_by_turn, turn_executed_successfully


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TurnNotConfirmableError(RuntimeError):
    """The turn cannot be confirmed: its audit record does not show a
    successful execution, or there is no audit record to check.

    Attributes
    ----------
    reason:
        ``"not_audited"`` (no record names the turn -- e.g. the audit log
        has since rotated past it) or ``"not_successful"`` (the record
        shows a rejection, a decline, an error, or no result).
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "confirmation_id": row["confirmation_id"],
        "session_id": row["session_id"],
        "turn_id": row["turn_id"],
        "principal_id": row["principal_id"],
        "request_id": row["request_id"],
        "created_at": row["created_at"],
    }


def get_confirmation(
    *, session_id: str, turn_id: str, principal_id: str,
) -> dict[str, Any] | None:
    """The *principal_id*'s confirmation of the turn, or ``None``.

    Parameters
    ----------
    session_id, turn_id:
        The turn.
    principal_id:
        Whose confirmation to look up.

    Returns
    -------
    dict | None
        The row's public representation.
    """
    engine = get_app_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(turn_confirmations).where(
                turn_confirmations.c.session_id == session_id,
                turn_confirmations.c.turn_id == turn_id,
                turn_confirmations.c.principal_id == principal_id,
            )
        ).mappings().first()
    return None if row is None else _public(dict(row))


def confirm_turn(
    *, session_id: str, turn_id: str, principal_id: str,
) -> tuple[dict[str, Any], bool]:
    """Record that *principal_id* confirmed *session_id*/*turn_id* as correct.

    Idempotent: when this principal has already confirmed the turn, the
    existing row is returned and nothing is written. Ownership (only the
    turn's own principal may confirm it) is the caller's responsibility --
    see the module docstring; this function stores whatever principal it
    is given.

    Parameters
    ----------
    session_id, turn_id:
        The turn being confirmed.
    principal_id:
        The confirming principal's id.

    Returns
    -------
    tuple[dict, bool]
        ``(row, created)``: the row's public representation, and whether
        this call created it (``False`` when it already existed).

    Raises
    ------
    TurnNotConfirmableError
        The audit log has no record for the turn, or the record does not
        show a successful execution.

    Examples
    --------
    >>> import tempfile, os
    >>> import config as cfg
    >>> from datetime import datetime
    >>> import observability.audit as audit
    >>> from appdb.engine import dispose_app_engine
    >>> d = tempfile.mkdtemp()
    >>> audit._AUDIT_LOG_FILE = os.path.join(d, "audit_log.jsonl")
    >>> audit.save_audit_record(audit.AuditRecord(
    ...     timestamp=datetime(2026, 1, 1), request_id="r_1", question="q",
    ...     generated_sql="SELECT 1", guard={"verdict": "allowed"}, row_count=1,
    ...     columns=["n"], session_id="s_1", turn_id="t_1"))
    >>> with cfg.override_settings(app_db_url="sqlite:///" + os.path.join(d, "app.db")):
    ...     dispose_app_engine()
    ...     row, created = confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
    ...     again, created_again = confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
    ...     dispose_app_engine()
    >>> created, created_again, row["request_id"], again["confirmation_id"] == row["confirmation_id"]
    (True, False, 'r_1', True)
    >>> audit._AUDIT_LOG_FILE = ""
    """
    audit_record = find_record_by_turn(session_id, turn_id)
    if audit_record is None:
        raise TurnNotConfirmableError(
            f"no audit record names session_id={session_id!r} turn_id={turn_id!r} "
            "-- there is nothing to show this answer was a successful one",
            reason="not_audited",
        )
    if not turn_executed_successfully(audit_record):
        raise TurnNotConfirmableError(
            "only an answer whose query ran and returned a result can be confirmed; "
            "this turn was rejected, declined or failed",
            reason="not_successful",
        )

    existing = get_confirmation(
        session_id=session_id, turn_id=turn_id, principal_id=principal_id,
    )
    if existing is not None:
        return existing, False

    created = True
    engine = get_app_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                turn_confirmations.insert().values(
                    session_id=session_id,
                    turn_id=turn_id,
                    principal_id=principal_id,
                    request_id=audit_record.get("request_id"),
                    created_at=_now_iso(),
                )
            )
    except IntegrityError:
        # A concurrent click won the race to the unique constraint: this
        # call created nothing, which is the same outcome as a repeat.
        created = False
    row = get_confirmation(session_id=session_id, turn_id=turn_id, principal_id=principal_id)
    assert row is not None  # this insert, or the racing one, stored it
    return row, created


def withdraw_confirmation(
    *, session_id: str, turn_id: str, principal_id: str,
) -> bool:
    """Delete *principal_id*'s own confirmation of the turn, if there is one.

    Never touches another principal's confirmation of the same turn.

    Parameters
    ----------
    session_id, turn_id:
        The turn.
    principal_id:
        Whose confirmation to withdraw.

    Returns
    -------
    bool
        ``True`` when a confirmation was deleted, ``False`` when there was
        none (withdrawing twice is not an error).
    """
    engine = get_app_engine()
    with engine.begin() as conn:
        result = conn.execute(
            delete(turn_confirmations).where(
                turn_confirmations.c.session_id == session_id,
                turn_confirmations.c.turn_id == turn_id,
                turn_confirmations.c.principal_id == principal_id,
            )
        )
    return bool(result.rowcount)


def list_confirmations(
    *,
    session_id: str | None = None,
    principal_id: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Every confirmation matching the given filters, newest first.

    Parameters
    ----------
    session_id:
        Restrict to one session -- the analyst-facing read ("which of my
        turns here did I confirm") passes this together with *principal_id*.
    principal_id:
        Restrict to one principal's confirmations.
    since:
        ISO-8601 timestamp (inclusive); compared as plain strings, which
        is safe because every timestamp stored here is
        ``datetime.now(timezone.utc).isoformat()``.

    Returns
    -------
    list[dict]
        Public representations, ``created_at`` descending.
    """
    stmt = select(turn_confirmations).order_by(
        turn_confirmations.c.created_at.desc(), turn_confirmations.c.confirmation_id.desc(),
    )
    if session_id is not None:
        stmt = stmt.where(turn_confirmations.c.session_id == session_id)
    if principal_id is not None:
        stmt = stmt.where(turn_confirmations.c.principal_id == principal_id)
    if since is not None:
        stmt = stmt.where(turn_confirmations.c.created_at >= since)
    engine = get_app_engine()
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [_public(dict(row)) for row in rows]
