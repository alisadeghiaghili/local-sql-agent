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
from llm.correction_hints import correction_hint
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
from retrieval.filter_spelling import respell_filters
from retrieval.filter_values import audit_block, check_filter_values, correction_text
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
    check_scan_truncated,
    compose_refinement_sql,
    predicate_columns,
)
from session.memory import MemoryEntry, apply_memory_to_assumptions
from session.models import (
    Ambiguity,
    Assumption,
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
{error}{hint}
--- INSTRUCTIONS ---
Fix ONLY the error above. Return only the corrected SQL statement.

SQL:
"""

def _correction_hint_block(
    exc: BaseException | None, seen: set[str], denied_columns: tuple[str, ...] | None,
    source: str | None,
) -> str:
    """The text a correction round adds after the error, or ``""``.

    The guard's rejection names a column or table that does not exist; the
    block lists the real ones (:func:`llm.correction_hints.correction_hint`).
    A block already shown earlier in the same turn is not repeated.
    """
    if exc is None:
        return ""
    block = correction_hint(exc, denied_columns=denied_columns, source=source)
    if not block or block in seen:
        return ""
    seen.add(block)
    return "\n" + block


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
    value_check: dict[str, Any] | None = None
    """What the filter value check found (``FILTER_VALUE_CHECK``) -- the
    audit record's ``value_check``; ``None`` with the setting off."""
    value_spelling: dict[str, Any] | None = None
    """What the filter value spelling fix did (``FILTER_VALUE_SPELLING``) --
    the audit record's ``value_spelling``; ``None`` with the setting off and
    when no literal was rewritten."""


@dataclass(frozen=True)
class _ValueRound:
    """What :meth:`TurnEngine._filter_value_round` found and did.

    ``audit`` is the audit record's ``value_check`` (``None`` when nothing
    could be checked); ``prompt`` the message sent (``None`` when no model
    call was made); ``executed`` the corrected statement's
    ``(df, sql, injected_top, llm_status, missing_filters)`` when it ran;
    ``spelling`` what the spelling fix did to that statement (it is respelled
    like any other before it runs).
    """

    audit: dict[str, Any] | None = None
    prompt: str | None = None
    executed: tuple[pd.DataFrame, str, int | None, dict[str, Any], dict[str, str]] | None = None
    spelling: dict[str, Any] | None = None


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
        principal_id: str | None = None,
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
        principal_id:
            The caller's :attr:`~security.auth.Principal.id`, written to the
            turn's audit record as ``principal_id`` (as ``api.runner.run_query``
            does for ``/query``), so the audit log says which key asked what.
            ``None`` (the default) leaves the field empty, as before.

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
        cache_prefix_version = _prefix_version_of(system_prompt)  # noqa: F841  # NOSONAR(S1481) reserved for a future T0 cache tier

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
        basis_decision, demoted_from_cte_override = _demote_overridden_refinement(
            basis_decision, assumption_overrides,
        )

        session_context_text = build_session_context_text(
            record.turns, cfg.settings.session_prompt_turns
        )

        effective_memory_entries = (
            memory_entries if (memory_entries and cfg.settings.memory_enabled) else None
        )

        is_cte_refinement = basis_decision.kind == "refines" and basis_decision.composition == "cte"
        if is_cte_refinement:
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
        if is_cte_refinement:
            # A §2 refinement composes over the previous turn's SQL, so it
            # reads what that statement read: it stays on that source with
            # no selection of its own.
            outcome.datasource_selection = continuity_audit(previous_source)
        # Memory warnings, then retrieval warnings. "The analyst must never be
        # silently misled" (2026 hall-filter audit): `context` is computed
        # above regardless of which path handled this turn, so a dimension
        # the question plausibly named but the vocabulary had no chance to
        # check (cold cache, or a stuck-failing background refresh -- see
        # ContextRetriever.retrieve's own docstring on RetrievalContext
        # .warnings) is surfaced here for both the fresh/carry-forward and
        # the CTE-refinement path, through the exact same `outcome
        # .warnings` an analyst already sees a truncated-scan or
        # policy-rejection warning through.
        for extra in (mem_warnings, context.warnings):
            if extra:
                outcome.warnings = list(outcome.warnings) + extra

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
        interpretation = (
            self._interpret(resolved_question or question, outcome, timer) if interpret else None
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
            interpretation=interpretation,
            tier=outcome.tier,
            warnings=outcome.warnings,
            llm=outcome.llm_status,
            timings=timer.snapshot(),
            error=outcome.error,
        )

        record.turns.append(turn)
        record.memory[turn_id] = _turn_memory(turn_id, memory_filters, outcome)

        self._write_audit(
            req_id, turn, outcome.datasource_selection, principal_id, outcome.value_check,
            outcome.value_spelling,
        )
        return turn

    def _interpret(self, question: str, outcome: _GenOutcome, timer: StageTimer) -> str | None:
        """A summary of the turn's rows, or ``None`` when there are none (or the model gives none)."""
        if outcome.result is None or not outcome.result.rows:
            return None
        with timer.stage("interpret"):
            return interpret_rows(self._router, question, outcome.result.rows) or None

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
        except ValueError as exc:  # includes session.composer.CompositionError
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
        merged_filters = _merged_filters(basis_decision, context)

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
            resolved_question = _carry_forward_question(
                question, assumptions, merged_filters, demoted_from_cte_override,
            )
        else:
            ambiguity_block = Ambiguity(
                is_ambiguous=is_ambiguous, assumptions=assumptions, clarifications=clarifications,
            )
            resolved_question = _resolved_question_for_fresh(question, assumptions, merged_filters)

        _write_back_overrides(assumptions, merged_filters)
        filters_to_enforce, token_tier_filters = _filters_to_enforce(context, merged_filters, assumptions)

        ctx = RetrievalContext(
            entities=context.entities, facts=context.facts, dimensions=context.dimensions,
            join_tables=context.join_tables,
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

        value_sink: dict[str, Any] = {}
        outcome = self._generate_validate_execute(
            segments, system_prompt, timer, denied_columns=denied_columns,
            filters_to_enforce=filters_to_enforce, token_tier_filters=token_tier_filters,
            routing=routing, value_sink=value_sink,
        )
        outcome.value_check = value_sink.get("block")
        outcome.value_spelling = value_sink.get("spelling")
        if routing is not None:
            outcome.datasource_selection = routing.audit()
        return outcome, resolved_question, ambiguity_block, merged_filters, mem_warnings

    def _generate_validate_execute(
        self, segments: PromptSegments, system_prompt: str, timer: StageTimer,
        *, denied_columns: tuple[str, ...] | None = None,
        filters_to_enforce: dict[str, str] | None = None,
        token_tier_filters: dict[str, tuple[str, ...]] | None = None,
        routing: SourceRouting | None = None,
        value_sink: dict[str, Any] | None = None,
    ) -> _GenOutcome:
        """Generate, validate and execute, correcting within the retry budget.

        With *routing* (several data sources) every round keeps the chosen
        source's prompt prefix, and an ``OUT_OF_SCOPE`` answer is retried
        once on the next candidate source -- one extra model call outside
        the correction budget, dropping the correction history, which
        describes the other source's tables. See :mod:`llm.source_routing`.

        With :data:`config.Settings.filter_value_check` on, the statement
        the guard accepted is checked once, before it runs, for a string
        literal that is not a value of the cached dimension column it is
        compared to (:func:`retrieval.filter_values.check_filter_values`).
        There is then ONE correction message, through the same prompt layout
        as any other correction, and it is the one thing that can add a model
        call beyond the correction budget (only when ``max_corrections`` is
        not zero). The corrected statement runs only if it passes the guard,
        keeps no unknown literal, drops no filter the statement had and
        executes; every other outcome runs the original statement as it
        would have run without the check. The check never refuses a turn.
        *value_sink*, when given, receives the audit block as
        ``value_sink["block"]``.

        With :data:`config.Settings.filter_value_spelling` on (the default), the
        statement the guard accepted, and the corrected one of the check above,
        is first spelled the way its columns store their values
        (:func:`retrieval.filter_spelling.respell_filters`): a deterministic
        rewrite of a filter literal, no model call, checked by the guard again.
        It runs before the filter value check, so a fixed literal is a known
        value; the statement that runs, and that the turn reports, is the
        rewritten one. *value_sink* receives what it did as
        ``value_sink["spelling"]``.
        """
        return _CorrectionLoop(
            self, segments, system_prompt, timer, denied_columns=denied_columns,
            filters_to_enforce=filters_to_enforce, token_tier_filters=token_tier_filters,
            routing=routing, value_sink=value_sink,
        ).run()

    def _filter_value_round(
        self,
        capped: str,
        gen_segments: PromptSegments,
        *,
        timer: StageTimer,
        status_of: Callable[[Any, int], dict[str, Any]],
        correction_round: int,
        denied_columns: tuple[str, ...] | None,
        filters_to_enforce: dict[str, str],
        token_tier_filters: dict[str, tuple[str, ...]],
        missing_filters: dict[str, str],
    ) -> _ValueRound:
        """Check the literals of *capped* and, if some do not exist, try one corrected statement.

        See :meth:`_generate_validate_execute`. Never raises. The corrected
        statement is used only when it passes the guard (the same steps as the
        loop's), keeps no unknown literal, leaves no filter unreferenced that
        *capped* referenced (*missing_filters* is what *capped* leaves out),
        and executes; anything else, including an error from the model, gives
        a round with no ``executed``, and the caller runs *capped*.
        """
        dialect = cfg.settings.sql_dialect
        check = check_filter_values(capped, denied_columns=denied_columns, dialect=dialect)
        if not check.unknown:
            return _ValueRound(audit_block(check, corrected=False, fallback=False))
        if self._max_corrections <= 0:
            return _ValueRound(audit_block(check, corrected=False, fallback=True))

        prompt = "\n\n" + correction_text(capped, check)
        segments = PromptSegments(
            static_prefix=gen_segments.static_prefix,
            session_context=gen_segments.session_context,
            question=gen_segments.question + prompt,
        )
        try:
            with timer.stage("llm"):
                route_result = self._router.generate_for_task(TaskType.SQL_GENERATION, segments)
            with timer.stage("guard"):
                cleaned = clean_sql(route_result.text or "")
                validate_sql(cleaned, denied_columns=denied_columns)
                new_sql = ensure_top(cleaned, cfg.settings.default_top_n)
                new_top = cfg.settings.default_top_n if new_sql != cleaned else None
                new_sql = transpile_and_revalidate(
                    new_sql, target_dialect=dialect, denied_columns=denied_columns,
                )
                spelling = None
                if cfg.settings.filter_value_spelling:
                    new_sql, spelling = respell_filters(new_sql, denied_columns=denied_columns, dialect=dialect)
                if check_filter_values(new_sql, denied_columns=denied_columns, dialect=dialect).unknown:
                    raise ValueError("the corrected statement keeps a value that does not exist")
                new_missing = (
                    _missing_dimension_filters(new_sql, filters_to_enforce, token_tier_filters, dialect)
                    if filters_to_enforce else {}
                )
                if set(new_missing) - set(missing_filters):
                    raise ValueError("the corrected statement drops a filter the question named")
            with timer.stage("execute"):
                df = self._execute(new_sql)
        except Exception as exc:  # noqa: BLE001  # whatever goes wrong, the original statement runs
            logger.info("Filter value correction not used (%s): %.120s", type(exc).__name__, exc)
            return _ValueRound(audit_block(check, corrected=False, fallback=True), prompt)
        return _ValueRound(
            audit_block(check, corrected=True, fallback=False), prompt,
            (df, new_sql, new_top, status_of(route_result, correction_round + 1), new_missing),
            spelling,
        )

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
        principal_id: str | None = None,
        value_check: dict[str, Any] | None = None,
        value_spelling: dict[str, Any] | None = None,
    ) -> None:
        """Build and persist exactly one :class:`AuditRecord` for *turn*.

        *datasource_selection* is the turn's :mod:`llm.source_routing`
        block (``None`` with one data source, and for a turn that never
        reached generation). *principal_id* is the caller's id, as in
        ``api.runner._write_audit``. *value_check* is what the filter value
        check found (``FILTER_VALUE_CHECK``), ``None`` with the setting off;
        *value_spelling* what the spelling fix did (``FILTER_VALUE_SPELLING``),
        ``None`` when it rewrote nothing.

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
                principal_id=principal_id,
                config_version_id=self._active_config_version_id_or_none(),
                assumptions=assumptions,
                datasource=target_datasource_or_none(turn.sql, cfg.settings.sql_dialect),
                datasource_selection=datasource_selection,
                value_check=value_check,
                value_spelling=value_spelling,
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


def _merged_filters(basis_decision: BasisDecision, context: RetrievalContext) -> dict[str, object]:
    """The inherited filters with the question's own on top, and a carried-forward period moved."""
    merged_filters: dict[str, object] = dict(basis_decision.inherited_filters)
    merged_filters.update(context.filters)  # the question's own words win
    if basis_decision.kind == "refines" and basis_decision.period_delta:
        base_year = merged_filters.get("PersianYear")
        if base_year is None:
            _, base_year_str = ambiguity.default_period_label()
            base_year = int(base_year_str)
        merged_filters["PersianYear"] = int(base_year) + basis_decision.period_delta
    return merged_filters


def _carry_forward_question(
    question: str,
    assumptions: list[Assumption],
    merged_filters: dict[str, object],
    demoted_from_cte_override: bool,
) -> str:
    """The resolved question of a carry-forward turn."""
    if demoted_from_cte_override:
        # Overridden filter on a refinement: this turn was headed for §2 CTE
        # composition until an override changed a filter already baked into
        # the previous turn's SQL (see TurnEngine.ask). The generic
        # carry-forward phrasing below only ever names the period -- useless
        # here, since the override is almost always the ring/scope filter,
        # not the period -- so build the same measure/ring/period sentence a
        # fresh turn gets instead, reading the POST-override assumptions.
        return _resolved_question_for_fresh(question, assumptions, merged_filters)
    return f"همان پرسش قبلی، برای {merged_filters.get('PersianYear', '')}"


def _write_back_overrides(assumptions: list[Assumption], merged_filters: dict[str, object]) -> None:
    """Feed overridden values back into the filters handed to the prompt, in place.

    An override may change a filter's resolved value directly (e.g. the user
    PATCHed "ring" to a different hall, or "period" to a different plain
    year) -- so the filters change, not just the displayed assumption. A
    "period" value that is a plain integer year (the carry-forward shape, or
    a PATCHed override) is written back; a free-text default label ("سال
    جاری (۱۴۰۵)") is left alone -- there is no filter key to reconcile it
    with.
    """
    for a in assumptions:
        if a.field == DEFAULT_SCOPE_FIELD_NAME:
            merged_filters[DEFAULT_SCOPE_FILTER_KEY] = a.value
        elif a.field == "period" and a.value.strip().isdigit():
            merged_filters["PersianYear"] = int(a.value.strip())


def _filters_to_enforce(
    context: RetrievalContext, merged_filters: dict[str, object], assumptions: list[Assumption],
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """The string filters the generated SQL must reference, and their token-tier matches.

    Filter-enforcement check: every STRING-valued dimension filter this turn
    presents to the analyst as applied -- resolved from the question or the
    vocabulary this turn (context.filters), or an override that changed the
    ring/scope assumption -- must be checked, after generation, against what
    the SQL actually references (_CorrectionLoop). Numeric/period filters are
    out of scope (PersianYear is never a str); a filter only ever *inherited*
    from a previous turn and never touched by this one was already checked
    when it was first introduced.
    """
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
    return filters_to_enforce, token_tier_filters


def _demote_overridden_refinement(
    basis_decision: BasisDecision, assumption_overrides: dict[str, str] | None,
) -> tuple[BasisDecision, bool]:
    """A §2 CTE refinement whose inherited filter an override changes, routed to fresh generation.

    Returns the decision to use and whether it was demoted (see the comment
    in :meth:`TurnEngine.ask` for why such a refinement cannot compose).
    """
    if not (basis_decision.kind == "refines" and basis_decision.composition == "cte"):
        return basis_decision, False
    overridden_inherited = _cte_override_conflict(
        basis_decision.inherited_filters, assumption_overrides,
    )
    if overridden_inherited is None:
        return basis_decision, False
    return BasisDecision(
        kind="refines",
        refines_turn_id=basis_decision.refines_turn_id,
        composition="none",
        inherited_filters=overridden_inherited,
        period_delta=basis_decision.period_delta,
    ), True


def _turn_memory(turn_id: str, filters: dict[str, object], outcome: _GenOutcome) -> TurnMemory:
    """The memory sidecar kept for a turn, for the turns that follow it."""
    return TurnMemory(
        turn_id=turn_id,
        filters=filters,
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
    except Exception:  # noqa: BLE001  # best-effort only, see docstring
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


@dataclass(frozen=True)
class _Guarded:
    """A statement the guard accepted this round: capped, transpiled, and the ``TOP`` it was given."""

    capped: str
    injected_top: int | None


class _CorrectionLoop:
    """One turn's generate -> guard -> execute loop, with its correction rounds.

    The state of :meth:`TurnEngine._generate_validate_execute` (see its
    docstring for the behaviour). Each round is :meth:`_round`; a round either
    ends the turn with a :class:`_GenOutcome` or returns ``None`` to start the
    next correction round.
    """

    def __init__(
        self,
        engine: TurnEngine,
        segments: PromptSegments,
        system_prompt: str,
        timer: StageTimer,
        *,
        denied_columns: tuple[str, ...] | None,
        filters_to_enforce: dict[str, str] | None,
        token_tier_filters: dict[str, tuple[str, ...]] | None,
        routing: SourceRouting | None,
        value_sink: dict[str, Any] | None,
    ) -> None:
        self.engine = engine
        self.segments = segments
        self.system_prompt = system_prompt
        self.timer = timer
        self.denied_columns = denied_columns
        self.filters_to_enforce = filters_to_enforce or {}
        self.token_tier_filters = token_tier_filters or {}
        self.routing = routing
        self.value_sink = value_sink
        self.static_prefix_tokens = static_prefix_token_estimate(system_prompt)
        self.last_error: str | None = None
        self.last_sql: str | None = None
        self.raw = ""
        # Filter-enforcement check: set once the first (and only) time a
        # filter-enforcement regeneration is attempted -- this reuses the SAME
        # correction budget as an ordinary guard-rejection retry, never a
        # second, unbounded one, and never fires twice for one turn.
        self.pending_filter_correction: list[str] | None = None
        self.filter_correction_used = False
        # The guard's rejection behind last_error, and the hint blocks the
        # model has already been shown in this turn (llm/correction_hints.py).
        self.last_exception: BaseException | None = None
        self.seen_hints: set[str] = set()
        # The filter value check (see the docstring): at most one extra model
        # call per turn, outside the correction budget.
        self.value_checked = False
        self.extra_round = 0

    @property
    def max_corrections(self) -> int:
        return self.engine._max_corrections

    def run(self) -> _GenOutcome:
        """Run rounds until one ends the turn."""
        for correction_round in range(self.max_corrections + 1):
            outcome = self._round(correction_round)
            if outcome is not None:
                return outcome
        raise RuntimeError("TurnEngine generation loop exited unexpectedly")  # pragma: no cover

    def status_of(self, result: Any, corrections: int) -> dict[str, Any]:
        """The turn's ``llm`` block for one model call, against the prefix currently in use."""
        return build_llm_status(
            result.meta.get("raw"), model=result.provider,
            endpoint=result.meta.get("endpoint"),
            trusted=bool(result.meta.get("trusted", False)),
            endpoint_status=result.meta.get("endpoint_status", 200),
            attempts=result.meta.get("attempts", 1),
            finish_reason=finish_reason_from_meta(result.meta),
            structured_output=bool(result.meta.get("structured_output", False)),
            static_prefix_tokens=self.static_prefix_tokens,
            temperature=cfg.settings.llm_temperature, seed=cfg.settings.llm_seed,
            total_ms=result.meta.get("total_ms"),
            corrections=corrections,
            provider=result.provider, fallback_used=result.fallback_used,
            reasoning_detected=bool(result.meta.get("reasoning_detected", False)),
            **latency_fields_from_meta(result.meta),
        )

    # -- one round -----------------------------------------------------------

    def _round(self, correction_round: int) -> _GenOutcome | None:
        if self.value_sink is not None:
            self.value_sink.pop("spelling", None)
        gen_segments = self._prompt_for(correction_round)

        try:
            with self.timer.stage("llm"):
                route_result, replaced = generate_with_source_fallback(
                    lambda s: self.engine._router.generate_for_task(TaskType.SQL_GENERATION, s),
                    self.routing, gen_segments,
                )
        except Exception as exc:  # noqa: BLE001
            code, message = _classify_router_failure(exc)
            return _GenOutcome(error=TurnErrorInfo(code=code, message=message), tier=None)

        if replaced is not None:
            # The retry on the next source answered: from here on this
            # request is that source's, prefix included, and nothing
            # learned about the other source's tables carries over.
            self.segments = replaced
            self.last_sql = None
            self.pending_filter_correction = None
        if self.routing is not None:
            # Each source has its own prefix; the cache-hit ratio is
            # measured against the one actually in use.
            self.static_prefix_tokens = static_prefix_token_estimate(
                self.system_prompt, self.routing.current,
            )

        self.raw = route_result.text or ""
        llm_status = self.status_of(route_result, correction_round + self.extra_round)

        if not self.raw.strip():
            return self._empty_reply(correction_round, route_result, llm_status)

        guarded = self._guard(correction_round, llm_status)
        if not isinstance(guarded, _Guarded):
            return guarded
        capped, injected_top = guarded.capped, guarded.injected_top
        self.last_sql = capped

        # Filter-enforcement check: a filter this turn presents as applied
        # must actually be referenced by the SQL the guard just passed.
        # Checked here, after the guard, before execution -- regenerating
        # once with the missing value(s) named explicitly is cheaper and more
        # useful to the model than executing SQL already known not to filter
        # on them. See _missing_dimension_filters's own docstring for what
        # "referenced" means for a token-tier match.
        missing_filters: dict[str, str] = (
            _missing_dimension_filters(
                capped, self.filters_to_enforce, self.token_tier_filters,
                cfg.settings.sql_dialect,
            )
            if self.filters_to_enforce else {}
        )
        if (
            missing_filters
            and not self.filter_correction_used
            and correction_round < self.max_corrections
        ):
            self.filter_correction_used = True
            self.pending_filter_correction = list(missing_filters.values())
            return None

        capped = self._respell(capped)
        value_df, capped, injected_top, llm_status, missing_filters = self._check_values(
            capped, gen_segments, correction_round, llm_status, injected_top, missing_filters,
        )
        return self._execute(correction_round, capped, injected_top, llm_status, missing_filters, value_df)

    def _prompt_for(self, correction_round: int) -> PromptSegments:
        """This round's prompt: the turn's own, or it plus a filter or guard correction."""
        segments = self.segments
        if self.pending_filter_correction is not None:
            missing = self.pending_filter_correction
            self.pending_filter_correction = None
            return PromptSegments(
                static_prefix=segments.static_prefix,
                session_context=segments.session_context,
                question=segments.question
                + _FILTER_CORRECTION_SUFFIX_TEMPLATE.format(
                    sql=self.last_sql or self.raw, missing=", ".join(missing),
                ),
            )
        if correction_round == 0:
            return segments
        return PromptSegments(
            static_prefix=segments.static_prefix,
            session_context=segments.session_context,
            question=segments.question
            + _CORRECTION_SUFFIX_TEMPLATE.format(
                sql=self.last_sql or self.raw, error=self.last_error,
                hint=_correction_hint_block(
                    self.last_exception, self.seen_hints, self.denied_columns,
                    self.routing.current if self.routing is not None else None,
                ),
            ),
        )

    def _empty_reply(
        self, correction_round: int, route_result: Any, llm_status: dict[str, Any],
    ) -> _GenOutcome | None:
        """A reply with no text: truncation ends the turn at once; otherwise retry until the budget runs out."""
        # A completion cut off at the token cap before it emitted anything is
        # not a bad answer to correct -- it is a configuration ceiling, and the
        # correction loop cannot move it. Retrying spends the same budget on
        # the same reasoning to reach the same truncation, three times over,
        # and then reports "empty response": a description of the symptom that
        # points at the model rather than at the setting. Returned on the
        # FIRST round for that reason.
        if is_truncated_empty_completion(self.raw, finish_reason_from_meta(route_result.meta)):
            return _GenOutcome(
                error=TurnErrorInfo(
                    code=TRUNCATED_OUTPUT_ERROR_CODE,
                    message=truncated_output_message(cfg.settings.llm_num_predict),
                ),
                llm_status=llm_status, tier=None,
            )
        self.last_error = "LLM returned an empty response."
        self.last_exception = None
        if correction_round == self.max_corrections:
            return _GenOutcome(
                error=TurnErrorInfo(code="EMPTY_SQL_RESPONSE", message=self.last_error),
                llm_status=llm_status, tier=None,
            )
        return None

    def _guard(self, correction_round: int, llm_status: dict[str, Any]) -> _Guarded | _GenOutcome | None:
        """Clean, validate, cap and transpile this round's reply.

        Returns the accepted statement, the turn's outcome (a policy
        rejection, or a correctable one on the last round), or ``None`` to
        retry.
        """
        # Reset every round: if `clean_sql` itself is what raises this round
        # (empty/non-SQL model output -- a `CorrectableRejection`, never a
        # `PolicyRejection`), there is no cleaned candidate to show as "the
        # statement that was refused" for THIS round, and a value left over
        # from an earlier round would misattribute it.
        cleaned: str | None = None
        try:
            with self.timer.stage("guard"):
                cleaned = clean_sql(self.raw)
                validate_sql(cleaned, denied_columns=self.denied_columns)
                capped = ensure_top(cleaned, cfg.settings.default_top_n)
                injected_top = cfg.settings.default_top_n if capped != cleaned else None
                # Multi-dialect: transpile the tsql-validated, capped SQL to
                # this deployment's target dialect and re-validate the
                # transpiled text before it is ever executed -- a no-op
                # passthrough when the target is "tsql" (the default). See
                # security.sql_guard.transpile_and_revalidate's docstring.
                # Raises the same PolicyRejection/CorrectableRejection
                # taxonomy as validate_sql above, so the except clauses below
                # already handle it correctly (terminal vs. retried).
                capped = transpile_and_revalidate(
                    capped,
                    target_dialect=cfg.settings.sql_dialect,
                    denied_columns=self.denied_columns,
                )
        except PolicyRejection as exc:
            # No re-prompt can fix this (a forbidden statement, a denied
            # column, ...) -- the policy behind it is not in the prompt, so
            # every further correction round would just spend another LLM
            # round trip to reach this exact same rejection. Return the SAME
            # outcome the last-round branch below would have produced,
            # immediately, on the very first occurrence -- see
            # security.sql_guard's module docstring for the taxonomy this
            # relies on. `cleaned` is always set here: `PolicyRejection` can
            # only come from `validate_sql`/`transpile_and_revalidate`, both
            # of which run after this round's `cleaned` assignment above.
            self.last_error = str(exc)
            return _guard_rejection(exc, self.last_error, cleaned, llm_status)
        except ValueError as exc:
            self.last_error = str(exc)
            self.last_exception = exc
            self.last_sql = None
            if correction_round == self.max_corrections:
                # The LAST round's refused statement -- `None` if this round's
                # `clean_sql` itself is what raised (nothing was ever handed
                # to `validate_sql` to refuse).
                return _guard_rejection(exc, self.last_error, cleaned, llm_status)
            return None
        return _Guarded(capped=capped, injected_top=injected_top)

    def _respell(self, capped: str) -> str:
        """The filter value spelling fix (see the docstring): stored spellings for filter literals."""
        if not cfg.settings.filter_value_spelling:
            return capped
        capped, spelling_audit = respell_filters(
            capped, denied_columns=self.denied_columns, dialect=cfg.settings.sql_dialect,
        )
        self.last_sql = capped
        if self.value_sink is not None:
            self.value_sink["spelling"] = spelling_audit
        return capped

    def _check_values(
        self,
        capped: str,
        gen_segments: PromptSegments,
        correction_round: int,
        llm_status: dict[str, Any],
        injected_top: int | None,
        missing_filters: dict[str, str],
    ) -> tuple[pd.DataFrame | None, str, int | None, dict[str, Any], dict[str, str]]:
        """The filter value check (off by default; see the docstring), at most once per turn.

        Returns ``(value_df, capped, injected_top, llm_status,
        missing_filters)``: *value_df* is the corrected statement's rows when
        that statement was used, and the other four are what then holds.
        """
        if not cfg.settings.filter_value_check or self.value_checked:
            return None, capped, injected_top, llm_status, missing_filters
        self.value_checked = True
        value_round = self.engine._filter_value_round(
            capped, gen_segments, timer=self.timer, status_of=self.status_of,
            correction_round=correction_round, denied_columns=self.denied_columns,
            filters_to_enforce=self.filters_to_enforce,
            token_tier_filters=self.token_tier_filters,
            missing_filters=missing_filters,
        )
        if self.value_sink is not None:
            self.value_sink["block"] = value_round.audit
        if value_round.prompt is not None:
            self.extra_round = 1
            # The original statement keeps its own call's status, with the
            # round counted: the corrected one carries its own.
            llm_status = {**llm_status, "corrections": correction_round + 1}
        if value_round.executed is None:
            return None, capped, injected_top, llm_status, missing_filters
        value_df, capped, injected_top, llm_status, missing_filters = value_round.executed
        self.last_sql = capped
        if self.value_sink is not None:
            self.value_sink["spelling"] = value_round.spelling
        return value_df, capped, injected_top, llm_status, missing_filters

    def _execute(
        self,
        correction_round: int,
        capped: str,
        injected_top: int | None,
        llm_status: dict[str, Any],
        missing_filters: dict[str, str],
        value_df: pd.DataFrame | None,
    ) -> _GenOutcome | None:
        """Run *capped* (unless the value check already did) and build the turn's answer."""
        try:
            with self.timer.stage("execute"):
                df = value_df if value_df is not None else self.engine._execute(capped)
        except Exception as exc:  # noqa: BLE001
            # `last_error` keeps the raw text -- it feeds the next correction
            # round's prompt, which is a server-internal use the LLM needs the
            # real error for. Only the client-facing TurnErrorInfo below goes
            # through classify_database_error's sanitising (see the identical
            # comment on the non-retry execute() failure in the refinement
            # path).
            self.last_error = str(exc)
            self.last_exception = None
            if correction_round != self.max_corrections:
                return None
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

        columns = [str(c) for c in df.columns]
        rows = df.to_dict(orient="records")
        truncated = injected_top is not None and len(rows) >= injected_top
        # Filter-enforcement check: the regeneration (if it ran) is over budget
        # or the rewritten SQL still misses one or more filters -- the answer
        # is still returned (never blocked), but with one warning per filter
        # the analyst was told applied and the SQL does not actually reference.
        filter_warnings = [
            _FILTER_NOT_APPLIED_WARNING.format(value=value)
            for value in missing_filters.values()
        ]
        return _GenOutcome(
            sql=capped,
            # The same display form the refinement path sets: the layout is
            # the codebase's, not whatever the model emitted, and the streamed
            # `sql` event and `done` then agree.
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
            corrections=correction_round + self.extra_round,
            result_columns=columns,
        )


def _guard_rejection(
    exc: Exception, rule: str, cleaned: str | None, llm_status: dict[str, Any],
) -> _GenOutcome:
    """The turn's outcome when the guard refused the statement *cleaned* (``None``: nothing was cleaned)."""
    reason, subject = _guard_reason_subject(exc)
    return _GenOutcome(
        guard=GuardVerdict(
            verdict="rejected", rule=rule, reason=reason, subject=subject,
            rejected_sql=cleaned,
            rejected_sql_display=_display_sql(cleaned),
        ),
        result=TurnResult(),
        warnings=[f"پرس‌وجوی تولیدشده توسط لایهٔ نگهبانی امنیتی رد شد: {rule}"],
        llm_status=llm_status,
    )
