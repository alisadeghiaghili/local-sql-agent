# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Draft the ``synonyms`` of ``aliases.yaml`` from the schema's own vocabulary.

``synonyms`` maps a word an analyst types to words the table retriever can see
(``schema_data.retriever._expand`` appends the mapped words to the question
before it scores the tables). The retriever sees the words of the table
descriptions and of the non-key column names, so a synonym is only worth
anything when

* its key is **one word** as the retriever tokenises a question (a key of two
  words is never looked up), has a run of two letters and is not shaped like a
  value read from a row (a date, a number, an address: :mod:`core.value_shapes`);
* its key is **not already one of those words** (it would match by itself);
* every word of each value **is** one of them (anything else adds nothing).

This module builds that vocabulary from table and column *names* (no row value
is read or sent anywhere), turns a model's answer into entries that obey the
three rules (:func:`clean_synonyms`) and derives entries from the single-word
aliases the wizard already has for each table (:func:`synonyms_from_entities`).
Nothing here touches a database, a model or a file.

Public API
----------
schema_vocabulary(tables) -> Vocabulary
clean_synonyms(answer, vocabulary, max_values, max_entries) -> CleanedSynonyms
synonyms_from_entities(entities, vocabulary) -> dict[str, list[str]]
synonym_key(word) -> str
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.persian import normalize_for_matching
from core.value_shapes import data_shape, identifier_words

__all__ = [
    "CleanedSynonyms",
    "MAX_ENTRIES_PER_ANSWER",
    "MAX_VALUES_PER_KEY",
    "Vocabulary",
    "clean_synonyms",
    "schema_vocabulary",
    "synonym_key",
    "synonyms_from_entities",
]

#: At most this many words per synonym value list.
MAX_VALUES_PER_KEY = 4

#: At most this many entries are kept from one answer of the model.
MAX_ENTRIES_PER_ANSWER = 80

#: Last words of a column name that make it a key (the retriever skips such columns).
_KEY_WORDS = frozenset({"id", "key"})

_WORD_RE = re.compile(r"\w+")
_LETTERS = re.compile(r"[^\W\d_]{2,}")


def synonym_key(word: str) -> str:
    """*word* folded as the retriever folds a question word (letter forms, digits, case).

    Examples:
        >>> synonym_key("  Volume ")
        'volume'
        >>> synonym_key("معامله") == synonym_key("معامله")
        True
    """
    return normalize_for_matching(str(word)).lower().strip()


def _stem(word: str) -> str:
    """The English plural stem the retriever uses (``trades`` -> ``trade``); other words unchanged.

    Examples:
        >>> [_stem(w) for w in ("trades", "categories", "status", "معاملات")]
        ['trade', 'category', 'status', 'معاملات']
    """
    if not word.isascii():
        return word
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _is_word(text: str) -> bool:
    """Whether *text* has a run of two letters (in any script)."""
    return bool(_LETTERS.search(text))


@dataclass(frozen=True)
class Vocabulary:
    """The words the retriever can match, per table and as a whole.

    Attributes:
        terms: Every word (folded, lower case), in first-seen order.
        by_table: Each table's own words, keyed by the name given to
            :func:`schema_vocabulary`.
    """

    terms: tuple[str, ...]
    by_table: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __contains__(self, word: object) -> bool:
        if not isinstance(word, str):
            return False
        return word in self._index or _stem(word) in self._index

    @property
    def _index(self) -> frozenset[str]:
        return frozenset(self.terms) | frozenset(_stem(t) for t in self.terms)

    def canonical(self, word: str) -> str | None:
        """The vocabulary's spelling of *word* (itself or the term with the same stem), or ``None``.

        Examples:
            >>> Vocabulary(terms=("trade", "volume")).canonical("trades")
            'trade'
            >>> Vocabulary(terms=("trade",)).canonical("price") is None
            True
        """
        if word in self.terms:
            return word
        stem = _stem(word)
        return next((t for t in self.terms if t == stem or _stem(t) == stem), None)


def _name_terms(name: str) -> list[str]:
    """The words of an identifier that may be a term: two letters at least, folded."""
    return [synonym_key(w) for w in identifier_words(name) if _is_word(w)]


def schema_vocabulary(tables: Iterable[tuple[str, str, Iterable[str]]]) -> Vocabulary:
    """The words of the table names and the non-key column names.

    Args:
        tables: ``(key, table name, column names)`` for each table.

    Returns:
        The :class:`Vocabulary`. A column whose last word is ``id`` or ``key``
        gives no term, as the retriever skips it.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> v = schema_vocabulary([("Trade", "Trade", ["ID", "TradeVolume", "Price", "Ring_ID"])])
        >>> v.terms
        ('trade', 'volume', 'price')
        >>> "trades" in v, "ring" in v
        (True, False)
    """
    order: dict[str, None] = {}
    by_table: dict[str, tuple[str, ...]] = {}
    for key, name, columns in tables:
        own: dict[str, None] = dict.fromkeys(_name_terms(name))
        for column in columns:
            words = identifier_words(column)
            if words and words[-1] in _KEY_WORDS:
                continue
            own.update(dict.fromkeys(_name_terms(column)))
        by_table[key] = tuple(own)
        order.update(own)
    return Vocabulary(terms=tuple(order), by_table=by_table)


@dataclass
class CleanedSynonyms:
    """The usable entries of one answer, and how many of the rest were dropped and why.

    Attributes:
        synonyms: ``{key: [words]}``.
        dropped: Count per reason (``not_one_word``, ``not_a_word``,
            ``value_shaped``, ``already_a_term``, ``no_known_term``, ``over_limit``).
    """

    synonyms: dict[str, list[str]] = field(default_factory=dict)
    dropped: Counter = field(default_factory=Counter)


def _pairs(answer: Any) -> Iterable[tuple[Any, Any]]:
    """``(word, terms)`` from the shapes a model gives: a mapping, a wrapped mapping or a list of objects."""
    if isinstance(answer, Mapping) and isinstance(answer.get("synonyms"), (Mapping, list)):
        answer = answer["synonyms"]
    if isinstance(answer, Mapping):
        return list(answer.items())
    if isinstance(answer, list):
        pairs = []
        for item in answer:
            if isinstance(item, Mapping):
                word = item.get("word", item.get("synonym", item.get("key")))
                terms = item.get("terms", item.get("values", item.get("maps_to")))
                pairs.append((word, terms))
        return pairs
    return []


def _value_words(value: Any, vocabulary: Vocabulary) -> str | None:
    """*value* as the vocabulary spells it when every word of it is a term, else ``None``."""
    if not isinstance(value, str):
        return None
    words = _WORD_RE.findall(synonym_key(value))
    if not words:
        return None
    spelled = [vocabulary.canonical(w) for w in words]
    if any(s is None for s in spelled):
        return None
    return " ".join(spelled)  # type: ignore[arg-type]


def clean_synonyms(
    answer: Any,
    vocabulary: Vocabulary,
    max_values: int = MAX_VALUES_PER_KEY,
    max_entries: int = MAX_ENTRIES_PER_ANSWER,
) -> CleanedSynonyms:
    """The entries of a model's *answer* that the retriever can use.

    Args:
        answer: The parsed answer: ``{"synonyms": {word: [terms]}}``, a bare
            mapping, or a list of ``{"word": ..., "terms": [...]}``. A single
            string in place of a list counts as a list of one.
        vocabulary: The words the terms must come from.
        max_values: At most this many words per key.
        max_entries: At most this many keys.

    Returns:
        :class:`CleanedSynonyms`. Keys are folded as a question word is, values
        are written as the vocabulary spells them, both without repeats. A key
        that comes twice keeps the union of its values.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> v = schema_vocabulary([("Trade", "Trade", ["Volume", "Price"])])
        >>> got = clean_synonyms({"synonyms": {"حجم": ["volume"], "Price": ["price"], "نرخ قیمت": ["price"],
        ...                                     "مبلغ": ["amount"], "1402": ["trade"], "بها": "prices"}}, v)
        >>> got.synonyms
        {'حجم': ['volume'], 'بها': ['price']}
        >>> sorted(got.dropped.items())
        [('already_a_term', 1), ('no_known_term', 1), ('not_a_word', 1), ('not_one_word', 1)]
    """
    result = CleanedSynonyms()
    for word, terms in _pairs(answer):
        if not isinstance(word, str):
            result.dropped["not_a_word"] += 1
            continue
        key = synonym_key(word)
        if len(_WORD_RE.findall(key)) != 1 or _WORD_RE.fullmatch(key) is None:
            result.dropped["not_one_word"] += 1
            continue
        if not _is_word(key):
            result.dropped["not_a_word"] += 1
            continue
        if data_shape(key):
            result.dropped["value_shaped"] += 1
            continue
        if key in vocabulary:
            result.dropped["already_a_term"] += 1
            continue
        values = [terms] if isinstance(terms, str) else terms if isinstance(terms, list) else []
        spelled = list(dict.fromkeys(v for v in (_value_words(t, vocabulary) for t in values) if v))
        if not spelled:
            result.dropped["no_known_term"] += 1
            continue
        if key not in result.synonyms and len(result.synonyms) >= max_entries:
            result.dropped["over_limit"] += 1
            continue
        current = result.synonyms.setdefault(key, [])
        current.extend(s for s in spelled if s not in current)
        del current[max_values:]
    return result


def synonyms_from_entities(
    entities: Mapping[str, Mapping[str, Any]],
    vocabulary: Vocabulary,
) -> dict[str, list[str]]:
    """Entries from the single-word aliases of each table: the alias maps to the table's name words.

    An alias of one word (``معاملات`` for the table ``Trade``) becomes a key
    whose values are the words of the table's name that are terms. An alias
    of several words is left to ``entities.yaml`` (a key must be one word). A
    word that is an alias of several tables maps to the words of all of them.

    Args:
        entities: The wizard's entities: ``{key: {"table": name, "aliases": [...]}}``.
        vocabulary: The words the values must come from.

    Returns:
        ``{key: [words]}`` obeying the rules of :func:`clean_synonyms`.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> v = schema_vocabulary([("Trade", "FutureTrade", ["Volume"])])
        >>> synonyms_from_entities({"Trade": {"table": "FutureTrade", "aliases": ["معاملات", "trade", "نوع معامله"]}}, v)
        {'معاملات': ['future', 'trade']}
    """
    answer: dict[str, list[str]] = {}
    for entry in entities.values():
        if not isinstance(entry, Mapping):
            continue
        words = [w for w in _name_terms(str(entry.get("table", ""))) if w in vocabulary]
        if not words:
            continue
        for alias in entry.get("aliases") or ():
            if isinstance(alias, str):
                answer.setdefault(alias, [])
                answer[alias].extend(w for w in words if w not in answer[alias])
    return clean_synonyms(answer, vocabulary, max_values=MAX_VALUES_PER_KEY, max_entries=10**6).synonyms
