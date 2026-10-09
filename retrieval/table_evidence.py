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

from core.persian import normalize_for_matching

__all__ = ["alias_hits", "scored_extras"]


def alias_hits(question: str, aliases: Mapping[str, Iterable[str]]) -> list[str]:
    """Tables with an alias that occurs in *question*, in configuration order.

    An alias matches as a substring of the question, after both are folded
    with :func:`core.persian.normalize_for_matching` (Arabic ``ي``/``ك``,
    digits, ZWNJ, case), so ``تأمین‌کننده`` typed without its half-space still
    hits. A substring match is kept on purpose: it is what lets the alias
    ``مشتری`` find ``مشتریان``.

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
    """
    text = normalize_for_matching(question)
    hits: list[str] = []
    for table, forms in aliases.items():
        for form in forms:
            folded = normalize_for_matching(form)
            if folded and folded in text:
                hits.append(table)
                break
    return hits


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
    from knowledge.retrieval_hints import FACT_TABLES
    from schema_data.retriever import _FORCED_SCORE, rank_tables
    from schema_data.tables import TABLE_DESCRIPTIONS

    limit = cfg.settings.retrieval_extra_tables
    if limit <= 0:
        return []
    ratio = cfg.settings.retrieval_extra_score_ratio
    taken = set(exclude)
    pool = [
        (name, score) for name, score in rank_tables(question)
        if name in TABLE_DESCRIPTIONS and (name in FACT_TABLES) == fact
    ]
    forced = [name for name, score in pool if score >= _FORCED_SCORE and name not in taken]
    unforced = [(name, score) for name, score in pool if score < _FORCED_SCORE]
    if not unforced:
        return forced
    threshold = ratio * unforced[0][1]
    extras = [name for name, score in unforced if score >= threshold and name not in taken]
    return forced + extras[:limit]
