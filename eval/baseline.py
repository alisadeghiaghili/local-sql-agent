# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
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
constant. The defaults below are tuning, not domain data or engine
behaviour, so they live on :class:`config.Settings`
(``eval_max_accuracy_drop_pct`` / ``eval_max_latency_p95_increase_pct`` /
``eval_max_guard_rejection_increase`` — see that module's "Three layers,
not two" docstring section) rather than as bare module constants here;
``python -m eval.cli run``'s own flags still take precedence when passed
explicitly, per-invocation.

The relative thresholds above only say "no worse than the baseline". They
cannot stop a release whose baseline was already poor, or one that slid
down in steps each smaller than the allowed drop. Two optional *absolute
floors* (:attr:`BaselineThresholds.min_accuracy_pct` and
:attr:`BaselineThresholds.min_source_accuracy_pct`, from
``EVAL_MIN_ACCURACY`` / ``EVAL_MIN_SOURCE_ACCURACY``) close that gap; both
are off unless set. :func:`check_absolute_floors` evaluates them on a
single report, so they work with or without a baseline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config as cfg
from eval.models import CaseResult, EvalReport


@dataclass(frozen=True, slots=True)
class BaselineThresholds:
    """Configurable regression thresholds for :func:`compare_to_baseline`.

    Parameters
    ----------
    max_accuracy_drop_pct:
        Maximum tolerated drop in ``accuracy_pct`` (percentage points,
        not relative percent), baseline minus current. Defaults to
        :attr:`config.Settings.eval_max_accuracy_drop_pct`, read at
        construction time so ``override_settings()`` reaches it in tests.
    max_latency_p95_increase_pct:
        Maximum tolerated *relative* increase in ``latency_p95``, as a
        percentage of the baseline value. Ignored (no latency check) when
        the baseline's ``latency_p95`` is ``0.0``, since a relative
        increase from zero is undefined. Defaults to
        :attr:`config.Settings.eval_max_latency_p95_increase_pct`.
    max_guard_rejection_increase:
        Maximum tolerated increase in ``guard_rejections`` (current minus
        baseline, absolute count). Defaults to
        :attr:`config.Settings.eval_max_guard_rejection_increase`.
    min_accuracy_pct:
        Optional absolute floor (0-100) on the run's overall
        ``accuracy_pct``, independent of any baseline. ``None`` (the
        default) disables it. Defaults to
        :attr:`config.Settings.eval_min_accuracy`.
    min_source_accuracy_pct:
        Optional absolute floor (0-100) on every data source's execution
        accuracy. ``None`` (the default) disables it. Defaults to
        :attr:`config.Settings.eval_min_source_accuracy`.

    Examples
    --------
    >>> t = BaselineThresholds()
    >>> t.max_accuracy_drop_pct
    5.0
    >>> t2 = BaselineThresholds(max_accuracy_drop_pct=10.0)
    >>> t2.max_accuracy_drop_pct
    10.0
    >>> t.min_accuracy_pct is None and t.min_source_accuracy_pct is None
    True
    """

    max_accuracy_drop_pct: float = field(
        default_factory=lambda: cfg.settings.eval_max_accuracy_drop_pct
    )
    max_latency_p95_increase_pct: float = field(
        default_factory=lambda: cfg.settings.eval_max_latency_p95_increase_pct
    )
    max_guard_rejection_increase: int = field(
        default_factory=lambda: cfg.settings.eval_max_guard_rejection_increase
    )
    min_accuracy_pct: float | None = field(
        default_factory=lambda: cfg.settings.eval_min_accuracy
    )
    min_source_accuracy_pct: float | None = field(
        default_factory=lambda: cfg.settings.eval_min_source_accuracy
    )


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Outcome of comparing a new :class:`~eval.models.EvalReport` against a baseline.

    Parameters
    ----------
    regressed:
        ``True`` if any threshold in the :class:`BaselineThresholds` used
        for the comparison was violated, including an absolute floor
        (``floor_messages``).
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
    source_deltas_pct:
        ``{source: current - baseline}`` execution-accuracy change in
        percentage points for every data source present in both reports.
        Informational: it does not trigger ``regressed`` on its own (a
        single case is a large share of a small source), because a source
        that really broke already lowers the overall accuracy the gate
        checks. Empty when either report has no per-source figures (a
        baseline written before they existed).
    floor_messages:
        One message per violated absolute floor (see
        :func:`check_absolute_floors`). Kept apart from ``messages`` so a
        caller can say "below the floor" rather than "regressed versus
        baseline". Empty when no floor is set or all were met.
    source_selection_delta_pct:
        Change in source-selection accuracy in percentage points, or
        ``None`` when either report has none. Informational, like
        ``source_deltas_pct``.
    """

    regressed: bool
    accuracy_delta_pct: float
    latency_p95_delta_pct: float | None
    guard_rejection_delta: int
    messages: list[str] = field(default_factory=list)
    source_deltas_pct: dict[str, float] = field(default_factory=dict)
    source_selection_delta_pct: float | None = None
    floor_messages: list[str] = field(default_factory=list)


def check_absolute_floors(
    report: EvalReport,
    thresholds: BaselineThresholds | None = None,
) -> list[str]:
    """Check *report* against the optional absolute accuracy floors.

    Unlike :func:`compare_to_baseline` this looks at one report only, so it
    also gates a first run with no baseline to compare with. A floor that is
    ``None`` is skipped entirely, which is why setting neither changes
    nothing. Comparisons are strict ``<``: a run exactly at the floor
    passes.

    Args:
        report: The report to check.
        thresholds: Source of the floors. Defaults to
            :class:`BaselineThresholds`'s defaults (the settings) when
            omitted.

    Returns:
        One human-readable message per violated floor, each naming the
        measured figure and the floor. Empty when every configured floor
        was met (or none is configured). A per-source floor that cannot be
        evaluated (the run has no per-source figures) is reported as a
        violation: an unverifiable floor must not pass.

    Raises:
        None.

    Examples:
        >>> from eval.models import CaseResult
        >>> from eval.report import build_report
        >>> rs = [
        ...     CaseResult("a", "q", [], "pass", "SELECT 1", "f", None, 0.1),
        ...     CaseResult("b", "q", [], "fingerprint_mismatch", "SELECT 2", "f", "x", 0.1),
        ... ]
        >>> report = build_report(rs, mode="live")
        >>> check_absolute_floors(report, BaselineThresholds(min_accuracy_pct=40.0))
        []
        >>> check_absolute_floors(report, BaselineThresholds(min_accuracy_pct=90.0))
        ['accuracy 50.00% (1/2) is below the absolute floor of 90.00%']
        >>> check_absolute_floors(report, BaselineThresholds())
        []
    """
    if thresholds is None:
        thresholds = BaselineThresholds()

    messages: list[str] = []

    floor = thresholds.min_accuracy_pct
    if floor is not None and report.accuracy_pct < floor:
        messages.append(
            f"accuracy {report.accuracy_pct:.2f}% ({report.passed}/{report.total}) "
            f"is below the absolute floor of {floor:.2f}%"
        )

    source_floor = thresholds.min_source_accuracy_pct
    if source_floor is not None:
        if not report.source_accuracy:
            messages.append(
                f"a per-source floor of {source_floor:.2f}% is set but this run has "
                f"no per-source figures (no golden case names an expected_datasource), "
                f"so it cannot be checked"
            )
        for source, (src_passed, src_total) in report.source_accuracy.items():
            src_pct = (100.0 * src_passed / src_total) if src_total else 0.0
            if src_pct < source_floor:
                messages.append(
                    f"source {source!r} accuracy {src_pct:.2f}% ({src_passed}/{src_total}) "
                    f"is below the per-source floor of {source_floor:.2f}%"
                )

    return messages


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
                # Additive fields: absent from a baseline written before
                # they existed.
                expected_datasource=r.get("expected_datasource"),
                selected_datasource=r.get("selected_datasource"),
                reference_fingerprint=r.get("reference_fingerprint"),
            )
            for r in data["results"]
        ]
        tag_accuracy = {
            tag: (counts["passed"], counts["total"])
            for tag, counts in data["tag_accuracy"].items()
        }
        reference = data.get("reference", "stored")
        if reference not in ("stored", "live"):
            raise ValueError(f"{baseline_path}: unknown reference {reference!r}")
        source_accuracy = {
            source: (counts["passed"], counts["total"])
            for source, counts in (data.get("source_accuracy") or {}).items()
        }
        selection = data.get("source_selection")
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
            reference=reference,
            source_accuracy=source_accuracy,
            source_selection=(
                None if not selection else (selection["correct"], selection["total"])
            ),
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

        Also if they judged results differently (``reference`` ``"stored"``
        against ``"live"``). A stored fingerprint goes stale as the
        warehouse changes, so a stored-reference baseline understates
        accuracy by an unknown amount, and a live-reference run compared
        with it would report an improvement that is only the end of the
        staleness (or, the other way round, a regression that is only its
        return). Re-record the baseline with the reference you gate on.

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
    if current.reference != baseline.reference:
        raise ValueError(
            f"Cannot compare a run judged against {current.reference!r} references "
            f"with a baseline judged against {baseline.reference!r} ones "
            f"(--reference). A stored fingerprint goes stale as the warehouse "
            f"changes while a live reference does not, so the two accuracy figures "
            f"are not measuring the same thing. Re-record the baseline with "
            f"--reference {current.reference} (--save-baseline)."
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

    source_deltas_pct: dict[str, float] = {}
    for source, (cur_p, cur_t) in current.source_accuracy.items():
        if source not in baseline.source_accuracy:
            continue
        base_p, base_t = baseline.source_accuracy[source]
        if cur_t and base_t:
            source_deltas_pct[source] = 100.0 * cur_p / cur_t - 100.0 * base_p / base_t
    source_selection_delta_pct: float | None = None
    if (
        current.source_selection is not None
        and baseline.source_selection is not None
        and current.source_selection[1]
        and baseline.source_selection[1]
    ):
        source_selection_delta_pct = (
            100.0 * current.source_selection[0] / current.source_selection[1]
            - 100.0 * baseline.source_selection[0] / baseline.source_selection[1]
        )

    floor_messages = check_absolute_floors(current, thresholds)

    return ComparisonResult(
        regressed=bool(messages or floor_messages),
        accuracy_delta_pct=accuracy_delta_pct,
        latency_p95_delta_pct=latency_p95_delta_pct,
        guard_rejection_delta=guard_rejection_delta,
        messages=messages,
        source_deltas_pct=source_deltas_pct,
        source_selection_delta_pct=source_selection_delta_pct,
        floor_messages=floor_messages,
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
