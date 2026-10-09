# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Fact table retriever.

Strategy (two-tier):
1. Pattern match — fast keyword match for known fact table signals (after
   Persian/Arabic folding), in configuration order.
2. TF-IDF — if nothing matched, delegate to schema_data.retriever and keep
   only known fact tables. If something matched, the strongest ranked fact
   tables the patterns did not name are added to it
   (:func:`retrieval.table_evidence.scored_extras`).
"""

from __future__ import annotations

from knowledge.retrieval_hints import FACT_PATTERNS, FACT_TABLES as _FACT_TABLES
from retrieval.table_evidence import alias_hits, scored_extras
from schema_data.retriever import retrieve_tables


class FactRetriever:

    @staticmethod
    def retrieve(question: str) -> list[str]:
        """Fact tables for *question*: pattern hits, then ranked extras.

        Parameters
        ----------
        question:
            The natural-language question.

        Returns
        -------
        list[str]
            Table names without duplicates, in a deterministic order.
        """
        matches = alias_hits(question, FACT_PATTERNS)

        if matches:
            return matches + scored_extras(question, fact=True, exclude=matches)

        # TF-IDF fallback — keep only fact tables
        tfidf_tables = retrieve_tables(question)
        return [t for t in tfidf_tables if t in _FACT_TABLES]
