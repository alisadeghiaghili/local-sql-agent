# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Aggregate report over ``logs/audit_log.jsonl`` — the deployment week's
only source of real accuracy/latency numbers, analysed WITHOUT letting the
raw log (which contains real user questions) leave the server.

Usage (from repo root, on the machine that produced the log)::

    python scripts/analyze_audit_log.py
    python scripts/analyze_audit_log.py logs/audit_log.jsonl
    python scripts/analyze_audit_log.py "logs/audit_log.jsonl*"
    python scripts/analyze_audit_log.py logs/audit_log.jsonl logs/audit_log.jsonl.1
    python scripts/analyze_audit_log.py --json > report.json
    python scripts/analyze_audit_log.py --include-examples > report_internal.txt

With no arguments, reads every ``logs/audit_log.jsonl*`` file next to this
script's repo root (the active file plus any rotated ``.1``, ``.2``, ...
backups — see ``logs/logger.py``'s rotation). Multiple explicit paths and/or
shell-style glob patterns are both accepted, so a rotated deployment's
history can be analysed in one run without hand-listing every backup.

Two modes — read this before sending a report anywhere
---------------------------------------------------------------------------
``AuditRecord.question`` (``observability/audit.py``) is real user text
from a commodity exchange's trading desk. This script runs *on the
server that produced the log* and its normal output is a fully
aggregated report — counts, percentiles, distributions — with **no
question text, no generated SQL, and no error messages** anywhere in it,
so that report is safe to copy off the server (paste into a chat, attach
to an email) without a second thought.

``--include-examples`` is the opt-in escape hatch for when the deployment
owner has separately decided that sharing example questions is
acceptable (e.g. debugging a specific miss with the person who wrote this
code). It adds a small number of verbatim ``question`` strings (and, for
guard rejections, the ``error_message``) alongside the aggregates. It is
never the default, and the report's own ``"mode"`` field always says
which one produced it — see :func:`build_report`'s ``mode`` key — so a
reader (or a future you) can tell at a glance whether a given report file
is safe to forward or not, without re-deriving that from the flags used
to generate it.

What leaves the machine in the default (safe) mode
----------------------------------------------------------
Record counts, latency percentiles, distributions (``finish_reason``,
``error_code``, provider, cache hit rates, correction-round counts), and
generated-SQL *shape* (which tables were touched and how many joins —
never the SQL text or the question that produced it). Nothing that could
identify a specific query, a specific user, or a specific piece of
commodity-exchange business data.

Report sections
----------------
1. :func:`_records_by_model`      — record counts by ``llm.model``, so a
   reader sees at a glance whether they are looking at real traffic or a
   stub/test backend (``mock:stub``, ``ollama:test``, ...) before trusting
   any other number in the report.
2. :func:`_latency_report`        — p50/p95/p99 overall and per stage
   (``plan``, ``prompt``, ``llm``, ``guard``, ``execute``, ``interpret``)
   from ``timings``. Where the time actually goes.
3. :func:`_finish_reason_distribution` — ``length`` means truncation, i.e.
   ``llm_num_predict`` is set too low (see ``docs/api-contract-v2.md`` §6).
4. :func:`_failure_taxonomy`      — counts by ``error_code``, plus guard
   rejections split policy (no retry could ever fix it) vs. correctable
   (a retry could plausibly have fixed it, but the budget ran out) — see
   ``security/sql_guard.py``'s ``PolicyRejection``/``CorrectableRejection``
   split, which :data:`_POLICY_ERROR_CODES` / :data:`_CORRECTABLE_ERROR_CODES`
   mirror via the ``error_code`` ``api/runner.py`` already assigns.
5. :func:`_cache_behaviour`       — T0 (cache-hit tier) rate, and
   ``prefix_cache_hit`` rate among calls that actually reached the LLM —
   the number the whole Phase 2 static-prefix latency premise rests on
   and that has never actually been measured against real traffic.
6. :func:`_sql_shape_clusters`    — intent clusters by generated-SQL
   *shape* (tables touched + join count), not by question text — Phase
   6's input, computed without reading a single word of any question.
7. :func:`_correction_rounds`     — how many self-correction rounds a
   query needed, and whether spending them ever actually succeeded.
8. :func:`_llm_meta_summary`      — reasoning-channel detections,
   provider / fallback usage, and how often a requested seed was
   confirmed honoured.

Handling a partly-stub log
----------------------------
A dev machine's log is typically dominated by ``mock:stub`` / ``ollama:test``
(or similarly named) records from the test suite, not real traffic — see
section 1 above. This script does not filter those out (a deployment week
should also show its own mix), it just makes the mix visible so nobody
mistakes 111 stub records for a week of real usage, or a week of real
usage for "mostly noise" because a few hundred leftover test records are
mixed in.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observability.timing import STAGE_NAMES

_DEFAULT_GLOB = "logs/audit_log.jsonl*"

#: ``error_code`` values that mean the guard rejected the SQL for a reason
#: no re-prompt could ever fix (a forbidden statement/keyword, an
#: injection attempt) — mirrors ``security.sql_guard.PolicyRejection`` via
#: the mapping ``api/runner.py``'s ``_GUARD_REJECTED_ERRORS`` already
#: performs onto these two ``NLQError`` subclasses' ``error_code``s.
_POLICY_ERROR_CODES: frozenset[str] = frozenset({"FORBIDDEN_SQL", "INJECTION_ATTEMPT"})

#: ``error_code`` values that mean a guard-shaped rejection was, in
#: principle, fixable by a retry (a malformed or hallucinated candidate)
#: but the self-correction budget ran out before one succeeded, or the
#: model returned nothing usable at all — mirrors
#: ``security.sql_guard.CorrectableRejection`` (``INVALID_SQL_RESPONSE``)
#: plus the closely related ``EMPTY_SQL_RESPONSE`` (never reached the
#: guard at all, but for the same "a retry might have helped" reason).
_CORRECTABLE_ERROR_CODES: frozenset[str] = frozenset(
    {"INVALID_SQL_RESPONSE", "EMPTY_SQL_RESPONSE"}
)

#: A genuine scope decline — not a guard rejection at all (the SQL was
#: never generated), but reported alongside the guard taxonomy since an
#: operator reading "how often did we refuse to answer" wants both.
_SCOPE_DECLINE_ERROR_CODES: frozenset[str] = frozenset({"OUT_OF_SCOPE"})

#: Transport-level failures: the call never completed against a real
#: backend, or the backend/database itself was unreachable/slow. Not a
#: property of the *question* at all — an operator sees these as
#: "something downstream is unhealthy", not "the model got this wrong".
_TRANSPORT_ERROR_CODES: frozenset[str] = frozenset(
    {"MODEL_UNAVAILABLE", "MODEL_TIMEOUT", "DATABASE_UNAVAILABLE",
     "QUERY_TIMEOUT", "SERVER_OVERLOAD"}
)

_JOIN_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Reading the log
# ---------------------------------------------------------------------------

def resolve_log_paths(patterns: Iterable[str]) -> list[Path]:
    """Expand *patterns* (literal paths and/or glob patterns) to a sorted,
    deduplicated list of existing files.

    Each entry in *patterns* is tried both as a literal path and as a glob
    pattern (``glob.glob`` on a path with no wildcard characters just
    returns that path itself if it exists, so this is safe either way).
    Non-existent literal paths that are not glob patterns are silently
    skipped rather than raising, so a caller can pass
    ``logs/audit_log.jsonl logs/audit_log.jsonl.1`` without the second one
    existing yet on a fresh deployment.

    Examples
    --------
    >>> import tempfile, os
    >>> d = tempfile.mkdtemp()
    >>> a = os.path.join(d, "audit_log.jsonl")
    >>> b = os.path.join(d, "audit_log.jsonl.1")
    >>> _ = open(a, "w").close()
    >>> _ = open(b, "w").close()
    >>> paths = resolve_log_paths([os.path.join(d, "audit_log.jsonl*")])
    >>> len(paths)
    2
    >>> resolve_log_paths([os.path.join(d, "does_not_exist.jsonl")])
    []
    """
    found: set[Path] = set()
    for pattern in patterns:
        matches = glob.glob(pattern)
        for m in matches:
            found.add(Path(m))
        if not matches:
            p = Path(pattern)
            if p.exists():
                found.add(p)
    return sorted(found)


def iter_records(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """Yield every well-formed JSON object across *paths*, in file order.

    A line that fails to parse as JSON is silently skipped (a truncated
    final line from a write in progress is expected, not an error worth
    stopping the whole report over).
    """
    for path in paths:
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        yield record
        except OSError:
            continue


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------

def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile of *values* (0-100 scale).

    Matches ``numpy.percentile``'s default ("linear") method so numbers
    computed here agree with anyone re-deriving them with pandas/numpy on
    the exported figures. *values* need not be sorted.

    Examples
    --------
    >>> _percentile([1, 2, 3, 4, 5], 50)
    3.0
    >>> _percentile([1, 2, 3, 4], 50)
    2.5
    >>> _percentile([], 50) is None
    True
    >>> _percentile([42], 99)
    42
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    frac = k - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _stats(values: list[float]) -> dict[str, Any]:
    """``{count, mean, p50, p95, p99}`` for *values*, or an all-``None``
    shape (with ``count`` still accurate) when *values* is empty.

    Examples
    --------
    >>> _stats([1, 2, 3])["count"]
    3
    >>> _stats([])["p50"] is None
    True
    """
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None}
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 1),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
    }


def latency_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Overall and per-stage latency percentiles from each record's
    ``timings`` block (``total_ms`` plus one ``<stage>_ms`` per
    ``observability.timing.STAGE_NAMES`` entry).

    Records with no ``timings`` (or an empty one — e.g. a request
    rejected before any stage timer ran) contribute to no bucket; each
    bucket's own ``count`` says how many records actually had a number
    for that specific stage, which need not equal the total record count.
    """
    totals: list[float] = []
    by_stage: dict[str, list[float]] = {name: [] for name in STAGE_NAMES}

    for rec in records:
        timings = rec.get("timings") or {}
        total = timings.get("total_ms")
        if isinstance(total, (int, float)):
            totals.append(total)
        for stage in STAGE_NAMES:
            val = timings.get(f"{stage}_ms")
            if isinstance(val, (int, float)):
                by_stage[stage].append(val)

    return {
        "overall_ms": _stats(totals),
        "by_stage_ms": {stage: _stats(vals) for stage, vals in by_stage.items()},
    }


# ---------------------------------------------------------------------------
# finish_reason
# ---------------------------------------------------------------------------

def finish_reason_distribution(records: list[dict[str, Any]]) -> dict[str, int]:
    """Counts of ``llm.finish_reason`` across every record that reached an
    LLM at all (``llm`` is ``None`` for e.g. a T0 cache hit — excluded,
    not counted as any particular finish reason).

    ``"length"`` means the response was truncated by ``llm_num_predict``
    — see ``docs/api-contract-v2.md`` §6 and PR #41, which made this
    field derived-not-hardcoded and therefore trustworthy for the first
    time.
    """
    counts: Counter[str] = Counter()
    for rec in records:
        llm = rec.get("llm")
        if not llm:
            continue
        reason = llm.get("finish_reason")
        if reason:
            counts[reason] += 1
    return dict(counts.most_common())


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------

def _classify_error_code(code: str) -> str:
    """Bucket label for one ``error_code`` — see the module-level
    ``_..._ERROR_CODES`` sets for exactly which codes land in which bucket.

    Examples
    --------
    >>> _classify_error_code("FORBIDDEN_SQL")
    'guard_policy'
    >>> _classify_error_code("INVALID_SQL_RESPONSE")
    'guard_correctable'
    >>> _classify_error_code("OUT_OF_SCOPE")
    'scope_decline'
    >>> _classify_error_code("MODEL_TIMEOUT")
    'transport'
    >>> _classify_error_code("QUERY_EXECUTION_ERROR")
    'execution'
    >>> _classify_error_code("SOME_FUTURE_CODE")
    'other'
    """
    if code in _POLICY_ERROR_CODES:
        return "guard_policy"
    if code in _CORRECTABLE_ERROR_CODES:
        return "guard_correctable"
    if code in _SCOPE_DECLINE_ERROR_CODES:
        return "scope_decline"
    if code in _TRANSPORT_ERROR_CODES:
        return "transport"
    if code == "QUERY_EXECUTION_ERROR":
        return "execution"
    return "other"


def failure_taxonomy(
    records: list[dict[str, Any]], *, include_examples: bool = False,
) -> dict[str, Any]:
    """Failure counts by ``error_code`` and by the coarser bucket
    :func:`_classify_error_code` assigns it to (guard policy / guard
    correctable / scope decline / transport / execution / other).

    A successful record (``error_code`` is ``None``) is counted in
    ``success_count`` and nowhere else.

    When *include_examples* is true, up to 3 verbatim ``(question,
    error_message)`` pairs are attached per ``error_code`` — never
    populated otherwise, per this module's two-mode design (see the
    module docstring).
    """
    by_code: Counter[str] = Counter()
    by_bucket: Counter[str] = Counter()
    examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    success_count = 0

    for rec in records:
        code = rec.get("error_code")
        if not code:
            success_count += 1
            continue
        by_code[code] += 1
        by_bucket[_classify_error_code(code)] += 1
        if include_examples and len(examples[code]) < 3:
            examples[code].append({
                "question": rec.get("question", ""),
                "error_message": rec.get("error_message") or "",
            })

    result: dict[str, Any] = {
        "success_count": success_count,
        "failure_count": sum(by_code.values()),
        "by_error_code": dict(by_code.most_common()),
        "by_bucket": dict(by_bucket.most_common()),
    }
    if include_examples:
        result["examples_by_error_code"] = {k: v for k, v in examples.items()}
    return result


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------

def cache_behaviour(records: list[dict[str, Any]]) -> dict[str, Any]:
    """T0 (cache-hit tier) rate over all records, and ``prefix_cache_hit``
    rate among the subset that actually reached the LLM (``llm`` present).

    ``prefix_cache_hit`` is the number the whole Phase 2 static-prefix
    latency premise rests on — see ``observability/llm_status.py``'s
    module docstring — and, per that field's own derivation, is never
    ``True`` for a record with zero prompt tokens (a transport failure),
    so this rate is never inflated by requests that never reached a
    model at all.
    """
    total = len(records)
    t0_count = sum(1 for rec in records if rec.get("tier") == "T0")

    llm_records = [rec for rec in records if rec.get("llm")]
    prefix_hits = sum(1 for rec in llm_records if rec["llm"].get("prefix_cache_hit"))

    return {
        "total_records": total,
        "t0_count": t0_count,
        "t0_rate": round(t0_count / total, 4) if total else None,
        "llm_call_count": len(llm_records),
        "prefix_cache_hit_count": prefix_hits,
        "prefix_cache_hit_rate": (
            round(prefix_hits / len(llm_records), 4) if llm_records else None
        ),
    }


# ---------------------------------------------------------------------------
# SQL-shape intent clustering (never reads question text)
# ---------------------------------------------------------------------------

def _join_bucket(sql: str) -> str:
    """``"0"``, ``"1"``, or ``"2+"`` — how many ``JOIN`` keywords *sql*
    contains, bucketed rather than reported as an exact count so the
    cluster table stays small and readable.

    Examples
    --------
    >>> _join_bucket("SELECT 1")
    '0'
    >>> _join_bucket("SELECT * FROM A JOIN B ON A.id = B.id")
    '1'
    >>> _join_bucket("A JOIN B JOIN C JOIN D")
    '2+'
    """
    n = len(_JOIN_RE.findall(sql or ""))
    if n == 0:
        return "0"
    if n == 1:
        return "1"
    return "2+"


def sql_shape_clusters(
    records: list[dict[str, Any]], *, include_examples: bool = False, top_n: int = 25,
) -> list[dict[str, Any]]:
    """Cluster successful, guard-allowed records by generated-SQL *shape*
    — the sorted tuple of ``guard.tables_touched`` plus a join-count
    bucket (:func:`_join_bucket`) — never by question text. This is
    exactly Phase 6's clustering input, and it is computable from the
    audit log alone without reading a single word any user typed.

    Only records with a non-empty ``tables_touched`` are clustered — a
    record that never reached table resolution (an early rejection, a
    transport failure) has no SQL shape to report.

    Returned as a list of clusters sorted by descending record count,
    capped at *top_n* entries so a long-tail of one-off shapes does not
    drown out the handful of shapes that actually dominate real traffic.
    """
    clusters: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = defaultdict(list)

    for rec in records:
        guard = rec.get("guard") or {}
        tables = guard.get("tables_touched")
        if not tables:
            continue
        key = (tuple(sorted(tables)), _join_bucket(rec.get("generated_sql", "")))
        clusters[key].append(rec)

    ranked = sorted(clusters.items(), key=lambda kv: len(kv[1]), reverse=True)[:top_n]

    result: list[dict[str, Any]] = []
    for (tables, join_bucket), recs in ranked:
        entry: dict[str, Any] = {
            "tables_touched": list(tables),
            "join_count": join_bucket,
            "count": len(recs),
        }
        if include_examples:
            entry["example_questions"] = [r.get("question", "") for r in recs[:3]]
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Correction rounds
# ---------------------------------------------------------------------------

def correction_rounds(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Distribution of ``llm.corrections`` (self-correction rounds spent
    before the final result), and whether spending any ever correlated
    with an eventual success (``error_code`` is ``None``).
    """
    distribution: Counter[int] = Counter()
    success_by_rounds: Counter[int] = Counter()
    total_by_rounds: Counter[int] = Counter()

    for rec in records:
        llm = rec.get("llm")
        if not llm:
            continue
        rounds = llm.get("corrections")
        if not isinstance(rounds, int):
            continue
        distribution[rounds] += 1
        total_by_rounds[rounds] += 1
        if not rec.get("error_code"):
            success_by_rounds[rounds] += 1

    success_rate_by_rounds = {
        str(rounds): round(success_by_rounds[rounds] / total, 4)
        for rounds, total in total_by_rounds.items()
    }

    corrected = {r: n for r, n in distribution.items() if r > 0}
    return {
        "distribution": {str(k): v for k, v in sorted(distribution.items())},
        "success_rate_by_rounds": success_rate_by_rounds,
        "records_with_any_correction": sum(corrected.values()),
        "records_with_any_correction_that_succeeded": sum(
            success_by_rounds[r] for r in corrected
        ),
    }


# ---------------------------------------------------------------------------
# LLM meta summary
# ---------------------------------------------------------------------------

def llm_meta_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Reasoning-channel detections, provider/fallback usage, and how
    often a requested seed was confirmed honoured — all among records
    that reached an LLM at all.
    """
    llm_records = [rec["llm"] for rec in records if rec.get("llm")]
    n = len(llm_records)
    if n == 0:
        return {
            "llm_call_count": 0,
            "reasoning_detected_count": 0,
            "reasoning_detected_rate": None,
            "provider_distribution": {},
            "fallback_used_count": 0,
            "fallback_used_rate": None,
            "seed_requested_count": 0,
            "seed_honored_count": 0,
            "seed_honored_rate_among_seed_requested": None,
        }

    reasoning = sum(1 for llm in llm_records if llm.get("reasoning_detected"))
    fallback = sum(1 for llm in llm_records if llm.get("fallback_used"))
    providers: Counter[str] = Counter(
        llm.get("provider") for llm in llm_records if llm.get("provider")
    )
    seed_requested = [llm for llm in llm_records if llm.get("seed") is not None]
    seed_honored = sum(1 for llm in seed_requested if llm.get("seed_honored") is True)

    return {
        "llm_call_count": n,
        "reasoning_detected_count": reasoning,
        "reasoning_detected_rate": round(reasoning / n, 4),
        "provider_distribution": dict(providers.most_common()),
        "fallback_used_count": fallback,
        "fallback_used_rate": round(fallback / n, 4),
        "seed_requested_count": len(seed_requested),
        "seed_honored_count": seed_honored,
        "seed_honored_rate_among_seed_requested": (
            round(seed_honored / len(seed_requested), 4) if seed_requested else None
        ),
    }


# ---------------------------------------------------------------------------
# Record counts by model, and time range
# ---------------------------------------------------------------------------

def records_by_model(records: list[dict[str, Any]]) -> dict[str, int]:
    """Record counts keyed by ``llm.model`` (``"(no llm call)"`` for a
    record whose ``llm`` block is absent, e.g. a T0 cache hit or a
    pre-generation rejection).

    Surfaced first in the report (see :func:`build_report`) so a reader
    sees, before trusting any other number, whether this log is real
    traffic or a stub/test backend (``mock:stub``, ``ollama:test``,
    ``counting:test``, ...) left over from local development or CI.
    """
    counts: Counter[str] = Counter()
    for rec in records:
        llm = rec.get("llm")
        model = (llm or {}).get("model") or "(no llm call)"
        counts[model] += 1
    return dict(counts.most_common())


def _time_range(records: list[dict[str, Any]]) -> dict[str, str | None]:
    """``{"start", "end"}`` ISO timestamps spanned by *records*, or
    ``None``/``None`` for an empty log."""
    timestamps = sorted(
        rec["timestamp"] for rec in records if isinstance(rec.get("timestamp"), str)
    )
    if not timestamps:
        return {"start": None, "end": None}
    return {"start": timestamps[0], "end": timestamps[-1]}


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------

def build_report(
    records: list[dict[str, Any]], *, include_examples: bool = False,
) -> dict[str, Any]:
    """Assemble the full aggregate report for *records*.

    Parameters
    ----------
    records:
        Parsed audit-log entries, e.g. from :func:`iter_records`.
    include_examples:
        Opt-in flag (default ``False``) — when true, a small number of
        verbatim ``question`` strings (and guard-rejection error
        messages) are attached alongside the aggregates. See the module
        docstring's "Two modes" section before ever setting this true on
        a report that will leave the server.

    Returns
    -------
    dict
        Always carries a top-level ``"mode"`` key
        (``"aggregate_safe"`` or ``"aggregate_with_examples"``) naming
        which mode produced it, so the report is self-describing even
        once separated from the command that generated it.
    """
    return {
        "mode": "aggregate_with_examples" if include_examples else "aggregate_safe",
        "record_count": len(records),
        "time_range": _time_range(records),
        "records_by_model": records_by_model(records),
        "latency": latency_report(records),
        "finish_reason_distribution": finish_reason_distribution(records),
        "failure_taxonomy": failure_taxonomy(records, include_examples=include_examples),
        "cache_behaviour": cache_behaviour(records),
        "sql_shape_clusters": sql_shape_clusters(records, include_examples=include_examples),
        "correction_rounds": correction_rounds(records),
        "llm_meta_summary": llm_meta_summary(records),
    }


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------

def _fmt_ms(stats: dict[str, Any]) -> str:
    if not stats or stats.get("count") == 0:
        return "(no data)"
    return (
        f"n={stats['count']:<6} p50={stats['p50']:.0f}ms  "
        f"p95={stats['p95']:.0f}ms  p99={stats['p99']:.0f}ms  "
        f"mean={stats['mean']:.0f}ms"
    )


def render_text(report: dict[str, Any]) -> str:
    """Render *report* (from :func:`build_report`) as a human-readable
    summary suitable for a terminal or a pasted-into-chat message."""
    lines: list[str] = []
    w = lines.append

    w("Audit log analysis report")
    w("=" * 60)
    w(f"mode            : {report['mode']}")
    w(f"record_count    : {report['record_count']}")
    w(f"time_range      : {report['time_range']['start']} .. {report['time_range']['end']}")
    if report["mode"] != "aggregate_safe":
        w(
            "*** THIS REPORT INCLUDES VERBATIM EXAMPLE QUESTIONS. "
            "Do not send it anywhere without checking that is acceptable. ***"
        )
    w("")

    w("Records by model (real traffic vs. stub/test backends)")
    w("-" * 60)
    for model, count in report["records_by_model"].items():
        w(f"  {model:<30} {count}")
    w("")

    w("Latency — overall")
    w("-" * 60)
    w(f"  {_fmt_ms(report['latency']['overall_ms'])}")
    w("")
    w("Latency — by stage")
    w("-" * 60)
    for stage, stats in report["latency"]["by_stage_ms"].items():
        w(f"  {stage:<12} {_fmt_ms(stats)}")
    w("")

    w("finish_reason distribution ('length' == truncated, raise llm_num_predict)")
    w("-" * 60)
    for reason, count in report["finish_reason_distribution"].items():
        w(f"  {reason:<20} {count}")
    w("")

    ft = report["failure_taxonomy"]
    w("Failure taxonomy")
    w("-" * 60)
    w(f"  success_count : {ft['success_count']}")
    w(f"  failure_count : {ft['failure_count']}")
    w("  by bucket:")
    for bucket, count in ft["by_bucket"].items():
        w(f"    {bucket:<18} {count}")
    w("  by error_code:")
    for code, count in ft["by_error_code"].items():
        w(f"    {code:<25} {count}")
    w("")

    cb = report["cache_behaviour"]
    w("Cache behaviour")
    w("-" * 60)
    w(f"  T0 rate                : {cb['t0_rate']}  ({cb['t0_count']}/{cb['total_records']})")
    w(
        f"  prefix_cache_hit rate  : {cb['prefix_cache_hit_rate']}  "
        f"({cb['prefix_cache_hit_count']}/{cb['llm_call_count']} llm calls)"
    )
    w("")

    w("SQL-shape intent clusters (tables touched + join count; no question text)")
    w("-" * 60)
    for cluster in report["sql_shape_clusters"]:
        tables = ", ".join(cluster["tables_touched"]) or "(none)"
        w(f"  [{cluster['count']:>4}] joins={cluster['join_count']:<3} tables={tables}")
    w("")

    cr = report["correction_rounds"]
    w("Correction rounds")
    w("-" * 60)
    w(f"  distribution           : {cr['distribution']}")
    w(f"  success rate by rounds : {cr['success_rate_by_rounds']}")
    w(
        f"  ever corrected         : {cr['records_with_any_correction']} "
        f"(succeeded: {cr['records_with_any_correction_that_succeeded']})"
    )
    w("")

    lm = report["llm_meta_summary"]
    w("LLM meta summary")
    w("-" * 60)
    w(f"  llm_call_count                : {lm['llm_call_count']}")
    w(f"  reasoning_detected             : {lm['reasoning_detected_count']} "
      f"({lm['reasoning_detected_rate']})")
    w(f"  provider_distribution          : {lm['provider_distribution']}")
    w(f"  fallback_used                  : {lm['fallback_used_count']} "
      f"({lm['fallback_used_rate']})")
    w(f"  seed_requested                 : {lm['seed_requested_count']}")
    w(f"  seed_honored (of requested)    : {lm['seed_honored_count']} "
      f"({lm['seed_honored_rate_among_seed_requested']})")
    w("=" * 60)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate report over logs/audit_log.jsonl (and rotated backups). "
            "Default output is fully aggregated and safe to copy off the server; "
            "see --include-examples."
        ),
    )
    parser.add_argument(
        "paths", nargs="*", default=[_DEFAULT_GLOB],
        help=(
            "Log file(s) and/or glob pattern(s) to read. Default: "
            f"'{_DEFAULT_GLOB}' (the active audit log plus any rotated backups)."
        ),
    )
    parser.add_argument(
        "--include-examples", action="store_true",
        help=(
            "Opt-in: attach a small number of verbatim example questions "
            "(and guard-rejection error messages) to the report. NOT the "
            "default — read the module docstring's 'Two modes' section "
            "before using this on a report that will leave the server."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print the full report as JSON instead of the human-readable summary.",
    )
    args = parser.parse_args(argv)

    paths = resolve_log_paths(args.paths)
    if not paths:
        print(f"No log files matched: {args.paths}", file=sys.stderr)
        return 1

    records = list(iter_records(paths))
    report = build_report(records, include_examples=args.include_examples)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())
