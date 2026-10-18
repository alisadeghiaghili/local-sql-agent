# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``python -m eval.cli bench-run bird``: run the imported benchmark and merge the reports.

The production pipeline binds ``project_config/`` when it is imported, so one
process can measure only one configuration. A per-database run therefore starts
``python -m eval.cli run --golden <db>/golden.jsonl --live --reference stored``
once per database, in its own process, with ``PROJECT_CONFIG_DIR`` pointing at
that database's configuration, and merges the reports into one.

Two modes are measured separately and never mixed in one report:

``per_db``
    One configuration per database; retrieval chooses among that database's
    tables only. The figure to use when comparing models or prompts.
``combined``
    One configuration over every imported schema, so retrieval must also find
    the right database. A harder, separate measurement.

Every merged report carries the benchmark header (public data, loaded into your
own SQL Server, not this deployment's accuracy) and says which mode, which
golden file (with or without the evidence hint) and which configuration (raw
draft or with the dataset's descriptions) produced it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from eval.baseline import load_baseline
from eval.models import CaseResult, EvalReport
from eval.report import build_report, render_text, save_json_report

__all__ = [
    "RunPlan",
    "merge_reports",
    "plan_runs",
    "read_import_report",
    "run_plans",
    "run_subprocess",
    "write_merged",
]

Mode = Literal["per_db", "combined"]

MODE_DESCRIPTIONS: dict[str, str] = {
    "per_db": (
        "Mode: per-database -- one project_config per database; retrieval chooses among that "
        "database's tables only."
    ),
    "combined": (
        "Mode: COMBINED -- one project_config over every imported schema, so retrieval must also "
        "find the right database. A separate, harder measurement: do not compare it with the "
        "per-database figure."
    ),
}


@dataclass(frozen=True, slots=True)
class RunPlan:
    """One ``eval.cli run`` process.

    Parameters
    ----------
    label:
        ``db_id``, or ``_combined``.
    golden, config, report:
        The golden file, the ``PROJECT_CONFIG_DIR`` and the ``--out`` report path.
    """

    label: str
    golden: Path
    config: Path
    report: Path


def plan_runs(
    bench_dir: Path, *, mode: Mode, evidence: bool = False, dbs: Sequence[str] | None = None
) -> list[RunPlan]:
    """The runs of *mode* over an import directory.

    Parameters
    ----------
    bench_dir:
        The ``--out`` directory of ``bench-import``.
    mode:
        ``per_db`` or ``combined``.
    evidence:
        Use ``golden.with_evidence.jsonl`` (the ablation).
    dbs:
        Limit a per-database run to these databases.

    Raises
    ------
    FileNotFoundError
        If a golden file or configuration the plan needs is missing.
    ValueError
        If *dbs* names a database that was not imported.

    Examples
    --------
    >>> import tempfile
    >>> d = Path(tempfile.mkdtemp())
    >>> for sub in ("a", "b"):
    ...     (d / sub / "project_config").mkdir(parents=True)
    ...     _ = (d / sub / "golden.jsonl").write_text("{}", encoding="utf-8")
    >>> [(p.label, p.report.name) for p in plan_runs(d, mode="per_db")]
    [('a', 'a.report.json'), ('b', 'b.report.json')]
    >>> plan_runs(d, mode="per_db", dbs=["zzz"])
    Traceback (most recent call last):
        ...
    ValueError: not imported: zzz
    """
    name = "golden.with_evidence.jsonl" if evidence else "golden.jsonl"
    runs_dir = bench_dir / "runs" / f"{mode}{'-evidence' if evidence else ''}"
    if mode == "combined":
        plans = [RunPlan("_combined", bench_dir / name, bench_dir / "_combined" / "project_config",
                         runs_dir / "_combined.report.json")]
    else:
        imported = sorted(
            p.name for p in bench_dir.iterdir()
            if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "project_config").is_dir()
        ) if bench_dir.is_dir() else []
        unknown = sorted(set(dbs or ()) - set(imported))
        if unknown:
            raise ValueError(f"not imported: {', '.join(unknown)}")
        plans = [
            RunPlan(db, bench_dir / db / name, bench_dir / db / "project_config",
                    runs_dir / f"{db}.report.json")
            for db in (sorted(dbs) if dbs else imported)
        ]
    if not plans:
        raise FileNotFoundError(f"no imported database under {bench_dir}")
    for plan in plans:
        for needed in (plan.golden, plan.config):
            if not needed.exists():
                raise FileNotFoundError(f"missing {needed} (run bench-import first)")
    return plans


def run_subprocess(plan: RunPlan, run_args: Sequence[str] = ()) -> int:
    """Run one plan as ``python -m eval.cli run --live --reference stored`` in its own process.

    ``PROJECT_CONFIG_DIR`` is set for that process only. *run_args* are added to the command line
    (for example ``--single-shot`` or ``--float-tolerance 1e-9``).
    """
    plan.report.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "eval.cli", "run", "--golden", str(plan.golden),
        "--live", "--reference", "stored", "--out", str(plan.report), *run_args,
    ]
    env = {**os.environ, "PROJECT_CONFIG_DIR": str(plan.config)}
    return subprocess.run(command, env=env, check=False).returncode


def run_plans(
    plans: Sequence[RunPlan],
    run: Callable[[RunPlan], int],
    *,
    progress: Callable[[str], None] = print,
) -> list[RunPlan]:
    """Run each plan; return the ones that produced no report.

    A run that exits non-zero but wrote its report (a threshold failure) still
    counts; one that wrote none (a crash, an unreachable model) is returned as
    failed.
    """
    failed: list[RunPlan] = []
    for plan in plans:
        progress(f"== {plan.label} ({plan.golden})")
        plan.report.unlink(missing_ok=True)
        code = run(plan)
        if not plan.report.is_file():
            progress(f"   no report written (exit {code})")
            failed.append(plan)
    return failed


def merge_reports(
    paths: Sequence[Path],
    *,
    mode: Mode,
    evidence: bool,
    import_report: dict[str, Any] | None = None,
) -> EvalReport:
    """One :class:`~eval.models.EvalReport` from the per-database reports.

    The cases of every report are pooled and re-aggregated (accuracy, intervals,
    error taxonomy, latency percentiles), so the merged figure is over all
    scored cases, not a mean of per-database means. The benchmark header says
    the mode, the evidence setting and, from *import_report*, the configuration
    and the exclusions.

    Raises
    ------
    ValueError
        If there is no report, a case id appears in two reports, or the reports
        disagree on the pipeline or the reference.
    """
    if not paths:
        raise ValueError("no per-database report to merge")
    reports = [load_baseline(p) for p in paths]
    results: list[CaseResult] = []
    seen: set[str] = set()
    for path, report in zip(paths, reports):
        for result in report.results:
            if result.case_id in seen:
                raise ValueError(f"case {result.case_id} appears in more than one report ({path.name})")
            seen.add(result.case_id)
            results.append(result)
    pipelines = {r.pipeline for r in reports}
    references = {r.reference for r in reports}
    modes = {r.mode for r in reports}
    if len(pipelines) > 1 or len(references) > 1 or len(modes) > 1:
        raise ValueError("the reports were made with different pipelines, references or modes")
    options = (import_report or {}).get("options", {})
    totals = (import_report or {}).get("totals", {})
    benchmark: dict[str, Any] = {
        "name": "bird",
        "mode": mode,
        "mode_description": MODE_DESCRIPTIONS[mode],
        "evidence_hint": evidence,
        "descriptions": bool(options.get("descriptions")),
        "databases": sorted({t[len("db:"):] for r in results for t in r.tags if t.startswith("db:")}),
        "scored": len(results),
        "excluded": totals.get("excluded_by_reason", {}),
        "setup": (
            ("Evidence: the annotators' hint is appended to every question (ablation). "
             if evidence else "Evidence: none (the default; analysts give no hint). ")
            + ("Configuration: the dataset's column descriptions were added."
               if options.get("descriptions") else "Configuration: raw inspector draft.")
        ),
    }
    benchmark["mode_description"] = f"{MODE_DESCRIPTIONS[mode]}\n{benchmark['setup']}"
    return build_report(
        results, mode=next(iter(modes)), reference=next(iter(references)),
        pipeline=next(iter(pipelines)), benchmark=benchmark,
    )


def write_merged(report: EvalReport, directory: Path) -> tuple[Path, Path]:
    """Write ``report.json`` and ``report.txt`` into *directory*; return both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path, text_path = directory / "report.json", directory / "report.txt"
    save_json_report(report, json_path)
    text_path.write_text(render_text(report) + "\n", encoding="utf-8")
    return json_path, text_path


def read_import_report(bench_dir: Path) -> dict[str, Any] | None:
    """``import_report.json`` of an import directory, or ``None`` when absent or unreadable."""
    path = bench_dir / "import_report.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
