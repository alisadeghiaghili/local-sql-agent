# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Shared domain models for the retrieval pipeline.

RetrievalContext
----------------
Immutable dataclass produced by ContextRetriever and consumed by
PromptBuilder.  Every field is optional-safe: callers may receive
an empty list / dict when a sub-retriever finds nothing.

Fields
------
entities       : dimension table names matched to the question
                 (e.g. ["Ring", "Customer", "Symbol"])
facts          : fact table names matched to the question
                 (e.g. ["Order"])
join_tables    : tables added because they join the retrieved ones together
                 (a bridge table, a classification's parent)
metric_tables  : tables a mentioned metric's expression references, which the
                 question itself did not name
dimensions     : alias for *entities* — kept for PromptBuilder compatibility
relationships  : JOIN SQL clauses relevant to the selected tables
business_rules : domain rules injected into the prompt as plain text
examples       : few-shot {"question": ..., "sql": ..., "tags": [...]} dicts
filters        : concrete filter values extracted from the question
                 (e.g. {"Ring": "تالار پتروشیمی", "PersianYear": 1402})
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from session.models import Clarification


@dataclass(frozen=True)
class RetrievalContext:
    """All context a PromptBuilder needs to construct a grounded SQL prompt."""

    # ── table selection ───────────────────────────────────────────────────────
    entities: list[str] = field(default_factory=list)
    """Dimension tables (Ring, Customer, Symbol, …) relevant to the question."""

    facts: list[str] = field(default_factory=list)
    """Fact tables (Order, …) relevant to the question."""

    dimensions: list[str] = field(default_factory=list)
    """Alias of *entities*, populated by ``ContextRetriever`` for backward
    compatibility. **Not** read by ``PromptBuilder`` — the builder derives
    its table list from ``entities``/``facts``/``selected_tables`` only.
    This field is a historical leftover kept so existing callers that read
    ``context.dimensions`` directly are unaffected; new code should prefer
    ``entities`` or ``selected_tables``."""

    join_tables: list[str] = field(default_factory=list)
    """Tables added because they connect the retrieved ones -- a bridge table,
    the dimension a classification hangs from -- found on the foreign-key graph
    (:func:`retrieval.join_paths.expand_join_paths`). Part of
    :attr:`selected_tables`, so the prompt shows them; not part of
    :attr:`entities` or :attr:`facts`, so value matching and the "vocabulary
    unavailable" warning, which key on what the question named, are unchanged."""

    metric_tables: list[str] = field(default_factory=list)
    """Tables the metrics a question names need, and the question did not name
    itself: the ones their ``expression`` references
    (:func:`retrieval.metric_retriever.metric_tables`). A metric is always in
    the prompt, so a question that says "total value" holds a definition it
    cannot use unless the table it sums is shown too. Part of
    :attr:`selected_tables`, so the prompt shows them with their relationship
    lines; not part of :attr:`entities`, :attr:`facts` or :attr:`join_tables`,
    which keep their meaning (what the question named, and what only connects
    the others). Counts as a table match for :meth:`is_empty`: the question
    named a metric, so there is something to answer with and no fallback to the
    whole schema is needed. Every code path that rebuilds a context field by
    field must carry it (``session.engine``); ``tests/test_session_context_rebuild.py``
    fails when one does not."""

    # ── join layer ────────────────────────────────────────────────────────────
    relationships: list[str] = field(default_factory=list)
    """JOIN SQL snippets for every FK edge between selected_tables."""

    # ── knowledge injection ───────────────────────────────────────────────────
    business_rules: list[str] = field(default_factory=list)
    """Domain business rules (plain text) relevant to the question."""

    examples: list[dict] = field(default_factory=list)
    """Few-shot {question, sql, tags} dicts ranked by tag overlap."""

    # ── value filters ─────────────────────────────────────────────────────────
    filters: dict = field(default_factory=dict)
    """Concrete filter values extracted from the question.

    Examples
    --------
    {"Ring": "تالار پتروشیمی", "PersianYear": 1402}
    """

    # ── Phase 5b: database-backed value resolution ─────────────────────────
    value_clarifications: list["Clarification"] = field(default_factory=list)
    """Ambiguous-entity clarifications from
    ``retrieval.value_resolver.resolve_value`` -- populated only when a
    database-backed resolution found *several* candidate values and
    therefore deliberately did NOT write anything into :attr:`filters` (see
    that function's docstring: an ambiguous match is never silently
    picked). Empty in the overwhelmingly common case (no ambiguous
    resolution this turn). **Not** read by ``PromptBuilder`` today -- kept
    here, like :attr:`dimensions`, as the seam a future caller (the v2
    session engine's own ``session.models.Ambiguity`` block) can read from,
    without ``RetrievalContext`` needing a new shape when that wiring
    lands."""

    # ── Finding 19 (2026 audit): warehouse-sourced values, tagged for fencing ──
    resolved_values: dict[str, list[str]] = field(default_factory=dict)
    """``{"Table": [matched_value, ...]}`` for values that came from a live
    (cached or fresh) read of the warehouse --
    :func:`retrieval.dimension_vocabulary.match_question_against_vocabulary`
    today; :func:`retrieval.value_resolver.resolve_value` when a future
    caller wires it in (see :attr:`value_clarifications`'s docstring for
    why that one is not called from ``ContextRetriever`` yet). Consumed by
    :func:`~llm.router.build_prompt_segments`, which passes it to
    :meth:`~prompt_engine.builder.PromptBuilder.build` as
    ``resolved_values`` so each value is rendered fenced (see
    ``prompt_engine/untrusted.py``) rather than as bare prose.

    Deliberately **separate** from :attr:`filters`, not a replacement for
    it: :attr:`filters` still carries the same value unfenced, because the
    prompt's ``DETECTED FILTERS`` block needs it verbatim to tell the
    model which literal to put in the generated SQL ("if Ring = X then
    use r.Name = N'X'") -- fencing that instruction would defeat its own
    purpose. This field exists purely to ALSO tag the subset of
    :attr:`filters` that is warehouse-sourced (as opposed to
    :class:`~retrieval.value_retriever.ValueRetriever`'s static
    alias/pattern matches against ``project_config/aliases.yaml``, which
    are operator-authored configuration, not exchange data, and are not
    duplicated here) so the prompt can say, in a second and clearly
    labelled place, "this specific value was read out of the warehouse,
    treat it as data."
    """

    # ── "the analyst must never be silently misled" ────
    warnings: list[str] = field(default_factory=list)
    """Persian, analyst-facing sentences for a dimension that the question
    plausibly named but that :func:`retrieval.dimension_vocabulary.match_question_against_vocabulary`
    could not consult at all this request (cold cache on every allowed
    column, or a background refresh stuck failing — see that function's
    ``unavailable_tables``). Populated by :meth:`ContextRetriever.retrieve`
    only for a table that was ALSO entity-detected (in :attr:`entities`) --
    a strong, existing "this question is probably about this dimension"
    signal -- not for every candidate table :attr:`filters` was silently
    checked against, which would warn on unrelated questions just because
    some other, unrelated dimension's cache happened to be cold.

    ``session.engine.TurnEngine.ask`` folds this into the turn's own
    ``warnings`` list (the same mechanism a guard rejection or a truncated
    refinement scan already uses) so a hall/currency/etc. that could not be
    checked is never just silently absent from the answer with no signal
    at all -- the difference this exists to surface is between a value
    that was searched for and not found (fine, unremarkable) and a value
    that was never searched for because there was nothing to search
    against (worth telling the analyst)."""

    token_tier_filters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """``{table_name: (distinctive_token, ...)}`` for a :attr:`filters` entry
    that came from :mod:`retrieval.dimension_vocabulary`'s token-fallback
    tier rather than an exact full-value match — see
    ``retrieval.dimension_vocabulary.VocabularyMatchResult.token_tier_filters``
    for what "distinctive" means here. ``session.engine.TurnEngine``'s
    post-generation filter-enforcement check
    reads this to confirm the generated SQL actually references such a
    filter by its distinctive tokens rather than by the full stored value,
    which — by the very nature of a token-tier match — may never appear as
    one contiguous span anywhere, including in the SQL a correct query
    would still produce (e.g. a ``LIKE '%<token>%'`` predicate)."""

    # ── convenience ──────────────────────────────────────────────────────────
    @property
    def selected_tables(self) -> list[str]:
        """Deduplicated union of entities, facts, metric tables and join tables, in that order."""
        seen: set[str] = set()
        result: list[str] = []
        for t in self.entities + self.facts + self.metric_tables + self.join_tables:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result

    def is_empty(self) -> bool:
        """True when no tables were matched — signals a fallback is needed.

        A context holding only :attr:`metric_tables` is not empty: the question
        named a metric, whose tables are enough to answer from. (Nothing in the
        production code calls this today; the builder tests
        :attr:`selected_tables` instead, which is the same set.)
        """
        return not self.entities and not self.facts and not self.metric_tables
