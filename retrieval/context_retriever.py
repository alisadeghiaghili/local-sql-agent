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
        └── ValueRetriever       → concrete filters (Ring name, Persian year …)

Each sub-retriever uses its own alias/pattern matching first, then falls
back to the TF-IDF engine in schema_data.retriever when nothing matches.
This keeps latency low (most questions hit the fast path) while guaranteeing
a result even for out-of-vocabulary queries.
"""

from __future__ import annotations

from core.models import RetrievalContext
from retrieval.entity_retriever import EntityRetriever
from retrieval.fact_retriever import FactRetriever
from retrieval.relationship_retriever import RelationshipRetriever
from retrieval.rule_retriever import RuleRetriever
from retrieval.example_retriever import ExampleRetriever
from retrieval.value_retriever import ValueRetriever


class ContextRetriever:
    """Orchestrates all sub-retrievers and returns a single RetrievalContext."""

    @staticmethod
    def retrieve(question: str) -> RetrievalContext:
        """Run the full retrieval pipeline for *question*.

        Parameters
        ----------
        question:
            Natural-language question in Persian or English.

        Returns
        -------
        RetrievalContext
            Fully populated context object ready for PromptBuilder.
        """
        entities = EntityRetriever.retrieve(question)
        facts = FactRetriever.retrieve(question)

        selected_tables = list(dict.fromkeys(entities + facts))  # order-preserving dedup

        relationships = RelationshipRetriever.retrieve(selected_tables)
        rules = RuleRetriever.retrieve(question)
        examples = ExampleRetriever.retrieve(question)
        filters = ValueRetriever.retrieve(question)

        return RetrievalContext(
            entities=entities,
            facts=facts,
            dimensions=entities,          # kept for backward compat; PromptBuilder does NOT read this
            relationships=relationships,
            business_rules=rules,
            examples=examples,
            filters=filters,
        )
