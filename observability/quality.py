# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The real-use quality report -- one computation, two surfaces.

Accuracy on the real warehouse is unknown until a golden set exists, and
latency and table selection have only been measured synthetically. This
module turns everyday use into evidence: it joins the analyst audit log
(what was asked, what ran, how long it took) with the analysts' own signals
(``appdb.confirmations``: "this answer is correct"; ``appdb.feedback``:
"this answer is wrong") into one report. The same function backs both
surfaces that show it:

* ``scripts/analyze_audit_log.py`` prints it as a section (counts only, no
  question text -- that script's privacy mode), and
* ``GET /admin/quality`` serves it to the admin panel's card
  "کیفیت در استفاده‌ی واقعی".

A lower-bound signal, not accuracy
-----------------------------------
:func:`build_quality_report` reports ``confirmed_share_of_rated``: of the
answers an analyst rated one way or the other, the share they confirmed.
That is **not** accuracy. Analysts rate only the answers they bother to
rate, and far more people click a correct answer than file a complaint
about a wrong one -- or the reverse. An unrated answer is not a correct
one. Read it as a rough lower-bound signal that grows as confirmation
becomes a habit; the golden set (``python -m eval.cli``) is what measures
accuracy. The report carries this caveat in its own ``caveats`` key so it
travels with the numbers.

What is counted
---------------
Every figure is computed from the audit records in a time window, overall
and once per data source (the record's ``datasource``; ``"(none)"`` when it
names none). Rates are over ``questions`` unless the field says otherwise.

``questions``
    Audit records in the window: one per question asked.
``executed`` / ``execution_success_rate``
    Records whose SQL ran and returned a result
    (:func:`observability.audit.turn_executed_successfully`).
    ``not_executed_without_error`` is the remainder: records with neither a
    result nor an ``error_code`` (a clarification turn, a ``mode="sql"``
    request), so the three counts add up.
``guard_rejection_rate``
    ``guard.verdict == "rejected"``.
``needed_correction_rate``
    ``llm.corrections > 0``: the model needed at least one self-correction
    round.
``empty_result_rate``
    Over ``executed``, not ``questions``: of the answers that ran, the
    share with zero rows.
``out_of_scope_rate``
    ``error_code == "OUT_OF_SCOPE"``.
``error_rate_by_class``
    ``error_code`` bucketed by :func:`scripts.analyze_audit_log.failure_taxonomy`
    (guard policy, guard correctable, scope decline, transport, execution,
    other). A scope decline is therefore in both this and ``out_of_scope``.
``ratings``
    ``confirmed``, ``flagged`` (distinct answers in the window with at
    least one confirmation / flag), ``rated`` (distinct answers with
    either) and ``confirmed_share_of_rated``. The ratings are joined to the
    audit record by ``(session_id, turn_id)``; a rating whose audit record
    has rotated away, or lies outside the window, is not counted.
``latency``
    End-to-end ``total_ms`` and each stage's p50/p95
    (:func:`scripts.analyze_audit_log.latency_report`), and the ``llm``
    stage split into ``ttft_ms`` and ``generation_ms``
    (:func:`scripts.analyze_audit_log.llm_latency_split`). The split exists
    only for calls made with ``LLM_STREAM_TIMINGS=true``; its ``count``
    says how many records had it.

Old and empty logs
------------------
Nothing here assumes a field exists. A record without ``timings``, ``llm``,
``guard``, ``datasource``, ``columns`` or ``timestamp`` contributes to the
figures it can and to none it cannot; an empty log yields zeros and
``None`` rates, never an exception or a division by zero. A record whose
timestamp is missing or unparseable is left out of the windowed section and
counted in the all-time section.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from observability.audit import turn_executed_successfully
from scripts.analyze_audit_log import (
    failure_taxonomy,
    latency_by_cache,
    latency_report,
    llm_latency_split,
)

#: Shown beside the report everywhere it appears. Persian copies of the
#: first two live in ``web/admin/main.js`` and the Persian guide.
CAVEAT_CONFIRMED_SHARE = (
    "confirmed_share_of_rated is a lower-bound signal, not accuracy: analysts "
    "rate only some answers, and an unrated answer is not a correct one."
)
CAVEAT_LATENCY_STAGES = (
    "The llm stage split (ttft_ms, generation_ms) appears only for calls made "
    "with LLM_STREAM_TIMINGS=true."
)


def _rate(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator`` rounded to 4 places, ``None`` when the
    denominator is zero.

    Examples
    --------
    >>> _rate(1, 4), _rate(0, 5), _rate(3, 0)
    (0.25, 0.0, None)
    """
    return round(numerator / denominator, 4) if denominator else None


def _parse_timestamp(value: Any) -> datetime | None:
    """A record's ``timestamp`` as a naive local datetime, or ``None``.

    Audit records are written with a naive local ``datetime.now()``; an
    aware timestamp (a hand-written or imported record) is converted to
    local time so the two compare.

    Examples
    --------
    >>> _parse_timestamp("2026-01-02T03:04:05").hour
    3
    >>> _parse_timestamp("garbage") is None, _parse_timestamp(None) is None
    (True, True)
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _datasource_of(record: dict[str, Any]) -> str:
    """The data source a record names (its ``datasource``, else the source
    routing's ``chosen``), or ``"(none)"``.

    Examples
    --------
    >>> _datasource_of({"datasource": "sales"})
    'sales'
    >>> _datasource_of({"datasource_selection": {"chosen": "stock"}})
    'stock'
    >>> _datasource_of({})
    '(none)'
    """
    value = record.get("datasource")
    if isinstance(value, str) and value.strip():
        return value
    selection = record.get("datasource_selection")
    if isinstance(selection, dict):
        chosen = selection.get("chosen")
        if isinstance(chosen, str) and chosen.strip():
            return chosen
    return "(none)"


def _normalised(record: dict[str, Any]) -> dict[str, Any]:
    """*record*, with each nested block that is not the expected shape dropped.

    A hand-edited or damaged line (``timings`` a string, ``llm`` a list)
    must cost that line its latency or LLM figures, not the whole report.
    The record is copied only when something is dropped.

    Examples
    --------
    >>> _normalised({"timings": "oops", "llm": {"model": "m"}, "guard": None})
    {'llm': {'model': 'm'}}
    >>> good = {"timings": {"total_ms": 5}}
    >>> _normalised(good) is good
    True
    """
    bad = [
        key for key in ("timings", "llm", "guard")
        if key in record and not isinstance(record[key], dict)
    ]
    if not bad:
        return record
    return {k: v for k, v in record.items() if k not in bad}


def _turn_key(record: dict[str, Any]) -> tuple[str, str] | None:
    session_id, turn_id = record.get("session_id"), record.get("turn_id")
    if isinstance(session_id, str) and isinstance(turn_id, str) and session_id and turn_id:
        return (session_id, turn_id)
    return None


def _keys(rows: Iterable[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        (str(r["session_id"]), str(r["turn_id"]))
        for r in rows
        if r.get("session_id") and r.get("turn_id")
    }


def compute_metrics(
    records: list[dict[str, Any]],
    *,
    confirmed_keys: set[tuple[str, str]] | None,
    flagged_keys: set[tuple[str, str]] | None,
) -> dict[str, Any]:
    """Every figure of the report for one set of *records*.

    Parameters
    ----------
    records:
        Audit records (any mix of old and new formats).
    confirmed_keys, flagged_keys:
        ``(session_id, turn_id)`` pairs that have at least one confirmation
        / flag, or ``None`` when the ratings could not be read (the
        ``ratings`` block then says ``available: False`` and carries no
        counts rather than reading as zero).

    Returns
    -------
    dict
        See the module docstring for each field.

    Examples
    --------
    >>> recs = [
    ...     {"generated_sql": "SELECT 1", "guard": {"verdict": "allowed"}, "columns": ["n"],
    ...      "row_count": 3, "session_id": "s", "turn_id": "t1", "timings": {"total_ms": 100}},
    ...     {"generated_sql": "", "error_code": "OUT_OF_SCOPE", "timings": {"total_ms": 300}},
    ... ]
    >>> m = compute_metrics(recs, confirmed_keys={("s", "t1")}, flagged_keys=set())
    >>> m["questions"], m["executed"], m["execution_success_rate"], m["out_of_scope_rate"]
    (2, 1, 0.5, 0.5)
    >>> m["ratings"]["confirmed_share_of_rated"], m["latency"]["total_ms"]["p50"]
    (1.0, 200.0)
    >>> compute_metrics([], confirmed_keys=None, flagged_keys=None)["execution_success_rate"] is None
    True
    """
    questions = len(records)
    executed = sum(1 for r in records if turn_executed_successfully(r))
    empty = sum(
        1 for r in records if turn_executed_successfully(r) and r.get("row_count") == 0
    )
    guard_rejected = sum(
        1 for r in records
        if isinstance(r.get("guard"), dict) and r["guard"].get("verdict") == "rejected"
    )
    needed_correction = 0
    for r in records:
        llm = r.get("llm")
        corrections = llm.get("corrections") if isinstance(llm, dict) else None
        if isinstance(corrections, int) and not isinstance(corrections, bool) and corrections > 0:
            needed_correction += 1
    out_of_scope = sum(1 for r in records if r.get("error_code") == "OUT_OF_SCOPE")

    taxonomy = failure_taxonomy(records)
    errors = taxonomy["failure_count"]
    by_class = {
        bucket: {"count": count, "rate": _rate(count, questions)}
        for bucket, count in taxonomy["by_bucket"].items()
    }

    ratings: dict[str, Any]
    if confirmed_keys is None or flagged_keys is None:
        ratings = {
            "available": False, "confirmed": None, "flagged": None, "rated": None,
            "confirmed_share_of_rated": None,
        }
    else:
        in_window = {k for r in records if (k := _turn_key(r)) is not None}
        confirmed = in_window & confirmed_keys
        flagged = in_window & flagged_keys
        rated = confirmed | flagged
        ratings = {
            "available": True,
            "confirmed": len(confirmed),
            "flagged": len(flagged),
            "rated": len(rated),
            "confirmed_share_of_rated": _rate(len(confirmed), len(rated)),
        }

    latency = latency_report(records)
    split = llm_latency_split(records)
    return {
        "questions": questions,
        "executed": executed,
        "not_executed_without_error": questions - executed - errors,
        "execution_success_rate": _rate(executed, questions),
        "guard_rejected": guard_rejected,
        "guard_rejection_rate": _rate(guard_rejected, questions),
        "needed_correction": needed_correction,
        "needed_correction_rate": _rate(needed_correction, questions),
        "empty_results": empty,
        "empty_result_rate": _rate(empty, executed),
        "out_of_scope": out_of_scope,
        "out_of_scope_rate": _rate(out_of_scope, questions),
        "errors": errors,
        "error_rate": _rate(errors, questions),
        "error_rate_by_class": by_class,
        "ratings": ratings,
        "latency": {
            "total_ms": latency["overall_ms"],
            "by_stage_ms": latency["by_stage_ms"],
            "llm_split": {
                "llm_call_count": split["llm_call_count"],
                "streamed_call_count": split["streamed_call_count"],
                "ttft_ms": split["ttft_ms"],
                "generation_ms": split["generation_ms"],
            },
            # The llm stage grouped by the server's own cached_tokens;
            # calls without it are "unreported", never guessed into a group.
            "llm_by_cache": {
                name: {"count": group["count"], "llm_ms": group["llm_ms"]}
                for name, group in latency_by_cache(records).items()
            },
        },
    }


def _section(
    records: list[dict[str, Any]],
    *,
    confirmed_keys: set[tuple[str, str]] | None,
    flagged_keys: set[tuple[str, str]] | None,
    since: datetime | None,
    until: datetime,
) -> dict[str, Any]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_source.setdefault(_datasource_of(record), []).append(record)
    ordered = sorted(by_source, key=lambda name: (-len(by_source[name]), name))
    return {
        "since": since.isoformat() if since is not None else None,
        "until": until.isoformat(),
        "overall": compute_metrics(
            records, confirmed_keys=confirmed_keys, flagged_keys=flagged_keys,
        ),
        "by_datasource": {
            name: compute_metrics(
                by_source[name], confirmed_keys=confirmed_keys, flagged_keys=flagged_keys,
            )
            for name in ordered
        },
    }


def build_quality_report(
    records: Iterable[dict[str, Any]],
    *,
    confirmations: Iterable[dict[str, Any]] | None = None,
    flags: Iterable[dict[str, Any]] | None = None,
    days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the real-use quality report over the last *days* days and all time.

    Parameters
    ----------
    records:
        Parsed audit-log records (e.g. from
        :func:`scripts.analyze_audit_log.iter_records`). Records from older
        versions that lack fields are fine.
    confirmations:
        Rows of ``appdb.confirmations`` (each with ``session_id`` and
        ``turn_id``), or ``None`` when they could not be read.
    flags:
        Rows of ``appdb.feedback`` flags, or ``None`` when unreadable.
        Ratings are reported as unavailable unless both are given.
    days:
        Width of the windowed section, counted back from *now*.
    now:
        The window's end (a naive local datetime); defaults to
        ``datetime.now()``. Injected by tests.

    Returns
    -------
    dict
        ``{"generated_at", "window_days", "recent", "all_time", "caveats"}``.
        ``recent`` and ``all_time`` each hold ``{"since", "until",
        "overall", "by_datasource"}``; ``overall`` and every entry of
        ``by_datasource`` have the shape :func:`compute_metrics` returns.
        ``caveats`` holds the two notes the report must always travel with
        (:data:`CAVEAT_CONFIRMED_SHARE`, :data:`CAVEAT_LATENCY_STAGES`).

    Raises
    ------
    ValueError
        *days* is less than 1.

    Examples
    --------
    >>> from datetime import datetime
    >>> recs = [
    ...     {"timestamp": "2026-10-12T10:00:00", "generated_sql": "SELECT 1",
    ...      "columns": ["n"], "row_count": 1, "datasource": "sales"},
    ...     {"timestamp": "2026-08-01T10:00:00", "generated_sql": "SELECT 1",
    ...      "columns": ["n"], "row_count": 1, "datasource": "sales"},
    ... ]
    >>> rep = build_quality_report(recs, now=datetime(2026, 10, 13))
    >>> rep["recent"]["overall"]["questions"], rep["all_time"]["overall"]["questions"]
    (1, 2)
    >>> list(rep["all_time"]["by_datasource"])
    ['sales']
    >>> build_quality_report([], now=datetime(2026, 10, 13))["all_time"]["overall"]["questions"]
    0
    """
    if days < 1:
        raise ValueError(f"days must be at least 1, got {days!r}")
    end = now if now is not None else datetime.now()
    since = end - timedelta(days=days)
    all_records = [_normalised(r) for r in records if isinstance(r, dict)]
    windowed = [
        r for r in all_records
        if (ts := _parse_timestamp(r.get("timestamp"))) is not None and ts >= since
    ]

    if confirmations is None or flags is None:
        confirmed_keys: set[tuple[str, str]] | None = None
        flagged_keys: set[tuple[str, str]] | None = None
    else:
        confirmed_keys = _keys(confirmations)
        flagged_keys = _keys(flags)

    return {
        "generated_at": end.isoformat(),
        "window_days": days,
        "recent": _section(
            windowed, confirmed_keys=confirmed_keys, flagged_keys=flagged_keys,
            since=since, until=end,
        ),
        "all_time": _section(
            all_records, confirmed_keys=confirmed_keys, flagged_keys=flagged_keys,
            since=None, until=end,
        ),
        "caveats": {
            "confirmed_share": CAVEAT_CONFIRMED_SHARE,
            "latency_stages": CAVEAT_LATENCY_STAGES,
        },
    }


@dataclass(frozen=True, slots=True)
class Ratings:
    """What :func:`load_ratings` read.

    Parameters
    ----------
    confirmations, flags:
        Rows of the two tables, or ``None`` when the application database
        could not be read (pass them on as they are: the report then says
        the ratings are unavailable).
    error:
        The exception class name behind a ``None``, else ``None``.
    """

    confirmations: list[dict[str, Any]] | None
    flags: list[dict[str, Any]] | None
    error: str | None = None


def load_ratings() -> Ratings:
    """Read every confirmation and flag from the application database.

    Never raises: an unreachable or misconfigured application database
    yields ``Ratings(None, None, error=<class name>)``, so a quality report
    still renders (its ratings unavailable) instead of failing whole. A
    file-backed SQLite application database that does not exist yet has had
    no ratings recorded, and is reported as empty without being created --
    running a report must not leave a new database file behind.

    Returns
    -------
    Ratings
    """
    try:
        from sqlalchemy.engine import make_url

        from appdb.confirmations import list_confirmations
        from appdb.engine import resolve_app_db_url
        from appdb.feedback import list_feedback

        url = make_url(resolve_app_db_url())
        if (
            url.get_backend_name() == "sqlite"
            and url.database not in (None, "", ":memory:")
            and not Path(str(url.database)).exists()
        ):
            return Ratings(confirmations=[], flags=[])
        return Ratings(confirmations=list_confirmations(), flags=list_feedback())
    except Exception as exc:  # noqa: BLE001 - a report must render without its ratings
        return Ratings(confirmations=None, flags=None, error=type(exc).__name__)


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _fmt_stats(stats: dict[str, Any]) -> str:
    if not stats or not stats.get("count"):
        return "(no data)"
    return f"n={stats['count']:<6} p50={stats['p50']:.0f}ms  p95={stats['p95']:.0f}ms"


def _render_metrics(label: str, m: dict[str, Any], indent: str = "  ") -> list[str]:
    ratings = m["ratings"]
    lines = [f"{indent}[{label}]  questions={m['questions']}"]
    pad = indent + "  "
    lines.append(f"{pad}execution success   : {_fmt_rate(m['execution_success_rate'])} ({m['executed']})")
    lines.append(f"{pad}guard rejection     : {_fmt_rate(m['guard_rejection_rate'])} ({m['guard_rejected']})")
    lines.append(f"{pad}needed correction   : {_fmt_rate(m['needed_correction_rate'])} ({m['needed_correction']})")
    lines.append(f"{pad}empty result        : {_fmt_rate(m['empty_result_rate'])} ({m['empty_results']} of {m['executed']} executed)")
    lines.append(f"{pad}out of scope        : {_fmt_rate(m['out_of_scope_rate'])} ({m['out_of_scope']})")
    lines.append(f"{pad}errors              : {_fmt_rate(m['error_rate'])} ({m['errors']})")
    for bucket, entry in m["error_rate_by_class"].items():
        lines.append(f"{pad}  {bucket:<18} {_fmt_rate(entry['rate'])} ({entry['count']})")
    if ratings["available"]:
        lines.append(
            f"{pad}confirmed / flagged : {ratings['confirmed']} / {ratings['flagged']}  "
            f"confirmed share of rated = {_fmt_rate(ratings['confirmed_share_of_rated'])} "
            "(lower bound, NOT accuracy)"
        )
    else:
        lines.append(f"{pad}confirmed / flagged : unavailable (application database not read)")
    latency = m["latency"]
    lines.append(f"{pad}latency total       : {_fmt_stats(latency['total_ms'])}")
    for stage, stats in latency["by_stage_ms"].items():
        lines.append(f"{pad}  {stage:<10} {_fmt_stats(stats)}")
    split = latency["llm_split"]
    lines.append(f"{pad}  llm ttft       {_fmt_stats(split['ttft_ms'])}")
    lines.append(f"{pad}  llm generation {_fmt_stats(split['generation_ms'])}")
    by_cache = latency.get("llm_by_cache")
    if by_cache:
        for name in ("hit", "miss", "unreported"):
            lines.append(
                f"{pad}  llm stage, cache {name:<10} {_fmt_stats(by_cache[name]['llm_ms'])}"
            )
    return lines


def render_quality_text(report: dict[str, Any]) -> str:
    """The report as plain text for a terminal: counts and percentiles only.

    No question text, SQL or error message appears in it -- the report
    carries none -- so it keeps ``analyze_audit_log.py``'s default privacy
    mode.

    Parameters
    ----------
    report:
        From :func:`build_quality_report`.

    Returns
    -------
    str

    Examples
    --------
    >>> text = render_quality_text(build_quality_report([]))
    >>> "Real-use quality" in text and "not accuracy" in text
    True
    """
    lines = ["Real-use quality (audit log + analyst confirmations and flags)", "=" * 60]
    sections = (
        (f"last {report['window_days']} days", report["recent"]),
        ("all time", report["all_time"]),
    )
    for title, section in sections:
        lines.append(f"{title}  ({section['since'] or 'beginning'} .. {section['until']})")
        lines.extend(_render_metrics("overall", section["overall"]))
        for name, metrics in section["by_datasource"].items():
            lines.extend(_render_metrics(f"source: {name}", metrics))
        lines.append("")
    lines.append(f"note: {report['caveats']['confirmed_share']}")
    lines.append(f"note: {report['caveats']['latency_stages']}")
    lines.append("=" * 60)
    return "\n".join(lines)
