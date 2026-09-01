# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Run a golden set of :class:`~eval.models.GoldenCase` through the pipeline.

This module never talks to a database or an LLM by itself.  Instead it
defines two narrow callable contracts — mirroring the injection pattern
already used by :class:`llm.sql_agent.SQLAgent` — and drives every case
through them:

``GenerateFn``
    ``Callable[[str], str]``. Takes a question, returns **cleaned** SQL
    text. Must raise ``ValueError("OUT_OF_SCOPE")`` when the question is
    out of scope (the same sentinel :class:`llm.base.LLMBackend` uses), or
    any other exception on generation failure. Cleaning (markdown-fence
    stripping, ``LIMIT``→``TOP`` conversion, …) is the generator's job —
    see :func:`security.sql_guard.clean_sql`. Applying the security guard
    is *not* the generator's job; see below.
``ExecuteFn``
    ``Callable[[str], pandas.DataFrame]``. Takes a validated SQL string,
    returns the result set, or raises on failure — exactly the contract
    :func:`database.executor.execute_sql` already satisfies.

:func:`run_case` owns the one piece of logic that must be applied
identically in every mode: calling :func:`security.sql_guard.validate_sql`
on whatever the generator produced. Centralising that call here (rather
than inside each generator) is what lets :class:`~eval.models.CaseResult`
report ``guard_rejected`` as a distinct, trustworthy metric regardless of
which generator produced the SQL.

Two ways to obtain a ``(generate_fn, execute_fn)`` pair are provided:

* **Offline/replay** (:func:`make_offline_generator` /
  :func:`make_offline_executor`) — built entirely from the golden set
  itself. No network, no database. Every reference ``expected_sql`` is
  still pushed through the real :func:`~security.sql_guard.validate_sql`,
  so this mode doubles as a guard-regression check on the golden set.
* **Live** (:func:`make_live_generator`) — wraps a real
  :class:`~llm.base.LLMBackend` plus the real retrieval/prompt pipeline.
  The caller is responsible for constructing the backend and for passing
  a real ``execute_fn`` (e.g. :func:`database.executor.execute_sql`); this
  module never imports either at module scope.

Design constraint
------------------
No name at module scope constructs a database engine, opens a socket, or
instantiates an LLM backend. Everything that could do so is received as a
parameter (``backend``, ``execute_fn``) by the function that needs it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pandas as pd

from eval.fingerprint import fingerprint_dataframe
from eval.models import CaseResult, CaseStatus, GoldenCase
from llm.base import LLMBackend
from prompt_engine.builder import PromptBuilder
from retrieval.context_retriever import ContextRetriever
from security.sql_guard import clean_sql, validate_sql

#: A callable that turns a natural-language question into cleaned SQL text.
#: Must raise ``ValueError("OUT_OF_SCOPE")`` for out-of-scope questions.
GenerateFn = Callable[[str], str]

#: A callable that executes a validated SQL string and returns a DataFrame.
ExecuteFn = Callable[[str], pd.DataFrame]

_OUT_OF_SCOPE_SENTINEL = "OUT_OF_SCOPE"


# ---------------------------------------------------------------------------
# Golden set loading
# ---------------------------------------------------------------------------


def load_golden_cases(path: str | Path) -> list[GoldenCase]:
    """Load every case from a ``golden.jsonl`` file.

    Each non-blank line must be one JSON object accepted by
    :meth:`~eval.models.GoldenCase.from_dict`.

    Parameters
    ----------
    path:
        Path to a ``.jsonl`` file, one golden case per line.

    Returns
    -------
    list[GoldenCase]

    Raises
    ------
    ValueError
        If a line is not valid JSON, if a line fails
        :class:`~eval.models.GoldenCase` validation, or if the file
        contains no cases at all.

    Examples
    --------
    >>> import tempfile, os
    >>> content = (
    ...     '{"id": "a", "question": "how many?", "expected_sql": "SELECT 1"}\\n'
    ...     '\\n'
    ...     '{"id": "b", "question": "how many more?", "expected_sql": "SELECT 2"}\\n'
    ... )
    >>> fd, path = tempfile.mkstemp(suffix=".jsonl")
    >>> _ = os.write(fd, content.encode("utf-8"))
    >>> os.close(fd)
    >>> cases = load_golden_cases(path)
    >>> [c.id for c in cases]
    ['a', 'b']
    >>> os.remove(path)
    """
    golden_path = Path(path)
    cases: list[GoldenCase] = []
    with golden_path.open("r", encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{golden_path}:{line_no}: invalid JSON: {exc}") from exc
            try:
                cases.append(GoldenCase.from_dict(data))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{golden_path}:{line_no}: {exc}") from exc

    if not cases:
        raise ValueError(f"{golden_path}: no golden cases found")
    return cases


# ---------------------------------------------------------------------------
# Single-case execution
# ---------------------------------------------------------------------------


def run_case(case: GoldenCase, generate_fn: GenerateFn, execute_fn: ExecuteFn) -> CaseResult:
    """Run one :class:`~eval.models.GoldenCase` through generate → guard → execute → fingerprint.

    Parameters
    ----------
    case:
        The golden case to run.
    generate_fn:
        See :data:`GenerateFn`. Called with ``case.question``.
    execute_fn:
        See :data:`ExecuteFn`. Called with the generator's cleaned SQL,
        only after :func:`~security.sql_guard.validate_sql` has accepted
        it.

    Returns
    -------
    CaseResult
        Always returned — this function never raises for an individual
        case's generation/guard/execution failures; those are captured as
        a non-``"pass"`` :class:`~eval.models.CaseStatus` on the result.
        A bug in ``generate_fn``/``execute_fn`` that raises something
        genuinely unexpected (not caught by the ``except`` clauses below,
        e.g. a ``KeyboardInterrupt``) still propagates.

    Notes
    -----
    ``case.expect == "error"`` cases are pushed through the exact same
    path as ``"success"``/``"empty"`` cases (they still require
    ``expected_sql`` per :meth:`~eval.models.GoldenCase.__post_init__`).
    That expectation value exists for the harness's *own* test suite, to
    exercise the ``generation_error``/``execution_error``/``guard_rejected``
    branches deterministically with a case that is *documented* as
    expected to fail — it is not expected to appear in a real golden set.

    Examples
    --------
    A case whose generator reproduces the reference SQL exactly passes:

    >>> import pandas as pd
    >>> from eval.models import GoldenCase
    >>> from eval.fingerprint import fingerprint_dataframe
    >>> df = pd.DataFrame({"n": [3]})
    >>> case = GoldenCase(
    ...     id="c1", question="how many?",
    ...     expected_sql="SELECT COUNT(*) AS n FROM Contract",
    ...     expected_fingerprint=fingerprint_dataframe(df),
    ... )
    >>> result = run_case(case, lambda q: case.expected_sql, lambda sql: df)
    >>> result.status
    'pass'
    >>> result.passed
    True

    A generator that raises the out-of-scope sentinel for an
    out-of-scope case also passes:

    >>> oos_case = GoldenCase(id="c2", question="who won the war?", expect="out_of_scope")
    >>> def oos_generate(q):
    ...     raise ValueError("OUT_OF_SCOPE")
    >>> run_case(oos_case, oos_generate, lambda sql: df).status
    'pass'
    """
    start = time.perf_counter()

    def _finish(
        status: CaseStatus,
        *,
        generated_sql: str | None = None,
        actual_fingerprint: str | None = None,
        error: str | None = None,
    ) -> CaseResult:
        return CaseResult(
            case_id=case.id,
            question=case.question,
            tags=list(case.tags),
            status=status,
            generated_sql=generated_sql,
            actual_fingerprint=actual_fingerprint,
            error=error,
            latency_seconds=time.perf_counter() - start,
        )

    try:
        raw_sql = generate_fn(case.question)
    except ValueError as exc:
        if str(exc) == _OUT_OF_SCOPE_SENTINEL:
            if case.is_out_of_scope:
                return _finish("pass")
            return _finish(
                "unexpected_out_of_scope",
                error="generator raised OUT_OF_SCOPE for a case that expects data",
            )
        return _finish("generation_error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - any generator failure is a harness result, not a crash
        return _finish("generation_error", error=f"{type(exc).__name__}: {exc}")

    if case.is_out_of_scope:
        return _finish(
            "missed_out_of_scope",
            generated_sql=raw_sql,
            error=f"expected OUT_OF_SCOPE but generator returned SQL: {raw_sql!r}",
        )

    try:
        validate_sql(raw_sql)
    except ValueError as exc:
        return _finish("guard_rejected", generated_sql=raw_sql, error=str(exc))

    try:
        df = execute_fn(raw_sql)
    except Exception as exc:  # noqa: BLE001 - any execution failure is a harness result, not a crash
        return _finish("execution_error", generated_sql=raw_sql, error=f"{type(exc).__name__}: {exc}")

    actual_fingerprint = fingerprint_dataframe(df)
    if case.expected_fingerprint is None or actual_fingerprint == case.expected_fingerprint:
        return _finish("pass", generated_sql=raw_sql, actual_fingerprint=actual_fingerprint)

    return _finish(
        "fingerprint_mismatch",
        generated_sql=raw_sql,
        actual_fingerprint=actual_fingerprint,
        error=(
            f"expected fingerprint {case.expected_fingerprint} but got "
            f"{actual_fingerprint}"
        ),
    )


def run_golden_set(
    cases: Sequence[GoldenCase], generate_fn: GenerateFn, execute_fn: ExecuteFn
) -> list[CaseResult]:
    """Run every case in *cases* through :func:`run_case`, in order.

    Parameters
    ----------
    cases:
        The golden set to run.
    generate_fn, execute_fn:
        See :func:`run_case`.

    Returns
    -------
    list[CaseResult]
        One result per input case, same order.

    Examples
    --------
    >>> import pandas as pd
    >>> from eval.models import GoldenCase
    >>> from eval.fingerprint import fingerprint_dataframe
    >>> df = pd.DataFrame({"n": [1]})
    >>> cases = [
    ...     GoldenCase(id="a", question="q1", expected_sql="SELECT 1",
    ...                expected_fingerprint=fingerprint_dataframe(df)),
    ...     GoldenCase(id="b", question="q2", expected_sql="SELECT 2",
    ...                expected_fingerprint=fingerprint_dataframe(df)),
    ... ]
    >>> results = run_golden_set(cases, lambda q: "SELECT 1", lambda sql: df)
    >>> [r.case_id for r in results]
    ['a', 'b']
    """
    return [run_case(case, generate_fn, execute_fn) for case in cases]


# ---------------------------------------------------------------------------
# Offline / CI mode: replay generator + executor built from the golden set
# ---------------------------------------------------------------------------


def make_offline_generator(cases: Sequence[GoldenCase]) -> GenerateFn:
    """Build a :data:`GenerateFn` that replays each case's ``expected_sql``.

    No LLM is called. This is a *fixture*, not a model of generation
    quality: it verifies the harness plumbing (guard, executor wiring,
    fingerprinting, reporting) and, as a side effect, that every
    reference SQL string in the golden set still passes
    :func:`~security.sql_guard.validate_sql` — a guard regression on
    legitimate queries would show up as ``guard_rejected`` here.

    Parameters
    ----------
    cases:
        The golden set. Every case's ``question`` must be unique (offline
        replay looks a case up by its exact question text).

    Returns
    -------
    GenerateFn

    Raises
    ------
    ValueError
        At construction time, if two cases share the same ``question``.

    Examples
    --------
    >>> from eval.models import GoldenCase
    >>> cases = [GoldenCase(id="a", question="how many?", expected_sql="SELECT 1")]
    >>> generate = make_offline_generator(cases)
    >>> generate("how many?")
    'SELECT 1'

    Out-of-scope cases raise the sentinel, matching the live contract:

    >>> oos = [GoldenCase(id="b", question="who won?", expect="out_of_scope")]
    >>> make_offline_generator(oos)("who won?")
    Traceback (most recent call last):
        ...
    ValueError: OUT_OF_SCOPE
    """
    by_question: dict[str, GoldenCase] = {}
    for case in cases:
        if case.question in by_question:
            raise ValueError(
                f"duplicate question in golden set: {case.question!r} "
                f"(case ids {by_question[case.question].id!r} and {case.id!r})"
            )
        by_question[case.question] = case

    def _generate(question: str) -> str:
        try:
            case = by_question[question]
        except KeyError:
            raise ValueError(
                f"offline generator: no golden case recorded for question: {question!r}"
            ) from None
        if case.is_out_of_scope:
            raise ValueError(_OUT_OF_SCOPE_SENTINEL)
        assert case.expected_sql is not None  # guaranteed by GoldenCase.__post_init__
        return case.expected_sql

    return _generate


def make_offline_executor(cases: Sequence[GoldenCase]) -> ExecuteFn:
    """Build an :data:`ExecuteFn` that replays each case's ``expected_rows``.

    No database is touched. The SQL argument is used only as a lookup key
    against the golden set's ``expected_sql`` strings (populated by
    :func:`make_offline_generator`, so in offline mode the lookup always
    hits by construction).

    Parameters
    ----------
    cases:
        The golden set. Every non-out-of-scope case's ``expected_sql``
        must be unique.

    Returns
    -------
    ExecuteFn

    Raises
    ------
    ValueError
        At construction time, if two cases share the same
        ``expected_sql``.

    Examples
    --------
    >>> from eval.models import GoldenCase
    >>> cases = [GoldenCase(
    ...     id="a", question="how many?", expected_sql="SELECT COUNT(*) AS n FROM T",
    ...     expected_rows=[{"n": 3}],
    ... )]
    >>> execute = make_offline_executor(cases)
    >>> execute("SELECT COUNT(*) AS n FROM T").to_dict("records")
    [{'n': 3}]

    A SQL string with no matching case raises, mirroring a database error:

    >>> execute("SELECT * FROM Nowhere")
    Traceback (most recent call last):
        ...
    RuntimeError: offline executor: no recorded rows for SQL: 'SELECT * FROM Nowhere'
    """
    by_sql: dict[str, GoldenCase] = {}
    for case in cases:
        if case.expected_sql is None:
            continue
        if case.expected_sql in by_sql:
            raise ValueError(
                f"duplicate expected_sql in golden set: case ids "
                f"{by_sql[case.expected_sql].id!r} and {case.id!r}"
            )
        by_sql[case.expected_sql] = case

    def _execute(sql: str) -> pd.DataFrame:
        try:
            case = by_sql[sql]
        except KeyError:
            raise RuntimeError(f"offline executor: no recorded rows for SQL: {sql!r}") from None
        if case.expected_rows is None:
            raise RuntimeError(
                f"offline executor: golden case {case.id!r} has no expected_rows "
                "recorded for offline replay"
            )
        return pd.DataFrame(case.expected_rows)

    return _execute


# ---------------------------------------------------------------------------
# Live mode: real LLM backend + real retrieval/prompt pipeline
# ---------------------------------------------------------------------------


def make_live_structured_generator(backend: LLMBackend, system_prompt: str) -> GenerateFn:
    """Build a :data:`GenerateFn` using Phase 2 task 3's constrained JSON output.

    Mirrors :func:`make_live_generator` exactly, except the LLM is asked
    for a single object matching
    :data:`~llm.structured_schema.SQL_GENERATION_SCHEMA` (via
    :meth:`~llm.base.LLMBackend.generate_structured`, fed segmented
    prompts built by :func:`~llm.router.build_prompt_segments`) instead of
    free text. This is the ``--structured`` half of ``eval.cli``'s
    comparison: run the same golden set through both generators and
    compare the reports.

    ``clean_sql`` is still applied to the extracted ``sql`` string (not
    skipped) — it is idempotent on already-clean SQL, and this function
    makes no claim about which fence-stripping/preamble-removal behaviour
    the structured path still needs; that is exactly what the comparison
    is for.

    Parameters
    ----------
    backend, system_prompt:
        Same as :func:`make_live_generator`.

    Returns
    -------
    GenerateFn

    Examples
    --------
    >>> class StubBackend:
    ...     name = "stub"
    ...     def generate(self, prompt: str) -> str:
    ...         raise AssertionError("text path should not be used")
    ...     def generate_structured(self, segments, schema):
    ...         return {"sql": "SELECT 1", "out_of_scope": False}, {}
    >>> generate = make_live_structured_generator(StubBackend(), "You are a T-SQL expert.")
    >>> generate("how many customers?")
    'SELECT 1'
    """
    from llm.router import build_prompt_segments
    from llm.structured_schema import SQL_GENERATION_SCHEMA, sql_from_structured

    def _generate(question: str) -> str:
        context = ContextRetriever.retrieve(question)
        segments = build_prompt_segments(question, system_prompt, context)
        obj, _meta = backend.generate_structured(segments, SQL_GENERATION_SCHEMA)
        sql = sql_from_structured(obj)  # raises ValueError("OUT_OF_SCOPE") if flagged
        return clean_sql(sql)

    return _generate


def measure_prefix_cache(
    backend: LLMBackend, system_prompt: str, question: str,
) -> dict[str, object]:
    """Run *question* through *backend* twice and report the prefix-cache effect.

    This is the Phase 2 (latency) measurement the CLI's ``--live`` run is
    responsible for producing: two identical calls, back to back, with
    wall-clock time and ``usage.prompt_tokens`` ("prompt_tokens") recorded
    for each. Task 1's static prefix is byte-identical across both calls
    (same *system_prompt*, same retrieved context for the same
    *question*), so on a caching-capable endpoint (llama.cpp/vLLM, or an
    OpenAI-compatible server with automatic prompt caching) the second
    call's ``prompt_tokens`` should be far lower than the first's — that
    gap, translated into the contract's ``prefix_cache_hit`` boolean via
    :func:`~observability.llm_status.build_llm_status`, is the number this
    function exists to surface. It draws no conclusion of its own beyond
    reporting what happened.

    Parameters
    ----------
    backend:
        A constructed :class:`~llm.base.LLMBackend` (e.g.
        :class:`~llm.providers.OpenAIBackend`). Never constructed
        here — same design constraint as :func:`make_live_generator`.
    system_prompt:
        The system prompt text.
    question:
        A single natural-language question, asked twice.

    Returns
    -------
    dict[str, object]
        ``{"first": {...}, "second": {...}, "prefix_cache_hit": bool}``,
        where each ``{...}`` is
        ``{"prompt_tokens": int, "wall_clock_seconds": float}``.

    Raises
    ------
    Exception
        Whatever *backend*'s transport raises (e.g. a connection error
        when no real endpoint is reachable) — this function makes no
        attempt to hide a failed measurement behind a fabricated result.

    Examples
    --------
    >>> class StubBackend:
    ...     name = "stub"
    ...     def __init__(self):
    ...         self._n = 0
    ...     def generate_with_meta(self, prompt):
    ...         self._n += 1
    ...         # Simulate a cache hit: far fewer prompt tokens the 2nd call.
    ...         count = 4000 if self._n == 1 else 50
    ...         return "SELECT 1", {"raw": {"usage": {"prompt_tokens": count}}}
    >>> report = measure_prefix_cache(StubBackend(), "You are a T-SQL expert.", "how many?")
    >>> report["first"]["prompt_tokens"], report["second"]["prompt_tokens"]
    (4000, 50)
    >>> report["prefix_cache_hit"]
    True
    """
    from observability.llm_status import build_llm_status
    from prompt_engine.static_prefix import static_prefix_token_estimate
    from prompt_engine.builder import PromptBuilder

    context = ContextRetriever.retrieve(question)
    prompt = PromptBuilder.build(question=question, system_prompt=system_prompt, context=context)
    static_prefix_tokens = static_prefix_token_estimate(system_prompt)

    def _one_call() -> dict[str, object]:
        start = time.perf_counter()
        _raw, meta = backend.generate_with_meta(prompt)
        elapsed = time.perf_counter() - start
        status = build_llm_status(
            meta.get("raw"), model=backend.name, static_prefix_tokens=static_prefix_tokens,
            finish_reason="stop",
        )
        return {"prompt_tokens": status["prompt_tokens"], "wall_clock_seconds": elapsed}

    first = _one_call()
    second = _one_call()
    prefix_cache_hit = (
        static_prefix_tokens > 0
        and second["prompt_tokens"] > 0
        and second["prompt_tokens"] < static_prefix_tokens * 0.5
    )
    return {"first": first, "second": second, "prefix_cache_hit": prefix_cache_hit}


def make_live_generator(backend: LLMBackend, system_prompt: str) -> GenerateFn:
    """Build a :data:`GenerateFn` backed by a real :class:`~llm.base.LLMBackend`.

    Runs the same retrieval → prompt → generate → clean pipeline as
    :func:`llm.wizard_llm.generate_sql`, minus the final
    :func:`~security.sql_guard.validate_sql` call — that step is applied
    uniformly by :func:`run_case` for every mode, not duplicated here.

    Parameters
    ----------
    backend:
        A constructed :class:`~llm.base.LLMBackend` (e.g.
        :class:`~llm.providers.OpenAIBackend`). Constructing it is
        the caller's responsibility — this function never instantiates a
        backend itself, so it never opens a network connection on its
        own.
    system_prompt:
        The system prompt text (see ``prompts/system_prompt.md``).

    Returns
    -------
    GenerateFn

    Examples
    --------
    >>> class StubBackend:
    ...     name = "stub"
    ...     def generate(self, prompt: str) -> str:
    ...         return "```sql\\nSELECT 1\\n```"
    >>> generate = make_live_generator(StubBackend(), "You are a T-SQL expert.")
    >>> generate("how many customers?")
    'SELECT 1'
    """

    def _generate(question: str) -> str:
        context = ContextRetriever.retrieve(question)
        prompt = PromptBuilder.build(
            question=question,
            system_prompt=system_prompt,
            context=context,
        )
        raw = backend.generate(prompt)
        return clean_sql(raw)

    return _generate
