# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Entity (dimension table) retriever.

Strategy (two-tier):
1. Alias match  — fast, exact substring match against knowledge.entities.ENTITIES.
2. TF-IDF fallback — if alias match returns nothing, delegate to the
   TF-IDF engine in schema_data.retriever and keep only dimension tables.
"""

from __future__ import annotations

from knowledge.entities import ENTITIES
from knowledge.retrieval_hints import FACT_TABLES as _FACT_TABLES
from schema_data.retriever import retrieve_tables


class EntityRetriever:

    @staticmethod
    def retrieve(question: str) -> list[str]:

        q = question.lower()
        results: list[str] = []

        for entity_name, entity_info in ENTITIES.items():
            for alias in entity_info["aliases"]:
                if alias.lower() in q:
                    results.append(entity_name)
                    break

        if results:
            return list(set(results))

        # TF-IDF fallback — strip fact tables, keep dimensions only
        tfidf_tables = retrieve_tables(question)
        return [t for t in tfidf_tables if t not in _FACT_TABLES]
