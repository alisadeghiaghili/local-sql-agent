# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Wrong-answer feedback and its triage -- admin panel phase 4.

``docs/admin-panel-architecture.md`` §3's tier 1 ("closes a loop nothing
else closes") and the frozen phase 4 spec are the design contract. This
module is the storage layer -- the same split ``appdb.roles``/
``appdb.key_store`` keep from the routes that call them: authorisation
(who may flag which turn, who may triage) is enforced by the caller
(``api/v2_routes.py`` for flagging, ``api/admin_feedback_routes.py`` for
triage), not here.

The join, not a copy (spec §2.2)
---------------------------------
A feedback row never carries the question, the generated SQL, or any
result data. Those already exist, keyed by the same ``session_id``/
``turn_id``, in the analyst audit log (``observability/audit.py``) --
phase 4.2.0 added those two fields to :class:`~observability.audit.AuditRecord`
precisely so this join is possible. :func:`submit_flag` resolves
``request_id`` and ``config_version_id`` from that same audit record *at
flag time* (never supplied by the client) and stores only those two
identifiers -- the same "an id, never the content it names" category
``AuditRecord.session_id`` already is. Triage reads (the admin routes, not
this module) perform the actual join for display.

Nothing here auto-applies (spec §3.2)
----------------------------------------
:func:`resolve_feedback` never creates, applies, or even touches a
``config_bundle_versions`` row -- for an ``"alias_fix"``/``"rule_fix"``
outcome, the admin makes the actual domain-knowledge edit through the
*existing*, unchanged phase 3 surface (``POST /admin/config/versions``),
which already runs it through validation, diff, dry-run and (for
``schema.yaml``) the operations/security draft split. Resolving a flag
only records *that* a fix was made and, optionally, which config version
id it was -- so "nothing auto-applies" is not a rule this module has to
be careful to uphold, it is a fact about what this module is capable of
doing at all: it has no code path that writes ``project_config/`` content
anywhere.

Promotion to a golden case (spec §4)
---------------------------------------
The golden set (``eval_data/golden.jsonl``) is a file outside the
application database and outside phase 3's versioning (architecture §3
Tier 1, "the golden set has no home in this design"). :func:`promote_to_golden_case`
resolves this the way the spec directs: the case itself is appended to
that file (the same file :mod:`eval.runner`/CI already read, so it never
diverges into a second store), written ``status="pending_expected"``
(:data:`eval.models.GoldenCaseStatus`) since the flagged answer was wrong
and there is, by definition, no confirmed expectation yet -- and this
module records only *that* the promotion happened, by whom, and which
case id resulted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

import config as cfg
from appdb.engine import get_app_engine
from appdb.models import turn_feedback
from observability.audit import find_record_by_turn

#: The closed set of categories an analyst may pick when flagging an
#: answer (spec §2.1). Free text is additionally offered, but only
#: alongside one of these, never instead of one -- see
#: ``web/js/render/feedback.js``.
FEEDBACK_CATEGORIES: tuple[str, ...] = (
    "wrong_number",
    "different_question",
    "wrong_filter_or_period",
    "other",
)

#: The closed set of triage outcomes (spec §3.1). Every flag must resolve
#: to exactly one of these -- there is no "dismiss without a reason".
RESOLUTION_OUTCOMES: tuple[str, ...] = (
    "alias_fix",
    "rule_fix",
    "golden_case",
    "not_a_defect",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TurnNotAuditedError(LookupError):
    """No audit record names the given ``session_id``/``turn_id`` (see
    :func:`~observability.audit.find_record_by_turn`) -- there is nothing
    to join this flag against, so it is refused rather than stored with a
    silently missing ``request_id``."""


class FeedbackNotFoundError(LookupError):
    """No ``turn_feedback`` row matches the given ``feedback_id``."""


class AlreadyResolvedError(RuntimeError):
    """:func:`resolve_feedback` called on a flag whose ``status`` is
    already ``"resolved"`` -- resolution is one-way, matching the
    triage queue's own "every outcome writes a record naming who resolved
    it and how" (spec §3.1): a second resolution would either silently
    overwrite the first admin's decision or need its own audit trail to
    avoid doing so, and neither is worth the complexity for what has never
    been asked for."""


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "feedback_id": row["feedback_id"],
        "session_id": row["session_id"],
        "turn_id": row["turn_id"],
        "request_id": row["request_id"],
        "reporter_principal_id": row["reporter_principal_id"],
        "category": row["category"],
        "note": row["note"],
        "config_version_id": row["config_version_id"],
        "created_at": row["created_at"],
        "status": row["status"],
        "resolution_outcome": row["resolution_outcome"],
        "resolution_note": row["resolution_note"],
        "resolution_config_version_id": row["resolution_config_version_id"],
        "resolution_golden_case_id": row["resolution_golden_case_id"],
        "resolved_by": row["resolved_by"],
        "resolved_at": row["resolved_at"],
    }


# ---------------------------------------------------------------------------
# Submit (analyst-facing; ownership is the caller's job -- see module
# docstring)
# ---------------------------------------------------------------------------

def submit_flag(
    *,
    session_id: str,
    turn_id: str,
    reporter_principal_id: str,
    category: str,
    note: str = "",
) -> dict[str, Any]:
    """Record one wrong-answer flag against *session_id*/*turn_id*.

    Parameters
    ----------
    session_id, turn_id:
        The turn being flagged. Ownership (only the turn's own principal
        may flag it) is the caller's responsibility -- see module
        docstring; this function stores whatever it is given.
    reporter_principal_id:
        The flagging principal's id.
    category:
        One of :data:`FEEDBACK_CATEGORIES`.
    note:
        Optional free text (spec §2.1's "one optional question").

    Returns
    -------
    dict
        The newly created row's public representation.

    Raises
    ------
    ValueError
        *category* is not one of :data:`FEEDBACK_CATEGORIES`.
    TurnNotAuditedError
        No audit record joins to *session_id*/*turn_id* -- see
        :func:`~observability.audit.find_record_by_turn`.
    """
    if category not in FEEDBACK_CATEGORIES:
        raise ValueError(
            f"category must be one of {FEEDBACK_CATEGORIES!r}, got {category!r}"
        )

    audit_record = find_record_by_turn(session_id, turn_id)
    if audit_record is None:
        raise TurnNotAuditedError(
            f"no audit record names session_id={session_id!r} turn_id={turn_id!r} "
            "-- nothing to join this flag against"
        )

    now = _now_iso()
    engine = get_app_engine()
    with engine.begin() as conn:
        result = conn.execute(
            turn_feedback.insert().values(
                session_id=session_id,
                turn_id=turn_id,
                request_id=audit_record.get("request_id"),
                reporter_principal_id=reporter_principal_id,
                category=category,
                note=note or None,
                config_version_id=audit_record.get("config_version_id"),
                created_at=now,
                status="open",
                resolution_outcome=None,
                resolution_note=None,
                resolution_config_version_id=None,
                resolution_golden_case_id=None,
                resolved_by=None,
                resolved_at=None,
            )
        )
        feedback_id = result.inserted_primary_key[0]
    return get_feedback(feedback_id)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_feedback(feedback_id: int) -> dict[str, Any]:
    """One flag's stored row (never the joined question/SQL -- see
    ``api.admin_feedback_routes`` for the endpoint that adds those).

    Raises
    ------
    FeedbackNotFoundError
    """
    engine = get_app_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(turn_feedback).where(turn_feedback.c.feedback_id == feedback_id)
        ).mappings().first()
    if row is None:
        raise FeedbackNotFoundError(f"no feedback row {feedback_id!r}")
    return _public(dict(row))


def list_feedback(
    *, status: str | None = None, session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Every flag matching the given filters, newest first.

    Parameters
    ----------
    status:
        ``"open"`` or ``"resolved"`` to filter, ``None`` (default) for
        every flag regardless of status -- the triage queue's own default
        view (spec §3: "newest first").
    session_id:
        Restrict to flags on one session -- used by the analyst-facing
        "did I already flag this turn" read (``GET
        /v2/sessions/{sid}/turns/{tid}/feedback``), never by the admin
        triage queue, which deliberately shows every principal's flags.
    """
    stmt = select(turn_feedback).order_by(turn_feedback.c.feedback_id.desc())
    if status is not None:
        stmt = stmt.where(turn_feedback.c.status == status)
    if session_id is not None:
        stmt = stmt.where(turn_feedback.c.session_id == session_id)
    engine = get_app_engine()
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [_public(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Triage (spec §3)
# ---------------------------------------------------------------------------

def resolve_feedback(
    feedback_id: int,
    *,
    outcome: str,
    actor_principal_id: str,
    note: str = "",
    config_version_id: int | None = None,
    golden_case_id: str | None = None,
) -> dict[str, Any]:
    """Resolve flag *feedback_id* into exactly one outcome (spec §3.1).

    This function never creates or applies a configuration version, and
    never writes to the golden-set file -- see module docstring. For
    ``"alias_fix"``/``"rule_fix"``, *config_version_id* is purely
    provenance: the id of a ``config_bundle_versions`` row the admin
    already created through ``POST /admin/config/versions`` (unchanged by
    this phase). For ``"golden_case"``, *golden_case_id* is the id
    :func:`promote_to_golden_case` assigned when writing the case to
    ``eval_data/golden.jsonl`` -- callers resolve with that outcome only
    after calling that function (see ``api.admin_feedback_routes`` for the
    single request that does both).

    Parameters
    ----------
    feedback_id:
        The flag to resolve.
    outcome:
        One of :data:`RESOLUTION_OUTCOMES`.
    actor_principal_id:
        The triaging admin's id.
    note:
        Required (non-blank) for ``"not_a_defect"`` (spec §3.1: "recorded
        with a reason, not silently dropped"); optional otherwise.
    config_version_id, golden_case_id:
        See above -- provenance only, never validated against the config
        version table or the golden file by this function.

    Returns
    -------
    dict
        The resolved row's public representation.

    Raises
    ------
    ValueError
        *outcome* is not one of :data:`RESOLUTION_OUTCOMES`, or
        *outcome* is ``"not_a_defect"`` and *note* is blank.
    FeedbackNotFoundError
    AlreadyResolvedError
    """
    if outcome not in RESOLUTION_OUTCOMES:
        raise ValueError(
            f"outcome must be one of {RESOLUTION_OUTCOMES!r}, got {outcome!r}"
        )
    if outcome == "not_a_defect" and not note.strip():
        raise ValueError(
            "a 'not_a_defect' outcome requires a non-blank note -- recorded "
            "with a reason, never silently dropped (spec §3.1)"
        )

    engine = get_app_engine()
    with engine.begin() as conn:
        row = conn.execute(
            select(turn_feedback).where(turn_feedback.c.feedback_id == feedback_id)
        ).mappings().first()
        if row is None:
            raise FeedbackNotFoundError(f"no feedback row {feedback_id!r}")
        if row["status"] == "resolved":
            raise AlreadyResolvedError(
                f"feedback {feedback_id!r} was already resolved as "
                f"{row['resolution_outcome']!r} by {row['resolved_by']!r} at "
                f"{row['resolved_at']!r}"
            )
        conn.execute(
            turn_feedback.update()
            .where(turn_feedback.c.feedback_id == feedback_id)
            .values(
                status="resolved",
                resolution_outcome=outcome,
                resolution_note=note or None,
                resolution_config_version_id=config_version_id,
                resolution_golden_case_id=golden_case_id,
                resolved_by=actor_principal_id,
                resolved_at=_now_iso(),
            )
        )
    return get_feedback(feedback_id)


# ---------------------------------------------------------------------------
# Promotion to a golden case (spec §4)
# ---------------------------------------------------------------------------

def promote_to_golden_case(
    feedback_id: int, *, tags: list[str] | None = None,
) -> dict[str, Any]:
    """Append a new, ``"pending_expected"`` golden case for *feedback_id*'s
    turn to ``cfg.settings.eval_golden_path`` (spec §4).

    The question is read from the audit log by re-joining on the flag's
    own ``session_id``/``turn_id`` (never stored on the feedback row
    itself -- see module docstring), so this is safe to call even long
    after :func:`submit_flag` ran, as long as the audit log has not
    rotated past that turn. ``expected_sql``/``expected_fingerprint`` are
    left unset: the flagged answer was wrong, so there is, by definition,
    no confirmed expectation yet -- someone must supply one and flip the
    case's ``status`` back to ``"active"`` before the regression gate will
    ever run it (:func:`eval.runner.run_golden_set` skips a
    ``"pending_expected"`` case entirely).

    Parameters
    ----------
    feedback_id:
        The flag being promoted.
    tags:
        Optional tags for the new case's per-tag accuracy breakdown once
        it is active. Defaults to none.

    Returns
    -------
    dict
        ``{"case_id": str, "question": str}`` for the newly appended case.

    Raises
    ------
    FeedbackNotFoundError
    TurnNotAuditedError
        The flag's turn can no longer be joined to an audit record.
    """
    from eval.models import GoldenCase

    feedback = get_feedback(feedback_id)
    audit_record = find_record_by_turn(feedback["session_id"], feedback["turn_id"])
    if audit_record is None:
        raise TurnNotAuditedError(
            f"no audit record names session_id={feedback['session_id']!r} "
            f"turn_id={feedback['turn_id']!r} -- cannot promote without the "
            "question it asked"
        )

    case_id = f"feedback_{feedback_id}"
    case = GoldenCase(
        id=case_id,
        question=audit_record["question"],
        tags=list(tags or []),
        status="pending_expected",
        notes=(
            f"Promoted from feedback #{feedback_id} "
            f"(session {feedback['session_id']!r}, turn {feedback['turn_id']!r}); "
            "awaiting a confirmed expected_sql/expected_fingerprint."
        ),
    )

    golden_path = Path(cfg.settings.eval_golden_path)
    golden_path.parent.mkdir(parents=True, exist_ok=True)
    with golden_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(case.to_dict(), ensure_ascii=False))
        fh.write("\n")

    return {"case_id": case_id, "question": case.question}


# ---------------------------------------------------------------------------
# Closing the loop visibly (spec §5)
# ---------------------------------------------------------------------------

def feedback_stats() -> dict[str, Any]:
    """Aggregate counts for the panel's "closing the loop visibly" view
    (spec §5): total/open flags, outcomes by category, and the golden
    set's size split by :data:`~eval.models.GoldenCaseStatus`.

    Never re-runs the evaluation harness -- accuracy trend is the admin
    routes' job (``api.admin_feedback_routes``), reading whatever baseline
    ``python -m eval.cli run --save-baseline`` last recorded
    (:mod:`eval.baseline`), since running the harness live against a real
    endpoint on every panel load would be exactly the "expensive, and
    surprising" thing this read-only view must not do.
    """
    rows = list_feedback()
    open_count = sum(1 for r in rows if r["status"] == "open")
    by_outcome: dict[str, int] = {outcome: 0 for outcome in RESOLUTION_OUTCOMES}
    for r in rows:
        if r["resolution_outcome"]:
            by_outcome[r["resolution_outcome"]] = by_outcome.get(r["resolution_outcome"], 0) + 1

    golden_path = Path(cfg.settings.eval_golden_path)
    golden_total = 0
    golden_pending = 0
    if golden_path.exists():
        from eval.runner import load_golden_cases

        try:
            cases = load_golden_cases(golden_path)
        except ValueError:
            cases = []
        golden_total = len(cases)
        golden_pending = sum(1 for c in cases if c.status == "pending_expected")

    return {
        "flags_total": len(rows),
        "flags_open": open_count,
        "outcomes_by_category": by_outcome,
        "golden_set_size": golden_total,
        "golden_set_pending": golden_pending,
    }
