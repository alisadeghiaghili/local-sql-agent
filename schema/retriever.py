"""Hybrid table retriever: TF-IDF scoring + synonym expansion + always-include rules.

Algorithm (applied in order)
-----------------------------
1. **Synonym expansion** — replace surface-form words in the question with
   canonical tokens from ``synonyms.SYNONYMS`` before any scoring.

2. **TF-IDF scoring** — build a corpus from all table descriptions,
   compute per-term IDF weights, then score each table with
   TF-IDF(term, table).  Bigrams are also scored (with 1.5× multiplier).

3. **Always-include rules** — temporal tables (Date) and hub Fact tables
   are injected into the result when the question contains domain signals
   that strongly imply them, regardless of their TF-IDF score.

4. **TOP-N selection** — return up to ``_TOP_N`` tables sorted by score.
   If no table scores above ``_MIN_SCORE``, return all tables as a fallback.

This module has **zero external dependencies** and runs in microseconds.
"""

from __future__ import annotations

import math
import unicodedata
from functools import lru_cache

from schema.synonyms import SYNONYMS
from schema.tables import TABLES

_TOP_N: int = 6
_MIN_SCORE: float = 0.01
_BIGRAM_MULTIPLIER: float = 1.5


# ---------------------------------------------------------------------------
# Unicode normalisation helper
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """NFC-normalise and strip Zero-Width Non-Joiners so substring checks work."""
    return unicodedata.normalize("NFC", text).replace("\u200c", "")


# ---------------------------------------------------------------------------
# Always-include signals
# ---------------------------------------------------------------------------
# Substring match after normalisation so "دوره‌ای" / "دورهای" both match "دوره"
_ALWAYS_INCLUDE: dict[str, list[str]] = {
    "Date": [
        "تاریخ", "سال", "ماه", "فصل", "هفته", "روز",
        "بهار", "تابستان", "پاییز", "زمستان",
        "دوره", "سالیانه", "ماهانه", "هفتگی", "روزانه",
        "date", "year", "month", "season", "week",
        "spring", "summer", "autumn", "winter",
        "quarterly", "monthly", "yearly", "annual", "period",
    ],
    "Contract": [
        "معامله", "قرارداد", "حجم", "ارزش",
        "trade", "contract", "deal", "volume", "value",
    ],
    "CustomerContract": [
        "خرید", "خریدار", "مشتری", "خرید مشتری",
        "purchase", "buyer", "customer purchase",
    ],
}


# ---------------------------------------------------------------------------
# IDF index (built once, cached)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return _normalise(text).lower().split()


def _ngrams(tokens: list[str], n: int) -> list[str]:
    return [" ".join(tokens[i: i + n]) for i in range(len(tokens) - n + 1)]


@lru_cache(maxsize=1)
def _build_idf() -> dict[str, float]:
    """Compute IDF for every unigram and bigram across all table descriptions."""
    N = len(TABLES)
    doc_freq: dict[str, int] = {}

    for info in TABLES.values():
        tokens = _tokenize(info["description"])
        terms = set(tokens) | set(_ngrams(tokens, 2))
        for term in terms:
            doc_freq[term] = doc_freq.get(term, 0) + 1

    return {
        term: math.log((N + 1) / (df + 1)) + 1.0
        for term, df in doc_freq.items()
    }


# ---------------------------------------------------------------------------
# Synonym expansion
# ---------------------------------------------------------------------------

def _expand(question: str) -> str:
    """Inject canonical synonym tokens into the question string."""
    tokens = _tokenize(question)
    extra: list[str] = []
    for token in tokens:
        if token in SYNONYMS:
            extra.extend(SYNONYMS[token])
    return question + (" " + " ".join(extra) if extra else "")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_table(
    q_tokens: list[str],
    q_bigrams: list[str],
    idf: dict[str, float],
    description: str,
) -> float:
    """Return a TF-IDF-based relevance score for one table."""
    d_tokens  = _tokenize(description)
    d_bigrams = _ngrams(d_tokens, 2)
    d_len     = len(d_tokens) or 1

    tf: dict[str, float] = {}
    for t in d_tokens:
        tf[t] = tf.get(t, 0) + 1.0 / d_len
    for bg in d_bigrams:
        tf[bg] = tf.get(bg, 0) + _BIGRAM_MULTIPLIER / d_len

    score = 0.0
    for term in set(q_tokens):
        if term in tf and term in idf:
            score += tf[term] * idf[term]
    for bg in set(q_bigrams):
        if bg in tf and bg in idf:
            score += tf[bg] * idf[bg] * _BIGRAM_MULTIPLIER

    return score


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def retrieve_tables(question: str) -> list[str]:
    """Return up to ``_TOP_N`` table names most relevant to *question*.

    Strategy
    --------
    1. Synonym-expand the question.
    2. Score every table with TF-IDF (unigrams + bigrams).
    3. Inject always-include tables when domain signals appear as
       substrings in the Unicode-normalised expanded question.
    4. Return top-N by score; fall back to all tables when nothing matches.
    """
    expanded   = _expand(question)
    expanded_n = _normalise(expanded).lower()
    q_tokens   = _tokenize(expanded)
    q_bigrams  = _ngrams(q_tokens, 2)
    idf        = _build_idf()

    scores: dict[str, float] = {}
    for table_name, info in TABLES.items():
        s = _score_table(q_tokens, q_bigrams, idf, info["description"])
        if s >= _MIN_SCORE:
            scores[table_name] = s

    for table_name, signals in _ALWAYS_INCLUDE.items():
        if table_name not in scores:
            if any(_normalise(sig).lower() in expanded_n for sig in signals):
                scores[table_name] = _MIN_SCORE

    if not scores:
        return list(TABLES.keys())

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [name for name, _ in ranked[:_TOP_N]]
