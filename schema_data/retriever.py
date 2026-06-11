"""Hybrid table retriever: TF-IDF scoring + synonym expansion + always-include rules.

This module is the canonical TF-IDF fallback engine used by EntityRetriever
and FactRetriever when alias/pattern matching returns no results.
"""

from __future__ import annotations

import math
import unicodedata
from functools import lru_cache

from knowledge.aliases import SYNONYMS
from schema_data.tables import TABLE_DESCRIPTIONS as TABLES

_TOP_N: int = 6
_MIN_SCORE: float = 0.01
_FORCED_SCORE: float = 1e9
_BIGRAM_MULTIPLIER: float = 1.5


def _normalise(text: str) -> str:
    """NFC-normalise and strip Zero-Width Non-Joiners."""
    return unicodedata.normalize("NFC", text).replace("\u200c", "")


_ALWAYS_INCLUDE: dict[str, list[str]] = {
    "Date": [
        "\u062a\u0627\u0631\u06cc\u062e", "\u0633\u0627\u0644", "\u0645\u0627\u0647", "\u0641\u0635\u0644", "\u0647\u0641\u062a\u0647", "\u0631\u0648\u0632",
        "\u0628\u0647\u0627\u0631", "\u062a\u0627\u0628\u0633\u062a\u0627\u0646", "\u067e\u0627\u06cc\u06cc\u0632", "\u0632\u0645\u0633\u062a\u0627\u0646",
        "\u062f\u0648\u0631\u0647", "\u062f\u0648\u0631\u0647\u0627\u06cc", "\u062f\u0648\u0631\u0647\u200c\u0627\u06cc",
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
        "\u062e\u0631\u06cc\u062f", "\u062e\u0631\u06cc\u062f\u0627\u0631", "\u062e\u0631\u06cc\u062f \u0645\u0634\u062a\u0631\u06cc",
        "purchase", "buyer", "customer purchase",
    ],
    "Offer": [
        "\u0639\u0631\u0636\u0647", "\u0639\u0631\u0636\u0647\u06a9\u0646\u0646\u062f\u0647", "\u0639\u0631\u0636\u0647\u06a9\u0646\u0646\u062f\u06af\u0627\u0646", "\u0639\u0631\u0636\u0647 \u06a9\u0627\u0644\u0627", "\u06a9\u0627\u0644\u0627",
        "offer", "supply", "listing",
    ],
    "Order": [
        "\u0633\u0641\u0627\u0631\u0634", "\u0633\u0641\u0627\u0631\u0634 \u062e\u0631\u06cc\u062f", "\u062f\u0631\u062e\u0648\u0627\u0633\u062a",
        "order", "purchase order",
    ],
    "Ring": [
        "\u062a\u0627\u0644\u0627\u0631", "\u0631\u06cc\u0646\u06af", "\u067e\u062a\u0631\u0648\u0634\u06cc\u0645\u06cc", "\u06a9\u06cc\u0634", "\u0641\u0644\u0632\u0627\u062a",
        "\u06a9\u0634\u0627\u0648\u0631\u0632\u06cc", "\u0646\u0641\u062a\u06cc", "\u062e\u0631\u062f", "\u0637\u0644\u0627", "\u0633\u06cc\u0645\u0627\u0646", "\u062e\u0648\u062f\u0631\u0648",
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


class _IdfDict(dict):
    """A dict that returns the maximum IDF value for any unseen term.

    This ensures that ``idf.get(rare_term, 0)`` is never needed — callers can
    use plain ``idf[term]`` or ``idf.get(term)`` and always receive a meaningful
    weight.  In particular it satisfies the property:

        idf[unseen_term] > idf[common_term_that_appears_everywhere]

    because a term absent from all documents has a higher IDF than one present
    in every document.
    """

    def __missing__(self, key: str) -> float:  # noqa: D105
        return self._max_idf

    @classmethod
    def build(cls, N: int, doc_freq: dict[str, int]) -> "_IdfDict":
        obj = cls()
        obj._max_idf = math.log(N + 1) + 1.0  # IDF when df == 0
        for term, df in doc_freq.items():
            obj[term] = math.log((N + 1) / (df + 1)) + 1.0
        return obj


@lru_cache(maxsize=1)
def _build_idf() -> _IdfDict:
    """Build IDF weights for all terms found in TABLES descriptions.

    Returns an :class:`_IdfDict` that yields the theoretical maximum IDF for
    any term not seen in any document, so callers never need a fallback of 0.
    """
    N = len(TABLES)
    doc_freq: dict[str, int] = {}
    for info in TABLES.values():
        desc = info if isinstance(info, str) else info.get("description", "")
        tokens = _tokenize(desc)
        terms = set(tokens) | set(_ngrams(tokens, 2))
        for term in terms:
            doc_freq[term] = doc_freq.get(term, 0) + 1
    return _IdfDict.build(N, doc_freq)


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
    idf: _IdfDict,
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
        if term in tf:
            score += tf[term] * idf[term]
    for bg in set(q_bigrams):
        if bg in tf:
            score += tf[bg] * idf[bg] * _BIGRAM_MULTIPLIER
    return score


def _forced_tables(q_tokens: list[str]) -> set[str]:
    q_token_set = set(q_tokens)
    q_joined    = " ".join(q_tokens)
    forced: set[str] = set()
    for table_name, signals in _ALWAYS_INCLUDE_NORMALISED.items():
        for sig in signals:
            if all(t in q_token_set for t in sig.split()) or sig in q_joined:
                forced.add(table_name)
                break
    return forced


def retrieve_tables(question: str, fallback: bool = True) -> list[str]:
    """Return up to _TOP_N table names most relevant to *question*.

    Parameters
    ----------
    fallback:
        When True (default) and nothing matches, return every table.
        Set False to return [] — useful for miss-detection.
    """
    expanded  = _expand(question)
    q_tokens  = _tokenize(expanded)
    q_bigrams = _ngrams(q_tokens, 2)
    idf       = _build_idf()
    forced    = _forced_tables(q_tokens)

    scores: dict[str, float] = {table: _FORCED_SCORE for table in forced}

    for table_name, info in TABLES.items():
        desc = info if isinstance(info, str) else info.get("description", "")
        s = _score_table(q_tokens, q_bigrams, idf, desc)
        if s >= _MIN_SCORE:
            scores[table_name] = max(scores.get(table_name, 0.0), s)

    if not scores:
        return list(TABLES.keys()) if fallback else []

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [name for name, _ in ranked[:_TOP_N]]
