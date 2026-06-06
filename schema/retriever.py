"""Keyword + bigram table retriever.

Scores each registered table by matching its description tokens
(unigrams and bigrams) against the user's question.
Returns up to ``_TOP_N`` table names sorted by relevance.

This is a lightweight, zero-dependency alternative to embedding-based
retrieval — no vector DB required.
"""

from __future__ import annotations

from schema.tables import TABLES

_TOP_N: int = 6
_MIN_SCORE: int = 1


def _ngrams(text: str, n: int) -> set[str]:
    tokens = text.lower().split()
    return {" ".join(tokens[i: i + n]) for i in range(len(tokens) - n + 1)}


def retrieve_tables(question: str) -> list[str]:
    """Return up to ``_TOP_N`` table names most relevant to *question*.

    Matching uses both unigrams and bigrams from the table description.
    Falls back to returning all tables if no match is found.
    """
    q_unigrams = set(question.lower().split())
    q_bigrams  = _ngrams(question, 2)

    scores: list[tuple[str, int]] = []

    for table_name, info in TABLES.items():
        desc       = info["description"].lower()
        t_unigrams = set(desc.split())
        t_bigrams  = _ngrams(desc, 2)

        score = (
            len(q_unigrams & t_unigrams)
            + len(q_bigrams & t_bigrams) * 2   # bigram match counts double
        )
        if score >= _MIN_SCORE:
            scores.append((table_name, score))

    if not scores:
        return list(TABLES.keys())   # fallback: return all

    scores.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in scores[:_TOP_N]]
