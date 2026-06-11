"""Hybrid table retriever: TF-IDF scoring + synonym expansion + always-include rules."""

from __future__ import annotations

import math
import unicodedata
from functools import lru_cache

from schema.synonyms import SYNONYMS
from schema.tables import TABLES

_TOP_N: int = 6
_MIN_SCORE: float = 0.01
_FORCED_SCORE: float = 1e9   # large enough to always sort to the front
_BIGRAM_MULTIPLIER: float = 1.5


def _normalise(text: str) -> str:
    """NFC-normalise and strip Zero-Width Non-Joiners so substring checks work."""
    return unicodedata.normalize("NFC", text).replace("\u200c", "")


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
        "خرید", "خریدار", "خرید مشتری",
        "purchase", "buyer", "customer purchase",
    ],
    "Ring": [
        "تالار", "رینگ", "پتروشیمی", "کیش", "فلزات",
        "کشاورزی", "نفتی", "خرد", "طلا", "سیمان", "خودرو",
        "ring", "trading hall", "trading ring",
    ],
}

_ALWAYS_INCLUDE_NORMALISED: dict[str, list[str]] = {
    table: [_normalise(s).lower() for s in signals]
    for table, signals in _ALWAYS_INCLUDE.items()
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


def _forced_tables(q_tokens: list[str]) -> set[str]:
    """Return table names whose always-include signals fire on *q_tokens*."""
    q_token_set = set(q_tokens)
    q_joined    = " ".join(q_tokens)
    forced: set[str] = set()
    for table_name, signals in _ALWAYS_INCLUDE_NORMALISED.items():
        for sig in signals:
            token_match  = all(t in q_token_set for t in sig.split())
            substr_match = sig in q_joined
            if token_match or substr_match:
                forced.add(table_name)
                break
    return forced


def retrieve_tables(question: str, fallback: bool = True) -> list[str]:
    """Return up to _TOP_N table names most relevant to *question*.

    Always-include tables whose signals fire are given a sentinel score
    (_FORCED_SCORE) so they always rank inside the top-N slice.
    The result therefore never exceeds _TOP_N entries.

    Parameters
    ----------
    fallback:
        When True (default) and nothing matches at all, return every table.
        Set to False to return [] instead — useful for miss-detection.
    """
    expanded  = _expand(question)
    q_tokens  = _tokenize(expanded)
    q_bigrams = _ngrams(q_tokens, 2)
    idf       = _build_idf()

    forced = _forced_tables(q_tokens)

    scores: dict[str, float] = {}

    for table_name in forced:
        scores[table_name] = _FORCED_SCORE

    for table_name, info in TABLES.items():
        s = _score_table(q_tokens, q_bigrams, idf, info["description"])
        if s >= _MIN_SCORE:
            scores[table_name] = max(scores.get(table_name, 0.0), s)

    if not scores:
        return list(TABLES.keys()) if fallback else []

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [name for name, _ in ranked[:_TOP_N]]
