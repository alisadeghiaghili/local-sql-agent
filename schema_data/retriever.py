# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Hybrid table retriever: TF-IDF scoring + synonym expansion + always-include rules.

This module is the canonical TF-IDF fallback engine used by EntityRetriever
and FactRetriever when alias/pattern matching returns no results.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache

from core.persian import normalize_for_matching
from knowledge.aliases import SYNONYMS
from knowledge.retrieval_hints import ALWAYS_INCLUDE
from schema_data.columns import TABLE_COLUMNS
from schema_data.tables import TABLE_DESCRIPTIONS as TABLES

#: A word is a run of letters, digits or underscores of any script. Used to
#: split text instead of ``str.split`` so that sentence punctuation
#: (``"customers?"``, ``"سال؟"``, ``"sales.Order"``) never glues itself to a
#: word and hides it from the index.
_WORD_RE = re.compile(r"\w+")

_TOP_N: int = 6
_MIN_SCORE: float = 0.01
_FORCED_SCORE: float = 1e9
_BIGRAM_MULTIPLIER: float = 1.5

#: Column-name evidence (see :func:`_column_scores`). A column term only counts
#: when it is rarer than ``_COLUMN_MIN_WEIGHT`` (its weight is
#: ``ln((N + 1) / (df + 1))``, so a term in every table weighs 0 and one in
#: about 60% of tables weighs 0.5); each matched term then adds
#: ``_COLUMN_SCALE`` times its weight, a term found only in a column's
#: description counting ``_COLUMN_DESCRIPTION_SHARE`` as much as one in its name.
_COLUMN_MIN_WEIGHT: float = 0.5
_COLUMN_SCALE: float = 0.1
_COLUMN_DESCRIPTION_SHARE: float = 0.5


def _normalise(text: str) -> str:
    """Fold *text* to the codebase's canonical Persian matching form.

    Delegates to :func:`core.persian.normalize_for_matching` (digit folding,
    Arabic-form letter folding, ZWNJ stripping, whitespace collapsing, ASCII
    lowercasing) instead of the NFC-only fold this module used before
    unification -- callers already lowercase again after this, so the
    added lowercasing here is harmless.
    """
    return normalize_for_matching(text)


def _tokenize(text: str) -> list[str]:
    """Words of *text*, normalised and lower-cased, punctuation removed.

    Examples
    --------
    >>> _tokenize("Total sales, by customer?")
    ['total', 'sales', 'by', 'customer']
    >>> _tokenize("sales.Order \u2014 purchase orders")
    ['sales', 'order', 'purchase', 'orders']
    >>> _tokenize("\u0645\u062c\u0645\u0648\u0639 \u0641\u0631\u0648\u0634\u061f")
    ['\u0645\u062c\u0645\u0648\u0639', '\u0641\u0631\u0648\u0634']
    """
    return _WORD_RE.findall(_normalise(text).lower())


#: Table -> forced-match trigger-phrase map, loaded from
#: ``project_config/retrieval_hints.yaml`` (see
#: :mod:`knowledge.retrieval_hints`). A retrieval heuristic, not schema
#: metadata -- it can name a table independently of whichever
#: ``schema.yaml`` happens to be loaded (see :func:`_forced_tables` and
#: ``tests/test_retriever.py::TestRetrieveTables::test_all_returned_names_are_valid_tables``).
#: Each phrase is stored as its words joined by single spaces, tokenised
#: exactly as a question is.
_ALWAYS_INCLUDE_NORMALISED: dict[str, list[str]] = {
    table: [" ".join(_tokenize(s)) for s in signals]
    for table, signals in ALWAYS_INCLUDE.items()
}


def _ngrams(tokens: list[str], n: int) -> list[str]:
    return [" ".join(tokens[i: i + n]) for i in range(len(tokens) - n + 1)]


class _IdfDict(dict):
    """Dict-like container whose get/index access returns max IDF for unseen terms."""

    def __missing__(self, key: str) -> float:  # noqa: D105
        return self._max_idf

    def get(self, key, default=None):  # noqa: D401
        return super().get(key, self._max_idf)

    @classmethod
    def build(cls, N: int, doc_freq: dict[str, int]) -> "_IdfDict":
        obj = cls()
        obj._max_idf = math.log(N + 1) + 1.0
        for term, df in doc_freq.items():
            obj[term] = math.log((N + 1) / (df + 1)) + 1.0
        return obj


@lru_cache(maxsize=1)
def _build_idf() -> _IdfDict:
    """Build IDF weights for all terms found in TABLES descriptions."""
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
    d_tokens = _tokenize(description)
    d_bigrams = _ngrams(d_tokens, 2)
    d_len = len(d_tokens) or 1
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


def _stem(token: str) -> str:
    """Strip a plural ending: English ``-s``/``-es``/``-ies``, Persian ``ها``/``های``.

    Deliberately conservative -- a token that might lose part of its root
    (``status``, ``address``, a short word) is left alone. Used on both sides
    of every comparison it appears in, so ``customers`` and ``customer`` meet.

    Examples
    --------
    >>> [_stem(t) for t in ("customers", "categories", "branches", "status", "address", "ids")]
    ['customer', 'category', 'branch', 'status', 'address', 'ids']
    >>> _stem("\u0645\u0634\u062a\u0631\u06cc\u0647\u0627") == "\u0645\u0634\u062a\u0631\u06cc"
    True
    """
    if token.isascii():
        if len(token) > 4 and token.endswith("ies"):
            return token[:-3] + "y"
        if len(token) > 4 and token.endswith(("sses", "xes", "ches", "shes")):
            return token[:-2]
        if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
            return token[:-1]
        return token
    for suffix in ("\u0647\u0627\u06cc", "\u0647\u0627"):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _name_words(identifier: str) -> list[str]:
    """Words of a column name: ``NetWeight`` / ``net_weight`` -> ``['net', 'weight']``.

    Examples
    --------
    >>> _name_words("NetWeight"), _name_words("order_date_ID"), _name_words("TotalAmount")
    (['net', 'weight'], ['order', 'date', 'id'], ['total', 'amount'])
    """
    return [w.lower() for w in re.split(r"[\W_]+", _CAMEL_RE.sub("_", identifier)) if w]


def _is_key_column(words: list[str]) -> bool:
    """True for ``ID`` and for a column whose last word is ``id`` or ``key``.

    A foreign key says which table it points at, not what the table is about;
    that is the join graph's business, not a lexical signal.
    """
    return bool(words) and words[-1] in ("id", "key")


@lru_cache(maxsize=1)
def _column_index() -> tuple[dict[str, tuple[frozenset[str], frozenset[str]]], dict[str, float]]:
    """Per table, the stemmed terms of its columns, and each term's weight.

    Returns
    -------
    tuple
        ``(terms, weight)``: ``terms[table] = (name_terms, description_terms)``
        where *name_terms* are the stemmed words and adjacent word pairs of its
        non-key column names and *description_terms* those of the column
        descriptions that are not already name terms; ``weight[term]`` is
        ``ln((N + 1) / (df + 1))`` over the tables.
    """
    terms: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    doc_freq: dict[str, int] = {}
    for table, columns in TABLE_COLUMNS.items():
        name_terms: set[str] = set()
        desc_terms: set[str] = set()
        for column, description in columns.items():
            words = _name_words(column)
            if _is_key_column(words):
                continue
            stems = [_stem(w) for w in words]
            name_terms.update(stems)
            name_terms.update(_ngrams(stems, 2))
            desc_tokens = [_stem(t) for t in _tokenize(description)]
            desc_terms.update(desc_tokens)
            desc_terms.update(_ngrams(desc_tokens, 2))
        desc_terms -= name_terms
        terms[table] = (frozenset(name_terms), frozenset(desc_terms))
        for term in name_terms | desc_terms:
            doc_freq[term] = doc_freq.get(term, 0) + 1
    n_tables = len(terms)
    weight = {t: math.log((n_tables + 1) / (df + 1)) for t, df in doc_freq.items()}
    return terms, weight


def _column_scores(q_tokens: list[str]) -> dict[str, float]:
    """Evidence from column names: ``{table: score}`` for tables that have any.

    A question word (or adjacent pair of words) that is also a word of one of a
    table's column names -- ``revenue``, ``net weight`` -- adds to that table's
    score in proportion to how rare the word is among all tables' columns, so
    ``name`` or ``code`` (in most tables) add nothing and ``tonnage`` adds a lot.
    Matching is on the stemmed, normalised words, so ``weights`` finds
    ``Weight`` and a Persian column description is found whether or not the
    question spells the half-space.

    Parameters
    ----------
    q_tokens:
        The question's tokens (see :func:`_tokenize`).

    Returns
    -------
    dict[str, float]
        Only tables with a positive score.
    """
    terms, weight = _column_index()
    stems = [_stem(t) for t in q_tokens]
    wanted = set(stems) | set(_ngrams(stems, 2))
    wanted = {t for t in wanted if weight.get(t, 0.0) >= _COLUMN_MIN_WEIGHT}
    if not wanted:
        return {}
    scores: dict[str, float] = {}
    for table, (name_terms, desc_terms) in terms.items():
        score = sum(weight[t] for t in wanted & name_terms)
        score += _COLUMN_DESCRIPTION_SHARE * sum(weight[t] for t in wanted & desc_terms)
        if score > 0.0:
            scores[table] = _COLUMN_SCALE * score
    return scores


def column_evidence(question: str) -> dict[str, float]:
    """Column-name evidence for *question*: ``{table: score}`` (see :func:`_column_scores`).

    Examples
    --------
    >>> column_evidence("xyzzy foobar nonexistent_word_12345")
    {}
    """
    return _column_scores(_tokenize(_expand(question)))


def forced_tables(question: str) -> list[str]:
    """Tables ``retrieval_hints.yaml``'s ``always_include`` forces for *question*.

    Parameters
    ----------
    question:
        The natural-language question.

    Returns
    -------
    list[str]
        Table names, sorted.

    Examples
    --------
    >>> forced_tables("xyzzy foobar nonexistent_word_12345")
    []
    """
    return sorted(_forced_tables(_tokenize(_expand(question))))


def _forced_tables(q_tokens: list[str]) -> set[str]:
    q_token_set = set(q_tokens)
    q_joined = " ".join(q_tokens)
    forced: set[str] = set()
    for table_name, signals in _ALWAYS_INCLUDE_NORMALISED.items():
        for sig in signals:
            if not sig:
                continue
            if all(t in q_token_set for t in sig.split()) or sig in q_joined:
                forced.add(table_name)
                break
    return forced


def rank_tables(question: str) -> list[tuple[str, float]]:
    """Every table with evidence for *question*, best first.

    The score of a table is its TF-IDF match against its description plus the
    column-name evidence of :func:`_column_scores`; a table that
    ``always_include`` forces scores ``_FORCED_SCORE``. Ties break on the table
    name, so the order is the same on every run.

    Parameters
    ----------
    question:
        The natural-language question.

    Returns
    -------
    list[tuple[str, float]]
        ``(table, score)``, highest first; empty when nothing matched.
    """
    expanded = _expand(question)
    q_tokens = _tokenize(expanded)
    q_bigrams = _ngrams(q_tokens, 2)
    idf = _build_idf()
    forced = _forced_tables(q_tokens)

    scores: dict[str, float] = {table: _FORCED_SCORE for table in forced}

    columns = _column_scores(q_tokens)
    for table_name, info in TABLES.items():
        desc = info if isinstance(info, str) else info.get("description", "")
        s = _score_table(q_tokens, q_bigrams, idf, desc) + columns.get(table_name, 0.0)
        if s >= _MIN_SCORE:
            scores[table_name] = max(scores.get(table_name, 0.0), s)

    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def retrieve_tables(question: str, fallback: bool = True) -> list[str]:
    """Return up to _TOP_N table names most relevant to *question*.

    With ``fallback`` and no evidence at all, every table is returned.
    """
    ranked = rank_tables(question)
    if not ranked:
        return list(TABLES.keys()) if fallback else []
    return [name for name, _ in ranked[:_TOP_N]]
