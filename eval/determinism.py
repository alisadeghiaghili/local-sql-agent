# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Measure whether a live generator produces byte-identical SQL under repetition.

Why this exists
----------------
Every SQL-generation call is sent with ``temperature: 0``, ``top_p: 1`` and a
fixed ``seed``, and it is tempting to describe that as "deterministic — the
same question always produces the same SQL". That claim does not hold in
general. ``seed`` is a server-side capability the endpoint may ignore
outright, and even where greedy decoding is honoured, a server doing
continuous batching (vLLM and friends) can accumulate floating-point
reductions in a different order depending on what else shares the batch.
The logits differ in their last few bits, and wherever two candidate tokens
are near-tied the argmax can flip — with the request's own ``seed`` playing
no part in it. GPU-level non-determinism (atomics, kernel selection)
compounds this further. So "is this endpoint deterministic" is not a global
assumption to assert; it is a per-endpoint property to *measure*, and this
module measures it: run each golden question through the live generator
several times and check whether the generated SQL comes back byte-identical.

The trap this module refuses to fall into
------------------------------------------
:mod:`eval.runner` also offers :func:`~eval.runner.make_offline_generator`,
a fixture that replays each golden case's own ``expected_sql`` from a
lookup table — no model involved. Running this probe against that
generator would report 100% determinism, always, because the "generator"
is a dictionary lookup, not a model: the same key always maps to the same
value. That figure would be indistinguishable in shape from a real
measurement while meaning nothing at all — the same failure mode this
codebase has already hit twice, with an offline accuracy figure that was
true by construction and a ``prefix_cache_hit`` that was vacuously true at
zero prompt tokens.

This module does not special-case that fixture, because it cannot: a
``Callable[[str], str]`` carries no marker saying which factory built it,
and inspecting a closure's origin would be a fragile, easily-defeated
guard. This is the same reason :func:`~eval.runner.run_case` and
:func:`~eval.runner.run_golden_set` never ask whether their ``generate_fn``
is "offline" or "live" either — that policy question is answered once, at
the edge, by :mod:`eval.cli`, which refuses to invoke this module's
:func:`probe_determinism` unless ``--live`` was explicitly requested. See
``eval/cli.py``'s ``--determinism`` handling for the actual refusal and its
message.

What this module does check for itself
----------------------------------------
:func:`probe_determinism` will not report anything for ``repeats < 2``: a
single draw has nothing to compare against, and would report 100% by
construction for the same reason offline replay does — there is no second
sample to disagree with the first.

Design notes
------------
* Determinism is measured **per question**, not as a single run-level
  boolean: an endpoint stable on 18 of 20 questions is a materially
  different situation from one stable on 2 of 20, and reporting only an
  aggregate rate would hide exactly the distinction that matters.
* Comparison is on the **generated SQL text**, not the executed result
  set. Two different queries can return identical rows, which would mask
  the very variation this probe exists to surface -- so
  :mod:`eval.fingerprint` (built for the opposite goal, treating
  differently-*written* but equal-*data* results as the same) is
  deliberately not used here.
* No retries, no smoothing, no "best of N". If the generated SQL varies
  across repeats, that variation *is* the finding, and every distinct
  variant is kept for inspection -- the diff between two variants is what
  tells an operator whether the drift was a harmless alias rename or an
  entirely different query.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval.models import GoldenCase
from eval.runner import GenerateFn

#: Fewer than this many repeats cannot measure determinism at all -- see
#: :func:`probe_determinism`.
MIN_REPEATS = 2

#: Default number of times each question is generated when the caller (the
#: CLI) does not override it. Small on purpose: this probe is meant to be
#: run deliberately against a real endpoint, not folded into a fast CI loop.
DEFAULT_REPEATS = 3

#: Sentinel recorded as a "variant" when the generator raises the
#: out-of-scope signal for a repeat, matching the string
#: :class:`~llm.base.LLMBackend` implementations raise via
#: ``ValueError("OUT_OF_SCOPE")``. Treating this as a variant (rather than
#: an error) is deliberate: a generator that answers a question with real
#: SQL on one repeat and refuses it as out-of-scope on the next is exactly
#: the kind of instability this probe exists to catch.
OUT_OF_SCOPE_VARIANT = "OUT_OF_SCOPE"


def _generate_variant(generate_fn: GenerateFn, question: str) -> str:
    """Call *generate_fn* once and reduce its outcome to one comparable string.

    Parameters
    ----------
    generate_fn:
        See :data:`eval.runner.GenerateFn`.
    question:
        The natural-language question to generate SQL for.

    Returns
    -------
    str
        The cleaned SQL text, or :data:`OUT_OF_SCOPE_VARIANT` if *generate_fn*
        raised the out-of-scope sentinel.

    Raises
    ------
    Exception
        Any exception *other* than ``ValueError("OUT_OF_SCOPE")`` propagates
        unchanged -- a transport failure is a different kind of problem
        than variation, and folding it into the determinism figure would
        hide it. See the module docstring's "no retries, no smoothing" note.

    Examples
    --------
    >>> _generate_variant(lambda q: "SELECT 1", "how many?")
    'SELECT 1'
    >>> def _refuse(q: str) -> str:
    ...     raise ValueError("OUT_OF_SCOPE")
    >>> _generate_variant(_refuse, "who won the war?")
    'OUT_OF_SCOPE'
    """
    try:
        return generate_fn(question)
    except ValueError as exc:
        if str(exc) == OUT_OF_SCOPE_VARIANT:
            return OUT_OF_SCOPE_VARIANT
        raise


@dataclass(frozen=True, slots=True)
class QuestionDeterminism:
    """Determinism outcome for one golden question, across ``runs`` repeats.

    Parameters
    ----------
    case_id:
        Matches :attr:`~eval.models.GoldenCase.id`.
    question:
        Copied from the case, for standalone readability of a report.
    runs:
        Number of times the generator was called for this question.
    variants:
        Every *distinct* generated-SQL string observed, in first-seen
        order. Length 1 means every repeat produced the same text; length
        greater than 1 means it varied.

    Examples
    --------
    A single variant across every repeat is deterministic:

    >>> stable = QuestionDeterminism("a", "how many?", runs=3, variants=["SELECT 1"])
    >>> stable.is_deterministic
    True

    More than one distinct variant is not:

    >>> varied = QuestionDeterminism("b", "how many?", runs=3, variants=["SELECT 1", "SELECT 1 "])
    >>> varied.is_deterministic
    False
    """

    case_id: str
    question: str
    runs: int
    variants: list[str]

    def __post_init__(self) -> None:
        if self.runs < 1:
            raise ValueError(f"QuestionDeterminism {self.case_id!r}: runs must be >= 1")
        if not self.variants:
            raise ValueError(f"QuestionDeterminism {self.case_id!r}: variants must be non-empty")
        if len(self.variants) > self.runs:
            raise ValueError(
                f"QuestionDeterminism {self.case_id!r}: {len(self.variants)} distinct variants "
                f"cannot exceed {self.runs} runs"
            )

    @property
    def is_deterministic(self) -> bool:
        """True iff every repeat produced the exact same SQL text."""
        return len(self.variants) <= 1

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict.

        Examples
        --------
        >>> QuestionDeterminism("a", "q", runs=2, variants=["SELECT 1"]).to_dict()["is_deterministic"]
        True
        """
        return {
            "case_id": self.case_id,
            "question": self.question,
            "runs": self.runs,
            "is_deterministic": self.is_deterministic,
            "variant_count": len(self.variants),
            "variants": list(self.variants),
        }


@dataclass(frozen=True, slots=True)
class DeterminismReport:
    """Aggregate result of probing a golden set's determinism once.

    Parameters
    ----------
    endpoint:
        Identifies which live endpoint produced this report (e.g. an
        :class:`~llm.base.LLMBackend`'s ``name``, such as
        ``"openai:gpt-oss-20b"``). Determinism is a property of the endpoint,
        not of the model weights alone -- the same weights served through
        vLLM versus llama.cpp will not necessarily score the same -- so
        every report carries this so results are comparable across runs.
    repeats:
        Number of times each question was generated.
    total:
        Number of questions probed.
    deterministic:
        Number of questions where every repeat produced the same SQL.
    determinism_rate_pct:
        ``100 * deterministic / total`` (``0.0`` when ``total == 0``).
    results:
        One :class:`QuestionDeterminism` per question, same order as the
        input cases.
    generated_at:
        ISO-8601 UTC timestamp of when the report was built.

    Examples
    --------
    >>> results = [
    ...     QuestionDeterminism("a", "q1", runs=3, variants=["SELECT 1"]),
    ...     QuestionDeterminism("b", "q2", runs=3, variants=["SELECT 2", "SELECT  2"]),
    ... ]
    >>> report = DeterminismReport(
    ...     endpoint="openai:gpt-oss-20b", repeats=3, total=2, deterministic=1,
    ...     determinism_rate_pct=50.0, results=results, generated_at="2026-08-30T00:00:00+00:00",
    ... )
    >>> report.varied
    [QuestionDeterminism(case_id='b', question='q2', runs=3, variants=['SELECT 2', 'SELECT  2'])]
    """

    endpoint: str
    repeats: int
    total: int
    deterministic: int
    determinism_rate_pct: float
    results: list[QuestionDeterminism]
    generated_at: str

    @property
    def varied(self) -> list[QuestionDeterminism]:
        """The subset of :attr:`results` that were *not* deterministic."""
        return [r for r in self.results if not r.is_deterministic]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict.

        Examples
        --------
        >>> report = DeterminismReport(
        ...     endpoint="openai:gpt-oss-20b", repeats=3, total=1, deterministic=1,
        ...     determinism_rate_pct=100.0,
        ...     results=[QuestionDeterminism("a", "q", runs=3, variants=["SELECT 1"])],
        ...     generated_at="2026-08-30T00:00:00+00:00",
        ... )
        >>> report.to_dict()["endpoint"]
        'openai:gpt-oss-20b'
        """
        return {
            "endpoint": self.endpoint,
            "repeats": self.repeats,
            "total": self.total,
            "deterministic": self.deterministic,
            "determinism_rate_pct": self.determinism_rate_pct,
            "generated_at": self.generated_at,
            "results": [r.to_dict() for r in self.results],
        }


def probe_determinism(
    generate_fn: GenerateFn,
    cases: Sequence[GoldenCase],
    *,
    endpoint: str,
    repeats: int = DEFAULT_REPEATS,
) -> DeterminismReport:
    """Run every case in *cases* through *generate_fn* ``repeats`` times and compare.

    For each case, ``generate_fn(case.question)`` is called ``repeats``
    times in a row and every distinct resulting SQL string (or
    :data:`OUT_OF_SCOPE_VARIANT`) is kept. A question is "deterministic" iff
    all ``repeats`` calls produced the exact same text.

    This function is deliberately agnostic about whether *generate_fn* is
    "live" or "offline" -- exactly as :func:`eval.runner.run_case` is
    agnostic about it -- because a plain ``Callable[[str], str]`` carries no
    such marker. Refusing to run against an offline replay fixture is
    :mod:`eval.cli`'s job, enforced at the one place that actually knows
    which factory built the generator; see the module docstring.

    Parameters
    ----------
    generate_fn:
        See :data:`eval.runner.GenerateFn`. Called ``repeats`` times per
        case, with no caching or memoisation -- each call is a fresh
        request to whatever *generate_fn* wraps.
    cases:
        The golden questions to probe. Only ``question`` and ``id`` are
        used; ``expected_sql`` is not consulted (this measures repetition
        stability, not correctness -- see :mod:`eval.runner` for accuracy).
    endpoint:
        Identifier for the endpoint under test, carried onto the report so
        results are comparable across runs. Must be non-empty.
    repeats:
        Number of times to call *generate_fn* per question. Must be at
        least :data:`MIN_REPEATS` (2) -- a single draw has no second sample
        to compare against and would report 100% by construction, the same
        vacuous-by-construction shape this module exists to avoid.

    Returns
    -------
    DeterminismReport

    Raises
    ------
    ValueError
        If *repeats* is below :data:`MIN_REPEATS`, if *endpoint* is empty,
        or if *cases* is empty.
    Exception
        Whatever *generate_fn* raises for a reason other than the
        out-of-scope sentinel propagates immediately and aborts the probe
        -- see :func:`_generate_variant`.

    Examples
    --------
    A generator that always returns the same SQL is fully deterministic:

    >>> from eval.models import GoldenCase
    >>> cases = [GoldenCase(id="a", question="how many?", expected_sql="SELECT 1")]
    >>> report = probe_determinism(lambda q: "SELECT 1", cases, endpoint="stub:v1")
    >>> report.determinism_rate_pct
    100.0
    >>> report.varied
    []

    A generator that alternates between two answers is caught, and both
    variants are kept:

    >>> responses = iter(["SELECT 1", "SELECT 2", "SELECT 1"])
    >>> report = probe_determinism(lambda q: next(responses), cases, endpoint="stub:v1")
    >>> report.determinism_rate_pct
    0.0
    >>> sorted(report.results[0].variants)
    ['SELECT 1', 'SELECT 2']

    ``repeats=1`` is rejected rather than silently reporting a meaningless 100%:

    >>> probe_determinism(lambda q: "SELECT 1", cases, endpoint="stub:v1", repeats=1)
    Traceback (most recent call last):
        ...
    ValueError: probe_determinism requires repeats >= 2 to measure anything (got repeats=1)
    """
    if repeats < MIN_REPEATS:
        raise ValueError(
            f"probe_determinism requires repeats >= {MIN_REPEATS} to measure anything "
            f"(got repeats={repeats})"
        )
    if not endpoint or not endpoint.strip():
        raise ValueError("probe_determinism requires a non-empty endpoint identifier")
    if not cases:
        raise ValueError("probe_determinism requires at least one golden case")

    results: list[QuestionDeterminism] = []
    for case in cases:
        variants: list[str] = []
        for _ in range(repeats):
            variant = _generate_variant(generate_fn, case.question)
            if variant not in variants:
                variants.append(variant)
        results.append(
            QuestionDeterminism(
                case_id=case.id, question=case.question, runs=repeats, variants=variants,
            )
        )

    total = len(results)
    deterministic = sum(1 for r in results if r.is_deterministic)
    determinism_rate_pct = (100.0 * deterministic / total) if total else 0.0

    return DeterminismReport(
        endpoint=endpoint,
        repeats=repeats,
        total=total,
        deterministic=deterministic,
        determinism_rate_pct=determinism_rate_pct,
        results=results,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def _variant_diff(a: str, b: str) -> str:
    """Unified line diff between two SQL variants.

    Parameters
    ----------
    a, b:
        The two variant strings to compare.

    Returns
    -------
    str
        A unified diff (``difflib.unified_diff`` joined with newlines),
        empty when *a* and *b* are identical.

    Examples
    --------
    >>> _variant_diff("SELECT 1", "SELECT 1")
    ''
    >>> "SELECT 2" in _variant_diff("SELECT 1", "SELECT 2")
    True
    """
    diff = difflib.unified_diff(
        a.splitlines(), b.splitlines(), fromfile="variant 1", tofile="variant N", lineterm="",
    )
    return "\n".join(diff)


def render_determinism_text(report: DeterminismReport) -> str:
    """Render *report* as a human-readable multi-line summary.

    Parameters
    ----------
    report:
        The report to render, typically from :func:`probe_determinism`.

    Returns
    -------
    str
        Multi-line text: header (endpoint, repeats, rate), and for every
        question that varied, its distinct SQL variants plus a unified
        diff of each variant against the first -- the diff is what tells
        an operator whether the drift is a harmless alias rename or an
        entirely different query.

    Examples
    --------
    >>> results = [QuestionDeterminism("a", "how many?", runs=3, variants=["SELECT 1"])]
    >>> report = DeterminismReport(
    ...     endpoint="openai:gpt-oss-20b", repeats=3, total=1, deterministic=1,
    ...     determinism_rate_pct=100.0, results=results, generated_at="2026-08-30T00:00:00+00:00",
    ... )
    >>> text = render_determinism_text(report)
    >>> "Determinism rate: 100.00%" in text
    True
    >>> "No variation" in text
    True
    """
    lines: list[str] = []
    lines.append(
        f"Determinism probe (endpoint={report.endpoint!r}) — generated {report.generated_at}"
    )
    lines.append(
        f"Determinism rate: {report.determinism_rate_pct:.2f}% "
        f"({report.deterministic}/{report.total} questions stable across {report.repeats} runs)"
    )
    lines.append("")

    varied = report.varied
    if not varied:
        lines.append("No variation detected across any question.")
        return "\n".join(lines)

    lines.append(f"Varied questions ({len(varied)}/{report.total}):")
    for result in varied:
        lines.append(
            f"  [{result.case_id}] {result.question!r} "
            f"-- {len(result.variants)} distinct variants of {result.runs} runs:"
        )
        for i, variant in enumerate(result.variants, start=1):
            lines.append(f"    variant {i}: {variant}")
        first = result.variants[0]
        for i, variant in enumerate(result.variants[1:], start=2):
            diff_text = _variant_diff(first, variant)
            if diff_text:
                lines.append(f"    diff (variant 1 vs variant {i}):")
                for diff_line in diff_text.splitlines():
                    lines.append(f"      {diff_line}")

    return "\n".join(lines)


def save_determinism_json(report: DeterminismReport, path: str | Path) -> None:
    """Write *report* as indented JSON to *path*.

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
    >>> results = [QuestionDeterminism("a", "q", runs=2, variants=["SELECT 1"])]
    >>> report = DeterminismReport(
    ...     endpoint="stub:v1", repeats=2, total=1, deterministic=1,
    ...     determinism_rate_pct=100.0, results=results, generated_at="2026-08-30T00:00:00+00:00",
    ... )
    >>> fd, path = tempfile.mkstemp(suffix=".json")
    >>> os.close(fd)
    >>> save_determinism_json(report, path)
    >>> json.loads(open(path, encoding="utf-8").read())["endpoint"]
    'stub:v1'
    >>> os.remove(path)
    """
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
