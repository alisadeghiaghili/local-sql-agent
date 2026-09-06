# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Typed records shared across the evaluation harness.

Three record types flow through the pipeline in order:

1. :class:`GoldenCase`  — one hand-authored question + reference answer,
   loaded from a ``golden.jsonl`` file.
2. :class:`CaseResult`  — the outcome of running one :class:`GoldenCase`
   through :func:`eval.runner.run_case`.
3. :class:`EvalReport`  — the aggregate of every :class:`CaseResult` in a
   run, produced by :func:`eval.report.build_report`.

All three are plain, immutable-by-convention dataclasses so they serialise
to/from JSON trivially (see :mod:`eval.report` and :mod:`eval.baseline`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: What a case is expected to do when run through the pipeline.
#:
#: ``"success"``
#:     The generator produces SQL, the guard accepts it, execution succeeds,
#:     and the result fingerprint matches ``expected_fingerprint``.
#: ``"empty"``
#:     Same as ``"success"`` but the reference answer is a zero-row result
#:     set (still requires a fingerprint match — an empty frame has a
#:     well-defined fingerprint, see :mod:`eval.fingerprint`).
#: ``"out_of_scope"``
#:     The generator is expected to raise ``ValueError("OUT_OF_SCOPE")``.
#: ``"error"``
#:     The case is expected to fail (e.g. a deliberately malformed golden
#:     case used to test the harness itself). Rare in practice.
CaseExpectation = Literal["success", "empty", "out_of_scope", "error"]

#: Whether a case currently participates in the regression gate.
#:
#: ``"active"``
#:     The default -- runs and counts toward ``EvalReport.total``/
#:     ``passed`` exactly as every case always has.
#: ``"pending_expected"``
#:     Admin panel phase 4 (``docs/admin-panel-architecture.md`` §3, the
#:     phase 4 spec §4): a triaged feedback flag promoted to a golden case
#:     before anyone has supplied its correct SQL/expected result. A golden
#:     case with a *wrong* expectation would be worse than no case at all
#:     -- it would make the regression gate enforce the bug the flag was
#:     raised about -- so :func:`eval.runner.run_golden_set` skips a
#:     ``"pending_expected"`` case entirely rather than running it against
#:     whatever placeholder expectation it might carry. It still appears
#:     in :func:`eval.runner.load_golden_cases`'s return value (so the
#:     golden set's *size*, including its still-pending cases, remains
#:     visible to anyone reading the file directly), and moves to
#:     ``"active"`` the moment someone edits its ``expected_sql``.
GoldenCaseStatus = Literal["active", "pending_expected"]

#: Coarse bucket a finished case falls into — used for the error taxonomy
#: in :class:`EvalReport` and to decide pass/fail.
CaseStatus = Literal[
    "pass",                 # matched expectation, fingerprint matched (or N/A)
    "fingerprint_mismatch", # SQL executed but result != expected
    "guard_rejected",       # security.sql_guard.validate_sql raised
    "generation_error",     # LLM/generator raised something unexpected
    "execution_error",      # execute_fn raised RuntimeError
    "unexpected_out_of_scope",  # generator said OUT_OF_SCOPE but case expected data
    "missed_out_of_scope",  # case expected OUT_OF_SCOPE but generator returned SQL
]


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One hand-authored (question -> reference answer) evaluation case.

    Parameters
    ----------
    id:
        Stable, unique identifier (e.g. ``"customer_count_basic"``). Used in
        reports and as the join key against a baseline.
    question:
        The natural-language question, verbatim as a user would type it.
        May be Persian, English, or mixed.
    tags:
        Free-form keywords used for the per-tag accuracy breakdown (e.g.
        ``["customer", "count"]``, ``["persian_digits", "date_filter"]``).
    expected_sql:
        The reference T-SQL query a domain expert verified produces the
        correct answer. ``None`` for cases whose ``expect`` is
        ``"out_of_scope"`` (there is no correct SQL for those).
    expect:
        What the pipeline should do with this question. See
        :data:`CaseExpectation`. Defaults to ``"success"``.
    expected_fingerprint:
        Precomputed :func:`eval.fingerprint.fingerprint_dataframe` hash of
        the reference result set. Required for offline/CI mode (there is no
        live database to compute it against). Optional in live mode, where
        it can instead be *recorded* from the first run to seed a baseline.
    expected_rows:
        Optional recorded reference rows (list of ``{column: value}``
        dicts), used only by the offline replay executor
        (:func:`eval.runner.make_offline_executor`) to serve as the
        "database" response for this case without a real connection. Not
        required when only ``expected_fingerprint`` is used for comparison
        in live mode.
    notes:
        Free-text explanation of *why* this case exists / what it guards
        against (e.g. "Persian digit normalisation must map ۱۴۰۲ -> 1402").
    status:
        See :data:`GoldenCaseStatus`. Defaults to ``"active"`` -- every
        case ever loaded before this field existed has no ``status`` key
        in its JSON line at all, and :meth:`from_dict` treats an absent
        key exactly like this default, so no existing golden set changes
        behaviour.

    Examples
    --------
    >>> case = GoldenCase(
    ...     id="customer_count_basic",
    ...     question="How many customers exist?",
    ...     tags=["customer", "count"],
    ...     expected_sql="SELECT COUNT(*) AS CustomerCount FROM [Auction_Dim].[Customer]",
    ...     expected_fingerprint="deadbeef",
    ... )
    >>> case.expect
    'success'
    >>> case.is_out_of_scope
    False

    A ``"pending_expected"`` case may omit ``expected_sql`` entirely --
    admin panel phase 4's promoted-but-not-yet-answered golden case (spec
    §4):

    >>> pending = GoldenCase(
    ...     id="promoted_from_feedback_7",
    ...     question="How many orders shipped late last quarter?",
    ...     status="pending_expected",
    ... )
    >>> pending.expected_sql is None
    True
    """

    id: str
    question: str
    tags: list[str] = field(default_factory=list)
    expected_sql: str | None = None
    expect: CaseExpectation = "success"
    expected_fingerprint: str | None = None
    expected_rows: list[dict[str, Any]] | None = None
    notes: str = ""
    status: GoldenCaseStatus = "active"

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("GoldenCase.id must be a non-empty string")
        if not self.question or not self.question.strip():
            raise ValueError(f"GoldenCase {self.id!r}: question must be non-empty")
        if self.status == "pending_expected":
            # No expectation exists yet, by definition (spec §4) -- neither
            # branch below applies until someone supplies one and moves
            # this case back to "active".
            return
        if self.expect != "out_of_scope" and not self.expected_sql:
            raise ValueError(
                f"GoldenCase {self.id!r}: expected_sql is required unless "
                f"expect='out_of_scope' (got expect={self.expect!r})"
            )
        if self.expect == "out_of_scope" and self.expected_sql:
            raise ValueError(
                f"GoldenCase {self.id!r}: expected_sql must be None when "
                f"expect='out_of_scope'"
            )

    @property
    def is_out_of_scope(self) -> bool:
        """True when this case documents an out-of-scope question."""
        return self.expect == "out_of_scope"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoldenCase":
        """Build a :class:`GoldenCase` from one decoded JSON line.

        Unknown keys are ignored so ``golden.jsonl`` can carry extra
        documentation fields (e.g. a human-only ``source`` column) without
        breaking the loader.

        Parameters
        ----------
        data:
            A dict decoded from one line of a ``golden.jsonl`` file.

        Returns
        -------
        GoldenCase

        Examples
        --------
        >>> GoldenCase.from_dict({
        ...     "id": "x", "question": "how many?", "expected_sql": "SELECT 1",
        ... }).id
        'x'
        """
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        """Serialise back to a plain dict (inverse of :meth:`from_dict`)."""
        return {
            "id": self.id,
            "question": self.question,
            "tags": list(self.tags),
            "expected_sql": self.expected_sql,
            "expect": self.expect,
            "expected_fingerprint": self.expected_fingerprint,
            "expected_rows": self.expected_rows,
            "notes": self.notes,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Outcome of running one :class:`GoldenCase` through the pipeline.

    Parameters
    ----------
    case_id:
        Matches :attr:`GoldenCase.id`.
    question:
        Copied from the case, for standalone readability of a report.
    tags:
        Copied from the case, used for the per-tag breakdown.
    status:
        Coarse outcome bucket, see :data:`CaseStatus`.
    passed:
        ``True`` iff the run matched the case's expectation. Convenience
        boolean derived from ``status`` at construction time (``status ==
        "pass"``).
    generated_sql:
        The cleaned SQL string the generator produced, or ``None`` if
        generation failed before producing SQL (e.g. ``OUT_OF_SCOPE``).
    actual_fingerprint:
        Fingerprint of the executed result set, or ``None`` if execution
        never happened.
    error:
        Human-readable error message when ``status != "pass"``, else
        ``None``.
    latency_seconds:
        Wall-clock time for the full case (generation + guard + execution).
    """

    case_id: str
    question: str
    tags: list[str]
    status: CaseStatus
    generated_sql: str | None
    actual_fingerprint: str | None
    error: str | None
    latency_seconds: float

    @property
    def passed(self) -> bool:
        """True iff this case matched its expectation."""
        return self.status == "pass"

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict."""
        return {
            "case_id": self.case_id,
            "question": self.question,
            "tags": list(self.tags),
            "status": self.status,
            "passed": self.passed,
            "generated_sql": self.generated_sql,
            "actual_fingerprint": self.actual_fingerprint,
            "error": self.error,
            "latency_seconds": self.latency_seconds,
        }


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Aggregate result of running a full golden set once.

    Parameters
    ----------
    mode:
        ``"offline"`` or ``"live"`` — which executor/generator wiring
        produced this report.
    total:
        Number of cases run.
    passed:
        Number of cases with ``status == "pass"``.
    accuracy_pct:
        ``100 * passed / total`` (0.0 when ``total == 0``).
    tag_accuracy:
        Per-tag accuracy, ``{tag: (passed, total)}``.
    status_counts:
        Count of results per :data:`CaseStatus` value — this is the error
        taxonomy.
    guard_rejections:
        Number of cases whose generated SQL was rejected by
        :func:`security.sql_guard.validate_sql` (subset of
        ``status_counts["guard_rejected"]``, surfaced separately since it
        is the single most safety-relevant metric).
    latency_p50, latency_p95, latency_p99:
        Latency percentiles across all cases, in seconds.
    results:
        The full list of per-case results, for drill-down.
    generated_at:
        ISO-8601 UTC timestamp of when the report was built.
    """

    mode: Literal["offline", "live"]
    total: int
    passed: int
    accuracy_pct: float
    tag_accuracy: dict[str, tuple[int, int]]
    status_counts: dict[str, int]
    guard_rejections: int
    latency_p50: float
    latency_p95: float
    latency_p99: float
    results: list[CaseResult]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict (used by :mod:`eval.baseline`)."""
        return {
            "mode": self.mode,
            "total": self.total,
            "passed": self.passed,
            "accuracy_pct": self.accuracy_pct,
            "tag_accuracy": {
                tag: {"passed": p, "total": t}
                for tag, (p, t) in self.tag_accuracy.items()
            },
            "status_counts": dict(self.status_counts),
            "guard_rejections": self.guard_rejections,
            "latency_p50": self.latency_p50,
            "latency_p95": self.latency_p95,
            "latency_p99": self.latency_p99,
            "generated_at": self.generated_at,
            "results": [r.to_dict() for r in self.results],
        }
