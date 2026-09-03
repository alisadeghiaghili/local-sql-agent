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
from collections.abc import Sequence
from pathlib import Path

import config as cfg
from eval.baseline import (
    BaselineThresholds,
    compare_to_baseline,
    exit_code,
    load_baseline,
    save_baseline,
)
from eval.determinism import DEFAULT_REPEATS as DEFAULT_DETERMINISM_REPEATS
from eval.models import GoldenCase
from eval.report import build_report, render_text, save_json_report
from eval.runner import (
    ExecuteFn,
    GenerateFn,
    load_golden_cases,
    make_live_generator,
    make_live_structured_generator,
    make_offline_executor,
    make_offline_generator,
    run_golden_set,
)

_DEFAULT_SYSTEM_PROMPT_PATH = Path("prompts/system_prompt.md")


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


def _build_live_callables(structured: bool = False) -> tuple[GenerateFn, ExecuteFn]:
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

    Returns
    -------
    tuple[GenerateFn, ExecuteFn]
    """
    from database.executor import execute_sql
    from llm.providers import OpenAIBackend

    system_prompt = _load_system_prompt()
    backend = OpenAIBackend.from_settings()
    generate_fn = (
        make_live_structured_generator(backend, system_prompt)
        if structured
        else make_live_generator(backend, system_prompt)
    )
    return generate_fn, execute_sql


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


def _run(args: argparse.Namespace) -> int:
    """Execute the ``run`` subcommand. Returns the process exit code."""
    if args.determinism and not args.live:
        _refuse_offline_determinism()

    cases = load_golden_cases(args.golden)

    if args.live:
        generate_fn, execute_fn = _build_live_callables(structured=args.structured)
        mode = "live"
    else:
        generate_fn, execute_fn = _build_offline_callables(cases)
        mode = "offline"

    results = run_golden_set(cases, generate_fn, execute_fn)
    report = build_report(results, mode=mode)

    print(render_text(report))

    if args.live:
        _print_prefix_cache_probe(cases[0].question)

    if args.determinism:
        _print_determinism_probe(
            cases,
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

    if args.baseline:
        baseline_report = load_baseline(args.baseline)
        thresholds = BaselineThresholds(
            max_accuracy_drop_pct=args.max_accuracy_drop_pct,
            max_latency_p95_increase_pct=args.max_latency_p95_increase_pct,
            max_guard_rejection_increase=args.max_guard_rejection_increase,
        )
        comparison = compare_to_baseline(report, baseline_report, thresholds)
        if comparison.regressed:
            print("\nREGRESSION DETECTED versus baseline:")
            for message in comparison.messages:
                print(f"  - {message}")
        else:
            print("\nNo regression versus baseline.")
        return exit_code(comparison)

    # No baseline was supplied: printing the report is the whole job, and
    # there is nothing to regress against.
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
    run_parser.set_defaults(func=_run)

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
        regression versus a baseline.

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
    sys.exit(main())
