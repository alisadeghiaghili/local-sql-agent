"""Hybrid table retriever: TF-IDF scoring + synonym expansion + always-include rules."""

from __future__ import annotations

import math
import unicodedata
from functools import lru_cache

from schema.synonyms import SYNONYMS
from schema.tables import TABLES

_TOP_N: int = 6
_MIN_SCORE: float = 0.01
_BIGRAM_MULTIPLIER: float = 1.5


def _normalise(text: str) -> str:
    """NFC-normalise and strip Zero-Width Non-Joiners so substring checks work."""
    return unicodedata.normalize("NFC", text).replace("\u200c", "")


# Substring signals — checked against the ZWNJ-stripped, lowercased expanded question.
_ALWAYS_INCLUDE: dict[str, list[str]] = {
    "Date": [
        "تاریخ", "سال", "ماه", "فصل", "هفته", "روز",
        "بهار", "تابستان", "پاییز", "زمستان",
        "دوره", "دورهای", "دوره‌ای",
        "سالیانه", "ماهانه", "هفتگی", "روزانه",
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


def _tokenize(text: str) -> list[str]:
    return _normalise(text).lower().split()


def _ngrams(tokens: list[str], n: int) -> list[str]:
    return [" ".join(tokens[i: i + n]) for i in range(len(tokens) - n + 1)]


@lru_cache(maxsize=1)
def _build_idf() -> dict[str, float]:
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


def _expand(question: str) -> str:
    tokens = _tokenize(question)
    extra: list[str] = []
    for token in tokens:
        if token in SYNONYMS:
            extra.extend(SYNONYMS[token])
    return question + (" " + " ".join(extra) if extra else "")


def _score_table(
    q_tokens: list[str],
    q_bigrams: list[str],
    idf: dict[str, float],
    description: str,
) -> float:
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


def retrieve_tables(question: str) -> list[str]:
    """Return up to _TOP_N table names most relevant to *question*."""
    expanded     = _expand(question)
    expanded_n   = _normalise(expanded).lower()
    expanded_raw = expanded.lower()
    q_tokens     = _tokenize(expanded)
    q_bigrams    = _ngrams(q_tokens, 2)
    idf          = _build_idf()

    scores: dict[str, float] = {}
    for table_name, info in TABLES.items():
        s = _score_table(q_tokens, q_bigrams, idf, info["description"])
        if s >= _MIN_SCORE:
            scores[table_name] = s

    # Always-include: force-add tables whose signals appear in the question,
    # regardless of whether TF-IDF already picked them up.
    for table_name, signals in _ALWAYS_INCLUDE.items():
        sig_n = [_normalise(s).lower() for s in signals]
        if any(s in expanded_n or s in expanded_raw for s in sig_n):
            if table_name not in scores:
                scores[table_name] = _MIN_SCORE

    if not scores:
        return list(TABLES.keys())

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [name for name, _ in ranked[:_TOP_N]]
