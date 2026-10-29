# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Shared evidence rules of the entity and fact retrievers.

Both retrievers work in two tiers: a configured alias (``entities.yaml``) or
fact pattern (``retrieval_hints.yaml``) names a table outright, and the
TF-IDF ranking of :mod:`schema_data.retriever` finds tables nobody wrote an
alias for. Until now a hit in the first tier ended the search, so a question
that named one aliased table and one without an alias lost the second.
:func:`scored_extras` keeps the ranking in play beside the aliases, and
:func:`alias_hits` matches the aliases the way every other comparison in this
code base does (after :func:`core.persian.normalize_for_matching`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache

from core.persian import normalize_for_matching, strip_plural_suffixes

__all__ = ["alias_hits", "named_tables", "scored_extras"]


@lru_cache(maxsize=8192)
def _alias_form(form: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """An alias folded for matching, its stemmed words, and those with attached plurals stripped.

    All empty for a blank alias. The third part equals the second for an alias with
    no word that carries an attached plural.
    """
    from schema_data.retriever import _stem, _tokenize

    tokens = _tokenize(form)
    return (
        normalize_for_matching(form),
        tuple(_stem(t) for t in tokens),
        tuple(_stem(t) for t in strip_plural_suffixes(tokens)),
    )


def alias_hits(question: str, aliases: Mapping[str, Iterable[str]]) -> list[str]:
    """Tables with an alias that occurs in *question*, in configuration order.

    An alias matches as a substring of the question, after both are folded
    with :func:`core.persian.normalize_for_matching` (Arabic ``ي``/``ك``,
    digits, ZWNJ, case), so ``تأمین‌کننده`` typed without its half-space still
    hits. A substring match is kept on purpose: it is what lets the alias
    ``مشتری`` find ``مشتریان``. Beside it, whole words are compared after
    plural stemming (:func:`schema_data.retriever._stem`, and
    :func:`core.persian.strip_plural_suffix` for the Persian endings it does not
    know), which finds the inflections a substring cannot (``city`` in ``cities``). An alias of
    several words also matches when all of them occur in the question in any
    order (``gear tooth pitch`` in "pitch of each tooth on the gear"): a question seldom repeats a configured phrase word for word.

    Parameters
    ----------
    question:
        The natural-language question.
    aliases:
        ``{table: surface forms}``.

    Returns
    -------
    list[str]
        Each matching table once, in the order *aliases* lists them.

    Examples
    --------
    >>> alias_hits("Total sales per Customer?", {"Customer": ["customer"], "Ring": ["ring"]})
    ['Customer']
    >>> alias_hits("كارگزار", {"Broker": ["کارگزار"]})
    ['Broker']
    >>> alias_hits("x", {"A": [""], "B": ["x"]})
    ['B']

    An inflected form of the alias word hits too (the substring test alone
    misses ``-ies`` for ``-y``, and a plural alias for a singular question):

    >>> alias_hits("How many cities?", {"Place": ["city"]})
    ['Place']
    >>> alias_hits("each supplier", {"Supplier": ["suppliers"]})
    ['Supplier']

    The words of a multi-word alias may be apart, and in another order:

    >>> alias_hits("pitch of each tooth on the gear", {"Part": ["gear tooth pitch"]})
    ['Part']
    >>> alias_hits("gear pitch only", {"Part": ["gear tooth pitch"]})
    []

    A Persian word written with an attached plural ending (``ها``, ``های``,
    ``هایی``, with a possessive) also counts as its stem, so an alias of several
    words is found whichever way the plural is written:

    >>> alias_hits("تالارهایی کالا",
    ...            {"Hall": ["تالار کالا"]})
    ['Hall']
    """
    text = normalize_for_matching(question)
    from schema_data.retriever import _stem, _tokenize

    tokens = _tokenize(question)
    q_stems = [_stem(t) for t in tokens]
    # a word with an attached plural also counts as its stem: the question and
    # the alias are compared a second time with such words reduced
    q_plural = [_stem(t) for t in strip_plural_suffixes(tokens)]
    plain = (f" {' '.join(q_stems)} ", set(q_stems))
    plural = (f" {' '.join(q_plural)} ", set(q_plural)) if q_plural != q_stems else None
    hits: list[str] = []
    for table, forms in aliases.items():
        for form in forms:
            folded, form_stems, form_plural = _alias_form(form)
            if not folded:
                continue
            if (
                folded in text
                or _words_match(form_stems, *plain)
                or (form_plural != form_stems and _words_match(form_plural, *plain))
                or (plural is not None and _words_match(form_plural, *plural))
            ):
                hits.append(table)
                break
    return hits


def _words_match(form_stems: tuple[str, ...], words: str, bag: set[str]) -> bool:
    """Whether the words of an alias are the question's: next to each other, or (several) in any order.

    *words* is the question's stemmed words joined by single spaces and padded with one
    at each end, *bag* the same words as a set.
    """
    stems = " ".join(form_stems)
    return bool((stems and f" {stems} " in words) or (len(form_stems) > 1 and bag.issuperset(form_stems)))


def named_tables(question: str, aliases: Mapping[str, Iterable[str]]) -> list[str]:
    """Like :func:`alias_hits`, but as ``schema.yaml`` keys.

    The configuration may name a table by a bare name where ``schema.yaml``
    keys it ``schema.Name``; the tables returned here are the keys, so they
    compare equal to what the ranking, the join graph and the prompt use
    (:func:`retrieval.table_keys.resolve_table_ref`). A name that resolves to
    no table is returned as written.

    Examples
    --------
    >>> named_tables("Total sales per Customer?", {"Customer": ["customer"], "Ring": ["ring"]})
    ['Customer']
    """
    from retrieval.table_keys import resolve_table_ref

    keys: list[str] = []
    for table in alias_hits(question, aliases):
        for key in resolve_table_ref(table, question) or (table,):
            if key not in keys:
                keys.append(key)
    return keys


def scored_extras(
    question: str,
    *,
    fact: bool,
    exclude: Sequence[str] = (),
) -> list[str]:
    """Tables the ranking finds that a configured alias did not name.

    Takes the tables of one kind (fact tables, or the rest) from
    :func:`schema_data.retriever.rank_tables`; those ``always_include`` forces
    come first, then up to ``retrieval_extra_tables`` more whose score is at
    least ``retrieval_extra_score_ratio`` of the best unforced score of that
    kind. Tables in *exclude* are left out.

    Parameters
    ----------
    question:
        The natural-language question.
    fact:
        ``True`` for fact tables, ``False`` for dimensions.
    exclude:
        Tables already selected.

    Returns
    -------
    list[str]
        Table names, best first; empty when ``retrieval_extra_tables`` is 0.
    """
    import config as cfg
    from retrieval.table_keys import configured_fact_tables
    from schema_data.retriever import _FORCED_SCORE, rank_tables
    from schema_data.tables import TABLE_DESCRIPTIONS

    limit = cfg.settings.retrieval_extra_tables
    if limit <= 0:
        return []
    ratio = cfg.settings.retrieval_extra_score_ratio
    taken = set(exclude)
    fact_tables = configured_fact_tables()
    pool = [
        (name, score) for name, score in rank_tables(question)
        if name in TABLE_DESCRIPTIONS and (name in fact_tables) == fact
    ]
    forced = [name for name, score in pool if score >= _FORCED_SCORE and name not in taken]
    unforced = [(name, score) for name, score in pool if score < _FORCED_SCORE]
    if not unforced:
        return forced
    threshold = ratio * unforced[0][1]
    extras = [name for name, score in unforced if score >= threshold and name not in taken]
    return forced + extras[:limit]
