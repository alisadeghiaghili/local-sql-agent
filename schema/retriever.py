"""Keyword-based table retriever.

Scores each table by how many of its description words appear in the question,
then returns the top-6 most relevant table names.

This is a lightweight alternative to embedding-based retrieval —
no vector DB required.
"""

from __future__ import annotations

from schema.tables import TABLES

_TOP_N = 6


def retrieve_tables(question: str) -> list[str]:
    """Return up to *_TOP_N* table names most relevant to *question*.

    Falls back to all tables if no keyword match is found.
    """
    q = question.lower()
    scores: list[tuple[str, int]] = []

    for table_name, info in TABLES.items():
        score = sum(
            1
            for term in info["description"].lower().split()
            if term and term in q
        )
        if score > 0:
            scores.append((table_name, score))

    if not scores:
        return list(TABLES.keys())   # no match: send all tables

    scores.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in scores[:_TOP_N]]
