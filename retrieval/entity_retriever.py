# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Entity (dimension table) retriever.

Strategy (two-tier):
1. Alias match  — fast substring match against knowledge.entities.ENTITIES
   (after Persian/Arabic folding), in configuration order.
2. TF-IDF — if alias match returns nothing, delegate to the TF-IDF engine in
   schema_data.retriever and keep only dimension tables. If it returns
   something, the strongest ranked dimension tables the aliases did not name
   are added to it (:func:`retrieval.table_evidence.scored_extras`): an alias
   hit no longer ends the search.
"""

from __future__ import annotations

from knowledge.entities import ENTITIES
from knowledge.retrieval_hints import FACT_TABLES as _FACT_TABLES
from retrieval.table_evidence import alias_hits, scored_extras
from schema_data.retriever import retrieve_tables


class EntityRetriever:

    @staticmethod
    def retrieve(question: str) -> list[str]:
        """Dimension tables for *question*: alias hits, then ranked extras.

        Parameters
        ----------
        question:
            The natural-language question.

        Returns
        -------
        list[str]
            Table names without duplicates, in a deterministic order (alias
            hits in ``entities.yaml`` order, then extras best first). When no
            alias hit, the dimension tables among the TF-IDF top results.
        """
        results = alias_hits(question, {name: info["aliases"] for name, info in ENTITIES.items()})

        if results:
            return results + [t for t in scored_extras(question, fact=False, exclude=results)]

        # TF-IDF fallback — strip fact tables, keep dimensions only
        tfidf_tables = retrieve_tables(question)
        return [t for t in tfidf_tables if t not in _FACT_TABLES]
