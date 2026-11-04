# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Prune the retrieved tables the evidence does not support.

Candidate generation (:mod:`retrieval.entity_retriever`,
:mod:`retrieval.fact_retriever`) is tuned for recall: a missing table
guarantees wrong SQL, an extra one costs tokens. This stage, run on its output
and before join-path expansion, takes the extra tokens back without a model,
using three deterministic signals:

1. **Evidence tier.** Each table is ``strong`` when a configured alias or fact
   pattern named it or ``always_include`` forced it; ``column`` when a question
   word matched one of its column names; ``lexical`` when only its description
   matched. A table that no signal explains at all (the "every table" fallback
   for a question nothing matched) is treated as strong and left alone.
2. **Relative score cutoff.** A ``column`` table scoring below
   ``retrieval_prune_score_ratio`` of the best unforced score is not an anchor.
   The best table is always kept.
3. **Connectivity.** What is left that is not an anchor survives only if it is
   joined (:func:`retrieval.join_paths.connected_within`, at most
   ``retrieval_prune_connect_hops`` foreign keys, relationships plus the
   ``<Table>_ID`` convention) to an anchor or to a table already kept this way
   -- a correct but weakly evidenced table is kept because it is connected.
   With ``retrieval_prune_corroborate``, two such tables joined to each other
   keep each other: each was matched on its own and the graph agrees, which
   saves a question whose strongest alias hit is a false one.

Anchors are the strong tables and the column tables that pass the cutoff
(the best table alone when there are none). Join-path expansion then runs on
what is kept, so a bridge table is added only when it lies on a path between
two kept tables. A fourth rule, a score margin that would let an unconnected
description-only table survive, was measured and removed: it changed nothing
(docs/design/RETRIEVAL.md).
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["prune_selection"]


def _evidence_tier(
    table: str,
    ranked: dict[str, float],
    named: set[str],
    columns: dict[str, float],
    forced_score: float,
) -> str:
    """How strongly the question supports *table*: ``"strong"``, ``"column"`` or ``"lexical"``.

    Strong: named in the question, forced, or not ranked at all (nothing to
    weigh it by). Column: a column name matched. Lexical: a description match
    only.
    """
    score = ranked.get(table)
    if table in named or score is None or score >= forced_score:
        return "strong"
    return "column" if columns.get(table, 0.0) > 0.0 else "lexical"


def _supported_tables(
    graph: object,
    candidates: list[str],
    anchors: set[str],
    *,
    hops: int,
    hub: int,
    corroborate: bool,
) -> set[str]:
    """The anchors, every candidate the join graph connects to them, and corroborated pairs.

    A table joined to a kept table (an anchor, or one rescued a round
    earlier) is kept: weak evidence is enough where the join graph agrees,
    and a classification hangs from the dimension that hangs from the fact.
    With *corroborate*, two candidates that no anchor supports but that are
    joined to each other are kept too (each was matched on its own and agrees
    with the join graph), and what joins to them is rescued in turn.
    """
    from retrieval.join_paths import connected_within

    kept: set[str] = set(anchors)
    pending = _rescue(graph, kept, [t for t in candidates if t not in kept], hops, hub)
    if corroborate:
        pairs = [
            t for t in pending
            if connected_within(graph, t, set(pending) - {t}, hops, max_hub_degree=hub)
        ]
        kept.update(pairs)
        _rescue(graph, kept, [t for t in pending if t not in kept], hops, hub)
    return kept


def _rescue(graph: object, kept: set[str], pending: list[str], hops: int, hub: int) -> list[str]:
    """Move to *kept*, round by round, every pending table connected to a kept one; the rest stay pending."""
    from retrieval.join_paths import connected_within

    while pending:
        rescued = [
            t for t in pending
            if connected_within(graph, t, kept, hops, max_hub_degree=hub)
        ]
        if not rescued:
            break
        kept.update(rescued)
        pending = [t for t in pending if t not in kept]
    return pending


def prune_selection(
    question: str, entities: Sequence[str], facts: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Drop the retrieved tables that neither evidence nor the join graph supports.

    Parameters
    ----------
    question:
        The natural-language question.
    entities, facts:
        The candidates of :class:`~retrieval.entity_retriever.EntityRetriever`
        and :class:`~retrieval.fact_retriever.FactRetriever`.

    Returns
    -------
    tuple[list[str], list[str]]
        ``(entities, facts)`` without the pruned tables, order kept. Returned
        unchanged when ``retrieval_prune`` is off or nothing was ranked.

    Examples
    --------
    >>> prune_selection("xyzzy foobar nonexistent_word_12345", ["Customer"], [])
    (['Customer'], [])
    """
    import config as cfg
    from knowledge.entities import ENTITIES
    from knowledge.retrieval_hints import FACT_PATTERNS
    from retrieval.join_paths import build_join_graph
    from retrieval.table_evidence import named_tables
    from schema_data.retriever import _FORCED_SCORE, column_evidence, rank_tables

    settings = cfg.settings
    candidates = list(dict.fromkeys([*entities, *facts]))
    if not settings.retrieval_prune or len(candidates) < 2:
        return list(entities), list(facts)

    ranked = dict(rank_tables(question))
    named = set(named_tables(question, {n: i["aliases"] for n, i in ENTITIES.items()}))
    named |= set(named_tables(question, FACT_PATTERNS))
    columns = column_evidence(question)

    scores = {t: ranked[t] for t in candidates if t in ranked and ranked[t] < _FORCED_SCORE}
    if not scores:
        return list(entities), list(facts)
    top = max(scores.values())
    best = min(scores, key=lambda t: (-scores[t], t))

    tiers = {t: _evidence_tier(t, ranked, named, columns, _FORCED_SCORE) for t in candidates}
    anchors = {
        t for t in candidates
        if tiers[t] == "strong"
        or (tiers[t] == "column" and scores[t] >= settings.retrieval_prune_score_ratio * top)
    } or {best}

    kept = _supported_tables(
        build_join_graph(), candidates, anchors,
        hops=settings.retrieval_prune_connect_hops,
        hub=settings.retrieval_join_max_hub_degree,
        corroborate=settings.retrieval_prune_corroborate,
    )
    return [t for t in entities if t in kept], [t for t in facts if t in kept]
