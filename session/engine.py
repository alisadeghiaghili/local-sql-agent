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

import config as cfg
from core.models import RetrievalContext
from knowledge.session_policy import DEFAULT_SCOPE_FIELD_NAME, DEFAULT_SCOPE_FILTER_KEY
from llm.router import (
    LLMRouter,
    PromptSegments,
    TaskType,
    build_prompt_segments,
)
from llm.sql_agent import MAX_CORRECTION_ATTEMPTS
from observability.audit import AuditRecord, save_audit_record
from observability.llm_status import build_llm_status, finish_reason_from_meta
from observability.timing import StageTimer
from prompt_engine.static_prefix import prefix_version as _prefix_version_of
from prompt_engine.static_prefix import static_prefix_token_estimate
from retrieval.context_retriever import ContextRetriever
from security.sql_guard import (
    PolicyRejection,
    clean_sql,
    ensure_top,
    extract_touched_tables,
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
from session.refinement import BasisDecision, classify_basis
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
        return "OUT_OF_SCOPE", "This question is outside the Auction domain."
    if isinstance(cause, TimeoutError) or "timeout" in msg.lower():
        return "MODEL_TIMEOUT", "The LLM took too long to respond. Please try again."
    return "MODEL_UNAVAILABLE", f"Cannot reach the LLM backend: {msg}"


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
        timer = StageTimer()
        req_id = request_id or uuid.uuid4().hex[:12]
        turn_id = f"t_{uuid.uuid4().hex[:8]}"
        index = len(record.turns) + 1
        static_prefix_tokens = static_prefix_token_estimate(system_prompt)
        cache_prefix_version = _prefix_version_of(system_prompt)  # noqa: F841 - reserved for a future T0 cache tier

        with timer.stage("plan"):
            previous_turn = record.last_turn()
            previous_memory = record.memory_for(previous_turn.turn_id if previous_turn else None)
            basis_decision = classify_basis(question, previous_turn, previous_memory)
            context = ContextRetriever.retrieve(question)

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
                )
            )
        if mem_warnings:
            outcome.warnings = list(outcome.warnings) + mem_warnings

        basis = Basis(
            kind=basis_decision.kind,
            refines_turn_id=basis_decision.refines_turn_id,
            composition=basis_decision.composition,
            inherited=basis_decision.inherited,
        )

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
            interpretation=None,
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
        )
        record.turns.append(turn)
        record.memory[turn_id] = memory

        self._write_audit(req_id, turn)
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
        )

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
            outcome = _GenOutcome(
                guard=GuardVerdict(verdict="rejected", rule=str(exc)),
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
            outcome = _GenOutcome(
                sql=capped,
                guard=GuardVerdict(
                    verdict="allowed", injected_top=injected_top,
                    tables_touched=extract_touched_tables(capped, dialect=cfg.settings.sql_dialect),
                ),
                error=TurnErrorInfo(code="QUERY_EXECUTION_ERROR", message=str(exc)),
                llm_status=llm_status,
            )
            return outcome, resolved_question, ambiguity_block, dict(basis_decision.inherited_filters), mem_warnings

        columns = [str(c) for c in df.columns]
        rows = df.to_dict(orient="records")
        outcome = _GenOutcome(
            sql=capped,
            sql_display=clean_sql(raw_outer),
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

        ctx = RetrievalContext(
            entities=context.entities, facts=context.facts, dimensions=context.dimensions,
            relationships=context.relationships, business_rules=context.business_rules,
            examples=context.examples, filters=merged_filters,
        )
        segments = build_prompt_segments(
            question, system_prompt, ctx, session_context=session_context_text,
        )

        outcome = self._generate_validate_execute(
            segments, system_prompt, timer, denied_columns=denied_columns,
        )
        return outcome, resolved_question, ambiguity_block, merged_filters, mem_warnings

    def _generate_validate_execute(
        self, segments: PromptSegments, system_prompt: str, timer: StageTimer,
        *, denied_columns: tuple[str, ...] | None = None,
    ) -> _GenOutcome:
        static_prefix_tokens = static_prefix_token_estimate(system_prompt)
        last_error: str | None = None
        last_sql: str | None = None
        raw = ""

        for correction_round in range(self._max_corrections + 1):
            gen_segments = segments
            if correction_round > 0:
                gen_segments = PromptSegments(
                    static_prefix=segments.static_prefix,
                    session_context=segments.session_context,
                    question=segments.question
                    + _CORRECTION_SUFFIX_TEMPLATE.format(sql=last_sql or raw, error=last_error),
                )

            try:
                with timer.stage("llm"):
                    route_result = self._router.generate_for_task(TaskType.SQL_GENERATION, gen_segments)
            except Exception as exc:  # noqa: BLE001
                code, message = _classify_router_failure(exc)
                return _GenOutcome(error=TurnErrorInfo(code=code, message=message), tier=None)

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
            )

            if not raw.strip():
                last_error = "LLM returned an empty response."
                if correction_round == self._max_corrections:
                    return _GenOutcome(
                        error=TurnErrorInfo(code="EMPTY_SQL_RESPONSE", message=last_error),
                        llm_status=llm_status, tier=None,
                    )
                continue

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
                return _GenOutcome(
                    guard=GuardVerdict(verdict="rejected", rule=last_error),
                    result=TurnResult(),
                    warnings=[f"پرس‌وجوی تولیدشده توسط لایهٔ نگهبانی امنیتی رد شد: {last_error}"],
                    llm_status=llm_status,
                )
            except ValueError as exc:
                last_error = str(exc)
                last_sql = None
                if correction_round == self._max_corrections:
                    return _GenOutcome(
                        guard=GuardVerdict(verdict="rejected", rule=last_error),
                        result=TurnResult(),
                        warnings=[f"پرس‌وجوی تولیدشده توسط لایهٔ نگهبانی امنیتی رد شد: {last_error}"],
                        llm_status=llm_status,
                    )
                continue

            last_sql = capped
            try:
                with timer.stage("execute"):
                    df = self._execute(capped)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if correction_round == self._max_corrections:
                    return _GenOutcome(
                        sql=capped,
                        guard=GuardVerdict(
                            verdict="allowed", injected_top=injected_top,
                            tables_touched=extract_touched_tables(capped, dialect=cfg.settings.sql_dialect),
                        ),
                        error=TurnErrorInfo(code="QUERY_EXECUTION_ERROR", message=last_error),
                        llm_status=llm_status,
                    )
                continue

            columns = [str(c) for c in df.columns]
            rows = df.to_dict(orient="records")
            truncated = injected_top is not None and len(rows) >= injected_top
            return _GenOutcome(
                sql=capped,
                guard=GuardVerdict(
                    verdict="allowed", injected_top=injected_top,
                    tables_touched=extract_touched_tables(capped, dialect=cfg.settings.sql_dialect),
                ),
                result=TurnResult(
                    columns=[ResultColumn(name=c, type=_infer_type(df[c])) for c in columns],
                    rows=rows, row_count=len(rows), truncated=truncated,
                ),
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

    def _write_audit(self, request_id: str, turn: Turn) -> None:
        """Build and persist exactly one :class:`AuditRecord` for *turn*.

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
            )
            save_audit_record(record)
        except Exception:  # noqa: BLE001 - auditing must never fail a user's turn
            logger.exception("Failed to build/save audit record for turn %s", turn.turn_id)


# ---------------------------------------------------------------------------
# Small free functions
# ---------------------------------------------------------------------------


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
