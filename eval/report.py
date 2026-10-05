# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Aggregate a list of :class:`~eval.models.CaseResult` into an :class:`~eval.models.EvalReport`.

:func:`build_report` computes every summary statistic the evaluation
harness cares about — overall execution accuracy, a per-tag breakdown,
the error taxonomy (:class:`~eval.models.CaseStatus` counts), the guard
rejection count, and latency percentiles — from a flat list of per-case
results. :func:`render_text` and :func:`render_json` turn that report into
the two output formats the CLI needs: a human-readable summary for a
terminal, and a machine-readable JSON document for
:mod:`eval.baseline` to compare across runs in CI.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args

from eval.models import CaseResult, CaseStatus, EvalReport

#: Every possible :class:`~eval.models.CaseStatus` value, in the order
#: declared on the type. Extracted with :func:`typing.get_args` (rather
#: than duplicated as a literal tuple) so this list can never drift out of
#: sync with :mod:`eval.models`.
ALL_STATUSES: tuple[str, ...] = get_args(CaseStatus)


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile of an already-sorted sequence.

    Parameters
    ----------
    sorted_values:
        Values sorted ascending. Empty input returns ``0.0``.
    pct:
        Percentile in ``[0, 100]``.

    Returns
    -------
    float

    Examples
    --------
    >>> _percentile([1.0, 2.0, 3.0, 4.0], 50)
    2.5
    >>> _percentile([1.0, 2.0, 3.0, 4.0], 0)
    1.0
    >>> _percentile([1.0, 2.0, 3.0, 4.0], 100)
    4.0
    >>> _percentile([], 50)
    0.0
    >>> _percentile([5.0], 99)
    5.0
    """
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_values[0]

    rank = (pct / 100.0) * (n - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]

    fraction = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def _source_stats(
    results: Sequence[CaseResult], mode: Literal["offline", "live"]
) -> tuple[dict[str, tuple[int, int]], tuple[int, int] | None]:
    """Per-source execution accuracy and (live only) source-selection accuracy.

    Only results whose case named a ``datasource`` take part. Selection
    accuracy compares :attr:`~eval.models.CaseResult.selected_datasource`
    with the case's; a run that never got as far as selecting counts as a
    miss. Offline, the "selection" replays the reference, so no figure is
    produced (it would be right by construction).

    Examples
    --------
    >>> from eval.models import CaseResult
    >>> rs = [
    ...     CaseResult("a", "q", [], "pass", "SELECT 1", "f", None, 0.1, "sales", "sales"),
    ...     CaseResult("b", "q", [], "fingerprint_mismatch", "SELECT 2", "f", "x", 0.1, "sales", "inventory"),
    ...     CaseResult("c", "q", [], "pass", "SELECT 3", "f", None, 0.1, None, None),
    ... ]
    >>> _source_stats(rs, "live")
    ({'sales': (1, 2)}, (1, 2))
    >>> _source_stats(rs, "offline")[1] is None
    True
    """
    totals: dict[str, int] = {}
    passed: dict[str, int] = {}
    correct = 0
    named = 0
    for result in results:
        expected = result.expected_datasource
        if expected is None:
            continue
        named += 1
        totals[expected] = totals.get(expected, 0) + 1
        if result.passed:
            passed[expected] = passed.get(expected, 0) + 1
        if result.selected_datasource == expected:
            correct += 1
    source_accuracy = {src: (passed.get(src, 0), totals[src]) for src in sorted(totals)}
    selection = (correct, named) if (mode == "live" and named) else None
    return source_accuracy, selection


def build_report(
    results: Sequence[CaseResult],
    mode: Literal["offline", "live"],
    reference: Literal["stored", "live"] = "stored",
) -> EvalReport:
    """Aggregate *results* into an :class:`~eval.models.EvalReport`.

    Parameters
    ----------
    results:
        One :class:`~eval.models.CaseResult` per case, typically produced
        by :func:`eval.runner.run_golden_set`. May be empty (an empty
        golden set produces a report with ``total == 0`` and
        ``accuracy_pct == 0.0``, not a division error).
    mode:
        Which pipeline wiring produced *results* — recorded on the report
        for downstream display and baseline comparison.
    reference:
        How each result was judged (``"stored"`` fingerprint or ``"live"``
        reference execution) — recorded on the report; a baseline can only
        be compared with a run that used the same one.

    Returns
    -------
    EvalReport

    Examples
    --------
    >>> from eval.models import CaseResult
    >>> results = [
    ...     CaseResult("a", "q1", ["count"], "pass", "SELECT 1", "fp1", None, 0.10),
    ...     CaseResult("b", "q2", ["count", "join"], "fingerprint_mismatch", "SELECT 2", "fp2", "bad", 0.20),
    ...     CaseResult("c", "q3", ["join"], "guard_rejected", "DROP TABLE x", None, "blocked", 0.05),
    ... ]
    >>> report = build_report(results, mode="offline")
    >>> report.total, report.passed
    (3, 1)
    >>> round(report.accuracy_pct, 2)
    33.33
    >>> report.tag_accuracy["count"]
    (1, 2)
    >>> report.tag_accuracy["join"]
    (0, 2)
    >>> report.guard_rejections
    1
    >>> report.status_counts["pass"]
    1
    >>> report.reference
    'stored'
    """
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    accuracy_pct = (100.0 * passed / total) if total else 0.0

    tag_totals: dict[str, int] = {}
    tag_passed: dict[str, int] = {}
    for result in results:
        for tag in result.tags:
            tag_totals[tag] = tag_totals.get(tag, 0) + 1
            if result.passed:
                tag_passed[tag] = tag_passed.get(tag, 0) + 1
    tag_accuracy = {
        tag: (tag_passed.get(tag, 0), tag_totals[tag]) for tag in sorted(tag_totals)
    }

    status_counter: Counter[str] = Counter({status: 0 for status in ALL_STATUSES})
    status_counter.update(r.status for r in results)
    status_counts = dict(status_counter)
    guard_rejections = status_counts.get("guard_rejected", 0)

    latencies = sorted(r.latency_seconds for r in results)
    source_accuracy, source_selection = _source_stats(results, mode)

    return EvalReport(
        mode=mode,
        total=total,
        passed=passed,
        accuracy_pct=accuracy_pct,
        tag_accuracy=tag_accuracy,
        status_counts=status_counts,
        guard_rejections=guard_rejections,
        latency_p50=_percentile(latencies, 50),
        latency_p95=_percentile(latencies, 95),
        latency_p99=_percentile(latencies, 99),
        results=list(results),
        generated_at=datetime.now(timezone.utc).isoformat(),
        reference=reference,
        source_accuracy=source_accuracy,
        source_selection=source_selection,
    )


def render_text(report: EvalReport) -> str:
    """Render *report* as a human-readable multi-line summary.

    Parameters
    ----------
    report:
        The report to render, typically from :func:`build_report`.

    Returns
    -------
    str
        Multi-line text: header (mode, totals, accuracy), per-tag
        breakdown, error taxonomy, guard rejections, and latency
        percentiles.

    Examples
    --------
    >>> from eval.models import CaseResult
    >>> results = [CaseResult("a", "q1", ["count"], "pass", "SELECT 1", "fp1", None, 0.1)]
    >>> report = build_report(results, mode="offline")
    >>> text = render_text(report)
    >>> "Execution accuracy: 100.00% (1/1)" in text
    True
    >>> "count" in text
    True
    """
    lines: list[str] = []
    lines.append(f"Evaluation report ({report.mode} mode) — generated {report.generated_at}")
    lines.append(
        f"Execution accuracy: {report.accuracy_pct:.2f}% ({report.passed}/{report.total})"
    )
    if report.mode == "offline":
        # Without this line the 100% is read as an engine-quality number.
        # Offline mode replays the golden set's own expected_sql, so the
        # accuracy figure is true by construction; what it actually proves
        # is that the harness, the guard and the fingerprinting still work.
        lines.append(
            "  NOTE: offline mode replays the golden set's own expected_sql, so "
            "this accuracy is true by construction and is NOT a measure of "
            "generation quality. Run with --live for that."
        )
    if report.mode == "live":
        if report.reference == "live":
            lines.append(
                "Reference: live -- each case's expected_sql was executed in this run "
                "and the two results compared (rows as a multiset, column names ignored)."
            )
        else:
            lines.append(
                "Reference: stored -- results compared with each case's recorded fingerprint."
            )
    lines.append("")

    lines.append("Per-tag accuracy:")
    if report.tag_accuracy:
        for tag, (tag_passed, tag_total) in report.tag_accuracy.items():
            pct = (100.0 * tag_passed / tag_total) if tag_total else 0.0
            lines.append(f"  {tag:<24s} {tag_passed:>3d}/{tag_total:<3d} ({pct:5.1f}%)")
    else:
        lines.append("  (no tagged cases)")
    lines.append("")

    if report.source_accuracy:
        lines.append("Per-source execution accuracy:")
        for source, (src_passed, src_total) in report.source_accuracy.items():
            pct = (100.0 * src_passed / src_total) if src_total else 0.0
            lines.append(f"  {source:<24s} {src_passed:>3d}/{src_total:<3d} ({pct:5.1f}%)")
        if report.source_selection is not None:
            sel_ok, sel_total = report.source_selection
            sel_pct = (100.0 * sel_ok / sel_total) if sel_total else 0.0
            lines.append(f"Source-selection accuracy: {sel_pct:.2f}% ({sel_ok}/{sel_total})")
        lines.append("")

    lines.append("Error taxonomy:")
    for status in ALL_STATUSES:
        count = report.status_counts.get(status, 0)
        if count:
            lines.append(f"  {status:<24s} {count}")
    lines.append("")

    lines.append(f"Guard rejections: {report.guard_rejections}")
    lines.append("")

    lines.append(
        "Latency (seconds): "
        f"p50={report.latency_p50:.3f}  p95={report.latency_p95:.3f}  "
        f"p99={report.latency_p99:.3f}"
    )

    return "\n".join(lines)


def render_json(report: EvalReport) -> str:
    """Render *report* as an indented JSON string (machine-readable).

    Parameters
    ----------
    report:
        The report to render.

    Returns
    -------
    str
        ``json.dumps(report.to_dict(), indent=2)`` output. Safe to write
        directly to a file or pipe into another tool.

    Examples
    --------
    >>> from eval.models import CaseResult
    >>> results = [CaseResult("a", "q1", [], "pass", "SELECT 1", "fp1", None, 0.1)]
    >>> report = build_report(results, mode="offline")
    >>> import json
    >>> data = json.loads(render_json(report))
    >>> data["total"]
    1
    >>> data["mode"]
    'offline'
    """
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)


def save_json_report(report: EvalReport, path: str | Path) -> None:
    """Write :func:`render_json` output for *report* to *path*.

    Parameters
    ----------
    report:
        The report to persist.
    path:
        Destination file path. Parent directories are created if needed.

    Returns
    -------
    None

    Examples
    --------
    >>> import tempfile, os, json
    >>> from eval.models import CaseResult
    >>> results = [CaseResult("a", "q1", [], "pass", "SELECT 1", "fp1", None, 0.1)]
    >>> report = build_report(results, mode="offline")
    >>> fd, path = tempfile.mkstemp(suffix=".json")
    >>> os.close(fd)
    >>> save_json_report(report, path)
    >>> json.loads(open(path, encoding="utf-8").read())["total"]
    1
    >>> os.remove(path)
    """
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_json(report), encoding="utf-8")
