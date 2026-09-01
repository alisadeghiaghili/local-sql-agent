"""The API audit record and its (never-fails) JSONL writer.

Closes the audit gap described in the package docstring: today
``logs/logger.py::save_log`` is only reached from ``app.py``'s
interactive REPL, so every query answered through the FastAPI service —
the path the organization actually uses in production — leaves no audit
trail at all. :class:`AuditRecord` and :func:`save_audit_record` are the
machinery a future API-layer change wires in per request; nothing here is
called from ``api/**`` yet (see the package docstring for why, and for
the exact seam).

Why a sibling record, not an extended ``QueryLog``
---------------------------------------------------
``logs.query_log.QueryLog`` models one *REPL* round-trip: a question, the
SQL it produced, and a flat ``execution_time_seconds``/``excel_file``
shape suited to a human sitting at a terminal. The API's ``Turn`` (see
``docs/api-contract-v2.md`` §4) is a different animal — it carries a
request id, a guard verdict, per-stage ``timings``, and a structured
``llm`` status block, none of which have any REPL equivalent, and it has
no ``excel_file`` at all. Bolting request/guard/timings/llm fields onto
``QueryLog`` would force every REPL call site to pass placeholder values
for API-only fields (and vice versa), and a schema change to satisfy the
API would risk breaking the REPL's already-stable log format. The two
call sites log genuinely different things, so :class:`AuditRecord` is a
deliberate sibling to ``QueryLog``, not a subclass or an extension of it
— consistent with the instruction not to distort ``QueryLog`` to fit.

Two hard rules
--------------
Both hold because this file is a compliance artefact, not a debugging
convenience:

1. **Never write result row data.** :attr:`AuditRecord.columns` may hold
   column *names* (useful for a compliance reviewer to see what was
   selected, and explicitly permitted by the same distinction the
   contract draws for prompts in §8: "Result columns go in the prompt,
   result rows do not"), but never row values. There is no field on this
   dataclass capable of holding row data at all — the constraint is
   structural, not just documented, and :meth:`AuditRecord.__post_init__`
   additionally rejects a ``columns`` list containing anything other than
   plain strings, to catch a caller accidentally passing row tuples.
2. **Writing an audit record must never fail a user's query.**
   :func:`save_audit_record` wraps its write exactly the way
   ``logs.logger.save_log`` already does: an ``OSError`` is logged and
   swallowed, never raised to the caller.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import config as cfg
from logs.logger import append_jsonl

logger = logging.getLogger(__name__)

# Module-level path variable so tests can patch "observability.audit._AUDIT_LOG_FILE",
# mirroring logs.logger._LOG_FILE.
_AUDIT_LOG_FILE: str = ""


def _audit_log_file() -> str:
    """Return the effective audit log file path.

    When ``_AUDIT_LOG_FILE`` is non-empty (patched by tests) that value is
    used; otherwise reads ``cfg.settings.log_dir`` lazily, at call time,
    so ``config.override_settings()`` patches are visible immediately —
    matching ``logs.logger._log_file``'s convention exactly.
    """
    if _AUDIT_LOG_FILE:
        return _AUDIT_LOG_FILE
    return os.path.join(cfg.settings.log_dir, "audit_log.jsonl")


@dataclass(slots=True)
class AuditRecord:
    """One structured, compliance-grade record per API query.

    Modelled on :class:`logs.query_log.QueryLog`'s spirit (one record per
    engine invocation, ``as_dict()`` for JSON serialisation) but not its
    shape — see the module docstring for why this is a sibling rather
    than an extension.

    Parameters
    ----------
    timestamp:
        When the query was received.
    request_id:
        Correlates this record with the request id
        ``api/middleware.py``'s ``RequestIDMiddleware`` stamps on every
        request and echoes in the ``X-Request-ID`` response header — the
        same id an operator would already be grepping server logs for.
    question:
        The user's natural-language question, verbatim.
    generated_sql:
        The SQL that was executed (or attempted), verbatim.
    guard:
        The guard verdict, shaped like ``docs/api-contract-v2.md`` §4's
        ``Turn.guard``: at minimum a ``"verdict"`` key (``"allowed"`` or
        ``"rejected"``); ``"rule"``, ``"injected_top"``, and
        ``"tables_touched"`` when known. Stored as given rather than
        re-modelled, so the API layer can pass its own guard dict
        through unchanged when wiring lands.
    row_count:
        Number of rows returned. **Not** the rows themselves — see the
        module docstring's hard rule.
    tier:
        Which serving tier answered the query — ``"T0"`` (cache),
        ``"T1"`` (template), ``"T2"`` (single-shot), or ``"T3"`` (agent),
        per the contract. ``None`` when the query never reached tier
        selection (e.g. it was rejected before that point).
    error_code:
        The ``NLQError.error_code`` (see ``api/errors.py``) if the query
        failed, else ``None``.
    error_message:
        Human-readable error detail if the query failed, else ``None``.
    timings:
        Per-stage timings, shaped exactly like
        :meth:`observability.timing.StageTimer.snapshot`'s return value
        (contract §4). Defaults to an empty dict when unavailable.
    llm:
        The LLM status block from
        :func:`observability.llm_status.build_llm_status` (contract §6),
        or ``None`` when the query never reached the LLM (e.g. a cache
        hit, or a guard rejection before generation).
    columns:
        Column **names** selected by the query, or ``None``. Never row
        values — see the module docstring.
    principal_id:
        The authenticated caller's :class:`~security.auth.Principal.id`
        (Phase 8), or ``None`` for a query with no principal at all (the
        CLI/REPL path, or a pre-Phase-8-shaped direct call). This is the
        field that makes the audit trail an actual audit trail — "who
        ran this query" — rather than just a log of what happened.

    Raises
    ------
    TypeError
        From :meth:`__post_init__` if ``columns`` contains anything
        other than plain strings.

    Examples
    --------
    >>> from datetime import datetime
    >>> record = AuditRecord(
    ...     timestamp=datetime(2026, 8, 26, 12, 0, 0),
    ...     request_id="r_abc123",
    ...     question="چند مشتری فعال داریم؟",
    ...     generated_sql="SELECT COUNT(*) FROM [Dim].[Customer]",
    ...     guard={"verdict": "allowed", "rule": None,
    ...            "injected_top": None, "tables_touched": ["Customer"]},
    ...     row_count=1,
    ...     tier="T2",
    ...     columns=["CustomerCount"],
    ... )
    >>> record.as_dict()["row_count"]
    1
    >>> record.as_dict()["guard"]["verdict"]
    'allowed'

    A row *value* is rejected, not just row data wholesale:

    >>> AuditRecord(
    ...     timestamp=datetime(2026, 8, 26, 12, 0, 0),
    ...     request_id="r_bad",
    ...     question="q",
    ...     generated_sql="SELECT 1",
    ...     guard={"verdict": "allowed"},
    ...     row_count=1,
    ...     columns=["CustomerCount", 42],
    ... )
    Traceback (most recent call last):
        ...
    TypeError: AuditRecord.columns must contain column-name strings only, never row values; got int at index 1
    """

    timestamp: datetime
    request_id: str
    question: str
    generated_sql: str
    guard: dict[str, Any]
    row_count: int = 0
    tier: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    timings: dict[str, int] = field(default_factory=dict)
    llm: dict[str, Any] | None = None
    columns: list[str] | None = None
    principal_id: str | None = None

    def __post_init__(self) -> None:
        if self.columns is not None:
            for i, col in enumerate(self.columns):
                if not isinstance(col, str):
                    raise TypeError(
                        "AuditRecord.columns must contain column-name strings "
                        f"only, never row values; got {type(col).__name__} at "
                        f"index {i}"
                    )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict (timestamp as ISO string)."""
        return {
            "timestamp":      self.timestamp.isoformat(),
            "request_id":     self.request_id,
            "question":       self.question,
            "generated_sql":  self.generated_sql,
            "guard":          self.guard,
            "row_count":      self.row_count,
            "tier":           self.tier,
            "error_code":     self.error_code,
            "error_message":  self.error_message,
            "timings":        self.timings,
            "llm":            self.llm,
            "columns":        self.columns,
            "principal_id":   self.principal_id,
        }


def save_audit_record(record: AuditRecord) -> None:
    """Append *record* as a single, size-rotated JSON line to the audit log.

    Delegates to :func:`logs.logger.append_jsonl` for the actual write
    (and its rotation), the same rotation-aware primitive
    ``logs.logger.save_log`` uses, so the two JSONL logs the application
    produces share one size-cap/retention behaviour rather than
    duplicating it.

    Parameters
    ----------
    record:
        The audit record to persist.

    Raises
    ------
    Nothing.
        Per the module docstring's second hard rule: any ``OSError``
        during the write (including one raised during rotation) is
        caught, logged at ``ERROR``, and swallowed. A broken audit log
        must never fail the user's query — exactly the contract
        ``logs.logger.save_log`` already upholds for the REPL path.

    Examples
    --------
    >>> import sys, tempfile, os, json
    >>> from datetime import datetime
    >>> d = tempfile.mkdtemp()
    >>> target = os.path.join(d, "audit_log.jsonl")
    >>> this_module = sys.modules[__name__]
    >>> this_module._AUDIT_LOG_FILE = target  # mirrors logs.logger._LOG_FILE
    >>> save_audit_record(AuditRecord(
    ...     timestamp=datetime(2026, 8, 26, 12, 0, 0),
    ...     request_id="r_1", question="q", generated_sql="SELECT 1",
    ...     guard={"verdict": "allowed"}, row_count=0,
    ... ))
    >>> json.loads(open(target, encoding="utf-8").read().strip())["request_id"]
    'r_1'
    >>> this_module._AUDIT_LOG_FILE = ""  # reset for other doctests/tests
    """
    log_path = _audit_log_file()
    try:
        append_jsonl(log_path, record.as_dict())
    except OSError as exc:
        logger.error("Failed to write audit record: %s", exc)
