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
    from retrieval.join_paths import build_join_graph, connected_within
    from retrieval.table_evidence import alias_hits
    from schema_data.retriever import _FORCED_SCORE, column_evidence, rank_tables

    settings = cfg.settings
    candidates = list(dict.fromkeys([*entities, *facts]))
    if not settings.retrieval_prune or len(candidates) < 2:
        return list(entities), list(facts)

    ranked = dict(rank_tables(question))
    named = set(alias_hits(question, {n: i["aliases"] for n, i in ENTITIES.items()}))
    named |= set(alias_hits(question, FACT_PATTERNS))
    columns = column_evidence(question)

    def tier(table: str) -> str:
        score = ranked.get(table)
        if table in named or score is None or score >= _FORCED_SCORE:
            return "strong"
        return "column" if columns.get(table, 0.0) > 0.0 else "lexical"

    scores = {t: ranked[t] for t in candidates if t in ranked and ranked[t] < _FORCED_SCORE}
    if not scores:
        return list(entities), list(facts)
    top = max(scores.values())
    best = min(scores, key=lambda t: (-scores[t], t))

    tiers = {t: tier(t) for t in candidates}
    anchors = {
        t for t in candidates
        if tiers[t] == "strong"
        or (tiers[t] == "column" and scores[t] >= settings.retrieval_prune_score_ratio * top)
    } or {best}

    graph = build_join_graph()
    hops = settings.retrieval_prune_connect_hops
    hub = settings.retrieval_join_max_hub_degree
    kept: set[str] = set(anchors)
    pending = [t for t in candidates if t not in kept]
    # A table joined to a kept table (an anchor, or one rescued a round
    # earlier) is kept: weak evidence is enough where the join graph agrees,
    # and a classification hangs from the dimension that hangs from the fact.
    def rescue() -> None:
        nonlocal pending
        while pending:
            rescued = [
                t for t in pending
                if connected_within(graph, t, kept, hops, max_hub_degree=hub)
            ]
            if not rescued:
                return
            kept.update(rescued)
            pending = [t for t in pending if t not in kept]

    rescue()
    if settings.retrieval_prune_corroborate:
        # Two candidates that no anchor supports but that are joined to each
        # other were each matched on their own and agree with the join graph.
        pairs = [
            t for t in pending
            if connected_within(graph, t, set(pending) - {t}, hops, max_hub_degree=hub)
        ]
        kept.update(pairs)
        pending = [t for t in pending if t not in kept]
        rescue()
    return [t for t in entities if t in kept], [t for t in facts if t in kept]
