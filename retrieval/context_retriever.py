# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Context retriever — single entry-point for the modular retrieval pipeline.

Pipeline
--------
    question
        ├── EntityRetriever      → dimension tables (Ring, Customer, Symbol …)
        ├── FactRetriever        → fact tables (Contract, CustomerContract …)
        ├── RelationshipRetriever → JOIN clauses for selected tables
        ├── RuleRetriever        → business rules injected into the prompt
        ├── ExampleRetriever     → few-shot SQL examples ranked by tag overlap
        ├── ValueRetriever       → concrete filters (Ring name, Persian year …)
        └── dimension_vocabulary → filters for other small dimensions, matched
                                    against a prefetched, cached value set
                                    (Phase 5b) — never a per-request DB call

Each sub-retriever uses its own alias/pattern matching first, then falls
back to the TF-IDF engine in schema_data.retriever when nothing matches.
This keeps latency low (most questions hit the fast path) while guaranteeing
a result even for out-of-vocabulary queries.
"""

from __future__ import annotations

from core.models import RetrievalContext
from core.persian import normalize_for_matching
from retrieval.dimension_vocabulary import match_question_against_vocabulary
from retrieval.entity_retriever import EntityRetriever
from retrieval.fact_retriever import FactRetriever
from retrieval.relationship_retriever import RelationshipRetriever
from retrieval.rule_retriever import RuleRetriever
from retrieval.example_retriever import ExampleRetriever
from retrieval.value_retriever import ValueRetriever
from security.auth import ANONYMOUS, Principal


class ContextRetriever:
    """Orchestrates all sub-retrievers and returns a single RetrievalContext."""

    @staticmethod
    def retrieve(
        question: str,
        *,
        principal: Principal = ANONYMOUS,
    ) -> RetrievalContext:
        """Run the full retrieval pipeline for *question*.

        Parameters
        ----------
        question:
            Natural-language question in Persian or English.
        principal:
            The caller's identity (Phase 8) — threaded to
            :func:`~retrieval.dimension_vocabulary.match_question_against_vocabulary`
            so a match never uses a value from a column the caller's ACL
            denies. Defaults to :data:`~security.auth.ANONYMOUS` (no
            restriction), matching every existing call site that does not
            pass this keyword.

        Returns
        -------
        RetrievalContext
            Fully populated context object ready for PromptBuilder.

        Notes
        -----
        Value resolution precedence, three tiers:

        1. :class:`~retrieval.value_retriever.ValueRetriever`'s static
           alias/pattern matching (ring aliases, Persian
           dates/years/months/seasons/weekdays) runs first and always wins
           when it matches — it is faster and needs no database round trip
           at all, cached or otherwise.
        2. :func:`~retrieval.dimension_vocabulary.match_question_against_vocabulary`
           runs next, only for entity tables tier 1 left unresolved (see
           ``db_candidate_tables`` below) — never overwrites a filter tier 1
           already set. This never touches the database *on this call*: it
           searches the question against a vocabulary
           :mod:`retrieval.dimension_vocabulary` prefetched and cached out
           of band (see that module's docstring for the cold-start/TTL
           story). Covers ``Ring``, ``Currency``, ``Broker``,
           ``DeliveryPlace``, ``Symbol``.
        3. ``Customer``/``Supplier`` are resolved by **neither** tier today.
           ``retrieval.value_resolver.resolve_value`` exists, is fully
           tested, and could resolve them — but is not called from here.
           Wiring it in with the whole question as its search span was
           measured to build one guaranteed-miss, unindexable-scan query
           per allowlisted column on every request; see that module's
           docstring for the numbers and why the call site was removed
           rather than kept as a "safe" no-op. These two dimensions need a
           real mention extractor before this tier can be re-enabled; until
           then, a question naming a specific customer or supplier is
           answered exactly as it was before Phase 5b — the model's own
           guess.
        """
        entities = EntityRetriever.retrieve(question)
        facts = FactRetriever.retrieve(question)

        selected_tables = list(dict.fromkeys(entities + facts))  # order-preserving dedup

        relationships = RelationshipRetriever.retrieve(selected_tables)
        rules = RuleRetriever.retrieve(question)
        examples = ExampleRetriever.retrieve(question)
        filters = ValueRetriever.retrieve(question)

        value_clarifications = []
        # Only entity tables the static pass left unresolved are worth
        # consulting the prefetched vocabulary for -- ValueRetriever already
        # won for anything already in `filters` (see the precedence note
        # above). match_question_against_vocabulary itself further narrows
        # this to the small-cardinality tables it actually has a vocabulary
        # for (Customer/Supplier are silently skipped, not queried).
        db_candidate_tables = [t for t in entities if t not in filters]
        if db_candidate_tables:
            match_result = match_question_against_vocabulary(
                normalize_for_matching(question),
                db_candidate_tables,
                principal=principal,
            )
            if match_result.filters:
                filters = {**filters, **match_result.filters}
            value_clarifications.extend(match_result.clarifications)

        return RetrievalContext(
            entities=entities,
            facts=facts,
            dimensions=entities,          # kept for backward compat; PromptBuilder does NOT read this
            relationships=relationships,
            business_rules=rules,
            examples=examples,
            filters=filters,
            value_clarifications=value_clarifications,
        )
