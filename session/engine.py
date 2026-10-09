# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``TurnEngine`` — answers one question in the context of a session.

Orchestrates, per turn:

1. :mod:`session.refinement` — decide ``basis`` (fresh / refines, and
   which §2 composition).
2. :mod:`session.ambiguity` — build the declared assumptions (§5) and a
   ``resolved_question``.
3. Either :mod:`session.composer` (§2 CTE refinement) or a direct
   generate-validate-execute loop (fresh / carry-forward refinement),
   routed through :class:`~llm.router.LLMRouter` — never a bespoke HTTP
   client (Phase 2's router is reused, not reinvented).
4. Exactly one :class:`~observability.audit.AuditRecord`, on every path
   (success, guard rejection, LLM/DB failure), mirroring
   ``api/runner.py``'s own discipline.

Unlike ``api/runner.py::run_query`` (v1, unchanged), :meth:`TurnEngine.ask`
**never raises** an ``NLQError`` to its caller. Per §5 ("answer, then
declare — never block"), every failure this module can identify becomes a
``Turn`` with ``error`` populated instead of an HTTP-level exception — the
one exception is ``OUT_OF_SCOPE``, which the contract explicitly carves out
as "the only case that legitimately returns no result" (§5), and even that
is expressed as ``Turn.error``, not a raised exception, because it is
still returned as an ordinary 200 response body (§7's SSE ``error`` event
lives inside the same stream as every success event, not as a transport
failure).
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import pandas as pd
import sqlglot
from sqlglot import exp

import config as cfg
from core.models import RetrievalContext
from core.persian import normalize_for_matching
from database.errors import classify_database_error
from database.routing import target_datasource_or_none
from knowledge.session_policy import DEFAULT_SCOPE_FIELD_NAME, DEFAULT_SCOPE_FILTER_KEY
from llm.router import (
    LLMRouter,
    PromptSegments,
    TaskType,
    build_prompt_segments,
)
from llm.interpret import interpret_rows
from llm.source_routing import (
    SourceRouting,
    continuity_audit,
    generate_with_source_fallback,
)
from llm.sql_agent import MAX_CORRECTION_ATTEMPTS
from observability.audit import AuditRecord, save_audit_record
from observability.llm_status import (
    TRUNCATED_OUTPUT_ERROR_CODE,
    build_llm_status,
    finish_reason_from_meta,
    is_truncated_empty_completion,
    latency_fields_from_meta,
    truncated_output_message,
)
from observability.timing import StageTimer
from prompt_engine.static_prefix import prefix_version as _prefix_version_of
from prompt_engine.static_prefix import static_prefix_token_estimate
from retrieval.context_retriever import ContextRetriever
from security.sql_guard import (
    PolicyRejection,
    clean_sql,
    ensure_top,
    extract_touched_tables,
    pretty_sql,
    transpile_and_revalidate,
    validate_sql,
)

from session import ambiguity
from session.composer import (
    CompositionError,
    check_scan_truncated,
    compose_refinement_sql,
    predicate_columns,
)
from session.memory import MemoryEntry, apply_memory_to_assumptions
from session.models import (
    Ambiguity,
    Basis,
    GuardVerdict,
    ResultColumn,
    Turn,
    TurnErrorInfo,
    TurnResult,
)
from session.refinement import BasisDecision, classify_basis, display_field_name
from session.store import SessionRecord, TurnMemory

logger = logging.getLogger(__name__)

_CORRECTION_SUFFIX_TEMPLATE = """

The SQL query you generated failed:
--- FAILED SQL ---
{sql}
--- ERROR ---
{error}
--- INSTRUCTIONS ---
Fix ONLY the error above. Return only the corrected SQL statement.

SQL:
"""

# Filter-enforcement check: a filter the turn presents as applied (from
# the question, the vocabulary, or an override) must actually be referenced
# by the generated SQL -- see _missing_dimension_filters. Reuses the SAME
# correction budget as _CORRECTION_SUFFIX_TEMPLATE above, once, before
# falling back to the warning below.
_FILTER_CORRECTION_SUFFIX_TEMPLATE = """

The SQL query you generated does not filter on every value this question
resolved:
--- SQL ---
{sql}
--- MISSING FILTER VALUE(S) ---
{missing}
--- INSTRUCTIONS ---
Rewrite the query so it also filters on every value listed above, in
addition to anything it already correctly filters on. Return only the
corrected SQL statement.

SQL:
"""

# Filter-enforcement check -- the Persian text is fixed and exercised by
# tests/test_session_engine_error_paths.py::TestFilterEnforcementAfterGeneration.
# {value} is the filter's own resolved value, unchanged.
_FILTER_NOT_APPLIED_WARNING = 'فیلتر «{value}» در پرس‌وجوی نهایی اعمال نشد؛ ممکن است نتیجه شامل داده‌های بیرون از این فیلتر باشد.'


# ---------------------------------------------------------------------------
# Module-level router singleton — mirrors api.runner's agent singleton
# ---------------------------------------------------------------------------

_router_lock = threading.Lock()
_router: LLMRouter | None = None


def _get_router() -> LLMRouter:
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = LLMRouter.from_settings()
    return _router


def _reset_router_for_testing(new_router: LLMRouter | None = None) -> None:
    """Replace (or clear) the cached router. **Test-only helper.**"""
    global _router
    with _router_lock:
        _router = new_router


def _default_execute(sql: str) -> pd.DataFrame:
    import database.executor as _executor_mod

    return _executor_mod.execute_query(sql)


# ---------------------------------------------------------------------------
# §8 session-context suffix
# ---------------------------------------------------------------------------


def build_session_context_text(turns: list[Turn], max_turns: int) -> str:
    """Render the last *max_turns* turns as §8's session-context block.

    Only question, SQL, result **column names**, and row_count — never row
    data (§8 rule 1). This is what :mod:`session.refinement` and the model
    itself use to resolve "among those"; putting row values here would
    leak business data into the prompt for no accuracy gain.

    Parameters
    ----------
    turns:
        The full transcript so far (oldest first).
    max_turns:
        ``cfg.settings.session_prompt_turns`` — how many of the most
        recent turns to include.

    Returns
    -------
    str
        Empty string if *turns* is empty or *max_turns* is ``0``.

    Examples
    --------
    >>> from session.models import Turn, TurnResult, ResultColumn
    >>> t = Turn(
    ...     turn_id="t_01", session_id="s_1", index=1, question="q1", sql="SELECT 1",
    ...     result=TurnResult(columns=[ResultColumn(name="X", type="number")], row_count=3),
    ... )
    >>> text = build_session_context_text([t], max_turns=3)
    >>> "q1" in text and "SELECT 1" in text and "X" in text
    True
    >>> "3" in text
    True
    """
    if not turns or max_turns <= 0:
        return ""
    recent = turns[-max_turns:]
    blocks: list[str] = []
    for t in recent:
        columns = ", ".join(c.name for c in t.result.columns) if t.result else "(none)"
        row_count = t.result.row_count if t.result else 0
        blocks.append(
            f"turn {t.turn_id}:\n"
            f"  question: {t.question}\n"
            f"  SQL: {t.sql or '(none)'}\n"
            f"  result columns: {columns}\n"
            f"  row_count: {row_count}"
        )
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Internal generation outcome
# ---------------------------------------------------------------------------


@dataclass
class _GenOutcome:
    sql: str | None = None
    sql_display: str | None = None
    guard: GuardVerdict | None = None
    result: TurnResult | None = None
    warnings: list[str] = field(default_factory=list)
    llm_status: dict[str, Any] | None = None
    error: TurnErrorInfo | None = None
    tier: str | None = "T2"
    corrections: int = 0
    result_columns: list[str] = field(default_factory=list)
    datasource_selection: dict[str, Any] | None = None
    """How the data source was chosen (several sources only) -- the audit
    record's ``datasource_selection``; see :mod:`llm.source_routing`."""


def _infer_type(series: "pd.Series") -> str:
    import pandas.api.types as ptypes

    if ptypes.is_bool_dtype(series):
        return "boolean"
    if ptypes.is_numeric_dtype(series):
        return "number"
    if ptypes.is_datetime64_any_dtype(series):
        return "datetime"
    return "string"


def _classify_router_failure(exc: Exception) -> tuple[str, str]:
    """Best-effort ``(error_code, message)`` for an ``LLMRouter`` failure.

    ``LLMRouter._call_chain`` wraps every backend's exception in one
    ``RuntimeError("Every backend in the chain failed ...")`` with the
    original exception chained as ``__cause__`` — see ``llm/router.py``.
    Unwrapping it here is what lets a genuine ``OUT_OF_SCOPE`` signal (a
    terminal, non-retryable model decision) read differently from an
    ordinary transport failure.
    """
    cause = exc.__cause__ or exc
    msg = str(cause)
    if msg == "OUT_OF_SCOPE":
        return "OUT_OF_SCOPE", "This question is outside the scope of the data this system covers."
    if isinstance(cause, TimeoutError) or "timeout" in msg.lower():
        return "MODEL_TIMEOUT", "The LLM took too long to respond. Please try again."
    # `TurnErrorInfo` has no `detail` field (finding 11) — unlike the v1
    # `ModelUnavailableError`, there is no client-model place to hide the raw
    # transport text, so it goes to the log instead of the field the client
    # reads, and the return value stays a summary an operator, not an
    # attacker probing the network, can act on.
    logger.warning("LLM router unreachable (MODEL_UNAVAILABLE): %s", msg)
    return "MODEL_UNAVAILABLE", "The language model is currently unreachable. Please try again."


def _guard_reason_subject(exc: Exception) -> tuple[str | None, str | None]:
    """``(reason, subject)`` for a caught guard-rejection exception, or ``(None, None)``.

    A guard-rejection ``except`` clause here catches two different things
    under one ``ValueError``/``PolicyRejection`` umbrella: a
    :class:`~security.sql_guard.SqlGuardRejection` (which carries these two
    attributes -- see that class's docstring for why) raised by
    ``validate_sql``/``ensure_top``/``transpile_and_revalidate``, and a
    :class:`~session.composer.CompositionError` raised by the §2 CTE
    composer, which is a plain :class:`ValueError` with neither attribute.
    ``getattr`` with a ``None`` default reads the former's structure where
    it exists and degrades the latter to "no structured reason" rather
    than raising ``AttributeError`` -- exactly the "generic action" fallback
    ``docs/design/DESIGN-INVARIANTS.md`` §8 asks for when a reason is not
    available, without this module needing to import
    :class:`~security.sql_guard.SqlGuardRejection` just to ``isinstance``
    check for it.
    """
    return getattr(exc, "reason", None), getattr(exc, "subject", None)


def _display_sql(sql: str | None) -> str | None:
    """*sql* laid out for the UI in the configured dialect, or ``None``.

    The display form of a statement that did not run (``GuardVerdict.rejected_sql``);
    ``Turn.sql_display`` is built the same way. Display only, and never
    raises: :func:`~security.sql_guard.pretty_sql` returns what it cannot
    lay out unchanged.
    """
    if not sql:
        return None
    return pretty_sql(sql, cfg.settings.sql_dialect)


class TurnEngine:
    """Answers one question in the context of a session — see module docstring.

    Parameters
    ----------
    router:
        An :class:`~llm.router.LLMRouter`. Defaults to the lazily-built,
        process-wide singleton from :func:`_get_router` (mirrors
        ``api.runner``'s ``agent`` singleton).
    execute_fn:
        ``(sql: str) -> pandas.DataFrame``. Defaults to
        :func:`database.executor.execute_query`, looked up at call time so
        ``monkeypatch`` in tests is visible. Injected directly in most
        tests instead.
    max_corrections:
        Retry budget for the fresh/carry-forward generation loop.
    """

    def __init__(
        self,
        router: LLMRouter | None = None,
        execute_fn: Callable[[str], pd.DataFrame] | None = None,
        max_corrections: int = MAX_CORRECTION_ATTEMPTS,
    ) -> None:
        self._router = router if router is not None else _get_router()
        self._execute = execute_fn if execute_fn is not None else _default_execute
        self._max_corrections = max_corrections

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ask(
        self,
        record: SessionRecord,
        question: str,
        system_prompt: str,
        *,
        request_id: str | None = None,
        assumption_overrides: dict[str, str] | None = None,
        denied_columns: tuple[str, ...] | None = None,
        memory_entries: dict[str, MemoryEntry] | None = None,
        on_stage: Callable[[str, str], None] | None = None,
        interpret: bool = False,
    ) -> Turn:
        """Answer *question* in the context of *record*, and record one audit entry.

        Parameters
        ----------
        record:
            The session's live state (transcript + memory sidecar).
            Mutated in place: the returned turn (and its memory sidecar)
            is appended before this method returns.
        question, system_prompt:
            As in ``api.runner.run_query``.
        request_id:
            Correlates the audit record with the HTTP request; falls back
            to a freshly minted id (mirrors ``api.runner.run_query``).
        assumption_overrides:
            ``{field: value}`` from ``PATCH .../assumptions`` — applied
            before generation; each overridden field's resulting
            assumption is re-sourced as ``"question"`` (the user now
            explicitly said so). ``None`` for an ordinary ``POST turns``.
        denied_columns:
            The caller's :class:`~security.auth.Principal.denied_columns`
            (Phase 8) — threaded through to every
            :func:`~security.sql_guard.validate_sql` call this turn makes,
            in both the CTE-refinement and the fresh-generation path.
            ``None`` (the default) applies no column restriction. Also
            re-checked against every applicable :class:`MemoryEntry` (§5) —
            an entry naming a now-denied column is dropped for this turn
            and reported in ``Turn.warnings``, never applied.
        memory_entries:
            ``{key: MemoryEntry}`` — the calling principal's stored
            cross-session memory (§5), as returned by
            ``session.persistence.SessionPersistence.get_memory_entries``.
            Applied to this turn's assumptions via
            :func:`session.memory.apply_memory_to_assumptions` unless
            ``cfg.settings.memory_enabled`` is ``False``, in which case it
            is ignored entirely. ``None`` (the default) applies nothing.

        Returns
        -------
        Turn
            Always returned — never raises an ``NLQError``; see module
            docstring.
        """
        # `on_stage` is how the SSE endpoint (api/v2_routes.py) learns which
        # pipeline stage is running WHILE this call is still blocking. This
        # method runs to completion in a worker thread before its caller
        # sees a Turn, so without a callback the five stages the web UI
        # draws could only ever be filled in after the answer had already
        # arrived -- which is what they did: all five sat at "waiting"
        # through the whole turn and then the result appeared beside them.
        timer = StageTimer(on_stage=on_stage)
        req_id = request_id or uuid.uuid4().hex[:12]
        turn_id = f"t_{uuid.uuid4().hex[:8]}"
        index = len(record.turns) + 1
        # SECURITY (Finding 8, 2026 audit): when this seam is wired to a
        # real T0 cache, its key MUST be built via
        # security.auth.scope_key(principal, memory_used=<this turn's
        # resolved memory entries>) -- passing memory_used=None here
        # (or copying a call that does) would let two analysts with
        # different pinned memory silently share a cached answer that
        # only one of them's memory actually justifies. scope_key's
        # memory_used parameter has no default specifically so this
        # cannot be gotten wrong by omission.
        cache_prefix_version = _prefix_version_of(system_prompt)  # noqa: F841 - reserved for a future T0 cache tier

        with timer.stage("plan"):
            previous_turn = record.last_turn()
            previous_memory = record.memory_for(previous_turn.turn_id if previous_turn else None)
            basis_decision = classify_basis(question, previous_turn, previous_memory)
            context = ContextRetriever.retrieve(question)
            previous_source = _previous_turn_source(previous_turn, previous_memory)

        # Overridden filter on a refinement (confirmed root cause): an assumption
        # override that changes a filter this §2 CTE refinement inherited
        # can never reach the composed SQL -- `_handle_cte_refinement`
        # composes over `previous_turn.sql`, which already has the OLD
        # value baked into its own WHERE clause, and the outer LLM
        # instruction it sends carries no filters/schema block at all for
        # an override to land in (see session.composer.compose_refinement_sql).
        # Silently composing anyway would show the analyst the new value
        # in the chip while the SQL (and the answer) kept reflecting the
        # old one. Once that conflict is detected, route through the
        # fresh-generation path instead (with the override folded into the
        # inherited filters it carries forward) and relabel `composition`
        # honestly: this turn no longer reuses the previous turn's SQL at
        # all, so it must not claim "cte" -- see docs/api-contract-v2.md §2.
        demoted_from_cte_override = False
        if basis_decision.kind == "refines" and basis_decision.composition == "cte":
            overridden_inherited = _cte_override_conflict(
                basis_decision.inherited_filters, assumption_overrides,
            )
            if overridden_inherited is not None:
                demoted_from_cte_override = True
                basis_decision = BasisDecision(
                    kind="refines",
                    refines_turn_id=basis_decision.refines_turn_id,
                    composition="none",
                    inherited_filters=overridden_inherited,
                    period_delta=basis_decision.period_delta,
                )

        session_context_text = build_session_context_text(
            record.turns, cfg.settings.session_prompt_turns
        )

        effective_memory_entries = (
            memory_entries if (memory_entries and cfg.settings.memory_enabled) else None
        )

        if basis_decision.kind == "refines" and basis_decision.composition == "cte":
            outcome, resolved_question, ambiguity_block, memory_filters, mem_warnings = (
                self._handle_cte_refinement(
                    question, system_prompt, previous_turn, basis_decision,
                    session_context_text, timer, assumption_overrides, denied_columns,
                    effective_memory_entries,
                )
            )
        else:
            outcome, resolved_question, ambiguity_block, memory_filters, mem_warnings = (
                self._handle_generative(
                    question, system_prompt, context, basis_decision,
                    session_context_text, timer, assumption_overrides, denied_columns,
                    effective_memory_entries,
                    demoted_from_cte_override=demoted_from_cte_override,
                    previous_source=previous_source,
                )
            )
        if basis_decision.kind == "refines" and basis_decision.composition == "cte":
            # A §2 refinement composes over the previous turn's SQL, so it
            # reads what that statement read: it stays on that source with
            # no selection of its own.
            outcome.datasource_selection = continuity_audit(previous_source)
        if mem_warnings:
            outcome.warnings = list(outcome.warnings) + mem_warnings
        # "The analyst must never be silently misled" (2026 hall-filter
        # audit): `context` is computed above regardless of which path
        # handled this turn, so a dimension the question plausibly named
        # but the vocabulary had no chance to check (cold cache, or a
        # stuck-failing background refresh -- see
        # ContextRetriever.retrieve's own docstring on RetrievalContext
        # .warnings) is surfaced here for both the fresh/carry-forward and
        # the CTE-refinement path, through the exact same `outcome
        # .warnings` an analyst already sees a truncated-scan or
        # policy-rejection warning through.
        if context.warnings:
            outcome.warnings = list(outcome.warnings) + context.warnings

        if outcome.error is not None:
            # Stamped here, once, rather than threaded as a parameter
            # through every `_GenOutcome`-producing branch above: `req_id`
            # is exactly the id this method passes to `self._write_audit`
            # a few lines down, for this SAME turn -- so setting it here
            # guarantees the two can never drift apart, which is the one
            # property that makes a `request_id` worth showing an operator
            # at all (docs/design/DESIGN-INVARIANTS.md §8's copy
            # discipline: "the request_id visible but subordinate").
            outcome.error.request_id = req_id

        basis = Basis(
            kind=basis_decision.kind,
            refines_turn_id=basis_decision.refines_turn_id,
            composition=basis_decision.composition,
            inherited=basis_decision.inherited,
        )

        # Opt-in per request, never per deployment: this sends real result
        # rows to the model, and it is the analyst asking the question who
        # is in a position to say whether these particular rows should go.
        # `/query` has made the same call since phase 2 (`interpret`
        # defaults to False there too).
        #
        # Timed as its own stage so the web UI's fifth pipeline step
        # reports what actually happened. Until this existed the engine
        # never ran an `interpret` stage at all, so that step could only
        # ever sit at "waiting" -- invisible while none of the five moved,
        # and conspicuous the moment the other four started ticking.
        interpretation: str | None = None
        if interpret and outcome.result is not None and outcome.result.rows:
            with timer.stage("interpret"):
                interpretation = interpret_rows(
                    self._router, resolved_question or question, outcome.result.rows,
                ) or None

        turn = Turn(
            turn_id=turn_id,
            session_id=record.session_id,
            index=index,
            question=question,
            resolved_question=resolved_question,
            basis=basis,
            sql=outcome.sql,
            sql_display=outcome.sql_display,
            ambiguity=ambiguity_block,
            guard=outcome.guard,
            result=outcome.result,
            interpretation=interpretation,
            tier=outcome.tier,
            warnings=outcome.warnings,
            llm=outcome.llm_status,
            timings=timer.snapshot(),
            error=outcome.error,
        )

        memory = TurnMemory(
            turn_id=turn_id,
            filters=memory_filters,
            result_columns=outcome.result_columns,
            sql=outcome.sql,
            injected_top=outcome.guard.injected_top if outcome.guard else None,
            row_count=outcome.result.row_count if outcome.result else 0,
            # The source this turn was answered from, for the next turn's
            # selection (None with one source, and for a turn with no SQL).
            datasource=(
                outcome.datasource_selection["chosen"]
                if outcome.datasource_selection and outcome.sql else None
            ),
        )
        record.turns.append(turn)
        record.memory[turn_id] = memory

        self._write_audit(req_id, turn, outcome.datasource_selection)
        return turn

    # ------------------------------------------------------------------
    # §2 CTE refinement path
    # ------------------------------------------------------------------

    def _handle_cte_refinement(
        self,
        question: str,
        system_prompt: str,
        previous_turn: Turn | None,
        basis_decision: BasisDecision,
        session_context_text: str,
        timer: StageTimer,
        assumption_overrides: dict[str, str] | None,
        denied_columns: tuple[str, ...] | None = None,
        memory_entries: dict[str, MemoryEntry] | None = None,
    ) -> tuple[_GenOutcome, str | None, Ambiguity, dict[str, object], list[str]]:
        assumptions = ambiguity.assumptions_for_cte_refinement(
            question, basis_decision.inherited_filters
        )
        assumptions, mem_warnings, _used_memory = apply_memory_to_assumptions(
            assumptions, memory_entries or {}, denied_columns,
        )
        assumptions = _apply_overrides(assumptions, assumption_overrides)
        ambiguity_block = Ambiguity(is_ambiguous=True, assumptions=assumptions, clarifications=[])
        measure = next((a.value for a in assumptions if a.field == "measure"), "")
        ring = basis_decision.inherited_filters.get(DEFAULT_SCOPE_FILTER_KEY)
        resolved_question = (
            f"برای معاملات {ring}، {measure} در میان همهٔ سطرهای منطبق با فیلتر قبلی "
            f"— نه فقط سطرهای نمایش‌داده‌شدهٔ پرسش قبل"
            if ring else f"در میان همهٔ سطرهای منطبق با فیلتر قبلی، {measure}"
        )

        if previous_turn is None or not previous_turn.sql:
            outcome = _GenOutcome(
                error=TurnErrorInfo(
                    code="NO_PREVIOUS_TURN",
                    message="This turn looks like a refinement, but there is no previous turn's SQL to refine.",
                ),
                tier=None,
            )
            return outcome, resolved_question, ambiguity_block, dict(basis_decision.inherited_filters), mem_warnings

        cap = cfg.settings.refinement_scan_cap
        try:
            available_columns = predicate_columns(previous_turn.sql)
        except Exception:  # noqa: BLE001 - best-effort prompt hint only
            available_columns = []
        columns_hint = ", ".join(available_columns) if available_columns else "(unknown)"
        outer_instruction = (
            f"\n\nThis question refines the previous turn. A CTE named `_prev` is already "
            f"prepared for you, containing every row matching the previous turn's filter "
            f"(not just the rows it displayed), with these columns available: {columns_hint}. "
            f"Write ONLY the final SELECT statement, selecting FROM `_prev` (do not define "
            f"your own WITH clause), to answer: {question}"
        )
        segments = PromptSegments(
            static_prefix="", session_context=session_context_text, question=outer_instruction,
        )

        try:
            with timer.stage("llm"):
                route_result = self._router.generate_for_task(TaskType.SQL_GENERATION, segments)
        except Exception as exc:  # noqa: BLE001 - translated below
            code, message = _classify_router_failure(exc)
            outcome = _GenOutcome(
                error=TurnErrorInfo(code=code, message=message), tier=None,
            )
            return outcome, resolved_question, ambiguity_block, dict(basis_decision.inherited_filters), mem_warnings

        raw_outer = route_result.text or ""
        llm_status = build_llm_status(
            route_result.meta.get("raw"), model=route_result.provider,
            endpoint=route_result.meta.get("endpoint"),
            trusted=bool(route_result.meta.get("trusted", False)),
            endpoint_status=route_result.meta.get("endpoint_status", 200),
            attempts=route_result.meta.get("attempts", 1),
            finish_reason=finish_reason_from_meta(route_result.meta),
            structured_output=bool(route_result.meta.get("structured_output", False)),
            static_prefix_tokens=static_prefix_token_estimate(system_prompt),
            temperature=cfg.settings.llm_temperature, seed=cfg.settings.llm_seed,
            provider=route_result.provider, fallback_used=route_result.fallback_used,
            total_ms=route_result.meta.get("total_ms"),
            reasoning_detected=bool(route_result.meta.get("reasoning_detected", False)),
            **latency_fields_from_meta(route_result.meta),
        )

        # Reset every round this branch could be entered (it is not a loop,
        # but keeping the same "no stale value" discipline as the
        # generative path's `cleaned` below): `composed` stays `None` if
        # `compose_refinement_sql` itself is what raised (a `CompositionError`
        # -- there is no validated statement to show in that case, only a
        # composition failure), and is set the moment composition succeeds,
        # before `validate_sql`/`ensure_top`/`transpile_and_revalidate` get a
        # chance to reject it.
        composed: str | None = None
        try:
            with timer.stage("guard"):
                composed = compose_refinement_sql(previous_turn.sql, raw_outer, cap)
                validate_sql(composed, denied_columns=denied_columns)
                capped = ensure_top(composed, cfg.settings.default_top_n)
                injected_top = cfg.settings.default_top_n if capped != composed else None
                # Multi-dialect: transpile the tsql-validated, capped SQL to
                # this deployment's target dialect and re-validate the
                # transpiled text before it is ever executed -- a no-op
                # passthrough when the target is "tsql" (the default). See
                # security.sql_guard.transpile_and_revalidate's docstring.
                # Raises the same PolicyRejection/CorrectableRejection
                # taxonomy as validate_sql above, so the existing except
                # clause below already handles it correctly.
                capped = transpile_and_revalidate(
                    capped,
                    target_dialect=cfg.settings.sql_dialect,
                    denied_columns=denied_columns,
                )
        except (CompositionError, ValueError) as exc:
            reason, subject = _guard_reason_subject(exc)
            outcome = _GenOutcome(
                guard=GuardVerdict(
                    verdict="rejected", rule=str(exc), reason=reason, subject=subject,
                    # `composed` -- after `compose_refinement_sql` (which
                    # itself runs `clean_sql` on the outer SQL, see its
                    # docstring), before `ensure_top` -- the exact text
                    # `validate_sql` refused. `None` when composition never
                    # produced a statement at all.
                    rejected_sql=composed,
                    rejected_sql_display=_display_sql(composed),
                ),
                result=TurnResult(),
                warnings=[f"پرس‌وجوی بازپالایی‌شده رد شد: {exc}"],
                llm_status=llm_status,
                tier="T2",
            )
            return outcome, resolved_question, ambiguity_block, dict(basis_decision.inherited_filters), mem_warnings

        warnings: list[str] = []
        try:
            truncated_scan = check_scan_truncated(self._execute, previous_turn.sql, cap)
        except Exception:  # noqa: BLE001 - the check itself must never break the turn
            truncated_scan = False
        if truncated_scan:
            warnings.append(
                f"اسکن پایهٔ این بازپالایش به دلیل محدودیت ایمنی (refinement_scan_cap = {cap:,} ردیف) "
                "متوقف شد؛ ممکن است تعداد واقعی سطرهای منطبق بیشتر بوده باشد. "
                "۱۰ مورد برتر واقعی ممکن است با نتیجهٔ زیر متفاوت باشد."
            )

        try:
            with timer.stage("execute"):
                df = self._execute(capped)
        except Exception as exc:  # noqa: BLE001
            # See database.errors.classify_database_error's docstring: a
            # connection/login/availability failure or a timeout must not
            # be reported as QUERY_EXECUTION_ERROR, and even a genuine
            # statement error must not carry the raw SQLAlchemy dump
            # (SQL text, bound parameters, host/instance, sqlalche.me URL)
            # into `TurnErrorInfo.message` -- which has no `detail` field
            # to hide it in (finding 11). The raw error is already logged
            # server-side by database.executor._execute.
            classification = classify_database_error(exc)
            outcome = _GenOutcome(
                sql=capped,
                guard=GuardVerdict(
                    verdict="allowed", injected_top=injected_top,
                    tables_touched=extract_touched_tables(capped, dialect=cfg.settings.sql_dialect),
                ),
                error=TurnErrorInfo(code=classification.code, message=classification.client_message),
                llm_status=llm_status,
            )
            return outcome, resolved_question, ambiguity_block, dict(basis_decision.inherited_filters), mem_warnings

        columns = [str(c) for c in df.columns]
        rows = df.to_dict(orient="records")
        outcome = _GenOutcome(
            sql=capped,
            # Formatted here, not where the SQL is executed: `sql` stays
            # byte-for-byte what the guard validated and the database ran,
            # while `sql_display` is what the UI renders and the copy
            # button copies. Without this the layout is whatever the model
            # felt like emitting -- the same deployment produces a tidy
            # multi-line statement for one question and a single
            # 300-character line for the next.
            sql_display=pretty_sql(clean_sql(raw_outer), cfg.settings.sql_dialect),
            guard=GuardVerdict(
                verdict="allowed", injected_top=injected_top,
                tables_touched=extract_touched_tables(capped, dialect=cfg.settings.sql_dialect),
            ),
            result=TurnResult(
                columns=[ResultColumn(name=c, type=_infer_type(df[c])) for c in columns],
                rows=rows, row_count=len(rows), truncated=False,
            ),
            warnings=warnings,
            llm_status=llm_status,
            result_columns=columns,
        )
        return outcome, resolved_question, ambiguity_block, dict(basis_decision.inherited_filters), mem_warnings

    # ------------------------------------------------------------------
    # Fresh / carry-forward generation path
    # ------------------------------------------------------------------

    def _handle_generative(
        self,
        question: str,
        system_prompt: str,
        context: RetrievalContext,
        basis_decision: BasisDecision,
        session_context_text: str,
        timer: StageTimer,
        assumption_overrides: dict[str, str] | None,
        denied_columns: tuple[str, ...] | None = None,
        memory_entries: dict[str, MemoryEntry] | None = None,
        *,
        demoted_from_cte_override: bool = False,
        previous_source: str | None = None,
    ) -> tuple[_GenOutcome, str | None, Ambiguity, dict[str, object], list[str]]:
        is_carry_forward = basis_decision.kind == "refines"
        merged_filters: dict[str, object] = dict(basis_decision.inherited_filters)
        merged_filters.update(context.filters)  # the question's own words win

        if is_carry_forward and basis_decision.period_delta:
            base_year = merged_filters.get("PersianYear")
            if base_year is None:
                _, base_year_str = ambiguity.default_period_label()
                base_year = int(base_year_str)
            merged_filters["PersianYear"] = int(base_year) + basis_decision.period_delta

        if is_carry_forward:
            assumptions = ambiguity.assumptions_for_carry_forward(merged_filters)
            clarifications: list = []
            is_ambiguous = None  # decided below, after memory may add one
        else:
            assumptions, clarifications, is_ambiguous = ambiguity.assumptions_for_fresh(
                question, merged_filters
            )

        # Memory (§5) is applied before any PATCH override -- precedence is
        # question > session > memory > default, and an override re-sources
        # a field "question" regardless of what it replaced.
        assumptions, mem_warnings, _used_memory = apply_memory_to_assumptions(
            assumptions, memory_entries or {}, denied_columns,
        )
        assumptions = _apply_overrides(assumptions, assumption_overrides)

        if is_carry_forward:
            ambiguity_block = Ambiguity(is_ambiguous=bool(assumptions), assumptions=assumptions, clarifications=[])
            if demoted_from_cte_override:
                # Overridden filter on a refinement: this turn was headed for §2 CTE composition until an
                # override changed a filter already baked into the
                # previous turn's SQL (see TurnEngine.ask). The generic
                # carry-forward phrasing below only ever names the period
                # -- useless here, since the override is almost always the
                # ring/scope filter, not the period -- so build the same
                # measure/ring/period sentence a fresh turn gets instead,
                # reading the POST-override assumptions.
                resolved_question = _resolved_question_for_fresh(question, assumptions, merged_filters)
            else:
                resolved_question = (
                    f"همان پرسش قبلی، برای {merged_filters.get('PersianYear', '')}"
                )
        else:
            ambiguity_block = Ambiguity(
                is_ambiguous=is_ambiguous, assumptions=assumptions, clarifications=clarifications,
            )
            resolved_question = _resolved_question_for_fresh(question, assumptions, merged_filters)

        # An override may change a filter's resolved value directly (e.g.
        # the user PATCHed "ring" to a different hall, or "period" to a
        # different plain year) — feed that back into the filters handed
        # to the prompt, not just the displayed assumption. A "period"
        # value that is a plain integer year (the carry-forward shape, or
        # a PATCHed override) is written back; a free-text default label
        # ("سال جاری (۱۴۰۵)") is left alone -- there is no filter key to
        # reconcile it with.
        for a in assumptions:
            if a.field == DEFAULT_SCOPE_FIELD_NAME:
                merged_filters[DEFAULT_SCOPE_FILTER_KEY] = a.value
            elif a.field == "period" and a.value.strip().isdigit():
                merged_filters["PersianYear"] = int(a.value.strip())

        # Filter-enforcement check: every STRING-valued dimension filter
        # this turn presents to the analyst as applied -- resolved from the
        # question or the vocabulary this turn (context.filters), or an
        # override that changed the ring/scope assumption above -- must be
        # checked, after generation, against what the SQL actually
        # references (_generate_validate_execute, below). Numeric/period
        # filters are out of scope (PersianYear is never a str); a filter
        # only ever *inherited* from a previous turn and never touched by
        # this one was already checked when it was first introduced.
        filters_to_enforce: dict[str, str] = {
            table: value for table, value in context.filters.items() if isinstance(value, str)
        }
        ring_value = merged_filters.get(DEFAULT_SCOPE_FILTER_KEY)
        if isinstance(ring_value, str) and any(
            a.field == DEFAULT_SCOPE_FIELD_NAME and a.source == "question" for a in assumptions
        ):
            filters_to_enforce[DEFAULT_SCOPE_FILTER_KEY] = ring_value
        token_tier_filters = {
            table: tokens
            for table, tokens in context.token_tier_filters.items()
            if table in filters_to_enforce
        }

        ctx = RetrievalContext(
            entities=context.entities, facts=context.facts, dimensions=context.dimensions,
            relationships=context.relationships, business_rules=context.business_rules,
            examples=context.examples, filters=merged_filters,
            # Without this, `resolved_values`
            # defaults to `{}` on every fresh-turn request regardless of
            # whether dimension_vocabulary actually matched something, so
            # the prompt's fenced "RESOLVED WAREHOUSE VALUES" section
            # (see RetrievalContext.resolved_values's own docstring) is
            # silently always empty -- a secondary audit/fencing signal,
            # not the filter application itself (the "DETECTED FILTERS"
            # block already carries the value from `merged_filters` with
            # its own "use exactly as provided" instruction either way).
            resolved_values=context.resolved_values,
        )
        # Several data sources: route the question to one first, so the
        # prompt describes only that source (llm/source_routing.py). A
        # follow-up turn (one `session.refinement` classed as refining the
        # previous question) starts from the previous turn's source; a
        # fresh question is judged on its own. `context` is the retrieval
        # over the whole schema, before the session's filters are merged in.
        routing = SourceRouting.plan(
            question, context,
            build=lambda source: build_prompt_segments(
                question, system_prompt, ctx,
                session_context=session_context_text, source=source,
                denied_columns=denied_columns,
            ),
            previous_source=previous_source if is_carry_forward else None,
        )
        segments = (
            routing.segments if routing is not None
            else build_prompt_segments(
                question, system_prompt, ctx, session_context=session_context_text,
                denied_columns=denied_columns,
            )
        )

        outcome = self._generate_validate_execute(
            segments, system_prompt, timer, denied_columns=denied_columns,
            filters_to_enforce=filters_to_enforce, token_tier_filters=token_tier_filters,
            routing=routing,
        )
        if routing is not None:
            outcome.datasource_selection = routing.audit()
        return outcome, resolved_question, ambiguity_block, merged_filters, mem_warnings

    def _generate_validate_execute(
        self, segments: PromptSegments, system_prompt: str, timer: StageTimer,
        *, denied_columns: tuple[str, ...] | None = None,
        filters_to_enforce: dict[str, str] | None = None,
        token_tier_filters: dict[str, tuple[str, ...]] | None = None,
        routing: SourceRouting | None = None,
    ) -> _GenOutcome:
        """Generate, validate and execute, correcting within the retry budget.

        With *routing* (several data sources) every round keeps the chosen
        source's prompt prefix, and an ``OUT_OF_SCOPE`` answer is retried
        once on the next candidate source -- one extra model call outside
        the correction budget, dropping the correction history, which
        describes the other source's tables. See :mod:`llm.source_routing`.
        """
        static_prefix_tokens = static_prefix_token_estimate(system_prompt)
        last_error: str | None = None
        last_sql: str | None = None
        raw = ""
        filters_to_enforce = filters_to_enforce or {}
        token_tier_filters = token_tier_filters or {}
        # Filter-enforcement check: set once the first (and only) time a filter-enforcement
        # regeneration is attempted -- this reuses the SAME correction
        # budget as an ordinary guard-rejection retry (below), never a
        # second, unbounded one, and never fires twice for one turn.
        pending_filter_correction: list[str] | None = None
        filter_correction_used = False

        for correction_round in range(self._max_corrections + 1):
            gen_segments = segments
            if pending_filter_correction is not None:
                gen_segments = PromptSegments(
                    static_prefix=segments.static_prefix,
                    session_context=segments.session_context,
                    question=segments.question
                    + _FILTER_CORRECTION_SUFFIX_TEMPLATE.format(
                        sql=last_sql or raw, missing=", ".join(pending_filter_correction),
                    ),
                )
                pending_filter_correction = None
            elif correction_round > 0:
                gen_segments = PromptSegments(
                    static_prefix=segments.static_prefix,
                    session_context=segments.session_context,
                    question=segments.question
                    + _CORRECTION_SUFFIX_TEMPLATE.format(sql=last_sql or raw, error=last_error),
                )

            try:
                with timer.stage("llm"):
                    route_result, replaced = generate_with_source_fallback(
                        lambda s: self._router.generate_for_task(TaskType.SQL_GENERATION, s),
                        routing, gen_segments,
                    )
            except Exception as exc:  # noqa: BLE001
                code, message = _classify_router_failure(exc)
                return _GenOutcome(error=TurnErrorInfo(code=code, message=message), tier=None)

            if replaced is not None:
                # The retry on the next source answered: from here on this
                # request is that source's, prefix included, and nothing
                # learned about the other source's tables carries over.
                segments = replaced
                last_sql = None
                pending_filter_correction = None
            if routing is not None:
                # Each source has its own prefix; the cache-hit ratio is
                # measured against the one actually in use.
                static_prefix_tokens = static_prefix_token_estimate(system_prompt, routing.current)

            raw = route_result.text or ""
            llm_status = build_llm_status(
                route_result.meta.get("raw"), model=route_result.provider,
                endpoint=route_result.meta.get("endpoint"),
                trusted=bool(route_result.meta.get("trusted", False)),
                endpoint_status=route_result.meta.get("endpoint_status", 200),
                attempts=route_result.meta.get("attempts", 1),
                finish_reason=finish_reason_from_meta(route_result.meta),
                structured_output=bool(route_result.meta.get("structured_output", False)),
                static_prefix_tokens=static_prefix_tokens,
                temperature=cfg.settings.llm_temperature, seed=cfg.settings.llm_seed,
                total_ms=route_result.meta.get("total_ms"),
                corrections=correction_round,
                provider=route_result.provider, fallback_used=route_result.fallback_used,
                reasoning_detected=bool(route_result.meta.get("reasoning_detected", False)),
                **latency_fields_from_meta(route_result.meta),
            )

            if not raw.strip():
                # A completion cut off at the token cap before it emitted
                # anything is not a bad answer to correct -- it is a
                # configuration ceiling, and the correction loop cannot
                # move it. Retrying spends the same budget on the same
                # reasoning to reach the same truncation, three times over,
                # and then reports "empty response": a description of the
                # symptom that points at the model rather than at the
                # setting. Returned on the FIRST round for that reason.
                if is_truncated_empty_completion(
                    raw, finish_reason_from_meta(route_result.meta)
                ):
                    return _GenOutcome(
                        error=TurnErrorInfo(
                            code=TRUNCATED_OUTPUT_ERROR_CODE,
                            message=truncated_output_message(cfg.settings.llm_num_predict),
                        ),
                        llm_status=llm_status, tier=None,
                    )
                last_error = "LLM returned an empty response."
                if correction_round == self._max_corrections:
                    return _GenOutcome(
                        error=TurnErrorInfo(code="EMPTY_SQL_RESPONSE", message=last_error),
                        llm_status=llm_status, tier=None,
                    )
                continue

            # Reset every round: if `clean_sql` itself is what raises this
            # round (empty/non-SQL model output -- a `CorrectableRejection`,
            # never a `PolicyRejection`), there is no cleaned candidate to
            # show as "the statement that was refused" for THIS round, and a
            # value left over from an earlier round would misattribute it.
            cleaned: str | None = None
            try:
                with timer.stage("guard"):
                    cleaned = clean_sql(raw)
                    validate_sql(cleaned, denied_columns=denied_columns)
                    capped = ensure_top(cleaned, cfg.settings.default_top_n)
                    injected_top = cfg.settings.default_top_n if capped != cleaned else None
                    # Multi-dialect: transpile the tsql-validated, capped SQL
                    # to this deployment's target dialect and re-validate the
                    # transpiled text before it is ever executed -- a no-op
                    # passthrough when the target is "tsql" (the default).
                    # See security.sql_guard.transpile_and_revalidate's
                    # docstring. Raises the same
                    # PolicyRejection/CorrectableRejection taxonomy as
                    # validate_sql above, so the except clauses below
                    # already handle it correctly (terminal vs. retried).
                    capped = transpile_and_revalidate(
                        capped,
                        target_dialect=cfg.settings.sql_dialect,
                        denied_columns=denied_columns,
                    )
            except PolicyRejection as exc:
                # No re-prompt can fix this (a forbidden statement, a
                # denied column, ...) -- the policy behind it is not in
                # the prompt, so every further correction round would
                # just spend another LLM round trip to reach this exact
                # same rejection. Return the SAME outcome the
                # correction_round == max_corrections branch below would
                # have produced, immediately, on the very first
                # occurrence -- see security.sql_guard's module docstring
                # for the taxonomy this relies on.
                last_error = str(exc)
                reason, subject = _guard_reason_subject(exc)
                return _GenOutcome(
                    guard=GuardVerdict(
                        verdict="rejected", rule=last_error, reason=reason, subject=subject,
                        # `cleaned` -- after `clean_sql`, before `ensure_top`
                        # -- is always set here: `PolicyRejection` can only
                        # come from `validate_sql`/`transpile_and_revalidate`,
                        # both of which run after this round's `cleaned`
                        # assignment above.
                        rejected_sql=cleaned,
                        rejected_sql_display=_display_sql(cleaned),
                    ),
                    result=TurnResult(),
                    warnings=[f"پرس‌وجوی تولیدشده توسط لایهٔ نگهبانی امنیتی رد شد: {last_error}"],
                    llm_status=llm_status,
                )
            except ValueError as exc:
                last_error = str(exc)
                last_sql = None
                if correction_round == self._max_corrections:
                    reason, subject = _guard_reason_subject(exc)
                    return _GenOutcome(
                        guard=GuardVerdict(
                            verdict="rejected", rule=last_error, reason=reason, subject=subject,
                            # The LAST round's refused statement -- `None`
                            # if this round's `clean_sql` itself is what
                            # raised (nothing was ever handed to
                            # `validate_sql` to refuse).
                            rejected_sql=cleaned,
                            rejected_sql_display=_display_sql(cleaned),
                        ),
                        result=TurnResult(),
                        warnings=[f"پرس‌وجوی تولیدشده توسط لایهٔ نگهبانی امنیتی رد شد: {last_error}"],
                        llm_status=llm_status,
                    )
                continue

            last_sql = capped

            # Filter-enforcement check: a filter this turn presents as
            # applied must actually be referenced by the SQL the guard just
            # passed. Checked here, after the guard, before execution --
            # regenerating once with the missing value(s) named explicitly
            # is cheaper and more useful to the model than executing SQL
            # already known not to filter on them. See
            # _missing_dimension_filters's own docstring for what
            # "referenced" means for a token-tier match.
            missing_filters: dict[str, str] = (
                _missing_dimension_filters(
                    capped, filters_to_enforce, token_tier_filters, cfg.settings.sql_dialect,
                )
                if filters_to_enforce else {}
            )
            if (
                missing_filters
                and not filter_correction_used
                and correction_round < self._max_corrections
            ):
                filter_correction_used = True
                pending_filter_correction = list(missing_filters.values())
                continue

            try:
                with timer.stage("execute"):
                    df = self._execute(capped)
            except Exception as exc:  # noqa: BLE001
                # `last_error` keeps the raw text -- it feeds the next
                # correction round's prompt (line ~781), which is a
                # server-internal use the LLM needs the real error for.
                # Only the client-facing TurnErrorInfo below goes through
                # classify_database_error's sanitising (see the identical
                # comment on the non-retry execute() failure above).
                last_error = str(exc)
                if correction_round == self._max_corrections:
                    classification = classify_database_error(exc)
                    return _GenOutcome(
                        sql=capped,
                        guard=GuardVerdict(
                            verdict="allowed", injected_top=injected_top,
                            tables_touched=extract_touched_tables(capped, dialect=cfg.settings.sql_dialect),
                        ),
                        error=TurnErrorInfo(code=classification.code, message=classification.client_message),
                        llm_status=llm_status,
                    )
                continue

            columns = [str(c) for c in df.columns]
            rows = df.to_dict(orient="records")
            truncated = injected_top is not None and len(rows) >= injected_top
            # Filter-enforcement check: the regeneration above (if it ran) is over budget or the
            # rewritten SQL still misses one or more filters -- the answer
            # is still returned (never blocked), but with one warning per
            # filter the analyst was told applied and the SQL does not
            # actually reference.
            filter_warnings = [
                _FILTER_NOT_APPLIED_WARNING.format(value=value)
                for value in missing_filters.values()
            ]
            return _GenOutcome(
                sql=capped,
                # The same display form the refinement path sets above: the
                # layout is the codebase's, not whatever the model emitted,
                # and the streamed `sql` event and `done` then agree.
                sql_display=pretty_sql(capped, cfg.settings.sql_dialect),
                guard=GuardVerdict(
                    verdict="allowed", injected_top=injected_top,
                    tables_touched=extract_touched_tables(capped, dialect=cfg.settings.sql_dialect),
                ),
                result=TurnResult(
                    columns=[ResultColumn(name=c, type=_infer_type(df[c])) for c in columns],
                    rows=rows, row_count=len(rows), truncated=truncated,
                ),
                warnings=filter_warnings,
                llm_status=llm_status,
                corrections=correction_round,
                result_columns=columns,
            )

        raise RuntimeError("TurnEngine generation loop exited unexpectedly")  # pragma: no cover

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    @staticmethod
    def _active_config_version_id_or_none() -> int | None:
        """The active :mod:`appdb.config_versions` bundle version id, or
        ``None`` when no versioned application database is reachable.

        Mirrors ``api.runner.cache_prefix_version_for``'s own fallback
        shape exactly: a caller reaching this line with an unreachable (or
        never-configured) application database gets ``None`` rather than a
        raised exception -- this method is always called from inside
        :meth:`_write_audit`'s own broad ``except``, so nothing here can
        fail a user's turn either way, but resolving it defensively here
        (rather than letting a raised exception be swallowed one frame up)
        keeps the *rest* of the audit record -- question, SQL, guard
        verdict -- from being lost to an application-database hiccup that
        has nothing to do with any of them.
        """
        try:
            from appdb.config_versions import get_active_version_id

            return get_active_version_id()
        except Exception:  # noqa: BLE001 - see docstring
            return None

    def _write_audit(
        self, request_id: str, turn: Turn,
        datasource_selection: dict[str, Any] | None = None,
    ) -> None:
        """Build and persist exactly one :class:`AuditRecord` for *turn*.

        *datasource_selection* is the turn's :mod:`llm.source_routing`
        block (``None`` with one data source, and for a turn that never
        reached generation).

        Never raises — mirrors ``api.runner._write_audit``.
        """
        try:
            guard_dict = (
                turn.guard.model_dump() if turn.guard is not None
                else {"verdict": "allowed", "rule": None, "injected_top": None, "tables_touched": None}
            )
            columns = [c.name for c in turn.result.columns] if turn.result else None
            assumptions = (
                [a.model_dump() for a in turn.ambiguity.assumptions]
                if turn.ambiguity and turn.ambiguity.assumptions else None
            )
            record = AuditRecord(
                timestamp=datetime.now(),
                request_id=request_id,
                question=turn.question,
                generated_sql=turn.sql or "",
                guard=guard_dict,
                row_count=turn.result.row_count if turn.result else 0,
                tier=turn.tier,
                error_code=turn.error.code if turn.error else None,
                error_message=turn.error.message if turn.error else None,
                timings=turn.timings,
                llm=turn.llm,
                columns=columns,
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                config_version_id=self._active_config_version_id_or_none(),
                assumptions=assumptions,
                datasource=target_datasource_or_none(turn.sql, cfg.settings.sql_dialect),
                datasource_selection=datasource_selection,
            )
            save_audit_record(record)
        except Exception:  # noqa: BLE001 - auditing must never fail a user's turn
            logger.exception("Failed to build/save audit record for turn %s", turn.turn_id)


# ---------------------------------------------------------------------------
# Small free functions
# ---------------------------------------------------------------------------


def _previous_turn_source(
    previous_turn: Turn | None, previous_memory: TurnMemory | None,
) -> str | None:
    """The data source the previous turn was answered from, or ``None``.

    What :class:`~session.store.TurnMemory` recorded; for a turn stored
    before that field existed, the source its SQL routes to. ``None`` when
    there was no previous turn or it produced no SQL.
    """
    import database.datasources as datasources

    if previous_turn is None or not previous_turn.sql:
        return None
    if len(datasources.datasource_names()) < 2:
        return None  # one source: there is nothing to continue
    if previous_memory is not None and previous_memory.datasource:
        return previous_memory.datasource
    return target_datasource_or_none(previous_turn.sql, cfg.settings.sql_dialect)


def _apply_overrides(assumptions, overrides: dict[str, str] | None):
    """Return *assumptions* with any ``PATCH``-supplied values applied.

    An overridden field's ``value`` is replaced and its ``source``
    re-labelled ``"question"`` (the user now explicitly said so) — unless
    it was ``policy``-sourced and non-editable, which is left untouched:
    §5 is explicit that a policy assumption is not something the user can
    override.
    """
    if not overrides:
        return assumptions
    result = []
    for a in assumptions:
        if a.field in overrides and a.editable:
            result.append(a.model_copy(update={"value": overrides[a.field], "source": "question"}))
        else:
            result.append(a)
    return result


def _resolved_question_for_fresh(question: str, assumptions, filters: dict[str, object]) -> str:
    if not assumptions:
        return question
    measure = next((a.value for a in assumptions if a.field == "measure"), None)
    ring = next(
        (a.value for a in assumptions if a.field == DEFAULT_SCOPE_FIELD_NAME),
        filters.get(DEFAULT_SCOPE_FILTER_KEY),
    )
    period = next((a.value for a in assumptions if a.field == "period"), None)
    parts = [p for p in (measure, ring, period) if p]
    if not parts:
        return question
    return f"{question} — بر اساس " + "، ".join(str(p) for p in parts)


def _cte_override_conflict(
    inherited_filters: dict[str, object], overrides: dict[str, str] | None,
) -> dict[str, object] | None:
    """Overridden filter on a refinement — does *overrides* change a filter this
    §2 CTE refinement has already inherited (and would otherwise compose
    over unchanged)?

    Compared by the same filter-key -> displayed-field mapping
    :func:`session.refinement.display_field_name` uses to show an
    inherited filter to the analyst (the identical mapping
    :func:`session.ambiguity.assumptions_for_cte_refinement` builds its
    ``"session"``-sourced assumptions from) — an override sent under any
    other field name (e.g. ``"measure"``) does not touch an inherited
    filter and is not this function's concern.

    Returns
    -------
    dict[str, object] | None
        ``None`` when nothing inherited was overridden (the common case —
        composing over ``_prev`` is safe). Otherwise a COPY of
        *inherited_filters* with every overridden key updated to its new
        (string) value, ready to become the demoted turn's
        ``BasisDecision.inherited_filters`` — see ``TurnEngine.ask``.
    """
    if not overrides:
        return None
    updated: dict[str, object] | None = None
    for key, value in inherited_filters.items():
        field_name = display_field_name(key)
        if field_name in overrides and str(overrides[field_name]) != str(value):
            if updated is None:
                updated = dict(inherited_filters)
            updated[key] = overrides[field_name]
    return updated


def _string_literal_texts(sql: str, dialect: str) -> list[str]:
    """Every string literal's text value in *sql* — a T-SQL national
    literal (``N'...'``) or a plain ``'...'`` string — best-effort.

    Used only by :func:`_missing_dimension_filters` (filter-enforcement check) below. Never raises: returns ``[]`` if *sql* cannot be parsed
    under *dialect* — this check must never itself break a turn whose SQL
    already passed the guard.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:  # noqa: BLE001 - best-effort only, see docstring
        return []
    if tree is None:
        return []
    texts: list[str] = []
    for node in tree.walk():
        if isinstance(node, exp.National):
            inner = node.this
            texts.append(inner.this if isinstance(inner, exp.Literal) else str(inner))
        elif isinstance(node, exp.Literal) and node.is_string:
            texts.append(node.this)
    return texts


def _missing_dimension_filters(
    sql: str,
    filters: dict[str, str],
    token_tier_filters: dict[str, tuple[str, ...]],
    dialect: str,
) -> dict[str, str]:
    """Filter-enforcement check — the subset of *filters* *sql* does not
    actually reference.

    A filter is "referenced" when some string literal in *sql*, normalised
    through the same :func:`~core.persian.normalize_for_matching` every
    other match in this codebase uses, contains the filter's own
    normalised value as a substring — or, for a table
    :func:`retrieval.dimension_vocabulary.match_question_against_vocabulary`
    matched through its token-fallback tier (*token_tier_filters*),
    contains every one of that match's own distinctive tokens instead,
    since the full stored value may never appear as one contiguous span
    anywhere by the very nature of that tier (a ``LIKE '%<token>%'``
    predicate is exactly as much "the filter applied" as an ``= N'...'``
    one is). Every value in *filters* is always a ``str`` — numeric/period
    filters are the caller's concern to exclude, not this function's.

    Returns
    -------
    dict[str, str]
        ``{table: value}`` for every filter not found — empty when every
        one was, including when *filters* itself is empty.
    """
    if not filters:
        return {}
    literals = [normalize_for_matching(text) for text in _string_literal_texts(sql, dialect)]
    missing: dict[str, str] = {}
    for table, value in filters.items():
        distinctive = token_tier_filters.get(table)
        if distinctive:
            referenced = any(all(tok in literal for tok in distinctive) for literal in literals)
        else:
            normalized_value = normalize_for_matching(value)
            referenced = any(normalized_value in literal for literal in literals)
        if not referenced:
            missing[table] = value
    return missing
