"""Fact table retriever.

Strategy (two-tier):
1. Pattern match — fast keyword match for known fact table signals.
2. TF-IDF fallback — if nothing matched, delegate to schema_data.retriever
   and keep only known fact tables.
"""

from __future__ import annotations

from schema_data.retriever import retrieve_tables

_FACT_TABLES = {"Contract", "CustomerContract", "Offer", "Order", "TalarLog"}

FACT_PATTERNS: dict[str, list[str]] = {
    "CustomerContract": ["خرید", "purchase", "customer purchase", "خریدار"],
    "Contract": ["معامله", "قرارداد", "trade", "sales"],
    "Offer": ["عرضه", "offer", "supply"],
    "Order": ["سفارش", "order"],
    "TalarLog": ["لاگ", "گزارش عملیات"],
}


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
