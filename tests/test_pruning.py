# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pruning of retrieved tables that neither evidence nor the join graph supports.

Ranking, aliases and the foreign-key graph are patched to small known values
so each rule has its own case: evidence tiers, the relative score cutoff, the
connectivity (and its hop limit and data-source rule), and the
guards (switch off, a single candidate, nothing ranked).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest

import config as cfg
from retrieval.join_paths import JoinGraph
from retrieval.pruning import prune_selection

_FORCED = 1e9


def _graph(edges, sources=None) -> JoinGraph:
    adjacency: dict[str, dict[str, bool]] = {}
    for a, b in edges:
        adjacency.setdefault(a, {})[b] = False
        adjacency.setdefault(b, {})[a] = False
    return JoinGraph(
        adjacency={t: tuple(sorted(n.items())) for t, n in adjacency.items()},
        sources={t: frozenset(s) for t, s in (sources or {}).items()},
    )


@contextmanager
def _world(*, ranked, aliases=(), columns=None, edges=(), sources=None):
    """Patch what prune_selection reads: ranking, aliased tables, column evidence, graph."""
    entity_table = {t: {"aliases": [t.lower()], "table": t, "label": None} for t in aliases}
    with patch("schema_data.retriever.rank_tables", return_value=sorted(ranked.items(), key=lambda kv: -kv[1])), \
            patch("schema_data.retriever.column_evidence", return_value=dict(columns or {})), \
            patch("knowledge.entities.ENTITIES", entity_table, create=True), \
            patch("knowledge.retrieval_hints.FACT_PATTERNS", {}, create=True), \
            patch("retrieval.join_paths.build_join_graph", return_value=_graph(edges, sources)):
        yield


@pytest.fixture(autouse=True)
def _round_numbers():
    """Pin the thresholds, so each case reads the same whatever the shipped defaults are."""
    with cfg.override_settings(
        retrieval_prune=True, retrieval_prune_score_ratio=0.5,
        retrieval_prune_connect_hops=2, retrieval_prune_corroborate=False,
        retrieval_join_max_hub_degree=10,
    ):
        yield


def _prune(question=None, entities=(), facts=()):
    # the question mentions every candidate by (lower-cased) name; only the
    # tables _world gave an alias are *named* by it
    text = question or " ".join(t.lower() for t in [*entities, *facts])
    return prune_selection(text, list(entities), list(facts))


class TestEvidenceTiers:
    def test_a_named_table_is_kept_even_with_no_lexical_score(self):
        with _world(ranked={"Other": 1.0}, aliases=["Named"]):
            assert _prune(entities=["Named", "Other"]) == (["Named"], [])
        with _world(ranked={"Other": 1.0}, aliases=["Named"], edges=[("Named", "Other")]):
            assert _prune(entities=["Named", "Other"]) == (["Named", "Other"], [])

    def test_forced_table_is_kept(self):
        with _world(ranked={"Date": _FORCED, "Lex": 1.0, "Lex2": 0.9}, aliases=["Anchor"]):
            kept, _ = _prune(entities=["Anchor", "Date", "Lex2"])
        assert "Date" in kept

    def test_a_table_no_signal_explains_is_left_alone(self):
        # the "every table" fallback for a question nothing matched
        with _world(ranked={}):
            assert _prune(entities=["A", "B", "C"]) == (["A", "B", "C"], [])

    def test_unranked_candidates_beside_ranked_ones_are_kept(self):
        with _world(ranked={"Lex": 1.0}, edges=[("Unknown", "Lex")]):
            assert _prune(entities=["Unknown", "Lex"])[0] == ["Unknown", "Lex"]
        with _world(ranked={"Lex": 1.0}):
            assert _prune(entities=["Unknown", "Lex"])[0] == ["Unknown"]


class TestLexicalOnly:
    def test_unconnected_lexical_table_is_dropped(self):
        with _world(ranked={"Strong": 1.0, "Lex": 0.3}, aliases=["Strong"]):
            assert _prune(entities=["Strong", "Lex"])[0] == ["Strong"]

    def test_a_near_top_unconnected_lexical_table_is_still_dropped(self):
        # a score margin was measured and found to change nothing; there is none
        with _world(ranked={"Strong": 1.0, "Lex": 0.99}, aliases=["Strong"]):
            assert _prune(entities=["Strong", "Lex"])[0] == ["Strong"]

    def test_weakly_evidenced_but_connected_table_is_kept(self):
        # the negative case: correct, barely matched, saved by the join graph
        with _world(ranked={"Strong": 1.0, "Lex": 0.05}, aliases=["Strong"], edges=[("Strong", "Lex")]):
            assert _prune(entities=["Strong", "Lex"])[0] == ["Strong", "Lex"]

    def test_connection_through_one_other_table_counts_at_two_hops_not_one(self):
        edges = [("Strong", "Bridge"), ("Bridge", "Lex")]
        with _world(ranked={"Strong": 1.0, "Lex": 0.05}, aliases=["Strong"], edges=edges):
            assert _prune(entities=["Strong", "Lex"])[0] == ["Strong", "Lex"]
            with cfg.override_settings(retrieval_prune_connect_hops=1):
                assert _prune(entities=["Strong", "Lex"])[0] == ["Strong"]

    def test_a_rescued_table_can_rescue_the_next_one(self):
        # Lex is three keys from the anchor, but two from Mid, which the anchor reaches in two
        edges = [("Strong", "A"), ("A", "Mid"), ("Mid", "B"), ("B", "Lex")]
        with _world(ranked={"Strong": 1.0, "Mid": 0.1, "Lex": 0.05}, aliases=["Strong"], edges=edges):
            assert _prune(entities=["Strong", "Mid", "Lex"])[0] == ["Strong", "Mid", "Lex"]

    def test_zero_hops_turns_the_rescue_off(self):
        with _world(ranked={"Strong": 1.0, "Lex": 0.05}, aliases=["Strong"], edges=[("Strong", "Lex")]), \
                cfg.override_settings(retrieval_prune_connect_hops=0):
            assert _prune(entities=["Strong", "Lex"])[0] == ["Strong"]

    def test_a_join_that_only_exists_in_another_data_source_does_not_count(self):
        sources = {"Strong": ["a"], "Bridge": ["b"], "Lex": ["b"]}
        edges = [("Strong", "Bridge"), ("Bridge", "Lex")]
        with _world(ranked={"Strong": 1.0, "Lex": 0.05}, aliases=["Strong"], edges=edges, sources=sources):
            assert _prune(entities=["Strong", "Lex"])[0] == ["Strong"]

    def test_a_hub_does_not_connect_two_unrelated_tables(self):
        edges = [("Strong", "Hub"), ("Lex", "Hub")] + [(f"X{i}", "Hub") for i in range(12)]
        with _world(ranked={"Strong": 1.0, "Lex": 0.05}, aliases=["Strong"], edges=edges):
            assert _prune(entities=["Strong", "Lex"])[0] == ["Strong"]


class TestCorroboration:
    """Candidates no anchor supports but that are joined to each other."""

    def _world(self):
        # Strong (a false alias hit) is unrelated to the pair Fact - Bridge - Dim
        edges = [("Fact", "Bridge"), ("Bridge", "Dim")]
        return _world(ranked={"Strong": 1.0, "Fact": 0.4, "Dim": 0.3}, aliases=["Strong"], edges=edges)

    def test_two_joined_candidates_keep_each_other(self):
        with self._world(), cfg.override_settings(retrieval_prune_corroborate=True):
            assert _prune(entities=["Strong", "Fact", "Dim"])[0] == ["Strong", "Fact", "Dim"]

    def test_switch_off_drops_them(self):
        with self._world():
            assert _prune(entities=["Strong", "Fact", "Dim"])[0] == ["Strong"]

    def test_an_unjoined_candidate_gets_no_help_from_a_corroborated_pair(self):
        ranked = {"Strong": 1.0, "Fact": 0.4, "Dim": 0.3, "Stray": 0.2}
        edges = [("Fact", "Bridge"), ("Bridge", "Dim")]
        with _world(ranked=ranked, aliases=["Strong"], edges=edges), \
                cfg.override_settings(retrieval_prune_corroborate=True):
            assert _prune(entities=["Strong", "Fact", "Dim", "Stray"])[0] == ["Strong", "Fact", "Dim"]


class TestColumnEvidence:
    def test_column_table_above_the_cutoff_is_an_anchor_and_kept(self):
        with _world(ranked={"Col": 1.0, "Col2": 0.6}, columns={"Col": 0.5, "Col2": 0.3}):
            assert _prune(entities=["Col", "Col2"])[0] == ["Col", "Col2"]

    def test_column_table_below_the_cutoff_is_dropped_when_unconnected(self):
        with _world(ranked={"Col": 1.0, "Col2": 0.2}, columns={"Col": 0.5, "Col2": 0.2}):
            assert _prune(entities=["Col", "Col2"])[0] == ["Col"]

    def test_column_table_below_the_cutoff_is_kept_when_connected(self):
        with _world(ranked={"Col": 1.0, "Col2": 0.2}, columns={"Col": 0.5, "Col2": 0.2},
                    edges=[("Col", "Col2")]):
            assert _prune(entities=["Col", "Col2"])[0] == ["Col", "Col2"]

    def test_cutoff_ratio_is_tunable(self):
        with _world(ranked={"Col": 1.0, "Col2": 0.2}, columns={"Col": 0.5, "Col2": 0.2}), \
                cfg.override_settings(retrieval_prune_score_ratio=0.1):
            assert _prune(entities=["Col", "Col2"])[0] == ["Col", "Col2"]

    def test_a_column_match_is_weaker_evidence_than_a_name_but_stronger_than_a_description(self):
        # same score and no join: the column-evidenced table survives the cutoff, the lexical one does not
        with _world(ranked={"Top": 1.0, "Col": 0.6, "Lex": 0.6}, aliases=["Top"], columns={"Col": 0.2}):
            assert _prune(entities=["Top", "Col", "Lex"])[0] == ["Top", "Col"]


class TestGuards:
    def test_best_table_is_kept_when_nothing_is_anchored(self):
        with _world(ranked={"A": 1.0, "B": 0.4, "C": 0.3}):
            assert _prune(entities=["A", "B", "C"])[0] == ["A"]

    def test_connected_tables_are_kept_around_the_best(self):
        with _world(ranked={"A": 1.0, "B": 0.4, "C": 0.3}, edges=[("A", "B")]):
            assert _prune(entities=["A", "B", "C"])[0] == ["A", "B"]

    def test_switch_off_returns_everything(self):
        with _world(ranked={"Strong": 1.0, "Lex": 0.3}, aliases=["Strong"]), \
                cfg.override_settings(retrieval_prune=False):
            assert _prune(entities=["Strong", "Lex"]) == (["Strong", "Lex"], [])

    def test_a_single_candidate_is_never_pruned(self):
        with _world(ranked={"Lex": 0.01}):
            assert _prune(entities=["Lex"]) == (["Lex"], [])

    def test_order_and_the_entity_fact_split_are_kept(self):
        edges = [("Fact", "DimA"), ("Fact", "DimB")]
        with _world(ranked={"DimB": 1.0, "Fact": 0.5, "DimA": 0.4}, aliases=["DimB"], edges=edges):
            assert _prune(entities=["DimB", "DimA"], facts=["Fact"]) == (["DimB", "DimA"], ["Fact"])

    def test_pruning_is_deterministic(self):
        with _world(ranked={"A": 1.0, "B": 1.0, "C": 1.0}):
            assert _prune(entities=["C", "B", "A"]) == _prune(entities=["C", "B", "A"])


class TestShippedDefaults:
    def test_defaults_are_the_tuned_values(self):
        fresh = cfg.Settings()
        assert fresh.retrieval_prune is True
        assert fresh.retrieval_prune_score_ratio == 0.85
        assert fresh.retrieval_prune_connect_hops == 2
        assert fresh.retrieval_prune_corroborate is True
        assert fresh.retrieval_extra_tables == 3
        assert fresh.retrieval_extra_score_ratio == 0.5
