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


# Signals for always-include tables.
# A signal matches when it appears as an exact token OR as a substring in the
# normalised, lowercased, ZWNJ-stripped expanded question.
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

# Pre-normalise all signals once at import time for fast lookup.
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
    q_joined = " ".join(q_tokens)
    forced: set[str] = set()
    for table_name, signals in _ALWAYS_INCLUDE_NORMALISED.items():
        for sig in signals:
            sig_tokens = sig.split()
            # exact token match (single or multi-word signal)
            token_match = all(t in q_token_set for t in sig_tokens)
            # substring match — catches partial stems / compound forms
            substr_match = sig in q_joined
            if token_match or substr_match:
                forced.add(table_name)
                break
    return forced


def retrieve_tables(question: str, fallback: bool = True) -> list[str]:
    """Return table names most relevant to *question*.

    Always-include tables that match signals are returned unconditionally
    and are **not** counted against *_TOP_N*, so the result list may
    contain more than _TOP_N entries when forced tables are present.

    Parameters
    ----------
    fallback:
        When True (default) and no table scores above _MIN_SCORE *and* no
        always-include signals fire, return all tables.  Set to False to
        return an empty list instead — useful when callers need to
        distinguish "no match" from "all".
    """
    expanded    = _expand(question)
    q_tokens    = _tokenize(expanded)
    q_bigrams   = _ngrams(q_tokens, 2)
    idf         = _build_idf()

    # 1. Collect always-include tables independently of scoring.
    forced = _forced_tables(q_tokens)

    # 2. TF-IDF scores for all tables.
    scores: dict[str, float] = {}
    for table_name, info in TABLES.items():
        s = _score_table(q_tokens, q_bigrams, idf, info["description"])
        if s >= _MIN_SCORE:
            scores[table_name] = s

    # 3. If nothing matched at all, apply fallback policy.
    if not scores and not forced:
        return list(TABLES.keys()) if fallback else []

    # 4. Rank by score, take top-N (forced tables excluded from this cap).
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_scored = [name for name, _ in ranked[:_TOP_N] if name not in forced]

    # 5. Always-include tables come first; scored tables fill the rest.
    return list(forced) + top_scored
