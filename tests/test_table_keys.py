# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Retrieval configuration that names tables the way ``schema.yaml`` does not.

``entities.yaml`` and ``retrieval_hints.yaml`` are written by hand, often before
a warehouse's schemas were qualified, so they say ``Customer`` where
``schema.yaml`` has ``sales.Customer``. A bare name that is not a key names no
table: the alias hit never reached the prompt, the ranking could not score it
and ``name in FACT_TABLES`` was false for every key. These tests pin the
resolution (``retrieval.table_keys``) and its use by every stage that reads
those files, with a small invented schema and invented configuration.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from retrieval.context_retriever import ContextRetriever
from retrieval.entity_retriever import EntityRetriever
from retrieval.fact_retriever import FactRetriever
from retrieval.join_paths import JoinGraph, expand_join_paths
from retrieval.pruning import prune_selection
from retrieval.table_evidence import named_tables, scored_extras
from retrieval.table_keys import configured_fact_tables, entity_for_table, resolve_table_ref
from schema_data.retriever import forced_tables

#: Keys of the invented warehouse: the same bare name in two schemas, a fact
#: schema, and a calendar.
_SCHEMA = {
    "shop_Dim.Customer": "People who buy.",
    "other_Dim.Customer": "Another system's customers.",
    "shop_Dim.Region": "Sales regions.",
    "shop_Fact.Invoice": "Billing documents.",
    "other_Fact.Invoice": "Another system's billing documents.",
    "shop_Fact.Payment": "Money received.",
    "common.Calendar": "Days, months and years.",
}


@contextmanager
def _world(
    *, entities=None, fact_tables=(), fact_patterns=None, always_include=None,
    sources=None, keywords=None, table_sources=None, ranked=(), labels=None,
):
    """Patch the schema keys, the configuration files and (optionally) the data sources."""
    entity_table = {
        name: {"aliases": list(aliases), "table": name, "label": (labels or {}).get(name)}
        for name, aliases in (entities or {}).items()
    }
    patterns = dict(fact_patterns or {})
    normalised = {t: [" ".join(p.lower().split()) for p in forms] for t, forms in (always_include or {}).items()}
    names = tuple(sources or ())
    with patch("schema_data.registry.get_table_descriptions", return_value=dict(_SCHEMA)), \
            patch("schema_data.tables.TABLE_DESCRIPTIONS", dict(_SCHEMA), create=True), \
            patch("knowledge.entities.ENTITIES", entity_table, create=True), \
            patch("retrieval.entity_retriever.ENTITIES", entity_table), \
            patch("knowledge.retrieval_hints.FACT_TABLES", set(fact_tables), create=True), \
            patch("knowledge.retrieval_hints.FACT_PATTERNS", patterns, create=True), \
            patch("retrieval.fact_retriever.FACT_PATTERNS", patterns), \
            patch("schema_data.retriever._ALWAYS_INCLUDE_NORMALISED", normalised), \
            patch("knowledge.retrieval_hints.ALWAYS_INCLUDE", dict(always_include or {}), create=True), \
            patch("database.datasources.datasource_names", return_value=names), \
            patch("database.datasources.datasource_keywords", return_value=dict(keywords or {})), \
            patch("database.datasources.table_datasource_sets", return_value=dict(table_sources or {})), \
            patch("schema_data.retriever.rank_tables", return_value=list(ranked)), \
            patch("schema_data.retriever.column_evidence", return_value={}):
        yield


class TestResolveTableRef:
    def test_a_key_is_itself_whatever_its_case_or_brackets(self):
        with _world():
            assert resolve_table_ref("shop_Fact.Payment") == ("shop_Fact.Payment",)
            assert resolve_table_ref("SHOP_FACT.payment") == ("shop_Fact.Payment",)
            assert resolve_table_ref("[shop_Fact].[Payment]") == ("shop_Fact.Payment",)

    def test_a_bare_name_that_one_key_has_resolves_to_it(self):
        with _world():
            assert resolve_table_ref("Payment") == ("shop_Fact.Payment",)
            assert resolve_table_ref("calendar") == ("common.Calendar",)

    def test_a_partly_qualified_name_picks_the_key_with_that_schema(self):
        with _world():
            assert resolve_table_ref("other_Dim.Customer") == ("other_Dim.Customer",)
            assert resolve_table_ref("other_Fact.Invoice") == ("other_Fact.Invoice",)
            assert resolve_table_ref("elsewhere.Invoice") == ()

    def test_an_unknown_name_resolves_to_nothing(self):
        with _world():
            assert resolve_table_ref("Nothing") == ()

    def test_a_bare_name_two_schemas_share_is_read_in_the_schemas_the_file_is_written_for(self):
        # Region, Payment and Invoice-in-shop_Fact are unambiguous: the file is about shop_*
        with _world(entities={"Region": ["region"], "Customer": ["customer"]},
                    fact_tables=["Payment"]):
            assert resolve_table_ref("Customer") == ("shop_Dim.Customer",)
            assert resolve_table_ref("Invoice") == ("shop_Fact.Invoice",)

    def test_a_name_the_file_cannot_place_stays_ambiguous(self):
        with _world():
            assert resolve_table_ref("Customer") == ("shop_Dim.Customer", "other_Dim.Customer")

    def test_the_question_s_data_source_keywords_decide_first(self):
        sets = {"shop_Dim.Customer": ("shop",), "other_Dim.Customer": ("legacy",)}
        with _world(entities={"Region": ["region"]}, sources=("shop", "legacy"),
                    keywords={"legacy": ("legacy system",)}, table_sources=sets):
            # without the keyword the home schema (shop_Dim, from Region) decides
            assert resolve_table_ref("Customer", "how many customers") == ("shop_Dim.Customer",)
            # with it the file's schema no longer wins
            assert resolve_table_ref("Customer", "customers in the legacy system") == ("other_Dim.Customer",)

    def test_a_keyword_for_a_source_with_no_candidate_decides_nothing(self):
        sets = {"shop_Dim.Customer": ("shop",), "other_Dim.Customer": ("shop",)}
        with _world(entities={"Region": ["region"]}, sources=("shop", "legacy"),
                    keywords={"legacy": ("legacy system",)}, table_sources=sets):
            assert resolve_table_ref("Customer", "the legacy system") == ("shop_Dim.Customer",)


class TestEveryStageReadsTheKeys:
    def test_alias_hits_come_back_as_keys(self):
        with _world(entities={"Customer": ["customer"], "Region": ["region"]}):
            assert named_tables("customers per region", {"Customer": ["customer"], "Region": ["region"]}) == [
                "shop_Dim.Customer", "shop_Dim.Region",
            ]

    def test_a_name_that_resolves_to_nothing_is_kept_as_written(self):
        with _world():
            assert named_tables("a ghost", {"Ghost": ["ghost"]}) == ["Ghost"]

    def test_entity_retriever_returns_keys_for_bare_entity_names(self):
        with _world(entities={"Region": ["region"]}):
            assert EntityRetriever.retrieve("sales by region") == ["shop_Dim.Region"]

    def test_fact_retriever_returns_keys_for_bare_patterns(self):
        with _world(fact_patterns={"Payment": ["payment"]}, fact_tables=["Payment"]):
            assert FactRetriever.retrieve("payments last week") == ["shop_Fact.Payment"]

    def test_bare_fact_tables_still_tell_facts_from_dimensions(self):
        ranked = [("shop_Fact.Payment", 2.0), ("shop_Dim.Region", 1.9)]
        with _world(fact_tables=["Payment"], ranked=ranked):
            assert configured_fact_tables() == {"shop_Fact.Payment"}
            assert scored_extras("q", fact=True) == ["shop_Fact.Payment"]
            assert scored_extras("q", fact=False) == ["shop_Dim.Region"]

    def test_the_tfidf_fallback_splits_facts_from_dimensions_by_key(self):
        ranked = [("shop_Fact.Payment", 2.0), ("shop_Dim.Region", 1.9)]
        with _world(fact_tables=["Payment"], ranked=ranked):
            assert FactRetriever.retrieve("anything") == ["shop_Fact.Payment"]
            assert EntityRetriever.retrieve("anything") == ["shop_Dim.Region"]

    def test_always_include_with_a_bare_name_forces_the_key(self):
        with _world(always_include={"Calendar": ["month"]}):
            assert forced_tables("totals per month") == ["common.Calendar"]

    def test_pruning_keeps_a_table_a_bare_alias_named(self):
        # the alias names Region only by its bare name; it must still count as named
        ranked = [("shop_Dim.Region", 1.0), ("shop_Fact.Payment", 0.9)]
        with _world(entities={"Region": ["region"]}, ranked=ranked), \
                patch("retrieval.join_paths.build_join_graph", return_value=JoinGraph()):
            kept, _ = prune_selection(
                "region", ["shop_Dim.Region", "shop_Fact.Payment"], [],
            )
        assert kept == ["shop_Dim.Region"]

    def test_join_expansion_starts_from_a_bare_named_fact_table(self):
        edges = {
            "shop_Fact.Payment": (("shop_Dim.Bridge", False),),
            "shop_Dim.Bridge": (("shop_Fact.Payment", False), ("shop_Dim.Region", False)),
            "shop_Dim.Region": (("shop_Dim.Bridge", False),),
        }
        graph = JoinGraph(adjacency=edges)
        with _world(fact_tables=["Payment"]), \
                patch("retrieval.join_paths.build_join_graph", return_value=graph):
            added = expand_join_paths(["shop_Dim.Region", "shop_Fact.Payment"], fits=lambda t: True)
        assert added == ["shop_Dim.Bridge"]

    def test_an_entity_label_is_found_for_the_key_its_bare_name_resolves_to(self):
        entities = {"Region": ["region"]}
        with _world(entities=entities):
            assert entity_for_table("shop_Dim.Region") is not None
            assert entity_for_table("shop_Dim.Region")["aliases"] == ["region"]
            assert entity_for_table("shop_Fact.Payment") is None


class TestContextRetrieverWithBareEntityNames:
    """``ContextRetriever`` keys value matching and its warning on the table keys."""

    @staticmethod
    def _retrieve(*, entities, static_filters, vocabulary):
        with patch("retrieval.context_retriever.EntityRetriever.retrieve", return_value=entities), \
                patch("retrieval.context_retriever.FactRetriever.retrieve", return_value=[]), \
                patch("retrieval.context_retriever.ValueRetriever.retrieve", return_value=dict(static_filters)), \
                patch("retrieval.context_retriever.match_question_against_vocabulary", vocabulary):
            return ContextRetriever.retrieve("sales by region")

    def test_a_filter_the_static_pass_set_under_a_bare_name_skips_the_vocabulary(self):
        vocabulary = MagicMock()
        with _world(entities={"Region": ["region"]}):
            self._retrieve(
                entities=["shop_Dim.Region"], static_filters={"Region": "north"},
                vocabulary=vocabulary,
            )
        vocabulary.assert_not_called()

    def test_an_unavailable_vocabulary_names_the_entity_s_label_through_its_bare_name(self):
        result = SimpleNamespace(
            filters={}, clarifications=[], unavailable_tables=["shop_Dim.Region"], token_tier_filters={},
        )
        with _world(entities={"Region": ["region"]}, labels={"Region": "Sales area"}):
            ctx = self._retrieve(
                entities=["shop_Dim.Region"], static_filters={},
                vocabulary=MagicMock(return_value=result),
            )
        assert len(ctx.warnings) == 1
        assert "Sales area" in ctx.warnings[0]
