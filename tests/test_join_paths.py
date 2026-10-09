# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Join-path expansion: the tables that connect the retrieved ones.

The path search is exercised on hand-built graphs (``build_join_graph`` is
patched) so every rule -- shortest path, tie-breaks, hub exclusion, data
source, caps, budget -- has its own small case; the example schema's real
graph covers the loading side.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import config as cfg
from retrieval import join_paths
from retrieval.join_paths import JoinGraph, _best_path, _infer_edges, build_join_graph, expand_join_paths


def _graph(edges: list[tuple[str, str]], *, inferred=(), sources=None) -> JoinGraph:
    adjacency: dict[str, dict[str, bool]] = {}
    marks = {frozenset(e) for e in inferred}
    for a, b in edges:
        flag = frozenset((a, b)) in marks
        adjacency.setdefault(a, {})[b] = flag
        adjacency.setdefault(b, {})[a] = flag
    return JoinGraph(
        adjacency={t: tuple(sorted(n.items())) for t, n in adjacency.items()},
        sources={t: frozenset(s) for t, s in (sources or {}).items()},
    )


def _expand(graph: JoinGraph, selected, **kwargs):
    with patch.object(join_paths, "build_join_graph", return_value=graph), \
            patch("knowledge.retrieval_hints.FACT_TABLES", {"Fact"}):
        return expand_join_paths(selected, fits=kwargs.pop("fits", lambda tables: True))


class TestPaths:
    def test_adds_the_table_between_two_retrieved_ones(self):
        graph = _graph([("Fact", "Bridge"), ("Bridge", "Dim")])
        assert _expand(graph, ["Fact", "Dim"]) == ["Bridge"]

    def test_adjacent_tables_need_nothing(self):
        assert _expand(_graph([("Fact", "Dim")]), ["Fact", "Dim"]) == []

    def test_a_single_or_unknown_table_needs_nothing(self):
        graph = _graph([("Fact", "Dim")])
        assert _expand(graph, ["Fact"]) == []
        assert _expand(graph, ["Fact", "Nowhere"]) == []

    def test_default_bridges_one_intermediate_and_three_hops_two(self):
        graph = _graph([("Fact", "Bridge"), ("Bridge", "Child"), ("Child", "Parent")])
        assert _expand(graph, ["Fact", "Parent"]) == []
        with cfg.override_settings(retrieval_join_max_hops=3):
            assert _expand(graph, ["Fact", "Parent"]) == ["Bridge", "Child"]

    def test_hop_limit_leaves_a_far_table_unconnected(self):
        graph = _graph([("Fact", "A"), ("A", "B"), ("B", "C"), ("C", "Far")])
        with cfg.override_settings(retrieval_join_max_hops=3):
            assert _expand(graph, ["Fact", "Far"]) == []
        with cfg.override_settings(retrieval_join_max_hops=4):
            assert _expand(graph, ["Fact", "Far"]) == ["A", "B", "C"]

    def test_shortest_path_wins_over_a_longer_one(self):
        graph = _graph([("Fact", "X"), ("X", "Dim"), ("Fact", "Y1"), ("Y1", "Y2"), ("Y2", "Dim")])
        assert _expand(graph, ["Fact", "Dim"]) == ["X"]

    def test_equal_paths_break_on_fewer_inferred_edges_then_on_names(self):
        graph = _graph([("Fact", "A"), ("A", "Dim"), ("Fact", "B"), ("B", "Dim")])
        assert _expand(graph, ["Fact", "Dim"]) == ["A"]                       # name order
        graph = _graph([("Fact", "A"), ("A", "Dim"), ("Fact", "B"), ("B", "Dim")],
                       inferred=[("Fact", "A")])
        assert _expand(graph, ["Fact", "Dim"]) == ["B"]                       # A's edge is inferred

    def test_result_does_not_depend_on_the_order_tables_were_retrieved_in(self):
        graph = _graph([("Fact", "Bridge"), ("Bridge", "D1"), ("Bridge", "D2")])
        assert _expand(graph, ["D2", "Fact", "D1"]) == _expand(graph, ["D1", "D2", "Fact"]) == ["Bridge"]

    def test_hub_is_not_a_stepping_stone(self):
        edges = [("Fact1", "Hub"), ("Fact2", "Hub")] + [(f"Other{i}", "Hub") for i in range(12)]
        graph = _graph(edges)
        assert _expand(graph, ["Fact1", "Fact2"]) == []
        with cfg.override_settings(retrieval_join_max_hub_degree=100):
            assert _expand(graph, ["Fact1", "Fact2"]) == ["Hub"]

    def test_a_hub_that_was_retrieved_is_still_joined(self):
        edges = [("Fact", "Hub")] + [(f"Other{i}", "Hub") for i in range(12)]
        assert _expand(_graph(edges), ["Fact", "Hub"]) == []

    def test_never_crosses_data_sources(self):
        # Calendar lives in both sources; a path may not hop from one source's
        # fact to the other's through it.
        graph = _graph(
            [("SalesFact", "Link"), ("Link", "StockFact"), ("SalesFact", "Cal"), ("Cal", "StockFact")],
            sources={"SalesFact": ["sales"], "StockFact": ["stock"], "Link": ["sales"],
                     "Cal": ["sales", "stock"]},
        )
        assert _expand(graph, ["SalesFact", "StockFact"]) == []

    def test_follows_a_path_that_stays_inside_one_source(self):
        graph = _graph(
            [("Fact", "Bridge"), ("Bridge", "Dim")],
            sources={"Fact": ["a"], "Bridge": ["a"], "Dim": ["a", "b"]},
        )
        assert _expand(graph, ["Fact", "Dim"]) == ["Bridge"]

    def test_added_table_cap(self):
        graph = _graph([("Fact", "A"), ("A", "B"), ("B", "Dim")])
        with cfg.override_settings(retrieval_join_max_hops=3, retrieval_join_max_added_tables=1):
            assert _expand(graph, ["Fact", "Dim"]) == []
        with cfg.override_settings(retrieval_join_max_hops=3, retrieval_join_max_added_tables=2):
            assert _expand(graph, ["Fact", "Dim"]) == ["A", "B"]

    def test_token_budget_vetoes_an_expansion(self):
        graph = _graph([("Fact", "Bridge"), ("Bridge", "Dim")])
        assert _expand(graph, ["Fact", "Dim"], fits=lambda tables: False) == []

    def test_switch_turns_it_off(self):
        graph = _graph([("Fact", "Bridge"), ("Bridge", "Dim")])
        with cfg.override_settings(retrieval_join_expansion=False):
            assert _expand(graph, ["Fact", "Dim"]) == []

    def test_three_tables_share_one_intermediate(self):
        graph = _graph([("Fact", "Bridge"), ("Bridge", "D1"), ("Bridge", "D2")])
        assert _expand(graph, ["Fact", "D1", "D2"]) == ["Bridge"]

    def test_best_path_prefers_nothing_over_an_unreachable_target(self):
        graph = _graph([("A", "B")])
        assert _best_path(graph, "A", {"Z"}, max_hops=3, max_hub_degree=10) is None


class TestInference:
    COLUMNS = {
        "Order": {"ID": "", "CustomerID": "", "Region_ID": "", "OrderDate_ID": "", "Batch_ID": ""},
        "Customer": {"ID": "", "Name": ""},
        "sales.Region": {"ID": ""},
        "ops.Region": {"ID": ""},
        "Legacy": {"Order_Code": ""},
    }
    BY_BARE = {"order": ["Order"], "customer": ["Customer"], "region": ["sales.Region", "ops.Region"],
               "legacy": ["Legacy"]}
    QUAL = {"Order": "sales", "Customer": "sales", "sales.Region": "sales", "ops.Region": "ops", "Legacy": "sales"}

    def _infer(self, **kw):
        known = kw.pop("known", set())
        return _infer_edges(kw.pop("columns", self.COLUMNS), self.BY_BARE, self.QUAL,
                            kw.pop("sources", {}), known)

    def test_x_id_columns_point_at_table_x(self):
        pairs = set(self._infer())
        assert ("Order", "Customer") in pairs

    def test_same_name_tables_resolve_by_the_qualifier_of_the_source_table(self):
        assert ("Order", "sales.Region") in set(self._infer())
        assert ("Order", "ops.Region") not in set(self._infer())

    def test_same_name_tables_without_a_qualifier_match_are_skipped_not_guessed(self):
        qual = dict(self.QUAL, Order="elsewhere")
        found = _infer_edges(self.COLUMNS, self.BY_BARE, qual, {}, set())
        assert not [p for p in found if p[1].endswith("Region")]

    def test_data_source_must_be_shared(self):
        sources = {t: frozenset({"a"}) for t in self.COLUMNS}
        sources["Customer"] = frozenset({"b"})
        sources["ops.Region"] = frozenset({"a"})
        sources["sales.Region"] = frozenset({"b"})
        found = set(_infer_edges(self.COLUMNS, self.BY_BARE, self.QUAL, sources, set()))
        assert ("Order", "Customer") not in found
        assert ("Order", "ops.Region") in found          # the only same-source candidate

    def test_columns_that_follow_no_convention_add_nothing(self):
        found = set(self._infer())
        assert not [p for p in found if p[0] == "Legacy"]          # Order_Code: not an id column
        assert not [p for p in found if p[1] in ("Date", "Batch")]  # no such tables

    def test_declared_pairs_are_not_duplicated(self):
        known = {frozenset(("Order", "Customer"))}
        assert ("Order", "Customer") not in set(self._infer(known=known))

    def test_a_bare_id_column_and_a_self_reference_are_not_keys(self):
        columns = {"Order": {"ID": "", "Order_ID": ""}}
        assert _infer_edges(columns, {"order": ["Order"]}, {}, {}, set()) == []

    def test_target_needs_an_id_column(self):
        columns = {"Order": {"CustomerID": ""}, "Customer": {"Name": ""}}
        assert _infer_edges(columns, {"customer": ["Customer"], "order": ["Order"]}, {}, {}, set()) == []


class TestLoadedGraph:
    def test_example_schema_edges_load_from_schema_yaml(self):
        graph = build_join_graph()
        neighbours = {n for n, _ in graph.adjacency.get("Order", ())}
        assert {"Customer", "Date"} <= neighbours

    def test_graph_is_cached_until_the_configuration_changes(self):
        assert build_join_graph() is build_join_graph()

    def test_two_dimensions_are_joined_through_their_fact(self):
        assert expand_join_paths(["Customer", "Ring"]) == ["Order"]

    def test_inference_switch_is_part_of_the_cache_key(self):
        on = build_join_graph()
        with cfg.override_settings(retrieval_infer_relationships=False):
            off = build_join_graph()
        assert off is not on
        assert not any(flag for edges in off.adjacency.values() for _, flag in edges)

    @pytest.mark.parametrize("name", ["retrieval_join_expansion", "retrieval_infer_relationships"])
    def test_switches_default_on(self, name):
        assert getattr(cfg.settings, name) is True


class TestContextWiring:
    def _retrieve(self, entities, facts):
        from retrieval.context_retriever import ContextRetriever

        with patch("retrieval.context_retriever.EntityRetriever.retrieve", return_value=entities), \
                patch("retrieval.context_retriever.FactRetriever.retrieve", return_value=facts):
            return ContextRetriever.retrieve("question")

    def test_join_tables_are_part_of_the_selected_tables_but_not_of_entities_or_facts(self):
        context = self._retrieve(["Customer", "Ring"], [])
        assert context.join_tables == ["Order"]
        assert context.entities == ["Customer", "Ring"] and context.facts == []
        assert context.selected_tables == ["Customer", "Ring", "Order"]

    def test_relationships_cover_the_added_tables(self):
        context = self._retrieve(["Customer", "Ring"], [])
        assert len(context.relationships) == 2           # Order-Customer and Order-Ring

    def test_nothing_is_added_when_the_switch_is_off(self):
        with cfg.override_settings(retrieval_join_expansion=False):
            context = self._retrieve(["Customer", "Ring"], [])
        assert context.join_tables == []
        assert context.selected_tables == ["Customer", "Ring"]

    def test_selected_tables_deduplicate_a_join_table_that_was_also_retrieved(self):
        from core.models import RetrievalContext

        context = RetrievalContext(entities=["A"], facts=["B"], join_tables=["B", "C"])
        assert context.selected_tables == ["A", "B", "C"]
        assert RetrievalContext().join_tables == []
