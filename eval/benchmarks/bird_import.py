# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``python -m eval.cli bench-import bird``: load BIRD mini-dev and write its golden files.

One import does, in order:

1. locate the downloaded package and read the questions;
2. refuse before writing anything if a target schema already holds tables
   (unless ``--replace``), then copy each selected SQLite database into one
   schema of the target SQL Server database, named after the ``db_id``, and
   check that every table has the same row count as the source;
3. run every question's SQLite reference on the original file, transpile it,
   run it on the copy and decide whether the case is scored
   (:mod:`eval.benchmarks.bird`);
4. write ``golden.jsonl`` (and, with ``--with-evidence``,
   ``golden.with_evidence.jsonl``), overall and per database;
5. draft a ``project_config/`` per database and one for all of them
   (:mod:`eval.benchmarks.bird_config`);
6. write ``import_report.json`` / ``import_report.txt`` listing every excluded case.

The target login needs DDL rights, which the application's login must never
have; the load therefore takes its own URL (``--target-url``), and the drafts
are read through the application's login. Output directory::

    <out>/golden.jsonl  golden.with_evidence.jsonl  import_report.json  import_report.txt  NOTICE.txt
    <out>/<db_id>/golden.jsonl  golden.with_evidence.jsonl  project_config/
    <out>/_combined/project_config/
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from eval.benchmarks.bird import (
    BENCH_NAME,
    DEFAULT_QUERY_TIMEOUT,
    EXCLUSION_REASONS,
    BirdQuestion,
    BirdSource,
    Tally,
    build_cases,
    load_questions,
    locate_source,
    open_connections,
    read_descriptions,
)
from eval.benchmarks.bird_config import InspectFn, default_base_config, draft_config
from eval.benchmarks.sqlite_load import (
    DEFAULT_CHUNK,
    LoadReport,
    SchemaNotEmpty,
    load_sqlite_schema,
    schema_is_empty,
    verify_loaded,
)
from eval.models import GoldenCase
from eval.store import write_golden_cases, write_text_atomic

__all__ = [
    "ATTRIBUTION",
    "ImportOptions",
    "ImportReport",
    "LoadMismatch",
    "render_import_report",
    "run_import",
]

#: Carried in the import report and in ``NOTICE.txt`` of the output directory.
ATTRIBUTION = (
    "Derived from BIRD mini-dev (https://github.com/bird-bench/mini_dev, "
    "https://huggingface.co/datasets/birdsql/bird_mini_dev), released under CC BY-SA 4.0 "
    "(https://creativecommons.org/licenses/by-sa/4.0/). The questions, SQL and databases "
    "belong to the BIRD authors; files in this directory are adapted material and, if "
    "shared, must carry this attribution and the same licence."
)

#: The notice every benchmark figure is read with.
NOT_THE_DEPLOYMENT = (
    "This is a public benchmark loaded into your own SQL Server. Its accuracy is NOT this "
    "deployment's accuracy and does not replace the private golden set."
)


class LoadMismatch(ValueError):
    """A table on the target does not hold as many rows as its source."""


@dataclass(frozen=True, slots=True)
class ImportOptions:
    """What ``bench-import bird`` was asked to do.

    Parameters
    ----------
    source, out:
        The downloaded package and the output directory.
    dbs:
        Databases to import (default: every database that has questions).
    replace:
        Drop and reload a schema that already holds tables.
    skip_load:
        Do not load; the schemas are already on the target. Their row counts
        are still compared with the source.
    descriptions:
        Add the dataset's column descriptions to each ``schema.yaml``.
    with_evidence:
        Also write the evidence-hint variant of the golden files.
    keep_unverified:
        Score questions whose load fidelity could not be checked.
    skip_config:
        Do not draft any ``project_config/``.
    base_config:
        Where ``system_prompt.md`` and the policies come from.
    max_rows:
        The server's row cap; a reference that reaches it is excluded.
    query_timeout:
        Seconds a reference may run, on SQLite and on the copy.
    chunk_size:
        Rows per insert batch.
    """

    source: Path
    out: Path
    dbs: tuple[str, ...] | None = None
    replace: bool = False
    skip_load: bool = False
    descriptions: bool = False
    with_evidence: bool = False
    keep_unverified: bool = False
    skip_config: bool = False
    base_config: Path | None = None
    max_rows: int = 1000
    query_timeout: float = DEFAULT_QUERY_TIMEOUT
    chunk_size: int = DEFAULT_CHUNK


@dataclass(slots=True)
class ImportReport:
    """What an import did; ``to_dict`` is ``import_report.json``."""

    options: dict[str, Any]
    loads: dict[str, LoadReport] = field(default_factory=dict)
    tallies: dict[str, Tally] = field(default_factory=dict)
    configs: dict[str, dict[str, object]] = field(default_factory=dict)
    generated_at: str = ""

    @property
    def scored(self) -> int:
        return sum(t.scored for t in self.tallies.values())

    @property
    def excluded(self) -> int:
        return sum(t.total - t.scored for t in self.tallies.values())

    def excluded_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for tally in self.tallies.values():
            for reason, items in tally.excluded.items():
                counts[reason] = counts.get(reason, 0) + len(items)
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark": BENCH_NAME,
            "notice": NOT_THE_DEPLOYMENT,
            "attribution": ATTRIBUTION,
            "generated_at": self.generated_at,
            "options": self.options,
            "totals": {
                "questions": self.scored + self.excluded,
                "scored": self.scored,
                "excluded": self.excluded,
                "excluded_by_reason": self.excluded_by_reason(),
            },
            "databases": {
                db: {
                    **tally.to_dict(),
                    "load": self.loads[db].to_dict() if db in self.loads else None,
                }
                for db, tally in sorted(self.tallies.items())
            },
            "configs": self.configs,
        }


def _check_limits(options: ImportOptions) -> None:
    if options.max_rows < 1 or options.chunk_size < 1 or options.query_timeout <= 0:
        raise ValueError("--max-rows, --chunk-size and --query-timeout must be positive")


def _select(source: BirdSource, questions: Sequence[BirdQuestion], dbs: Sequence[str] | None) -> list[str]:
    """The databases to import, validated against the package and the questions."""
    available = set(source.database_ids())
    asked = sorted({q.db_id for q in questions})
    if dbs:
        unknown = sorted(set(dbs) - set(asked))
        if unknown:
            raise ValueError(f"no question is about: {', '.join(unknown)} (the package has {', '.join(asked)})")
        wanted = sorted(set(dbs))
    else:
        wanted = asked
    missing = [d for d in wanted if d not in available]
    if missing:
        raise FileNotFoundError(
            "no SQLite file under dev_databases/ for: " + ", ".join(missing)
        )
    return wanted


def run_import(
    options: ImportOptions,
    engine: Engine,
    *,
    inspect: InspectFn | None = None,
    progress: Callable[[str], None] = print,
) -> ImportReport:
    """Run one import (see the module docstring).

    Parameters
    ----------
    options:
        What to do.
    engine:
        The target database, with a login that may create schemas and tables
        (:func:`eval.benchmarks.sqlite_load.create_target_engine`).
    inspect:
        Drafts a ``project_config/`` (:data:`~eval.benchmarks.bird_config.InspectFn`);
        required unless ``options.skip_config``.
    progress:
        Receives progress lines.

    Returns
    -------
    ImportReport

    Raises
    ------
    FileNotFoundError, ValueError
        For an unusable package, an unknown ``--dbs`` entry or a bad limit.
    SchemaNotEmpty
        If a target schema holds tables and neither ``replace`` nor
        ``skip_load`` was given. Raised before anything is written.
    LoadMismatch
        If a table's row count on the target differs from its source.
    """
    _check_limits(options)
    if inspect is None and not options.skip_config:
        raise ValueError("a schema inspector is needed unless skip_config is set")
    source = locate_source(options.source)
    questions = load_questions(source.questions_file)
    dbs = _select(source, questions, options.dbs)
    questions = [q for q in questions if q.db_id in dbs]
    report = ImportReport(
        options={
            "dbs": dbs, "replace": options.replace, "skip_load": options.skip_load,
            "descriptions": options.descriptions, "with_evidence": options.with_evidence,
            "keep_unverified": options.keep_unverified, "max_rows": options.max_rows,
            "query_timeout_seconds": options.query_timeout,
        },
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    # 1. Refuse before anything is written.
    if not options.skip_load and not options.replace:
        taken = [db for db in dbs if not schema_is_empty(engine, db)]
        if taken:
            raise SchemaNotEmpty(
                f"schema(s) {', '.join(repr(t) for t in taken)} of the target already hold tables; "
                "pass --replace to drop and reload them, or --skip-load to use them as they are"
            )

    # 2. Load (or verify what is there) and compare the row counts.
    for db in dbs:
        path = source.sqlite_path(db)
        if options.skip_load:
            progress(f"verifying schema {db} against {path.name}")
            load = verify_loaded(path, engine, db)
        else:
            progress(f"loading {path.name} into schema {db}")
            load = load_sqlite_schema(
                path, engine, db, replace=options.replace, chunk_size=options.chunk_size,
                progress=lambda message: progress(f"  {message}"),
            )
        report.loads[db] = load
    wrong = [
        f"{db}.{t.name} (source {t.source_rows}, target {t.target_rows})"
        for db, load in report.loads.items() for t in load.mismatched
    ]
    if wrong:
        raise LoadMismatch(
            "row counts on the target differ from the source: " + ", ".join(wrong[:10])
            + (f" and {len(wrong) - 10} more" if len(wrong) > 10 else "")
        )
    for load in report.loads.values():
        for warning in load.warnings:
            progress(f"  warning: {warning}")

    # 3. One decision per question.
    progress(f"checking {len(questions)} reference queries (SQLite, then the copy)")
    connections = open_connections(source, dbs)
    try:
        plain, hinted, report.tallies = build_cases(
            questions, connections, engine, max_rows=options.max_rows,
            keep_unverified=options.keep_unverified, timeout=options.query_timeout,
            progress=progress,
        )
    finally:
        for conn in connections.values():
            conn.close()

    # 4. Golden files.
    out = options.out
    _write_goldens(out, dbs, plain, hinted if options.with_evidence else None)

    # 5. Configurations.
    if not options.skip_config:
        assert inspect is not None
        base = options.base_config or default_base_config()
        lookup = (
            {db: read_descriptions(source.description_dir(db)) for db in dbs}
            if options.descriptions else None
        )
        for db in dbs:
            progress(f"drafting project_config for {db}")
            report.configs[db] = draft_config(
                out / db / "project_config", [db], inspect=inspect, base_config=base,
                descriptions=lookup,
            )
        if len(dbs) > 1:
            progress("drafting the combined project_config")
            report.configs["_combined"] = draft_config(
                out / "_combined" / "project_config", dbs, inspect=inspect, base_config=base,
                descriptions=lookup,
            )

    # 6. Report.
    write_text_atomic(out / "import_report.json", json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n")
    write_text_atomic(out / "import_report.txt", render_import_report(report) + "\n")
    write_text_atomic(out / "NOTICE.txt", ATTRIBUTION + "\n\n" + NOT_THE_DEPLOYMENT + "\n")
    return report


def _write_goldens(
    out: Path, dbs: Sequence[str], plain: Sequence[GoldenCase], hinted: Sequence[GoldenCase] | None
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    write_golden_cases(out / "golden.jsonl", plain)
    if hinted is not None:
        write_golden_cases(out / "golden.with_evidence.jsonl", hinted)
    for db in dbs:
        tag = f"db:{db}"
        write_golden_cases(out / db / "golden.jsonl", [c for c in plain if tag in c.tags])
        if hinted is not None:
            write_golden_cases(out / db / "golden.with_evidence.jsonl", [c for c in hinted if tag in c.tags])


def render_import_report(report: ImportReport) -> str:
    """The import report as text: counts, and every excluded case with its reason.

    Examples
    --------
    >>> r = ImportReport(options={}, generated_at="2026-01-01T00:00:00+00:00")
    >>> r.tallies["shop"] = Tally(scored=2, excluded={"load_fidelity": [("bird_3", "result values differ")]})
    >>> text = render_import_report(r)
    >>> "BENCHMARK IMPORT (bird)" in text and "load_fidelity" in text and "bird_3" in text
    True
    """
    lines = [
        f"BENCHMARK IMPORT ({BENCH_NAME}) -- {NOT_THE_DEPLOYMENT}",
        f"Generated {report.generated_at}",
        "",
        f"Questions: {report.scored + report.excluded}; scored {report.scored}; "
        f"excluded {report.excluded}",
    ]
    by_reason = report.excluded_by_reason()
    for reason in EXCLUSION_REASONS:
        if reason in by_reason:
            lines.append(f"  excluded, {reason}: {by_reason[reason]}")
    lines.append("")
    lines.append("Per database:")
    for db, tally in sorted(report.tallies.items()):
        extra = ", ".join(f"{r} {len(v)}" for r, v in sorted(tally.excluded.items()))
        lines.append(f"  {db:<28s} {tally.total:>4d} questions, scored {tally.scored:>4d}"
                     + (f", excluded: {extra}" if extra else ""))
    lines.append("")
    lines.append("Row counts (source = target for every table):")
    for db, load in sorted(report.loads.items()):
        rows = sum(t.target_rows for t in load.tables)
        lines.append(f"  {db:<28s} {len(load.tables):>3d} tables, {rows} rows"
                     + ("" if load.ok else "  MISMATCH"))
        for warning in load.warnings:
            lines.append(f"    warning: {warning}")
    lines.append("")
    lines.append("Excluded cases (never scored; listed, not dropped):")
    any_excluded = False
    for db, tally in sorted(report.tallies.items()):
        for reason, items in sorted(tally.excluded.items()):
            for case_id, detail in items:
                any_excluded = True
                lines.append(f"  {case_id:<14s} {db:<24s} {reason}" + (f" -- {detail}" if detail else ""))
    if not any_excluded:
        lines.append("  (none)")
    if report.configs:
        lines.append("")
        lines.append("Configurations drafted:")
        for name, info in sorted(report.configs.items()):
            status = "ok" if info.get("inspector_exit") == 0 else f"inspector exit {info.get('inspector_exit')}"
            described = info.get("described_columns")
            lines.append(f"  {name:<28s} {status}" + (f", {described} columns described" if described is not None else ""))
    lines.append("")
    lines.append(ATTRIBUTION)
    return "\n".join(lines)
