# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Fact table retriever.

Strategy (two-tier):
1. Pattern match — fast keyword match for known fact table signals.
2. TF-IDF fallback — if nothing matched, delegate to schema_data.retriever
   and keep only known fact tables.
"""

from __future__ import annotations

from knowledge.retrieval_hints import FACT_PATTERNS, FACT_TABLES as _FACT_TABLES
from schema_data.retriever import retrieve_tables


class FactRetriever:

    @staticmethod
    def retrieve(question: str) -> list[str]:

        q = question.lower()
        matches: list[str] = []

        for fact, aliases in FACT_PATTERNS.items():
            for alias in aliases:
                if alias.lower() in q:
                    matches.append(fact)
                    break

        if matches:
            return matches

        # TF-IDF fallback — keep only fact tables
        tfidf_tables = retrieve_tables(question)
        return [t for t in tfidf_tables if t in _FACT_TABLES]
