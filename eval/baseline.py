"""Persist an :class:`~eval.models.EvalReport` as a baseline and gate CI on regressions.

Workflow
--------
1. Run the golden set once (usually in ``--live`` mode against a known-good
   commit) and :func:`save_baseline` the resulting report.
2. On every subsequent run (CI or local), :func:`load_baseline` that file
   and :func:`compare_to_baseline` it against the new report.
3. :func:`exit_code` turns the comparison into a process exit code so a CI
   step can simply run ``sys.exit(eval.baseline.exit_code(comparison))``.

What counts as a regression is deliberately explicit and configurable via
:class:`BaselineThresholds` rather than hard-coded, since "how much
latency increase is acceptable" is a product decision, not a technical
constant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eval.models import CaseResult, EvalReport

#: Percentage-point drop in ``accuracy_pct`` (current vs baseline) above
#: which a run is considered regressed. E.g. ``5.0`` means baseline 90% ->
#: current 84% (a 6-point drop) fails, but baseline 90% -> current 86% (a
#: 4-point drop) does not.
DEFAULT_MAX_ACCURACY_DROP_PCT = 5.0

#: Relative percentage increase in ``latency_p95`` (current vs baseline)
#: above which a run is considered regressed. E.g. ``20.0`` means a
#: baseline p95 of 2.0s tolerates up to 2.4s before failing.
DEFAULT_MAX_LATENCY_P95_INCREASE_PCT = 20.0

#: Absolute increase in ``guard_rejections`` (current vs baseline) above
#: which a run is considered regressed. Defaults to ``0`` — any new guard
#: rejection versus baseline is treated as safety-relevant and flagged,
#: since it means SQL that used to pass the security guard no longer does
#: (or the generator started producing worse SQL).
DEFAULT_MAX_GUARD_REJECTION_INCREASE = 0


@dataclass(frozen=True, slots=True)
class BaselineThresholds:
    """Configurable regression thresholds for :func:`compare_to_baseline`.

    Parameters
    ----------
    max_accuracy_drop_pct:
        Maximum tolerated drop in ``accuracy_pct`` (percentage points,
        not relative percent), baseline minus current. See module-level
        default for the exact semantics.
    max_latency_p95_increase_pct:
        Maximum tolerated *relative* increase in ``latency_p95``, as a
        percentage of the baseline value. Ignored (no latency check) when
        the baseline's ``latency_p95`` is ``0.0``, since a relative
        increase from zero is undefined.
    max_guard_rejection_increase:
        Maximum tolerated increase in ``guard_rejections`` (current minus
        baseline, absolute count).

    Examples
    --------
    >>> t = BaselineThresholds()
    >>> t.max_accuracy_drop_pct
    5.0
    >>> t2 = BaselineThresholds(max_accuracy_drop_pct=10.0)
    >>> t2.max_accuracy_drop_pct
    10.0
    """

    max_accuracy_drop_pct: float = DEFAULT_MAX_ACCURACY_DROP_PCT
    max_latency_p95_increase_pct: float = DEFAULT_MAX_LATENCY_P95_INCREASE_PCT
    max_guard_rejection_increase: int = DEFAULT_MAX_GUARD_REJECTION_INCREASE


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Outcome of comparing a new :class:`~eval.models.EvalReport` against a baseline.

    Parameters
    ----------
    regressed:
        ``True`` if any threshold in the :class:`BaselineThresholds` used
        for the comparison was violated.
    accuracy_delta_pct:
        ``current.accuracy_pct - baseline.accuracy_pct`` (percentage
        points; negative means accuracy dropped).
    latency_p95_delta_pct:
        Relative change in ``latency_p95``, as a percentage of the
        baseline value (``None`` when the baseline value was ``0.0`` and
        the check was skipped).
    guard_rejection_delta:
        ``current.guard_rejections - baseline.guard_rejections``.
    messages:
        Human-readable description of each violated threshold. Empty when
        ``regressed`` is ``False``.
    """

    regressed: bool
    accuracy_delta_pct: float
    latency_p95_delta_pct: float | None
    guard_rejection_delta: int
    messages: list[str] = field(default_factory=list)


def save_baseline(report: EvalReport, path: str | Path) -> None:
    """Persist *report* as a baseline JSON file.

    Parameters
    ----------
    report:
        The report to save (usually a known-good run).
    path:
        Destination file path. Parent directories are created if needed.

    Returns
    -------
    None

    Examples
    --------
    >>> import tempfile, os
    >>> from eval.models import CaseResult
    >>> from eval.report import build_report
    >>> results = [CaseResult("a", "q1", [], "pass", "SELECT 1", "fp1", None, 0.1)]
    >>> report = build_report(results, mode="live")
    >>> fd, path = tempfile.mkstemp(suffix=".json")
    >>> os.close(fd)
    >>> save_baseline(report, path)
    >>> loaded = load_baseline(path)
    >>> loaded.accuracy_pct == report.accuracy_pct
    True
    >>> os.remove(path)
    """
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_baseline(path: str | Path) -> EvalReport:
    """Load a baseline :class:`~eval.models.EvalReport` previously saved with :func:`save_baseline`.

    Parameters
    ----------
    path:
        Path to the baseline JSON file.

    Returns
    -------
    EvalReport

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If the file's JSON does not describe a valid report (missing
        keys).

    Examples
    --------
    >>> import tempfile, os
    >>> from eval.models import CaseResult
    >>> from eval.report import build_report
    >>> results = [CaseResult("a", "q1", ["t"], "pass", "SELECT 1", "fp1", None, 0.1)]
    >>> report = build_report(results, mode="offline")
    >>> fd, path = tempfile.mkstemp(suffix=".json")
    >>> os.close(fd)
    >>> save_baseline(report, path)
    >>> loaded = load_baseline(path)
    >>> loaded.total
    1
    >>> loaded.tag_accuracy["t"]
    (1, 1)
    >>> os.remove(path)
    """
    baseline_path = Path(path)
    if not baseline_path.exists():
        raise FileNotFoundError(f"baseline file not found: {baseline_path}")

    try:
        data: dict[str, Any] = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{baseline_path}: invalid JSON: {exc}") from exc

    try:
        results = [
            CaseResult(
                case_id=r["case_id"],
                question=r["question"],
                tags=list(r["tags"]),
                status=r["status"],
                generated_sql=r["generated_sql"],
                actual_fingerprint=r["actual_fingerprint"],
                error=r["error"],
                latency_seconds=r["latency_seconds"],
            )
            for r in data["results"]
        ]
        tag_accuracy = {
            tag: (counts["passed"], counts["total"])
            for tag, counts in data["tag_accuracy"].items()
        }
        return EvalReport(
            mode=data["mode"],
            total=data["total"],
            passed=data["passed"],
            accuracy_pct=data["accuracy_pct"],
            tag_accuracy=tag_accuracy,
            status_counts=dict(data["status_counts"]),
            guard_rejections=data["guard_rejections"],
            latency_p50=data["latency_p50"],
            latency_p95=data["latency_p95"],
            latency_p99=data["latency_p99"],
            results=results,
            generated_at=data["generated_at"],
        )
    except KeyError as exc:
        raise ValueError(f"{baseline_path}: missing expected key {exc}") from exc


def compare_to_baseline(
    current: EvalReport,
    baseline: EvalReport,
    thresholds: BaselineThresholds | None = None,
) -> ComparisonResult:
    """Compare *current* against *baseline* and decide whether it regressed.

    Parameters
    ----------
    current:
        The report from the run being evaluated.
    baseline:
        The reference report, from :func:`load_baseline`.
    thresholds:
        Regression thresholds. Defaults to :class:`BaselineThresholds`'s
        defaults when omitted.

    Returns
    -------
    ComparisonResult

    Raises
    ------
    ValueError
        If *current* and *baseline* were produced in different modes.
        Offline and live runs are not commensurable, and comparing them
        yields a meaningless verdict — most dangerously a false *pass*,
        because offline accuracy is 100% by construction (the fixture
        replays the golden set's own ``expected_sql``). Gating a live run
        against an offline baseline would therefore report "no
        regression" no matter how badly the engine had degraded.

    Examples
    --------
    No regression when accuracy and latency are flat:

    >>> from eval.models import CaseResult
    >>> from eval.report import build_report
    >>> good = [CaseResult("a", "q", [], "pass", "SELECT 1", "fp", None, 1.0)]
    >>> baseline = build_report(good, mode="live")
    >>> current = build_report(good, mode="live")
    >>> result = compare_to_baseline(current, baseline)
    >>> result.regressed
    False

    An accuracy drop beyond the threshold is flagged:

    >>> bad = [
    ...     CaseResult("a", "q", [], "fingerprint_mismatch", "SELECT 1", "fp", "x", 1.0),
    ...     CaseResult("b", "q2", [], "pass", "SELECT 2", "fp2", None, 1.0),
    ... ]
    >>> good2 = [
    ...     CaseResult("a", "q", [], "pass", "SELECT 1", "fp", None, 1.0),
    ...     CaseResult("b", "q2", [], "pass", "SELECT 2", "fp2", None, 1.0),
    ... ]
    >>> baseline2 = build_report(good2, mode="live")
    >>> current2 = build_report(bad, mode="live")
    >>> result2 = compare_to_baseline(current2, baseline2, BaselineThresholds(max_accuracy_drop_pct=10.0))
    >>> result2.regressed
    True
    >>> result2.accuracy_delta_pct
    -50.0
    """
    if current.mode != baseline.mode:
        raise ValueError(
            f"Cannot compare a {current.mode!r} run against a {baseline.mode!r} "
            f"baseline. Offline and live runs are not commensurable: offline "
            f"latencies are microseconds against live seconds, and offline "
            f"accuracy is 100% by construction because the fixture replays the "
            f"golden set's own expected_sql. Re-record the baseline in the same "
            f"mode you intend to gate on."
        )

    if thresholds is None:
        thresholds = BaselineThresholds()

    messages: list[str] = []

    accuracy_delta_pct = current.accuracy_pct - baseline.accuracy_pct
    accuracy_drop = -accuracy_delta_pct
    if accuracy_drop > thresholds.max_accuracy_drop_pct:
        messages.append(
            f"accuracy dropped {accuracy_drop:.2f} points "
            f"(baseline {baseline.accuracy_pct:.2f}% -> current {current.accuracy_pct:.2f}%), "
            f"exceeding the allowed {thresholds.max_accuracy_drop_pct:.2f} points"
        )

    latency_p95_delta_pct: float | None
    if baseline.latency_p95 > 0.0:
        latency_p95_delta_pct = (
            100.0 * (current.latency_p95 - baseline.latency_p95) / baseline.latency_p95
        )
        if latency_p95_delta_pct > thresholds.max_latency_p95_increase_pct:
            messages.append(
                f"latency p95 increased {latency_p95_delta_pct:.2f}% "
                f"(baseline {baseline.latency_p95:.3f}s -> current {current.latency_p95:.3f}s), "
                f"exceeding the allowed {thresholds.max_latency_p95_increase_pct:.2f}%"
            )
    else:
        latency_p95_delta_pct = None

    guard_rejection_delta = current.guard_rejections - baseline.guard_rejections
    if guard_rejection_delta > thresholds.max_guard_rejection_increase:
        messages.append(
            f"guard rejections increased by {guard_rejection_delta} "
            f"(baseline {baseline.guard_rejections} -> current {current.guard_rejections}), "
            f"exceeding the allowed increase of {thresholds.max_guard_rejection_increase}"
        )

    return ComparisonResult(
        regressed=bool(messages),
        accuracy_delta_pct=accuracy_delta_pct,
        latency_p95_delta_pct=latency_p95_delta_pct,
        guard_rejection_delta=guard_rejection_delta,
        messages=messages,
    )


def exit_code(comparison: ComparisonResult) -> int:
    """Map a :class:`ComparisonResult` to a process exit code.

    Parameters
    ----------
    comparison:
        The comparison to convert.

    Returns
    -------
    int
        ``1`` if ``comparison.regressed`` else ``0``.

    Examples
    --------
    >>> exit_code(ComparisonResult(False, 0.0, 0.0, 0, []))
    0
    >>> exit_code(ComparisonResult(True, -10.0, None, 0, ["accuracy dropped"]))
    1
    """
    return 1 if comparison.regressed else 0
