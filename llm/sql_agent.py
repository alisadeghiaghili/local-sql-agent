# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""LLM-agnostic SQL agent with self-correction.

Pipeline (every attempt, first and corrections alike)
-------------------------------------------------------
    question
        → ContextRetriever
        → build_prompt_segments  (initial PromptSegments; corrections append
                                   every prior round's correction text to
                                   the *question* segment only — never the
                                   static_prefix, see "Prefix invariance"
                                   below)
        → LLMRouter.generate_for_task(SQL_GENERATION)  ─────────────────┐
        → clean_sql                                                     │ ValueError
        → validate_sql                                                  │ (bad syntax,
        → ensure_top             (injects TOP n if still absent)        │  unknown table,
        → execute_sql  ──────────────────────────────────────┐          │  forbidden kw...)
                                                              │ RuntimeError
Correction loop (up to MAX_CORRECTION_ATTEMPTS rounds)        │          │
────────────────────────────────────────────────             │          │
    error message                                            ↓          ↓
        → _build_correction_prompt   ←────────────────────────────────────┘
        (loops back to LLMRouter.generate_for_task above)

Both failure modes above — a guard rejecting the generated SQL
(ValueError) and the database rejecting a syntactically-valid query at
execution time (RuntimeError) — feed the SAME correction loop and share
the same ``max_corrections`` budget. Previously only execution failures
were retried; a validation failure (bad syntax, an unknown table, a
forbidden keyword — the most common small-model mistake) aborted the run
immediately with no correction attempt at all.

Not every guard rejection is retried, though: ``security.sql_guard`` now
raises one of two typed ``ValueError`` subclasses (see its module
docstring). A :class:`~security.sql_guard.CorrectableRejection` (bad
syntax, an unknown table, ...) keeps looping exactly as described above.
A :class:`~security.sql_guard.PolicyRejection` (a forbidden statement, a
denied column, ...) breaks out of the loop immediately instead — the
policy that caused it is not in the prompt, so re-prompting the model
cannot change the outcome, and doing so anyway would spend
``max_corrections`` extra LLM round trips just to reach the exact same
rejection a second, third, and fourth time. The raised exception and the
returned :class:`SQLGenerationResult` (were the call to succeed on a later
round, which it structurally cannot after this) are unaffected either way
— a caller switching on message text, as ``api/runner.py`` does, sees no
difference.

The loop aborts as soon as execution succeeds, a ``PolicyRejection`` is
hit, or the attempt cap is reached. On the final failed attempt (or the
first ``PolicyRejection``, whichever comes first) the last exception
(``ValueError`` for a validation failure, ``RuntimeError`` for an
execution failure) is re-raised so the caller (app.py / api/runner.py)
can log and translate it normally. ``OUT_OF_SCOPE`` is the one exception
this loop never retries — it's a terminal signal from the model, not a
fixable mistake.

Prefix invariance (why correction text never touches ``static_prefix``)
-------------------------------------------------------------------------
``build_prompt_segments`` (``llm/router.py``) splits the prompt into a
byte-identical ``static_prefix`` (schema, business rules, examples — cached
per system-prompt version) and a small variable ``question`` suffix. Every
correction round below rebuilds its :class:`~llm.router.PromptSegments`
by copying ``static_prefix``/``session_context`` from the FIRST round's
segments unchanged and appending the accumulated correction text only to
``question``. If correction text ever leaked into ``static_prefix``
instead, every retry would present the provider with a different prefix,
silently defeating implicit KV-cache reuse (llama.cpp/vLLM) or automatic
prompt caching (OpenAI's own hosted API) on the very requests that most need a fast retry — with
no test failing and no error raised. ``tests/test_sql_agent_router.py``
asserts this holds across correction rounds.

Design notes
------------
* ``SQLAgent`` depends on ``LLMBackend``/``LLMRouter`` (abstract), not on
  any one transport specifically. Pass ``backend=`` to use one backend directly (it is
  wrapped in a single-element :class:`~llm.router.LLMRouter` chain), or
  ``router=`` for full task-based routing, fallback chains, and governance
  — see :meth:`SQLAgent.__init__`.
* ``execute_fn`` is injected so the agent can be unit-tested without a
  real database (pass a mock / stub).
* When *execute_fn* is **not** supplied, the agent calls
  ``database.executor.execute_query`` through its module reference every
  time — this ensures that ``monkeypatch.setattr(database.executor,
  'execute_query', ...)`` in tests is visible at call time.
* Every correction round's prompt is the initial ``question`` segment
  followed by **every** correction prompt built so far (not just the
  latest), so the model always has the full history of what it already
  tried and was told was wrong — otherwise it can (and in practice does)
  repeat the exact same mistake on the next attempt.
* Worst-case LLM call count is unchanged by bringing validation failures
  into this loop: it was already bounded at ``max_corrections + 1`` total
  generation calls (by the execution-failure retry path alone);
  validation failures now consume rounds from that same existing budget
  instead of bypassing it via an immediate raise. A
  :class:`~security.sql_guard.PolicyRejection` is the one case that exits
  well below that bound — exactly one generation call, regardless of
  ``max_corrections`` — since no later round could ever do better.
* :meth:`run` calls :meth:`~llm.router.LLMRouter.generate_for_task`, which
  wraps a chain-exhausted failure in one ``RuntimeError`` with the
  original backend exception as ``__cause__`` (see ``llm/router.py``).
  :meth:`run` unwraps that immediately (``exc.__cause__ or exc``) so
  callers that switch on the ORIGINAL exception type — ``api/runner.py``'s
  ``ValueError("OUT_OF_SCOPE")`` / ``requests.Timeout`` /
  ``requests.ConnectionError`` / plain ``RuntimeError`` translation — keep
  working unchanged whether or not a router with multiple chain entries is
  involved. ``ValueError("OUT_OF_SCOPE")`` is the one exception the router
  never wraps at all: it short-circuits the chain and is re-raised as-is
  (``__cause__`` is ``None``, which ``exc.__cause__ or exc`` resolves back
  to the exception itself), because a decline is a terminal domain
  decision — a second backend must not get to answer a question the first
  one correctly refused.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

import pandas as pd

import config as cfg
from llm.base import LLMBackend, SQLGenerationResult
from llm.router import LLMRouter, PromptSegments, TaskType, build_prompt_segments
from observability.timing import StageTimer
from retrieval.context_retriever import ContextRetriever
from security.sql_guard import (
    PolicyRejection,
    clean_sql,
    ensure_top,
    transpile_and_revalidate,
    validate_sql,
)

logger = logging.getLogger(__name__)


def _stage(timer: StageTimer | None, name: str) -> AbstractContextManager[None]:
    """Return ``timer.stage(name)``, or a no-op context manager when *timer* is ``None``.

    Lets every internal step below be wrapped unconditionally, so
    :meth:`SQLAgent.run` reads the same whether or not a caller passed a
    timer (e.g. from a unit test that doesn't care about timings).
    """
    return timer.stage(name) if timer is not None else nullcontext()

MAX_CORRECTION_ATTEMPTS: int = 2

_CORRECTION_TEMPLATE = """
The SQL query you generated failed to execute.

--- FAILED SQL ---
{sql}

--- DATABASE ERROR ---
{error}

--- INSTRUCTIONS ---
Fix ONLY the SQL error above. Do not change the intent of the query.
Return only the corrected SQL statement with no explanation.

SQL:
"""


def _default_execute(sql: str) -> pd.DataFrame:
    """Call ``database.executor.execute_query`` via module lookup.

    Looking up through the module (rather than capturing the function at
    import time) means ``monkeypatch.setattr(database.executor,
    'execute_query', stub)`` is always visible at call time.
    """
    import database.executor as _executor_mod
    return _executor_mod.execute_query(sql)


class SQLAgent:
    """Orchestrates retrieval → prompt → LLM → execute with self-correction.

    Parameters
    ----------
    backend:
        Any :class:`LLMBackend` implementation. Honoured for backward
        compatibility: wrapped in a single-element
        :class:`~llm.router.LLMRouter` chain (``LLMRouter(default_chain=[backend])``)
        so callers that construct ``SQLAgent(backend=...)`` directly — a
        lot of the existing test suite, and ``llm/wizard_llm.py`` —
        keep working unchanged, just now routed through the same
        task-based machinery every other backend uses. Ignored if
        *router* is also given.
    router:
        An :class:`~llm.router.LLMRouter` for full task-based routing,
        fallback chains, per-task budgets, and remote-provider governance.
        Takes precedence over *backend* when both are given.
    execute_fn:
        Callable ``(sql: str) -> pd.DataFrame``.  Defaults to
        :func:`database.executor.execute_query` (looked up at call time so
        monkeypatching works).  Override in tests for explicit injection.
    max_corrections:
        How many correction rounds to attempt before giving up.

    Examples
    --------
    Backward-compatible single-backend construction still works, routed
    through a one-entry chain under the hood:

    >>> from llm.providers import MockBackend
    >>> agent = SQLAgent(backend=MockBackend(response="SELECT 1"), execute_fn=lambda sql: None)
    >>> agent._backend.name
    'mock:stub'

    A second provider serves SQL generation with no call-site change —
    only the constructor argument differs:

    >>> import pandas as pd
    >>> agent2 = SQLAgent(
    ...     backend=MockBackend(response="SELECT TOP 10 * FROM [Auction_Dim].[Customer]"),
    ...     execute_fn=lambda sql: pd.DataFrame({"Id": [1]}),
    ... )
    >>> df, result = agent2.run("q", system_prompt="")
    >>> result.sql
    'SELECT TOP 10 * FROM [Auction_Dim].[Customer]'
    """

    def __init__(
        self,
        backend: LLMBackend | None = None,
        execute_fn: Callable[[str], pd.DataFrame] | None = None,
        max_corrections: int = MAX_CORRECTION_ATTEMPTS,
        *,
        router: LLMRouter | None = None,
    ) -> None:
        if router is not None:
            self._router = router
        elif backend is not None:
            self._router = LLMRouter(default_chain=[backend])
        else:
            self._router = LLMRouter.from_settings()

        # Backward-compat attribute: api/runner.py reads `agent._backend`
        # directly (model name for QueryResponse.model, audit llm-status
        # display) -- mirrors the primary SQL_GENERATION backend, i.e. the
        # one this agent talks to before any fallback. The actual backend
        # that serves a given request may differ (see RouteResult.provider
        # / .fallback_used, threaded through SQLGenerationResult.llm_meta).
        self._backend = (
            backend if backend is not None
            else self._router._chain_for(TaskType.SQL_GENERATION)[0]
        )

        self._execute = execute_fn if execute_fn is not None else _default_execute
        self._max_corrections = max_corrections

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        question: str,
        system_prompt: str,
        *,
        timer: StageTimer | None = None,
        sql_cache_lookup: Callable[[str], pd.DataFrame | None] | None = None,
        denied_columns: Iterable[str] | None = None,
    ) -> tuple[pd.DataFrame, SQLGenerationResult]:
        """Answer *question* and return ``(DataFrame, SQLGenerationResult)``.

        Both a guard failure (bad syntax, unknown table, forbidden
        keyword — ``ValueError`` from ``clean_sql``/``validate_sql``) and
        a database execution failure (``RuntimeError``) are corrected in
        the SAME loop, sharing the same ``max_corrections`` budget — with
        one exception: a guard failure that is a
        :class:`~security.sql_guard.PolicyRejection` (a forbidden
        statement, a denied column, ...) is never retried, since the
        policy behind it isn't in the prompt and no re-prompt can change
        it; it raises immediately instead of consuming the rest of the
        budget on rounds that would only repeat the identical rejection.
        A :class:`~security.sql_guard.CorrectableRejection` keeps looping
        exactly as before. Each later round's prompt includes every prior
        round's correction prompt, not just the most recent, so the model
        has the full history of what it already tried.

        Parameters
        ----------
        question, system_prompt:
            As before.
        timer:
            Optional :class:`~observability.timing.StageTimer` supplied by
            the caller (e.g. ``api/runner.py``, one per request) to record
            per-stage durations — ``"plan"`` (context retrieval),
            ``"prompt"`` (prompt assembly), ``"llm"`` (accumulated across
            every ``generate_with_meta`` call, including retried
            corrections), ``"guard"`` (clean/validate/cap), and
            ``"execute"``. ``None`` (the default) disables timing
            entirely — every stage below becomes a no-op context manager
            in that case, so callers that don't care about timings (most
            unit tests) are unaffected.
        sql_cache_lookup:
            Optional callable ``(sql: str) -> pandas.DataFrame | None``,
            consulted immediately before executing the guard-approved SQL.
            A non-``None`` return is used as the result *instead of*
            calling ``execute_fn`` — this is Phase 2 task 6's "cache by
            generated SQL" win: two different questions that happen to
            produce the identical SQL string share one execution, skipping
            the (potentially slow) database round-trip for the second.
            ``None`` (the default) disables this — every call falls
            through to ``execute_fn`` as before. See
            ``api/runner.py``'s ``_sql_cache_lookup`` for how the cache
            key is built (prefix-version + mode + SQL text).
        denied_columns:
            Column names the caller's :class:`~security.auth.Principal`
            must never see (Phase 8) -- threaded straight through to every
            :func:`~security.sql_guard.validate_sql` call this method
            makes, on the first attempt and every correction round alike.
            ``None`` (the default) applies no column restriction, exactly
            this method's pre-Phase-8 behaviour.

        Raises
        ------
        ValueError("OUT_OF_SCOPE")
            When the model signals the question is out of scope.  Never
            retried — this is a terminal signal, not a fixable mistake.
        ValueError
            When SQL validation still fails after all correction attempts.
        RuntimeError
            When execution still fails after all correction attempts.
        """
        with _stage(timer, "plan"):
            context = ContextRetriever.retrieve(question)
        with _stage(timer, "prompt"):
            initial_segments = build_prompt_segments(question, system_prompt, context)

        correction_prompts: list[str] = []
        last_error: str | None = None
        sql = ""
        raw = ""
        llm_meta: dict[str, Any] = {}
        injected_top: int | None = None
        attempt = 1

        for correction_round in range(self._max_corrections + 1):
            segments = initial_segments
            if correction_round > 0:
                correction_prompt = _CORRECTION_TEMPLATE.format(
                    sql=sql or raw,
                    error=last_error,
                )
                correction_prompts.append(correction_prompt)
                # Every prior round's correction prompt, not just this one,
                # is appended to the *question* segment only -- never to
                # static_prefix/session_context -- so the model has the
                # full history of what it already tried while the prefix
                # stays byte-identical across every round (see the module
                # docstring's "Prefix invariance" section). Leaking this
                # text into static_prefix would silently defeat KV-cache
                # reuse on every retry.
                segments = PromptSegments(
                    static_prefix=initial_segments.static_prefix,
                    session_context=initial_segments.session_context,
                    question=initial_segments.question + "".join(correction_prompts),
                )
                attempt = correction_round + 1
                logger.info(
                    "Self-correction attempt %d/%d for question: %.80s",
                    correction_round,
                    self._max_corrections,
                    question,
                )

            # ValueError("OUT_OF_SCOPE") (or any other exception a backend
            # itself raises — a transport failure, say) is intentionally
            # NOT swallowed here: LLMRouter.generate_for_task wraps a
            # chain-exhausted failure in one RuntimeError with the
            # original exception as __cause__ (see llm/router.py), so it
            # is unwrapped and re-raised as that original exception --
            # propagating straight out of run(), uncorrected, exactly as
            # before the router existed. OUT_OF_SCOPE never reaches that
            # wrapper: the router short-circuits the chain and re-raises it
            # unwrapped, so `exc.__cause__ or exc` yields the very same
            # ValueError here (its __cause__ is None). The "llm" stage still records its
            # elapsed time (StageTimer.stage times the block even when it
            # raises — see its docstring), and OpenAIBackend attaches an
            # "llm_meta" attribute to the OUT_OF_SCOPE ValueError itself,
            # which survives the unwrap (it's the same exception object),
            # so a caller further up (api/runner.py) can still recover
            # call metadata for the audit trail on this uncaught path.
            with _stage(timer, "llm"):
                try:
                    route_result = self._router.generate_for_task(
                        TaskType.SQL_GENERATION, segments
                    )
                except Exception as exc:  # noqa: BLE001 - unwrapped and re-raised below
                    raise exc.__cause__ or exc
            raw = route_result.text or ""
            llm_meta = route_result.meta

            try:
                with _stage(timer, "guard"):
                    sql, injected_top = self._clean_validate_cap(raw, denied_columns=denied_columns)
            except PolicyRejection as exc:
                # The guard rejected this SQL for a reason no re-prompt can
                # fix (a forbidden statement, a denied column, ...) -- the
                # policy that caused it is not in the prompt, so every
                # further correction round would just spend another LLM
                # round trip to reach this exact same rejection. Break out
                # immediately, on the very first occurrence, with the SAME
                # outcome the final-attempt branch below produces (attempt
                # still 1, candidate_sql already attached by
                # _clean_validate_cap) -- see security.sql_guard's module
                # docstring for the taxonomy this relies on.
                last_error = str(exc)
                logger.warning(
                    "SQL failed validation (attempt %d): %s -- policy rejection, aborting without retry",
                    attempt,
                    last_error,
                )
                exc.llm_meta = llm_meta  # type: ignore[attr-defined]
                # How many generation attempts this request actually cost
                # -- 1 here, always, regardless of max_corrections -- so a
                # caller further up (api/runner.py's audit trail) can
                # report the honest count instead of implying the full
                # budget was spent chasing an unfixable rejection.
                exc.attempt = attempt  # type: ignore[attr-defined]
                raise
            except ValueError as exc:
                last_error = str(exc)
                logger.warning(
                    "SQL failed validation (attempt %d): %s",
                    attempt,
                    last_error,
                )
                if correction_round == self._max_corrections:
                    exc.llm_meta = llm_meta  # type: ignore[attr-defined]
                    exc.attempt = attempt  # type: ignore[attr-defined]
                    # _clean_validate_cap already attached candidate_sql
                    # (see its docstring) -- nothing further to add here.
                    raise
                continue

            try:
                with _stage(timer, "execute"):
                    df = None
                    if sql_cache_lookup is not None:
                        df = sql_cache_lookup(sql)
                    if df is None:
                        df = self._execute(sql)
                result = SQLGenerationResult(
                    sql=sql,
                    raw_response=raw,
                    attempt=attempt,
                    correction_prompts=correction_prompts,
                    llm_meta=llm_meta,
                    injected_top=injected_top,
                )
                if attempt > 1:
                    logger.info(
                        "Self-correction succeeded on attempt %d: %.120s",
                        attempt,
                        sql,
                    )
                return df, result

            except RuntimeError as exc:
                last_error = str(exc)
                logger.warning(
                    "Execution failed (attempt %d): %s",
                    attempt,
                    last_error,
                )
                if correction_round == self._max_corrections:
                    exc.llm_meta = llm_meta  # type: ignore[attr-defined]
                    # Unlike the ValueError branch above, this sql DID
                    # pass the guard -- it is exactly what SQLGenerationResult.sql
                    # would have carried on success, so attach it (and the
                    # cap that was actually applied) for the audit trail.
                    exc.candidate_sql = sql  # type: ignore[attr-defined]
                    exc.injected_top = injected_top  # type: ignore[attr-defined]
                    raise

        # unreachable
        raise RuntimeError("SQLAgent loop exited unexpectedly")  # pragma: no cover

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _clean_validate_cap(
        self, raw: str, *, denied_columns: Iterable[str] | None = None,
    ) -> tuple[str, int | None]:
        """clean_sql + validate_sql + ensure_top (+ transpile, for a non-tsql target).

        Returns
        -------
        tuple[str, int | None]
            ``(sql, injected_top)`` — *sql* is the validated, capped query,
            in this deployment's target dialect
            (:data:`config.Settings.sql_dialect`); *injected_top* is the
            row cap :func:`~security.sql_guard.ensure_top` injected, or
            ``None`` if the model's own SQL already carried a row-limit
            clause (``ensure_top`` returned its input unchanged). Detected
            by comparing ``ensure_top``'s output to its input, since
            ``ensure_top`` itself only returns the capped string, not
            whether it changed anything. This is measured on the model's
            ``tsql`` output *before* any transpilation, since that is where
            the cap is actually injected (see
            :func:`~security.sql_guard.transpile_and_revalidate`'s
            docstring: capping happens once, on tsql, and sqlglot carries
            the resulting row-limiting clause into the target dialect's
            own syntax when transpiling).

        Multi-dialect pipeline
        --------------------------
        The model always generates ``tsql`` (see
        :data:`config.Settings.sql_dialect`'s docstring for why), so
        :func:`~security.sql_guard.clean_sql`,
        :func:`~security.sql_guard.validate_sql`, and
        :func:`~security.sql_guard.ensure_top` all run pinned to ``tsql``
        here, completely unchanged from this method's pre-multi-dialect
        behaviour. Only the *last* step is new:
        :func:`~security.sql_guard.transpile_and_revalidate` transpiles the
        guard-approved, capped ``tsql`` text to
        ``cfg.settings.sql_dialect`` and re-validates the **transpiled**
        text with the guard pinned to that dialect before this method ever
        returns it for execution — never trusting that transpiled SQL
        which merely *parses* is also safe (see that function's own
        docstring for the finding that makes this non-negotiable). When
        the target dialect is ``"tsql"`` (this deployment's default), that
        step is a documented no-op passthrough: nothing above changes for
        an existing tsql-only deployment.

        A rejected candidate is still recoverable for the audit trail: on
        a ``ValueError``, the raised exception gets a ``candidate_sql``
        attribute — the last successfully-produced SQL text (cleaned,
        capped, or transpiled, whichever stage got furthest before the
        rejection), or ``None`` if :func:`clean_sql` itself failed (no
        candidate SQL ever existed to report).
        """
        try:
            sql = clean_sql(raw)
        except ValueError as exc:
            exc.candidate_sql = None  # type: ignore[attr-defined]
            raise
        try:
            validate_sql(sql, denied_columns=denied_columns)
        except ValueError as exc:
            exc.candidate_sql = sql  # type: ignore[attr-defined]
            raise
        capped = ensure_top(sql, cfg.settings.default_top_n)
        injected_top = cfg.settings.default_top_n if capped != sql else None

        try:
            final_sql = transpile_and_revalidate(
                capped,
                target_dialect=cfg.settings.sql_dialect,
                denied_columns=denied_columns,
            )
        except ValueError as exc:
            exc.candidate_sql = capped  # type: ignore[attr-defined]
            raise

        return final_sql, injected_top
