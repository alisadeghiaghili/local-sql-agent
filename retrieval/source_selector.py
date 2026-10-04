# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pick the one data source a question is about, before the prompt is built.

A statement can only ever run on one data source (a cross-source join is
refused), so a deployment with several sources does not have to show the
model every table of every source. :func:`select_source` chooses the source
first, from signals that need no model call, and the prompt is then built
from that source's tables alone (see :mod:`llm.source_routing` and
``docs/design/DATASOURCES.md``, "Choosing a source per question").

The signals, strongest first; the first that picks a winner decides:

1. **Keywords.** ``keywords:`` in ``datasources.yaml`` lists words or
   phrases that mark a question as being about a source. A phrase matches
   when it appears in the question as whole words, after the folding
   :func:`core.persian.normalize_for_matching` applies to every other
   comparison in this code base (Arabic ``ي``/``ك`` to ``ی``/``ک``,
   digits to ASCII, ZWNJ removed, case folded). ``stock`` does not match
   inside ``stockholder``. A source with more distinct matching phrases
   than every other source wins (reason ``keyword``).
2. **Session continuity.** A follow-up turn in a conversation stays on the
   previous turn's source unless the keywords point elsewhere (reason
   ``session``).
3. **Retrieval evidence.** The tables the retrieval layer selected for the
   question, and the tables whose warehouse values matched it, score for
   the sources they live in. A table that lives in several sources
   (``datasource: [A, B]``) does not tell them apart, so its point is
   shared between them. The highest score wins (reason ``retrieval``).
4. **Default.** Nothing decided: the default source (reason ``default``).

Besides the winner the result lists every source in fallback order, so the
caller can retry the next one if the model declines (``OUT_OF_SCOPE``).

Everything here is deterministic. :func:`select_source` is pure: the
configuration (sources, keywords, table-to-source sets) is passed in, which
is how the unit tests drive it. :func:`select_source_for_question` reads
that configuration from :mod:`database.datasources` and is what the engines
call; it returns ``None`` when only one source is configured, in which case
nothing in the prompt path changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping, Sequence

from core.persian import normalize_for_matching

__all__ = [
    "REASON_DEFAULT",
    "REASON_KEYWORD",
    "REASON_RETRIEVAL",
    "REASON_SESSION",
    "SourceSelection",
    "keyword_hits",
    "retrieval_scores",
    "select_source",
    "select_source_for_question",
]

#: A distinct keyword of exactly one source (or of more of them than of any
#: other source) matched the question.
REASON_KEYWORD = "keyword"

#: The previous turn's source was kept.
REASON_SESSION = "session"

#: The tables retrieval selected, or whose values matched, point at it.
REASON_RETRIEVAL = "retrieval"

#: No signal decided; the default source (or the first of a tie, in
#: ``datasources.yaml`` order).
REASON_DEFAULT = "default"


@dataclass(frozen=True)
class SourceSelection:
    """The outcome of :func:`select_source`.

    Attributes
    ----------
    chosen:
        The selected source.
    reason:
        Why: ``"keyword"``, ``"session"``, ``"retrieval"`` or ``"default"``.
    candidates:
        Every configured source in fallback order, *chosen* first. The
        others follow by strength of evidence (keyword matches, then
        retrieval score), ties in ``datasources.yaml`` order.

    Examples
    --------
    >>> sel = SourceSelection("sales", "keyword", ("sales", "inventory", "archive"))
    >>> sel.after("sales")
    'inventory'
    >>> sel.after("archive") is None
    True
    >>> sel.audit()["candidates"]
    ['sales', 'inventory', 'archive']
    >>> sel.audit(chosen="inventory", fallback_from="sales")["fallback_from"]
    'sales'
    """

    chosen: str
    reason: str
    candidates: tuple[str, ...]

    def after(self, source: str) -> str | None:
        """The candidate that follows *source*, or ``None`` after the last one."""
        try:
            position = self.candidates.index(source)
        except ValueError:
            return None
        following = self.candidates[position + 1 :]
        return following[0] if following else None

    def audit(
        self, *, chosen: str | None = None, fallback_from: str | None = None,
    ) -> dict[str, Any]:
        """The ``datasource_selection`` block an audit record carries.

        Parameters
        ----------
        chosen:
            The source the answer was finally generated for, when it is not
            :attr:`chosen` (after a retry). Defaults to :attr:`chosen`.
        fallback_from:
            The source that declined, when a retry happened.

        Returns
        -------
        dict
            ``{"chosen", "reason", "candidates", "fallback_from"}``, plain
            JSON types only.
        """
        return {
            "chosen": self.chosen if chosen is None else chosen,
            "reason": self.reason,
            "candidates": list(self.candidates),
            "fallback_from": fallback_from,
        }


@lru_cache(maxsize=256)
def _phrase_pattern(phrase: str) -> re.Pattern[str] | None:
    """Compile *phrase* into a whole-word pattern over normalised text.

    ``None`` for a phrase that is empty once normalised. Word characters
    are letters and digits of any script (Persian included) and ``_``, so
    a phrase must not be directly preceded or followed by one.
    """
    folded = normalize_for_matching(phrase)
    if not folded:
        return None
    return re.compile(rf"(?<!\w){re.escape(folded)}(?!\w)")


def keyword_hits(
    question: str, keywords: Mapping[str, Sequence[str]],
) -> dict[str, int]:
    """Count, per source, how many of its keywords occur in *question*.

    Parameters
    ----------
    question:
        The natural-language question; normalised here.
    keywords:
        ``{source: phrases}``. A phrase counts once however often it
        occurs; two phrases that both occur count twice.

    Returns
    -------
    dict[str, int]
        An entry for every source in *keywords* (``0`` for no match).

    Examples
    --------
    >>> keyword_hits("What is the stock level?", {"inventory": ["stock level", "bin"], "sales": ["revenue"]})
    {'inventory': 1, 'sales': 0}

    A phrase must match whole words, not part of a longer word:

    >>> keyword_hits("stockholders", {"inventory": ["stock"]})
    {'inventory': 0}

    Spelling variants fold to the same text (Arabic ي/ك, ZWNJ, digits):

    >>> keyword_hits("کارگزار", {"sales": ["كارگزار"]})
    {'sales': 1}
    >>> keyword_hits("می‌خواهم", {"sales": ["میخواهم"]})
    {'sales': 1}
    """
    text = normalize_for_matching(question)
    hits: dict[str, int] = {}
    for source, phrases in keywords.items():
        count = 0
        for phrase in phrases:
            pattern = _phrase_pattern(phrase)
            if pattern is not None and pattern.search(text):
                count += 1
        hits[source] = count
    return hits


def _table_of(identifier: str, known: Mapping[str, Any]) -> str | None:
    """The table a ``resolved_values`` key names, or ``None`` if unknown.

    Keys are ``Table`` or ``Table.Column``; a table key may itself be
    qualified (``sales.Customer``), so the whole key is tried first and
    then the key without its last dotted part.
    """
    if identifier in known:
        return identifier
    head, _, _tail = identifier.rpartition(".")
    return head if head in known else None


def retrieval_scores(
    context: object, table_sets: Mapping[str, Sequence[str]], sources: Sequence[str],
) -> dict[str, float]:
    """Score each source by the tables retrieval found for the question.

    A table the retriever selected, and a table whose warehouse values
    matched the question (``context.resolved_values``), each give one
    point, split evenly over the sources the table lives in: a table in one
    source is a full point for it, a table in two sources half a point for
    each, because a shared table is present everywhere and so tells the
    sources apart less. A table that ``table_sets`` does not list is
    ignored.

    Parameters
    ----------
    context:
        A :class:`~core.models.RetrievalContext`, or anything with
        ``selected_tables`` and ``resolved_values`` attributes (either may
        be missing).
    table_sets:
        ``{table: (source, ...)}``, as
        :func:`database.datasources.table_datasource_sets` returns it.
    sources:
        The sources to score; each gets an entry.

    Returns
    -------
    dict[str, float]

    Examples
    --------
    >>> from core.models import RetrievalContext
    >>> sets = {"sales_fact": ("sales",), "stock_dim": ("inventory",), "shared_dim": ("sales", "inventory")}
    >>> ctx = RetrievalContext(entities=["shared_dim"], facts=["sales_fact"],
    ...                        resolved_values={"stock_dim.Name": ["x"]})
    >>> retrieval_scores(ctx, sets, ["sales", "inventory"])
    {'sales': 1.5, 'inventory': 1.5}
    """
    scores = {source: 0.0 for source in sources}

    def credit(table: str | None) -> None:
        if table is None:
            return
        owners = [s for s in table_sets[table] if s in scores]
        for owner in owners:
            scores[owner] += 1 / len(table_sets[table])

    for table in getattr(context, "selected_tables", None) or ():
        credit(table if table in table_sets else None)
    for identifier in getattr(context, "resolved_values", None) or {}:
        credit(_table_of(identifier, table_sets))
    return scores


def select_source(
    question: str,
    context: object,
    *,
    sources: Sequence[str],
    keywords: Mapping[str, Sequence[str]],
    table_sets: Mapping[str, Sequence[str]],
    previous_source: str | None = None,
) -> SourceSelection:
    """Choose the data source *question* is about.

    See the module docstring for the signals and their order.

    Parameters
    ----------
    question:
        The natural-language question.
    context:
        The retrieval context computed over the whole schema (see
        :func:`retrieval_scores`).
    sources:
        Every configured source, default first, then in
        ``datasources.yaml`` order. Must not be empty; the first entry is
        the default source.
    keywords:
        ``{source: phrases}``; a source may be missing or have no phrases.
    table_sets:
        ``{table: (source, ...)}``.
    previous_source:
        The source the previous turn of this conversation was answered
        from, for a follow-up turn; ``None`` otherwise (and when it is not
        one of *sources* any more).

    Returns
    -------
    SourceSelection

    Raises
    ------
    ValueError
        If *sources* is empty.

    Notes
    -----
    A tie between sources with the same, highest number of keyword matches
    is not a decision: the previous source wins it when it is one of the
    tied sources, otherwise retrieval evidence decides among them.

    Examples
    --------
    >>> from core.models import RetrievalContext
    >>> sets = {"sales_fact": ("sales",), "stock_dim": ("inventory",)}
    >>> kw = {"inventory": ["stock level"]}
    >>> srcs = ["sales", "inventory"]
    >>> sel = select_source("stock level today", RetrievalContext(facts=["sales_fact"]),
    ...                     sources=srcs, keywords=kw, table_sets=sets)
    >>> (sel.chosen, sel.reason, sel.candidates)
    ('inventory', 'keyword', ('inventory', 'sales'))

    Without keywords the retrieved tables decide:

    >>> sel = select_source("who bought", RetrievalContext(entities=["stock_dim"]),
    ...                     sources=srcs, keywords={}, table_sets=sets)
    >>> (sel.chosen, sel.reason)
    ('inventory', 'retrieval')

    And with no evidence at all, the default source:

    >>> sel = select_source("hello", RetrievalContext(), sources=srcs, keywords={}, table_sets=sets)
    >>> (sel.chosen, sel.reason)
    ('sales', 'default')
    """
    if not sources:
        raise ValueError("no data source to choose from")
    order = {source: index for index, source in enumerate(sources)}
    if previous_source not in order:
        previous_source = None

    hits = keyword_hits(question, {s: keywords.get(s, ()) for s in sources})
    scores = retrieval_scores(context, table_sets, sources)

    def ranked(pool: Sequence[str]) -> list[str]:
        return sorted(pool, key=lambda s: (-hits[s], -scores[s], order[s]))

    def finish(chosen: str, reason: str) -> SourceSelection:
        rest = [s for s in ranked(sources) if s != chosen]
        return SourceSelection(chosen, reason, (chosen, *rest))

    pool = list(sources)

    # 1. Keywords: the source(s) with the most distinct matches.
    best_hits = max(hits.values())
    if best_hits > 0:
        pool = [s for s in sources if hits[s] == best_hits]
        if len(pool) == 1:
            return finish(pool[0], REASON_KEYWORD)

    # 2. Session continuity: no keyword evidence against the previous
    # source (the pool is still every source), or it is among the tied
    # leaders.
    if previous_source is not None and previous_source in pool:
        return finish(previous_source, REASON_SESSION)

    # 3. Retrieval evidence, among whatever the keywords left standing.
    best_score = max(scores[s] for s in pool)
    leaders = [s for s in pool if scores[s] == best_score]
    if best_score > 0 and len(leaders) == 1:
        return finish(leaders[0], REASON_RETRIEVAL)

    # 4. Nothing decided: first of the leaders in configuration order.
    return finish(leaders[0], REASON_DEFAULT)


def select_source_for_question(
    question: str,
    context: object,
    *,
    previous_source: str | None = None,
) -> SourceSelection | None:
    """:func:`select_source` with the deployment's own configuration.

    Parameters
    ----------
    question, context, previous_source:
        As for :func:`select_source`.

    Returns
    -------
    SourceSelection | None
        ``None`` when fewer than two data sources are configured: there is
        nothing to choose, and the prompt must be exactly what it always
        was.

    Examples
    --------
    >>> import config as cfg
    >>> from core.models import RetrievalContext
    >>> with cfg.override_settings(project_config_dir="/nonexistent"):
    ...     select_source_for_question("anything", RetrievalContext()) is None
    True
    """
    # Module attribute lookups, so the configuration is read at call time.
    import database.datasources as datasources

    sources = datasources.datasource_names()
    if len(sources) < 2:
        return None
    return select_source(
        question,
        context,
        sources=sources,
        keywords=datasources.datasource_keywords(),
        table_sets=datasources.table_datasource_sets(),
        previous_source=previous_source,
    )
