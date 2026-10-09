# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Command-line entry point for the evaluation harness.

Usage::

    python -m eval.cli run --golden eval_data.example/golden.jsonl
    python -m eval.cli run --golden eval_data/golden.jsonl --live
    python -m eval.cli run --golden eval_data/golden.jsonl --live \\
        --baseline eval_data/baseline.json
    python -m eval.cli run --golden eval_data/golden.jsonl --live \\
        --save-baseline eval_data/baseline.json

    # Absolute floors (off unless set): fail the run when overall accuracy,
    # or any one data source's accuracy, is below a fixed percentage --
    # whatever the baseline says, and with or without --baseline.
    python -m eval.cli run --golden eval_data/golden.jsonl --live \\
        --min-accuracy 90 --min-source-accuracy 80

    # Release gate against a warehouse that changes daily: run every case's
    # expected_sql in the same run and compare the two results (execution
    # accuracy, see eval.compare) instead of a fingerprint recorded once.
    python -m eval.cli run --golden eval_data/golden.jsonl --live \\
        --reference live --baseline eval_data/baseline.json

    # Last step of building a golden set: run each reviewed case's
    # expected_sql read-only, record its rows/fingerprint, and (only with
    # --accept) activate the cases that passed. See eval.verify.
    python -m eval.cli verify --golden eval_data/golden.jsonl
    python -m eval.cli verify --golden eval_data/golden.jsonl --accept

    # Table-selection recall, offline (no LLM, no database): which of the
    # tables each case's expected_sql reads does retrieval put in the prompt?
    # See eval.recall.
    python -m eval.cli recall --golden eval_data/golden.jsonl
    python -m eval.cli recall --golden eval_data/golden.jsonl --json
    python -m eval.cli recall --golden eval_data/golden.jsonl --min-recall 0.9

    # Phase 2 task 3: compare free-text-plus-clean_sql against constrained
    # JSON output on the same golden set (requires a real, reachable endpoint):
    python -m eval.cli run --golden eval_data.example/golden.jsonl --live
    python -m eval.cli run --golden eval_data.example/golden.jsonl --live --structured

    # Determinism probe (see eval.determinism): run every golden question
    # through the live generator several times and report how often it
    # comes back byte-identical. Requires a real, reachable endpoint --
    # --determinism without --live is refused, see _run below.
    python -m eval.cli run --golden eval_data.example/golden.jsonl --live --determinism
    python -m eval.cli run --golden eval_data.example/golden.jsonl --live --determinism \\
        --determinism-repeats 5 --determinism-out eval_data/determinism.json

By default the harness runs in **offline** mode: no database connection,
no LLM call — see :func:`eval.runner.make_offline_generator` and
:func:`eval.runner.make_offline_executor`. Passing ``--live`` switches to
a real :class:`~llm.providers.OpenAIBackend` (built from :mod:`config`'s
``OPENAI_*`` settings) and :func:`database.executor.execute_sql`; both are
imported lazily, inside :func:`_build_live_callables`, only when
``--live`` is actually requested — this module never opens a network or
database connection at import time.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import config as cfg
from core.console import use_utf8_console
from eval.baseline import (
    BaselineThresholds,
    check_absolute_floors,
    compare_to_baseline,
    exit_code,
    load_baseline,
    save_baseline,
)
from eval.compare import ComparisonOptions
from eval.determinism import DEFAULT_REPEATS as DEFAULT_DETERMINISM_REPEATS
from eval.models import GoldenCase
from eval.recall import evaluate_recall, render_recall_text, report_to_json
from eval.report import build_report, render_text, save_json_report
from eval.runner import (
    ExecuteFn,
    GenerateFn,
    SourceTrace,
    load_golden_cases,
    make_live_generator,
    make_live_structured_generator,
    make_offline_executor,
    make_offline_generator,
    run_golden_set,
)
from eval.store import write_golden_cases
from eval.verify import render_verify_text, verify_cases
from knowledge.config_loader import resolve_system_prompt_path

_DEFAULT_SYSTEM_PROMPT_PATH = resolve_system_prompt_path()


def _load_system_prompt(path: Path = _DEFAULT_SYSTEM_PROMPT_PATH) -> str:
    """Read the system prompt text used to build a live prompt.

    Parameters
    ----------
    path:
        Path to the system prompt file.

    Returns
    -------
    str

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.

    Examples
    --------
    >>> import tempfile, os
    >>> fd, path = tempfile.mkstemp(suffix=".md")
    >>> _ = os.write(fd, b"You are a T-SQL expert.")
    >>> os.close(fd)
    >>> _load_system_prompt(Path(path))
    'You are a T-SQL expert.'
    >>> os.remove(path)
    """
    if not path.exists():
        raise FileNotFoundError(f"system prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def _build_live_callables(
    structured: bool = False, trace: SourceTrace | None = None
) -> tuple[GenerateFn, ExecuteFn]:
    """Lazily construct a real ``(generate_fn, execute_fn)`` pair for ``--live`` mode.

    Every import here is deferred to call time so that plain ``import
    eval.cli`` (e.g. from a test collector) never opens a network
    connection or a database engine.

    Parameters
    ----------
    structured:
        When ``True``, use :func:`~eval.runner.make_live_structured_generator`
        (Phase 2 task 3's constrained-JSON path) instead of
        :func:`~eval.runner.make_live_generator` (free text + ``clean_sql``).
        This is what ``--structured`` compares against the default.
    trace:
        Optional :class:`~eval.runner.SourceTrace` the generator records
        its data-source choice on, for source-selection accuracy.

    Returns
    -------
    tuple[GenerateFn, ExecuteFn]
    """
    from database.executor import execute_sql
    from llm.providers import OpenAIBackend

    system_prompt = _load_system_prompt()
    backend = OpenAIBackend.from_settings()
    generate_fn = (
        make_live_structured_generator(backend, system_prompt, trace)
        if structured
        else make_live_generator(backend, system_prompt, trace)
    )
    return generate_fn, execute_sql


def _build_executor() -> ExecuteFn:
    """The application's own executor, imported lazily (see the module docstring).

    Used by ``verify``: the same routing, ``NOLOCK`` rewriting, timeouts and
    rolled-back read-only transaction as a production query.
    """
    from database.executor import execute_sql

    return execute_sql


def _build_offline_callables(cases: Sequence[GoldenCase]) -> tuple[GenerateFn, ExecuteFn]:
    """Build the offline/CI replay ``(generate_fn, execute_fn)`` pair for *cases*."""
    return make_offline_generator(cases), make_offline_executor(cases)


def _print_prefix_cache_probe(question: str) -> None:
    """Print the Phase 2 (latency) prefix-cache measurement for ``--live`` runs.

    Asks the exact same *question* twice against a real
    :class:`~llm.providers.OpenAIBackend` and reports each call's
    ``prompt_tokens`` and wall-clock time, plus the derived
    ``prefix_cache_hit`` — see :func:`~eval.runner.measure_prefix_cache`.
    This is deliberately printed, never silently skipped or guessed at: if
    no endpoint is reachable, the failure is reported as UNMEASURED with
    the underlying exception, rather than an invented number.
    """
    from llm.providers import OpenAIBackend
    from eval.runner import measure_prefix_cache

    system_prompt = _load_system_prompt()
    backend = OpenAIBackend.from_settings()
    print("\nPrefix-cache probe (Phase 2 latency baseline):")
    try:
        report = measure_prefix_cache(backend, system_prompt, question)
    except Exception as exc:  # noqa: BLE001 - report, never crash the whole run
        print(f"  UNMEASURED -- could not reach {backend.name!r}: {type(exc).__name__}: {exc}")
        return
    first, second = report["first"], report["second"]
    print(
        f"  first call:  prompt_tokens={first['prompt_tokens']}  "
        f"wall_clock={first['wall_clock_seconds']:.3f}s"
    )
    print(
        f"  second call: prompt_tokens={second['prompt_tokens']}  "
        f"wall_clock={second['wall_clock_seconds']:.3f}s"
    )
    print(f"  prefix_cache_hit={report['prefix_cache_hit']}")


def _refuse_offline_determinism() -> None:
    """Raise the explicit refusal for ``--determinism`` without ``--live``.

    Raises
    ------
    ValueError
        Always. Named after the fixture it refuses to run against:
        offline mode's generator (:func:`eval.runner.make_offline_generator`)
        replays each case's own ``expected_sql`` from a lookup table, so
        every repeat would return byte-identical text purely because the
        "generator" is a dictionary lookup, not a model. The determinism
        probe would then report 100% determinism unconditionally -- a
        confident, precise, and completely meaningless number, the same
        shape of bug this project has already been bitten by twice (an
        offline accuracy figure that was true by construction, and a
        ``prefix_cache_hit`` that was vacuously true at zero prompt
        tokens). This is a hard refusal, not a silent skip or a footnote.
    """
    raise ValueError(
        "--determinism requires --live: offline mode's generator "
        "(eval.runner.make_offline_generator) replays each golden case's own "
        "expected_sql from a lookup table, so every repeat returns byte-identical "
        "text by construction -- there is no model in the loop to vary. Running the "
        "determinism probe against it would report 100% determinism unconditionally, "
        "which measures nothing about any real endpoint. Pass --live to measure a "
        "real endpoint instead."
    )


def _print_determinism_probe(
    cases: Sequence[GoldenCase],
    *,
    structured: bool,
    repeats: int,
    out: str | None,
) -> None:
    """Print the live-only determinism probe for ``--live --determinism`` runs.

    Builds its own real generator (mirroring :func:`_print_prefix_cache_probe`,
    which likewise constructs its own :class:`~llm.providers.OpenAIBackend`
    rather than reusing the one built for the main accuracy run) and drives
    every case in *cases* through :func:`eval.determinism.probe_determinism`.
    Never silently skipped and never invented: a failure to reach the
    endpoint propagates as a real exception, exactly like
    :func:`_print_prefix_cache_probe` would rather report UNMEASURED than a
    fabricated number -- except here there is nothing sensible to fall
    back to print, so it is left to propagate.

    Parameters
    ----------
    cases:
        The golden questions to probe.
    structured:
        Same meaning as :func:`_build_live_callables`'s ``structured``
        argument -- use the constrained-JSON generation path instead of
        free text + ``clean_sql``.
    repeats:
        Number of times to generate each question. See
        :data:`eval.determinism.MIN_REPEATS`.
    out:
        Optional path to also save the determinism report as JSON.
    """
    from llm.providers import OpenAIBackend

    from eval.determinism import probe_determinism, render_determinism_text, save_determinism_json

    system_prompt = _load_system_prompt()
    backend = OpenAIBackend.from_settings()
    generate_fn = (
        make_live_structured_generator(backend, system_prompt)
        if structured
        else make_live_generator(backend, system_prompt)
    )

    print("\nDeterminism probe (live only -- see eval.determinism):")
    report = probe_determinism(generate_fn, cases, endpoint=backend.name, repeats=repeats)
    print(render_determinism_text(report))

    if out:
        save_determinism_json(report, out)
        print(f"Determinism report written to {out}")


def _refuse_offline_reference() -> None:
    """Raise the explicit refusal for ``--reference live`` without ``--live``.

    Raises
    ------
    ValueError
        Always. Offline mode has no database: its "executor" serves each
        case's recorded rows, so running the reference query through it
        would compare a recorded answer with itself and report 100%
        unconditionally.
    """
    raise ValueError(
        "--reference live requires --live: offline mode has no database, its executor "
        "replays each case's recorded expected_rows, so the reference result would be "
        "compared with itself and every case would pass by construction. Pass --live to "
        "run the reference queries against the real warehouse."
    )


def _percent(text: str) -> float:
    """``argparse`` type for a percentage between 0 and 100.

    Args:
        text: The command-line value.

    Returns:
        The value as a float.

    Raises:
        argparse.ArgumentTypeError: If *text* is not a number in ``[0, 100]``.

    Examples:
        >>> _percent("92.5")
        92.5
        >>> _percent("101")
        Traceback (most recent call last):
            ...
        argparse.ArgumentTypeError: must be a percentage between 0 and 100 (got '101')
    """
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not (0.0 <= value <= 100.0):  # also rejects nan
        raise argparse.ArgumentTypeError(
            f"must be a percentage between 0 and 100 (got {text!r})"
        )
    return value


def _print_floor_failures(messages: Sequence[str]) -> None:
    """Print the absolute-floor violations under a header of their own."""
    print("\nBELOW ABSOLUTE ACCURACY FLOOR:")
    for message in messages:
        print(f"  - {message}")


def _run(args: argparse.Namespace) -> int:
    """Execute the ``run`` subcommand. Returns the process exit code."""
    if args.determinism and not args.live:
        _refuse_offline_determinism()
    if args.reference == "live" and not args.live:
        _refuse_offline_reference()

    cases = load_golden_cases(args.golden)
    # Only active cases are replayed, determinism-probed or counted; a
    # pending/reviewed case must not trip the offline fixture's
    # duplicate-question check either.
    runnable = [c for c in cases if c.is_runnable]

    trace: SourceTrace | None = None
    if args.live:
        trace = SourceTrace()
        generate_fn, execute_fn = _build_live_callables(structured=args.structured, trace=trace)
        mode = "live"
    else:
        generate_fn, execute_fn = _build_offline_callables(runnable)
        mode = "offline"

    results = run_golden_set(
        cases,
        generate_fn,
        execute_fn,
        reference=args.reference,
        options=ComparisonOptions(tolerance=args.float_tolerance),
        source_trace=trace,
    )
    report = build_report(results, mode=mode, reference=args.reference)

    print(render_text(report))

    if args.live and runnable:
        _print_prefix_cache_probe(runnable[0].question)

    if args.determinism:
        _print_determinism_probe(
            runnable,
            structured=args.structured,
            repeats=args.determinism_repeats,
            out=args.determinism_out,
        )

    if args.out:
        save_json_report(report, args.out)
        print(f"\nJSON report written to {args.out}")

    if args.save_baseline:
        save_baseline(report, args.save_baseline)
        print(f"Baseline saved to {args.save_baseline}")

    thresholds = BaselineThresholds(
        max_accuracy_drop_pct=args.max_accuracy_drop_pct,
        max_latency_p95_increase_pct=args.max_latency_p95_increase_pct,
        max_guard_rejection_increase=args.max_guard_rejection_increase,
        min_accuracy_pct=args.min_accuracy,
        min_source_accuracy_pct=args.min_source_accuracy,
    )

    if args.baseline:
        baseline_report = load_baseline(args.baseline)
        comparison = compare_to_baseline(report, baseline_report, thresholds)
        if comparison.messages:
            print("\nREGRESSION DETECTED versus baseline:")
            for message in comparison.messages:
                print(f"  - {message}")
        else:
            print("\nNo regression versus baseline.")
        if comparison.floor_messages:
            _print_floor_failures(comparison.floor_messages)
        for source, delta in comparison.source_deltas_pct.items():
            print(f"  source {source}: execution accuracy {delta:+.2f} points versus baseline")
        if comparison.source_selection_delta_pct is not None:
            print(
                "  source-selection accuracy "
                f"{comparison.source_selection_delta_pct:+.2f} points versus baseline"
            )
        return exit_code(comparison)

    # No baseline was supplied: there is nothing to regress against, so the
    # report is the whole job unless an absolute floor is set, which needs
    # no baseline.
    floor_failures = check_absolute_floors(report, thresholds)
    if floor_failures:
        _print_floor_failures(floor_failures)
        return 1
    if args.min_accuracy is not None or args.min_source_accuracy is not None:
        print("\nAbsolute accuracy floors met.")
    return 0


def _verify(
    args: argparse.Namespace,
    executor_factory: Callable[[], ExecuteFn] | None = None,
) -> int:
    """Execute the ``verify`` subcommand. Returns the process exit code.

    ``0`` when every examined case's reference held up, ``1`` when any
    had a problem. Prints counts and case ids only -- never a question or
    a row.
    """
    cases = load_golden_cases(args.golden)
    execute_fn = (executor_factory or _build_executor)()
    result = verify_cases(cases, execute_fn, accept=args.accept, refresh=args.refresh)

    written = False
    if result.changed and not args.dry_run:
        write_golden_cases(args.golden, result.cases, backup=True)
        written = True
    print(render_verify_text(result, accept=args.accept, written=written))
    return 1 if result.problems else 0


def _recall(args: argparse.Namespace) -> int:
    """Execute the ``recall`` subcommand. Returns the process exit code.

    ``0`` normally; ``1`` when ``--min-recall`` is given and the mean recall
    is below it, or when no case could be scored at all (a recall of ``0.0``
    over zero cases must not read as a pass).
    """
    cases = load_golden_cases(args.golden)
    report = evaluate_recall(cases, token_budget=args.token_budget)

    if args.json:
        print(report_to_json(report))
    else:
        print(render_recall_text(report, all_cases=args.all_cases))

    if args.out:
        Path(args.out).write_text(report_to_json(report) + "\n", encoding="utf-8")
        if not args.json:
            print(f"\nJSON report written to {args.out}")

    if report.scored == 0:
        print("No case could be scored.", file=sys.stderr)
        return 1
    if args.min_recall is not None and report.mean_recall < args.min_recall:
        print(
            f"Mean recall {report.mean_recall:.4f} is below --min-recall {args.min_recall:g}.",
            file=sys.stderr,
        )
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the ``eval.cli`` argument parser.

    Returns
    -------
    argparse.ArgumentParser

    Examples
    --------
    >>> parser = build_parser()
    >>> args = parser.parse_args(["run", "--golden", "golden.jsonl"])
    >>> args.golden
    'golden.jsonl'
    >>> args.live
    False
    """
    parser = argparse.ArgumentParser(
        prog="python -m eval.cli",
        description="Run the NL->SQL golden-set evaluation harness.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a golden set and report results.")
    run_parser.add_argument(
        "--golden", required=True, help="Path to a golden.jsonl file."
    )
    run_parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Use a real OpenAI-compatible backend and database connection instead of offline replay.",
    )
    run_parser.add_argument(
        "--structured",
        action="store_true",
        default=False,
        help=(
            "With --live, use Phase 2 task 3's constrained-JSON generation path "
            "(llm.structured_schema.SQL_GENERATION_SCHEMA) instead of free text + "
            "clean_sql. Ignored without --live."
        ),
    )
    run_parser.add_argument(
        "--determinism",
        action="store_true",
        default=False,
        help=(
            "Run the determinism probe (eval.determinism): generate every golden "
            "question several times and report how often the SQL comes back "
            "byte-identical. Requires --live -- offline replay is refused outright, "
            "since it would report 100%% determinism unconditionally (see "
            "eval/determinism.py's module docstring)."
        ),
    )
    run_parser.add_argument(
        "--determinism-repeats",
        type=int,
        default=DEFAULT_DETERMINISM_REPEATS,
        dest="determinism_repeats",
        help=(
            "Number of times to generate each golden question for the determinism "
            "probe. Must be at least 2 (a single draw has nothing to compare "
            "against). Ignored without --determinism."
        ),
    )
    run_parser.add_argument(
        "--determinism-out",
        default=None,
        dest="determinism_out",
        help="Path to save the determinism probe's report as JSON. Ignored without --determinism.",
    )
    run_parser.add_argument(
        "--reference",
        choices=("stored", "live"),
        default="stored",
        help=(
            "How a --live run decides a result is right. 'stored' (default) compares "
            "the result's fingerprint with the case's recorded expected_fingerprint. "
            "'live' executes each case's expected_sql in the same run and compares the "
            "two results (eval.compare: rows as a multiset, column names ignored, row "
            "order only when the reference has ORDER BY with TOP/OFFSET, numeric "
            "tolerance, NULL equals NULL) -- use it against a warehouse whose data "
            "changes, where a recorded fingerprint goes stale. A baseline can only be "
            "compared with a run that used the same reference. Requires --live."
        ),
    )
    run_parser.add_argument(
        "--float-tolerance",
        type=float,
        default=ComparisonOptions().tolerance,
        dest="float_tolerance",
        help=(
            "With --reference live: relative tolerance for numbers that are not both "
            "integers (|a-b| <= tol * max(1, |a|, |b|)). Integers always compare "
            "exactly. Default %(default)g; 0 demands exact equality."
        ),
    )
    run_parser.add_argument(
        "--baseline",
        default=None,
        help="Path to a baseline JSON file to compare this run against (non-zero exit on regression).",
    )
    run_parser.add_argument(
        "--save-baseline",
        default=None,
        help="Path to save this run's report as a new baseline JSON file.",
    )
    run_parser.add_argument(
        "--out",
        default=None,
        help="Path to save this run's full JSON report.",
    )
    run_parser.add_argument(
        "--max-accuracy-drop-pct",
        type=float,
        default=cfg.settings.eval_max_accuracy_drop_pct,
        dest="max_accuracy_drop_pct",
        help=(
            "Maximum tolerated accuracy drop, in percentage points, versus the "
            "baseline. Defaults to config.Settings.eval_max_accuracy_drop_pct "
            "(env EVAL_MAX_ACCURACY_DROP_PCT)."
        ),
    )
    run_parser.add_argument(
        "--max-latency-p95-increase-pct",
        type=float,
        default=cfg.settings.eval_max_latency_p95_increase_pct,
        dest="max_latency_p95_increase_pct",
        help=(
            "Maximum tolerated relative increase in latency p95 versus the "
            "baseline. Defaults to config.Settings.eval_max_latency_p95_increase_pct "
            "(env EVAL_MAX_LATENCY_P95_INCREASE_PCT)."
        ),
    )
    run_parser.add_argument(
        "--max-guard-rejection-increase",
        type=int,
        default=cfg.settings.eval_max_guard_rejection_increase,
        dest="max_guard_rejection_increase",
        help=(
            "Maximum tolerated increase in guard-rejected cases versus the "
            "baseline. Defaults to config.Settings.eval_max_guard_rejection_increase "
            "(env EVAL_MAX_GUARD_REJECTION_INCREASE)."
        ),
    )
    run_parser.add_argument(
        "--min-accuracy",
        type=_percent,
        default=cfg.settings.eval_min_accuracy,
        dest="min_accuracy",
        metavar="PCT",
        help=(
            "Absolute floor (0-100) on overall execution accuracy: the run fails "
            "(exit 1) when accuracy is below it, with or without --baseline. Off "
            "unless set. Defaults to config.Settings.eval_min_accuracy (env "
            "EVAL_MIN_ACCURACY). Only meaningful with --live."
        ),
    )
    run_parser.add_argument(
        "--min-source-accuracy",
        type=_percent,
        default=cfg.settings.eval_min_source_accuracy,
        dest="min_source_accuracy",
        metavar="PCT",
        help=(
            "Absolute floor (0-100) on each data source's execution accuracy: the "
            "run fails when any source is below it, or when the run has no "
            "per-source figures to check. Off unless set. Defaults to "
            "config.Settings.eval_min_source_accuracy (env EVAL_MIN_SOURCE_ACCURACY). "
            "Only meaningful with --live."
        ),
    )
    run_parser.set_defaults(func=_run)

    verify_parser = subparsers.add_parser(
        "verify",
        help=(
            "Run each reviewed case's expected_sql read-only against the database, "
            "report failures and record expected_rows / expected_fingerprint."
        ),
    )
    verify_parser.add_argument(
        "--golden", required=True, help="Path to the golden.jsonl file to verify (rewritten in place)."
    )
    verify_parser.add_argument(
        "--accept",
        action="store_true",
        default=False,
        help=(
            "Move 'reviewed' cases that passed to 'active' so the regression gate runs "
            "them. Without it the recorded rows are written but statuses stay."
        ),
    )
    verify_parser.add_argument(
        "--refresh",
        action="store_true",
        default=False,
        help=(
            "Also overwrite the recorded expected_rows / expected_fingerprint of "
            "'active' cases that already have them (by default only missing ones are filled)."
        ),
    )
    verify_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Report only; do not rewrite the golden file.",
    )
    verify_parser.set_defaults(func=_verify)

    recall_parser = subparsers.add_parser(
        "recall",
        help=(
            "Measure table-selection recall offline (no LLM, no database): the "
            "share of each case's expected_sql tables that retrieval puts in the prompt."
        ),
    )
    recall_parser.add_argument(
        "--golden", required=True, help="Path to a golden.jsonl file."
    )
    recall_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Print the full report as JSON instead of the text summary.",
    )
    recall_parser.add_argument(
        "--out",
        default=None,
        help="Also write the full JSON report to this path.",
    )
    recall_parser.add_argument(
        "--all-cases",
        action="store_true",
        default=False,
        dest="all_cases",
        help="List every scored case in the text summary, not only those that missed a table.",
    )
    recall_parser.add_argument(
        "--min-recall",
        type=float,
        default=None,
        dest="min_recall",
        help="Exit non-zero when the mean recall is below this value (0 to 1).",
    )
    recall_parser.add_argument(
        "--token-budget",
        type=int,
        default=None,
        dest="token_budget",
        help=(
            "Estimated schema tokens a retrieved set may take before the in-budget recall "
            "counts the case as 0. Defaults to PROMPT_RETRIEVAL_TOKEN_BUDGET."
        ),
    )
    recall_parser.set_defaults(func=_recall)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse *argv* and dispatch to the requested subcommand.

    Parameters
    ----------
    argv:
        Command-line arguments (excluding the program name). Defaults to
        ``sys.argv[1:]`` when ``None``.

    Returns
    -------
    int
        Process exit code: ``0`` on success/no-regression, ``1`` on
        regression versus a baseline or a run below an absolute
        accuracy floor.

    Examples
    --------
    Missing required ``--golden`` produces argparse's usual usage error:

    >>> import contextlib, io
    >>> buf = io.StringIO()
    >>> with contextlib.redirect_stderr(buf):
    ...     code = None
    ...     try:
    ...         main(["run"])
    ...     except SystemExit as exc:
    ...         code = exc.code
    >>> code
    2
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    use_utf8_console()
    sys.exit(main())
