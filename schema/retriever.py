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


# Substring/token signals — checked against the ZWNJ-stripped, lowercased expanded question.
# Each value is a list of strings; a signal matches if it appears as a substring
# OR as an exact token in the normalised question.
_ALWAYS_INCLUDE: dict[str, list[str]] = {
    "Date": [
        "\u062a\u0627\u0631\u06cc\u062e", "\u0633\u0627\u0644", "\u0645\u0627\u0647", "\u0641\u0635\u0644", "\u0647\u0641\u062a\u0647", "\u0631\u0648\u0632",
        "\u0628\u0647\u0627\u0631", "\u062a\u0627\u0628\u0633\u062a\u0627\u0646", "\u067e\u0627\u06cc\u06cc\u0632", "\u0632\u0645\u0633\u062a\u0627\u0646",
        "\u062f\u0648\u0631\u0647", "\u062f\u0648\u0631\u0647\u0627\u06cc", "\u062f\u0648\u0631\u0647\u0627\u06cc\u06cc",
        "\u0633\u0627\u0644\u06cc\u0627\u0646\u0647", "\u0645\u0627\u0647\u0627\u0646\u0647", "\u0647\u0641\u062a\u06af\u06cc", "\u0631\u0648\u0632\u0627\u0646\u0647",
        "date", "year", "month", "season", "week",
        "spring", "summer", "autumn", "winter",
        "quarterly", "monthly", "yearly", "annual", "period",
    ],
    "Contract": [
        "\u0645\u0639\u0627\u0645\u0644\u0647", "\u0642\u0631\u0627\u0631\u062f\u0627\u062f", "\u062d\u062c\u0645", "\u0627\u0631\u0632\u0634",
        "trade", "contract", "deal", "volume", "value",
    ],
    "CustomerContract": [
        "\u062e\u0631\u06cc\u062f", "\u062e\u0631\u06cc\u062f\u0627\u0631", "\u0645\u0634\u062a\u0631\u06cc", "\u062e\u0631\u06cc\u062f \u0645\u0634\u062a\u0631\u06cc",
        "purchase", "buyer", "customer purchase",
    ],
}

# Pre-normalise all signals once at import time for fast lookup
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


def retrieve_tables(question: str, fallback: bool = True) -> list[str]:
    """Return up to _TOP_N table names most relevant to *question*.

    Parameters
    ----------
    fallback:
        When True (default) and no table scores above _MIN_SCORE, return
        all tables.  Set to False to return an empty list instead, which
        is useful when callers need to distinguish “no match” from “all”.
    """
    expanded   = _expand(question)
    q_tokens   = _tokenize(expanded)
    q_token_set = set(q_tokens)
    q_bigrams  = _ngrams(q_tokens, 2)
    idf        = _build_idf()

    scores: dict[str, float] = {}
    for table_name, info in TABLES.items():
        s = _score_table(q_tokens, q_bigrams, idf, info["description"])
        if s >= _MIN_SCORE:
            scores[table_name] = s

    # Always-include: match signal as substring OR as exact token
    for table_name, signals in _ALWAYS_INCLUDE_NORMALISED.items():
        for sig in signals:
            sig_tokens = sig.split()
            # substring match (covers multi-word signals like 'خرید مشتری')
            token_match = all(t in q_token_set for t in sig_tokens)
            substr_match = sig in " ".join(q_tokens)
            if token_match or substr_match:
                if table_name not in scores:
                    scores[table_name] = _MIN_SCORE
                break

    if not scores:
        return list(TABLES.keys()) if fallback else []

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [name for name, _ in ranked[:_TOP_N]]
