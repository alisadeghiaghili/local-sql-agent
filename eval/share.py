# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The share-safe results bundle of ``eval.cli run --share``.

An operator runs the evaluation on a server the reviewer cannot reach, then
sends the reviewer one file. This module builds that file from a finished
``--live --reference live`` run: enough to see *what* failed and *how* (the
question, both SQL texts, the shape of both results, the error class), and
nothing that identifies a person or exposes a stored value.

What is in the bundle
---------------------
* a header: ``format_version``, the application version, ``prefix_version``,
  ``run.pipeline`` (``production``: the server's own correction loop, or
  ``single_shot``),
  the model name, the golden file's *name* and hash, the
  ``PROMPT_RETRIEVAL_TOKEN_BUDGET``, the prompt path (static or retrieval)
  of each data source, the run's start and end, and a ``privacy`` block that
  states what is and is not inside;
* per case: the id, tags, question, the reference and generated SQL, the
  status, both data sources, the guard's rejections and run failures round by
  round, the correction count, the latency and a mismatch diagnosis (:mod:`eval.diagnosis`: a
  class, row and column counts, column *names*, and for a single number a
  coarse error bucket);
* a summary: execution accuracy overall, per tag and per data source, the
  distribution of mismatch classes, correction rounds and latency
  percentiles.

What is never in it
-------------------
Result rows or cell values, credentials, ``.env`` values, endpoint URLs and
hosts, API keys, principal ids, a case's ``notes`` and its recorded rows.
Error text passes through :func:`core.redaction.redact_error_text` (quoted
literals become ``'…'``, runs of four or more digits become ``#``). The SQL
texts are verbatim by default, because diagnosing a wrong filter needs the
filter; ``redact_literals`` replaces every string and numeric literal in
both with a placeholder (:func:`core.redaction.redact_sql_literals`).

Two things stay readable and are said so in the bundle's ``privacy`` block:
the **question** (it is what the operator's users typed) and the **column
names** of the results (schema, but a ``PIVOT`` turns values into names).

The file is UTF-8, keys sorted, cases sorted by id, so two bundles diff
cleanly. It is written owner-only, atomically, and never inside the
repository's tracked tree (:func:`check_share_path`).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.redaction import redact_error_text, redact_sql_literals
from eval.diagnosis import mismatch_class_for
from eval.models import MISMATCH_CLASSES, CaseResult, EvalReport, GoldenCase

__all__ = [
    "FORMAT_VERSION",
    "build_share_bundle",
    "check_share_path",
    "golden_file_info",
    "privacy_block",
    "render_share_json",
    "write_share_bundle",
]

#: Bumped when a key is removed or changes meaning (adding one does not).
FORMAT_VERSION = 1

#: The directory under the repository root that ``.gitignore`` excludes and
#: that holds a deployment's evaluation data; the only place inside the
#: checkout a share file may be written.
IGNORED_DATA_DIR = "eval_data"

def golden_file_info(path: str | Path) -> dict[str, Any]:
    """The golden file's base name and a hash of its content.

    The hash is SHA-256 of the text with line endings normalised to ``\\n``,
    so the same file checked out on Windows and on Linux hashes the same.
    Only the base name is kept: a directory path says where on the server
    the data lives.

    Examples
    --------
    >>> import tempfile
    >>> d = Path(tempfile.mkdtemp())
    >>> _ = (d / "g.jsonl").write_bytes(b"a\\r\\nb\\r\\n")
    >>> info = golden_file_info(d / "g.jsonl")
    >>> info["file_name"], info["sha256"] == hashlib.sha256(b"a\\nb\\n").hexdigest()
    ('g.jsonl', True)
    """
    p = Path(path)
    text = p.read_bytes().replace(b"\r\n", b"\n")
    return {"file_name": p.name, "sha256": hashlib.sha256(text).hexdigest()}


def privacy_block(*, redact_literals: bool, detail_ids_only: bool = False) -> dict[str, Any]:
    """The ``privacy`` header: exactly what is, and is not, inside the bundle.

    Examples
    --------
    >>> privacy_block(redact_literals=False)["sql_literals"]
    'verbatim'
    >>> privacy_block(redact_literals=True)["sql_literals"]
    'redacted'
    >>> "detail" in privacy_block(redact_literals=False, detail_ids_only=True)
    True
    """
    block = {
        "contains": [
            "case ids, tags and the question text exactly as written in the golden file",
            "the reference SQL and the generated SQL",
            "status, data sources, guard rejection reasons, correction counts, latency",
            "per case: a mismatch class, row counts, column names and, for a single "
            "numeric cell, an error bucket (<1%, <10%, >=10%)",
            "error messages with quoted literals and long digit runs removed",
            "the application version, prompt prefix version, model name, golden file "
            "name and hash",
        ],
        "does_not_contain": [
            "result rows or any cell value, from either query",
            "credentials, .env values, API keys, endpoint URLs or hosts",
            "principal ids or user names",
            "a case's notes or recorded rows",
        ],
        "sql_literals": "redacted" if redact_literals else "verbatim",
        "sql_literals_note": (
            "string and numeric literals in both SQL texts are replaced by :str and "
            ":num (ORDER BY / GROUP BY positions are kept)"
            if redact_literals
            else "the SQL texts are kept verbatim, filters and literal values included; "
            "run with --share-redact-literals to replace them"
        ),
        "error_text": (
            "quoted string literals are replaced by '…' and digit runs of four or more "
            "by #; cut at 600 characters"
        ),
        "questions": "verbatim: the question is what a user typed and may name real entities",
        "column_names": (
            "result column names are exported; they are schema, but a PIVOT query turns "
            "values into column names"
        ),
    }
    if detail_ids_only:
        block["detail"] = (
            "written with --share-only-ids: only the listed case ids carry the question, "
            "SQL, error text and diagnosis above; every other case carries only case_id, "
            "tags, passed and mismatch_class, and the summary still counts all of them"
        )
    return block


def check_share_path(
    path: str | Path, *, repo_root: str | Path | None = None, what: str = "share file"
) -> str | None:
    """Why *path* may not be written, or ``None`` when it may.

    A share file holds real questions and SQL, so it must never land where
    ``git add .`` would pick it up. Allowed: any path outside the repository
    checkout, and any path under its git-ignored ``eval_data/``. Refused: a
    directory, and everything else inside the checkout (``docs/``,
    ``eval_data.example/``, the root, ...). The decision looks only at the
    resolved path, so it needs neither git nor a ``.gitignore`` parser.

    Args:
        path: The requested output path.
        repo_root: The checkout root; the directory above ``eval/`` by default.
        what: What is being written, for the message ("share file", "split files").

    Returns:
        A one-line reason, or ``None``.

    Examples:
        >>> import tempfile
        >>> check_share_path(Path(tempfile.gettempdir()) / "round-1.json", repo_root="/srv/app")
        >>> check_share_path("/srv/app/eval_data/share/round-1.json", repo_root="/srv/app")
        >>> check_share_path("/srv/app/docs/round-1.json", repo_root="/srv/app")
        "refusing to write the share file inside the repository's tracked tree (docs/round-1.json): use a path under eval_data/ or outside the checkout"
    """
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    target = Path(path).expanduser()
    try:
        resolved = target.resolve()
        resolved_root = root.resolve()
    except (OSError, RuntimeError) as exc:
        return f"cannot resolve the share path: {type(exc).__name__}"
    if resolved.is_dir():
        return f"the {what} path is a directory, not a file: {target}"
    try:
        relative = resolved.relative_to(resolved_root)  # case-insensitive on Windows
    except ValueError:
        return None  # outside the checkout
    first = relative.parts[0] if relative.parts else ""
    if os.path.normcase(first) == os.path.normcase(IGNORED_DATA_DIR) and len(relative.parts) > 1:
        return None
    return (
        f"refusing to write the {what} inside the repository's tracked tree "
        f"({relative.as_posix()}): use a path under {IGNORED_DATA_DIR}/ or outside the checkout"
    )


def _sql(text: str | None, redact_literals: bool) -> str | None:
    if text is None:
        return None
    return redact_sql_literals(text) if redact_literals else text


def _error_text(result: CaseResult, redact_literals: bool) -> str | None:
    """The result's error text, redacted for a file that leaves the server."""
    if not result.error:
        return None
    if redact_literals and result.status == "missed_out_of_scope":
        # The runner's message repeats the generated SQL, literals and all.
        return "expected OUT_OF_SCOPE but generator returned SQL"
    return redact_error_text(result.error)


def _rounds(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per-round rejections or run failures: round, class, redacted message."""
    return [
        {
            "round": int(entry.get("round", 1)),
            "class": str(entry.get("class", "")),
            "message": redact_error_text(entry.get("message", "")),
        }
        for entry in entries
    ]


def _case_record(
    result: CaseResult, expected_sql: str | None, redact_literals: bool
) -> dict[str, Any]:
    diagnosis = result.diagnosis.to_dict() if result.diagnosis is not None else {
        "mismatch_class": None,
        "expected_rows": None,
        "actual_rows": None,
        "expected_columns": None,
        "actual_columns": None,
        "relative_error_bucket": None,
        "capped": False,
        "row_cap": None,
        "uncapped_rows": None,
        "reference_uncapped_rows": None,
        "capped_unverified": False,
    }
    diagnosis["mismatch_class"] = mismatch_class_for(
        result.status, result.passed,
        None if result.diagnosis is None else result.diagnosis.mismatch_class,
    )
    return {
        "case_id": result.case_id,
        "tags": list(result.tags),
        "question": result.question,
        "expected_sql": _sql(expected_sql, redact_literals),
        "generated_sql": _sql(result.generated_sql, redact_literals),
        "status": result.status,
        "passed": result.passed,
        "expected_datasource": result.expected_datasource,
        "selected_datasource": result.selected_datasource,
        "corrections": result.corrections,
        "guard_rejections": _rounds(result.guard_rejections),
        "execution_errors": _rounds(result.execution_errors),
        "error": _error_text(result, redact_literals),
        "latency_seconds": round(result.latency_seconds, 3),
        "diagnosis": diagnosis,
    }


def _pct(passed: int, total: int) -> float:
    return round(100.0 * passed / total, 2) if total else 0.0


def _summary(report: EvalReport, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    classes = {name: 0 for name in MISMATCH_CLASSES}
    unclassified = 0
    for record in records:
        name = record["diagnosis"]["mismatch_class"]
        if name is None:
            unclassified += 1
        else:
            classes[name] += 1
    if unclassified:
        classes["unclassified"] = unclassified
    corrections = [r["corrections"] for r in records]
    return {
        "total": report.total,
        "passed": report.passed,
        "execution_accuracy_pct": round(report.accuracy_pct, 2),
        "per_tag": {
            tag: {"passed": p, "total": t, "accuracy_pct": _pct(p, t)}
            for tag, (p, t) in sorted(report.tag_accuracy.items())
        },
        "per_datasource": {
            src: {"passed": p, "total": t, "accuracy_pct": _pct(p, t)}
            for src, (p, t) in sorted(report.source_accuracy.items())
        },
        "source_selection": (
            None
            if report.source_selection is None
            else {
                "correct": report.source_selection[0],
                "total": report.source_selection[1],
                "accuracy_pct": _pct(*report.source_selection),
            }
        ),
        "mismatch_classes": classes,
        "status_counts": dict(sorted(report.status_counts.items())),
        "correction_rounds": {
            "total": sum(corrections),
            "cases_with_corrections": sum(1 for c in corrections if c > 0),
        },
        "guard_rejections": sum(len(r["guard_rejections"]) for r in records),
        "execution_errors": sum(len(r["execution_errors"]) for r in records),
        "latency_seconds": {
            "p50": round(report.latency_p50, 3),
            "p95": round(report.latency_p95, 3),
        },
    }


def _pipeline_notes(pipeline: str | None) -> list[str]:
    if pipeline == "production":
        return [
            "pipeline production: the generated SQL came from the server's own SQLAgent "
            "loop (guard, correction rounds with hints, row cap); corrections, "
            "guard_rejections and execution_errors are per round and real",
            "no principal policy was applied: the evaluation passes no denied_columns",
        ]
    return [
        "pipeline single_shot: one model call per question, no correction rounds, so "
        "corrections is 0 by construction; a model-only figure, below what users get",
    ]


def build_share_bundle(
    report: EvalReport,
    cases: Sequence[GoldenCase],
    *,
    golden_path: str | Path,
    run_info: Mapping[str, Any],
    started_at: str,
    finished_at: str,
    redact_literals: bool = False,
    detail_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """Build the share bundle of a finished ``--live --reference live`` run.

    Parameters
    ----------
    report:
        The run's :class:`~eval.models.EvalReport`.
    cases:
        The golden cases that were loaded; a result's reference SQL is taken
        from the case with the same id (``CaseResult`` does not carry it).
    golden_path:
        The golden file; only its name and content hash are used.
    run_info:
        What the caller read from the live configuration: ``app_version``,
        ``prefix_version``, ``model``, ``prompt_retrieval_token_budget``,
        ``sources`` (``{name: "static" | "retrieval"}``), ``structured`` and
        ``float_tolerance``. Taken as given so this module needs no
        ``project_config/``.
    started_at, finished_at:
        ISO-8601 UTC timestamps of the run.
    redact_literals:
        Replace string and numeric literals in both SQL texts.
    detail_ids:
        When given (``--share-only-ids``), only these case ids carry the
        full record. Every other case is reduced to ``case_id``, ``tags``,
        ``passed`` and ``mismatch_class``: no question, SQL, error text or
        counts. The summary is computed from all cases first.

    Returns
    -------
    dict
        A JSON-serialisable mapping; see the module docstring.

    Raises
    ------
    ValueError
        If *report* is not a live run judged by ``--reference live``: the
        diagnosis it carries only exists for that combination. Also if it
        does not say which pipeline generated the SQL.

    Examples
    --------
    >>> import tempfile
    >>> from eval.models import CaseResult
    >>> from eval.report import build_report
    >>> d = Path(tempfile.mkdtemp()); _ = (d / "g.jsonl").write_text("{}\\n", encoding="utf-8")
    >>> case = GoldenCase(id="a", question="how many?", expected_sql="SELECT 1")
    >>> rep = build_report([CaseResult("a", "how many?", ["t"], "pass", "SELECT 1", "f", None, 0.1)],
    ...                    mode="live", reference="live", pipeline="production")
    >>> info = {"app_version": "1", "prefix_version": "p", "model": "m",
    ...         "prompt_retrieval_token_budget": 6000, "sources": {}, "structured": False,
    ...         "float_tolerance": 1e-06}
    >>> b = build_share_bundle(rep, [case], golden_path=d / "g.jsonl", run_info=info,
    ...                        started_at="t0", finished_at="t1")
    >>> b["format_version"], b["summary"]["execution_accuracy_pct"], b["cases"][0]["case_id"]
    (1, 100.0, 'a')
    """
    if report.mode != "live" or report.reference != "live":
        raise ValueError(
            "a share bundle needs a --live run judged with --reference live "
            f"(this report is mode={report.mode!r}, reference={report.reference!r})"
        )
    if report.pipeline not in ("production", "single_shot"):
        raise ValueError(
            "a share bundle must say which pipeline generated the SQL "
            f"(this report has pipeline={report.pipeline!r})"
        )
    expected_by_id = {c.id: c.expected_sql for c in cases}
    records = [
        _case_record(result, expected_by_id.get(result.case_id), redact_literals)
        for result in sorted(report.results, key=lambda r: r.case_id)
    ]
    summary = _summary(report, records)
    full_cases = len(records)
    if detail_ids is not None:
        keep = set(detail_ids)
        full_cases = sum(1 for r in records if r["case_id"] in keep)
        records = [
            r if r["case_id"] in keep else {
                "case_id": r["case_id"],
                "tags": r["tags"],
                "passed": r["passed"],
                "mismatch_class": r["diagnosis"]["mismatch_class"],
            }
            for r in records
        ]
    golden = golden_file_info(golden_path)
    golden["cases_in_file"] = len(cases)
    golden["cases_run"] = len(report.results)
    model = str(run_info.get("model", ""))
    if "://" in model:  # never a URL, whatever the configuration holds
        model = model.rsplit("/", 1)[-1]
    return {
        "format_version": FORMAT_VERSION,
        "app_version": str(run_info.get("app_version", "")),
        "prefix_version": str(run_info.get("prefix_version", "")),
        "model": model,
        "golden": golden,
        "prompt_retrieval_token_budget": run_info.get("prompt_retrieval_token_budget"),
        "sources": {
            name: {"path": path}
            for name, path in sorted(dict(run_info.get("sources") or {}).items())
        },
        "run": {
            "mode": report.mode,
            "reference": report.reference,
            "pipeline": report.pipeline,
            "structured": bool(run_info.get("structured", False)),
            "float_tolerance": run_info.get("float_tolerance"),
            "started_at": started_at,
            "finished_at": finished_at,
        },
        "detail": {
            "scope": "all" if detail_ids is None else "listed_ids",
            "cases_with_detail": full_cases,
            "cases_total": len(records),
        },
        "notes": _pipeline_notes(report.pipeline) + [
            "mismatch_class is computed on the server from the two result frames; "
            "no value leaves it",
        ],
        "privacy": privacy_block(
            redact_literals=redact_literals, detail_ids_only=detail_ids is not None
        ),
        "summary": summary,
        "cases": records,
    }


def render_share_json(bundle: Mapping[str, Any]) -> str:
    """The bundle as stable text: UTF-8-safe, keys sorted, one trailing newline.

    Examples
    --------
    >>> render_share_json({"b": 1, "a": "چ"})
    '{\\n  "a": "چ",\\n  "b": 1\\n}\\n'
    """
    return json.dumps(bundle, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def write_share_bundle(bundle: Mapping[str, Any], path: str | Path) -> None:
    """Write *bundle* to *path*: owner-only, atomically, UTF-8 with ``\\n`` line ends.

    The path is checked again with :func:`check_share_path`; a caller that
    already did gets the same answer.

    Raises
    ------
    ValueError
        If the path is not allowed.
    """
    from eval.store import write_text_atomic

    problem = check_share_path(path)
    if problem is not None:
        raise ValueError(problem)
    write_text_atomic(path, render_share_json(bundle))
