"""Hybrid table retriever: TF-IDF scoring + synonym expansion + always-include rules.

Algorithm (applied in order)
-----------------------------
1. **Synonym expansion** — replace surface-form words in the question with
   canonical tokens from ``synonyms.SYNONYMS`` before any scoring.
   Inflected forms (e.g. دورهای, سالیانهای) are stem-matched against SYNONYMS
   keys so Persian morphological variants are handled automatically.

2. **TF-IDF scoring** — build a corpus from all table descriptions,
   compute per-term IDF weights, then score each table with
   TF-IDF(term, table).  Bigrams are also scored (with 1.5× multiplier).

3. **Always-include rules** — temporal tables (Date) and hub Fact tables
   are injected into the result when the question contains domain signals
   that strongly imply them, regardless of their TF-IDF score.

4. **TOP-N selection** — return up to ``_TOP_N`` tables sorted by score.
   If no table scores above ``_MIN_SCORE``, return an empty list (the caller
   should treat this as "out of domain" rather than retrieving everything).
   Use ``retrieve_tables(question, fallback=True)`` to restore the old
   "return all tables" fallback when needed.

This module has **zero external dependencies** and runs in microseconds.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Optional

from schema.synonyms import SYNONYMS
from schema.tables import TABLES

_TOP_N: int = 6
_MIN_SCORE: float = 0.01
_BIGRAM_MULTIPLIER: float = 1.5

# ---------------------------------------------------------------------------
# Always-include signals
# ---------------------------------------------------------------------------
# If ANY of these tokens appear in the (expanded) question, the paired table
# is force-added to the result set (even if its TF-IDF score is zero).
_ALWAYS_INCLUDE: dict[str, list[str]] = {
    "Date": [
        # exact Persian tokens
        "تاریخ", "سال", "ماه", "فصل", "هفته", "روز",
        "بهار", "تابستان", "پاییز", "زمستان",
        "دوره", "سالیانه", "ماهانه", "هفتگی", "روزانه",
        # common inflected / compound forms
        "دورهای", "دورهٔ", "سالانه", "ماهیانه",
        "تاریخی", "سالهای", "ماهها", "هفتهها", "روزهای",
        # English
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
    return text.lower().split()


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
        term: math.log((N + 1) / (df + 1)) + 1.0   # smoothed IDF
        for term, df in doc_freq.items()
    }


# ---------------------------------------------------------------------------
# Synonym expansion
# ---------------------------------------------------------------------------

def _stem_match(token: str) -> list[str]:
    """Return canonical expansions for *token* using prefix/stem matching.

    Handles common Persian inflections where a token is a longer form of a
    SYNONYMS key (e.g. 'دورهای' starts with 'دوره', 'سالیانهای' → 'سالیانه').
    Only matches when the key is at least 3 characters and the token starts
    with the key to avoid spurious partial matches on short keys.
    """
    extra: list[str] = []
    token_lower = token.lower()
    for key, expansions in SYNONYMS.items():
        if len(key) >= 3 and token_lower.startswith(key) and token_lower != key:
            extra.extend(expansions)
    return extra


def _expand(question: str) -> str:
    """Inject canonical synonym tokens into the question string.

    Performs two passes:
    1. Exact match against SYNONYMS keys.
    2. Prefix/stem match for inflected forms not present as exact keys.
    """
    tokens = _tokenize(question)
    extra: list[str] = []
    for token in tokens:
        if token in SYNONYMS:
            extra.extend(SYNONYMS[token])
        else:
            extra.extend(_stem_match(token))
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

    # term frequency in description (normalised)
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

def retrieve_tables(question: str, fallback: bool = False) -> list[str]:
    """Return up to ``_TOP_N`` table names most relevant to *question*.

    Parameters
    ----------
    question:
        The natural-language question to match against table descriptions.
    fallback:
        When *True*, return all table names if nothing scores above
        ``_MIN_SCORE`` (legacy behaviour used by ``test_fallback_on_no_match``).
        When *False* (default), return an empty list for out-of-domain questions
        so that callers (e.g. ``analyze_misses``) can detect "nothing retrieved".

    Strategy
    --------
    1. Synonym-expand the question (exact + stem matching).
    2. Score every table with TF-IDF (unigrams + bigrams).
    3. Inject always-include tables when domain signals are present.
    4. Return top-N by score; behaviour when nothing matches is controlled
       by the *fallback* parameter.
    """
    expanded   = _expand(question)
    q_tokens   = _tokenize(expanded)
    q_bigrams  = _ngrams(q_tokens, 2)
    idf        = _build_idf()

    scores: dict[str, float] = {}
    for table_name, info in TABLES.items():
        s = _score_table(q_tokens, q_bigrams, idf, info["description"])
        if s >= _MIN_SCORE:
            scores[table_name] = s

    # --- always-include injection ---
    expanded_lower = expanded.lower()
    for table_name, signals in _ALWAYS_INCLUDE.items():
        if table_name not in scores:
            if any(sig in expanded_lower for sig in signals):
                scores[table_name] = _MIN_SCORE

    if not scores:
        # fallback=True: return all tables (used when caller expects a non-empty
        # result for completely novel / nonsense queries)
        return list(TABLES.keys()) if fallback else []

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [name for name, _ in ranked[:_TOP_N]]
