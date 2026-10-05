# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Review harvested golden-case candidates in Excel, then bring the verdicts back.

Analysts who are not developers can judge "is this SQL right for this
question?" in a spreadsheet. This script moves candidates there and back::

    python scripts/golden_sheet.py export \\
        --cases eval_data/candidates.jsonl --out eval_data/review.csv
    #   ... analysts fill in `verdict` (and `correct_sql` where it is wrong) ...
    python scripts/golden_sheet.py import \\
        --sheet eval_data/review.csv --candidates eval_data/candidates.jsonl \\
        --golden eval_data/golden.jsonl
    python -m eval.cli verify --golden eval_data/golden.jsonl --accept

The sheet
---------
``export`` writes a CSV, UTF-8 **with a byte-order mark** so Excel shows
Persian text correctly when the file is opened by double-clicking, with
the columns

``id``
    The case id. Do not edit.
``question``
    The question as users asked it. For reading; the question stored on
    import is the one in the candidates file when the id is found there.
``proposed_sql``
    The system's own SQL for it (blank when there is none).
``correct_sql``
    Blank. Fill in the right SQL when the proposal is wrong or missing.
``verdict``
    Blank. ``correct`` (the proposal is right), ``wrong`` (use
    ``correct_sql`` instead) or ``skip`` (not a usable test question: drop
    it). A blank verdict means "not reviewed yet": the row is left alone,
    so a sheet can be imported in several rounds.
``expect``
    ``success`` (default), ``empty`` (the right answer is a zero-row
    result) or ``out_of_scope`` (the system should decline; no SQL).
``datasource``
    The data source the answer lives in (e.g. ``sales``), optional.
``notes``
    Where the case came from; add your own remarks.

Every text cell that would open as a formula in Excel (starts with ``=``,
``+``, ``-``, ``@``) is written with a leading ``'`` --
:func:`exporters.sanitize.defuse_formula`, the same defence the result
exports use -- and the import removes it again.

Import rules
------------
* ``correct`` -> ``expected_sql`` is the proposed SQL (``expect`` of
  ``out_of_scope`` needs none).
* ``wrong`` + ``correct_sql`` -> ``expected_sql`` is ``correct_sql`` (and a
  candidate that was marked ``out_of_scope`` becomes ``success``).
* ``skip`` -> the case is dropped.
* Every SQL goes through :func:`security.sql_guard.validate_sql`; a row
  with a problem is reported **by spreadsheet row number** and not
  imported, the others are. Fix the cell and import again: rows already
  imported unchanged are recognised and skipped.
* Imported cases get ``status = "reviewed"``: still not run by the
  regression gate until ``python -m eval.cli verify --accept`` has executed
  their SQL against the database.

The sheet and the candidates file contain real questions; both stay on the
server (``eval_data/`` is git-ignored). This script prints counts, row
numbers and ids, never question text.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.persian import normalize_for_matching
from eval.models import GoldenCase
from eval.store import load_cases_or_empty, write_golden_cases, write_text_atomic
from exporters.sanitize import defuse_formula
from security.sql_guard import validate_sql

#: The sheet's columns, in order.
COLUMNS: tuple[str, ...] = (
    "id", "question", "proposed_sql", "correct_sql", "verdict", "expect", "datasource", "notes",
)

_VERDICTS = ("correct", "wrong", "skip")
_EXPECTATIONS = ("success", "empty", "out_of_scope")
_BOM = "﻿"


def refuse_formula(value: str) -> str:
    """Undo :func:`exporters.sanitize.defuse_formula` on a cell read back.

    Only a leading ``'`` that was put there to defuse a formula (one
    followed by ``=``, ``+``, ``-``, ``@``, tab or carriage return) is
    removed.

    Examples
    --------
    >>> refuse_formula("'=SUM(A1)"), refuse_formula("'quoted"), refuse_formula("plain")
    ('=SUM(A1)', "'quoted", 'plain')
    """
    if value.startswith("'") and len(value) > 1 and value[1] in "=+-@\t\r":
        return value[1:]
    return value


def render_sheet(cases: Sequence[GoldenCase]) -> str:
    """The CSV text (with BOM, CRLF line ends) for *cases*.

    Parameters
    ----------
    cases:
        The cases to review.

    Returns
    -------
    str

    Examples
    --------
    >>> case = GoldenCase(id="c1", question="چند مشتری؟", expected_sql="SELECT 1",
    ...                   status="pending_review", datasource="sales", notes="=x")
    >>> text = render_sheet([case])
    >>> text.startswith("\\ufeffid,question,proposed_sql,correct_sql,verdict,expect,datasource,notes")
    True
    >>> "چند مشتری؟" in text and "'=x" in text
    True
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for case in cases:
        writer.writerow([
            defuse_formula(case.id),
            defuse_formula(case.question),
            defuse_formula(case.expected_sql or ""),
            "",
            "",
            case.expect,
            case.datasource or "",
            defuse_formula(case.notes),
        ])
    return _BOM + buffer.getvalue()


def _detect_delimiter(header_line: str) -> str:
    """Excel in some locales saves ``;`` or tab instead of ``,``."""
    counts = {d: header_line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


@dataclass(slots=True)
class ImportReport:
    """What :func:`import_sheet` did.

    Parameters
    ----------
    imported:
        Cases newly added (status ``"reviewed"``), in sheet order.
    counts:
        ``{"correct": n, "wrong": n, "skipped": n, "unreviewed": n,
        "already_imported": n, "blank_rows": n}``.
    problems:
        ``(spreadsheet row number, case id, message)`` for every row that
        was not imported because of a problem. Row 1 is the header.
    """

    imported: list[GoldenCase] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    problems: list[tuple[int, str, str]] = field(default_factory=list)


def read_sheet(path: str | Path) -> list[dict[str, str]]:
    """Read a review sheet (UTF-8, with or without BOM) into row dicts.

    Parameters
    ----------
    path:
        The CSV file.

    Returns
    -------
    list[dict[str, str]]
        One dict per data row (header names stripped and lower-cased; every
        value stripped of the formula-defusing apostrophe), plus the key
        ``"_row"`` holding the spreadsheet row number as a string. Rows whose
        cells are all empty are skipped.

    Raises
    ------
    ValueError
        If the file is not UTF-8 (Excel's plain "CSV" save mangles Persian;
        use "CSV UTF-8") or lacks a required column.

    Examples
    --------
    >>> import tempfile
    >>> p = Path(tempfile.mkdtemp()) / "s.csv"
    >>> _ = p.write_text(render_sheet([GoldenCase(id="a", question="q", expected_sql="SELECT 1",
    ...                                           status="pending_review")]), encoding="utf-8", newline="")
    >>> rows = read_sheet(p)
    >>> rows[0]["id"], rows[0]["proposed_sql"], rows[0]["_row"]
    ('a', 'SELECT 1', '2')
    """
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path}: not UTF-8. In Excel use File > Save As > 'CSV UTF-8 (Comma delimited)' "
            "so Persian text survives."
        ) from exc
    first_line = text.split("\n", 1)[0]
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_detect_delimiter(first_line))
    try:
        header = [h.strip().lower() for h in next(reader)]
    except StopIteration:
        raise ValueError(f"{path}: empty file") from None
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        raise ValueError(f"{path}: missing column(s): {', '.join(missing)}")
    rows: list[dict[str, str]] = []
    for number, cells in enumerate(reader, start=2):
        if not any(cell.strip() for cell in cells):
            # A blank line, or a row Excel kept with every cell empty: not a
            # case. Skipped without renumbering, so "_row" stays the row
            # number the analyst sees in the spreadsheet.
            continue
        row = {name: refuse_formula(cells[i].strip()) if i < len(cells) else ""
               for i, name in enumerate(header)}
        row["_row"] = str(number)
        rows.append(row)
    return rows


def import_sheet(
    rows: Sequence[dict[str, str]],
    candidates: Sequence[GoldenCase],
    existing: Sequence[GoldenCase],
) -> ImportReport:
    """Apply the verdicts in *rows* (see the module docstring's import rules).

    Parameters
    ----------
    rows:
        From :func:`read_sheet`.
    candidates:
        The candidates file's cases, for each id's question, tags and
        datasource (may be empty).
    existing:
        The golden set so far (every status).

    Returns
    -------
    ImportReport

    Examples
    --------
    >>> cand = GoldenCase(id="c1", question="How many customers?", tags=["lang:en"],
    ...                   expected_sql="SELECT COUNT(*) AS n FROM Customer", status="pending_review")
    >>> row = {"_row": "2", "id": "c1", "question": "", "proposed_sql": cand.expected_sql,
    ...        "correct_sql": "", "verdict": "correct", "expect": "success",
    ...        "datasource": "sales", "notes": ""}
    >>> rep = import_sheet([row], [cand], [])
    >>> [(c.id, c.status, c.datasource, c.tags) for c in rep.imported], rep.problems
    ([('c1', 'reviewed', 'sales', ['lang:en'])], [])
    >>> bad = dict(row, verdict="wrong", correct_sql="DELETE FROM Customer")
    >>> import_sheet([bad], [cand], []).problems[0][:2]
    (2, 'c1')
    """
    report = ImportReport()
    by_id = {c.id: c for c in candidates}
    existing_by_id = {c.id: c for c in existing}
    taken_questions = {normalize_for_matching(c.question): c.id for c in existing}
    seen_ids: set[str] = set()

    for row in rows:
        number = int(row["_row"])
        case_id = row.get("id", "")
        if not any(v for k, v in row.items() if k != "_row"):
            report.counts["blank_rows"] += 1
            continue

        def problem(message: str) -> None:
            report.problems.append((number, case_id or "(no id)", message))

        if not case_id:
            problem("id is empty")
            continue
        if case_id in seen_ids:
            problem("id appears twice in the sheet")
            continue
        seen_ids.add(case_id)

        verdict = row.get("verdict", "").strip().lower()
        if not verdict:
            report.counts["unreviewed"] += 1
            continue
        if verdict not in _VERDICTS:
            problem(f"unknown verdict {verdict!r} (use correct / wrong / skip)")
            continue
        if verdict == "skip":
            report.counts["skipped"] += 1
            continue

        expect = (row.get("expect") or "").strip().lower() or "success"
        if expect not in _EXPECTATIONS:
            problem(f"unknown expect {expect!r} (use success / empty / out_of_scope)")
            continue
        proposed = row.get("proposed_sql", "").strip()
        correct_sql = row.get("correct_sql", "").strip()

        sql: str | None
        if verdict == "correct":
            if correct_sql:
                problem("correct_sql is filled in but the verdict is 'correct'; use 'wrong' to replace the proposal")
                continue
            if expect == "out_of_scope":
                sql = None
            elif not proposed:
                problem(
                    "verdict is 'correct' but proposed_sql is empty; give the SQL in "
                    "correct_sql with verdict 'wrong'"
                )
                continue
            else:
                sql = proposed
        else:
            if correct_sql:
                sql = correct_sql
                if expect == "out_of_scope":
                    expect = "success"
            elif expect == "out_of_scope":
                sql = None
            else:
                problem("verdict is 'wrong' but correct_sql is empty")
                continue

        if sql is not None:
            try:
                validate_sql(sql)
            except ValueError as exc:
                problem(f"SQL rejected by the guard: {str(exc).splitlines()[0][:200]}")
                continue

        candidate = by_id.get(case_id)
        question = candidate.question if candidate is not None else row.get("question", "")
        if not question:
            problem("no question: the id is not in the candidates file and the question cell is empty")
            continue
        tags = list(candidate.tags) if candidate is not None else []
        datasource = row.get("datasource", "") or (candidate.datasource if candidate else None) or None
        notes = row.get("notes", "") or (candidate.notes if candidate else "")

        earlier = existing_by_id.get(case_id)
        if earlier is not None:
            if earlier.question == question and earlier.expected_sql == sql:
                report.counts["already_imported"] += 1
            else:
                problem("id already exists in the golden set with different content")
            continue
        owner = taken_questions.get(normalize_for_matching(question))
        if owner is not None:
            problem(f"the same question is already in the golden set (id {owner})")
            continue

        try:
            case = GoldenCase(
                id=case_id,
                question=question,
                tags=tags,
                expected_sql=sql,
                expect=expect,  # type: ignore[arg-type]
                notes=(notes + " " if notes else "") + "[reviewed via sheet]",
                status="reviewed",
                datasource=datasource,
            )
        except ValueError as exc:
            problem(str(exc))
            continue
        report.imported.append(case)
        report.counts[verdict] += 1
        taken_questions[normalize_for_matching(question)] = case_id
    return report


def render_import_report(report: ImportReport, *, written: bool, golden: str) -> str:
    """Counts, row numbers and ids -- never question text.

    Examples
    --------
    >>> rep = ImportReport(counts=Counter({"correct": 2}), problems=[(5, "c9", "id is empty")])
    >>> print(render_import_report(rep, written=False, golden="g.jsonl"))
    imported: 0 (correct 2, wrong 0), skipped (verdict skip): 0, not reviewed yet: 0, already imported: 0
    1 row(s) with a problem, not imported:
      row 5 (c9): id is empty
    Nothing written to g.jsonl.
    """
    c = report.counts
    lines = [
        f"imported: {len(report.imported)} (correct {c['correct']}, wrong {c['wrong']}), "
        f"skipped (verdict skip): {c['skipped']}, not reviewed yet: {c['unreviewed']}, "
        f"already imported: {c['already_imported']}",
    ]
    if report.problems:
        lines.append(f"{len(report.problems)} row(s) with a problem, not imported:")
        for number, case_id, message in report.problems:
            lines.append(f"  row {number} ({case_id}): {message}")
    lines.append(
        f"Wrote {golden} (previous version kept as .bak); run 'python -m eval.cli verify "
        f"--golden {golden} --accept' next."
        if written else f"Nothing written to {golden}."
    )
    return "\n".join(lines)


def _export(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists() and not args.force:
        print(
            f"{out} already exists and may hold an analyst's work; refusing to overwrite "
            "it (use --force).",
            file=sys.stderr,
        )
        return 2
    cases = load_cases_or_empty(args.cases)
    reviewable = [c for c in cases if c.status in ("pending_review", "pending_expected")]
    if not reviewable:
        print(f"No pending_review / pending_expected cases in {args.cases}.", file=sys.stderr)
        return 1
    write_text_atomic(out, render_sheet(reviewable), backup=True)
    print(f"Wrote {len(reviewable)} row(s) to {out} (UTF-8 with BOM; open it in Excel).")
    return 0


def _import(args: argparse.Namespace) -> int:
    try:
        rows = read_sheet(args.sheet)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    candidates = load_cases_or_empty(args.candidates) if args.candidates else []
    existing = load_cases_or_empty(args.golden)
    report = import_sheet(rows, candidates, existing)
    written = False
    if report.imported and not args.dry_run:
        write_golden_cases(args.golden, [*existing, *report.imported], backup=True)
        written = True
    print(render_import_report(report, written=written, golden=args.golden))
    return 1 if report.problems else 0


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Returns the process exit code.

    ``0`` on success, ``1`` when a row had a problem (``import``) or there
    was nothing to export, ``2`` for an unreadable sheet or a refused
    overwrite.
    """
    parser = argparse.ArgumentParser(
        description="Export harvested candidates to a CSV for Excel review and import the verdicts."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Write the review sheet (CSV, UTF-8 with BOM).")
    export.add_argument("--cases", default="eval_data/candidates.jsonl",
                        help="Candidates .jsonl (default eval_data/candidates.jsonl).")
    export.add_argument("--out", default="eval_data/review.csv",
                        help="Sheet to write (default eval_data/review.csv).")
    export.add_argument("--force", action="store_true", help="Overwrite an existing sheet.")
    export.set_defaults(func=_export)

    imp = sub.add_parser("import", help="Apply the verdicts in a reviewed sheet to the golden set.")
    imp.add_argument("--sheet", default="eval_data/review.csv", help="The reviewed CSV.")
    imp.add_argument("--candidates", default="eval_data/candidates.jsonl",
                     help="Candidates .jsonl, for each id's question and tags.")
    imp.add_argument("--golden", default="eval_data/golden.jsonl",
                     help="Golden set to add the reviewed cases to (created if missing).")
    imp.add_argument("--dry-run", action="store_true", dest="dry_run",
                     help="Report only; do not write the golden set.")
    imp.set_defaults(func=_import)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
