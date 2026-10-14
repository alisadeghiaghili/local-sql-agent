# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""An alias or pattern hit no longer ends the search for tables.

``retrieval.entity_retriever`` / ``retrieval.fact_retriever`` used to return
only the aliased tables when any alias matched, so a question naming one
aliased dimension and one without an alias lost the second, and an
``always_include`` table (the date dimension) was dropped as soon as any other
dimension matched. These tests pin the new behaviour against the example
schema (``Order`` is its only fact table) with the alias maps patched to
known values.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import config as cfg
from retrieval.entity_retriever import EntityRetriever
from retrieval.fact_retriever import FactRetriever
from retrieval.table_evidence import alias_hits, scored_extras
from schema_data.retriever import forced_tables


class TestAliasHits:
    def test_matches_in_configuration_order_without_duplicates(self):
        aliases = {"B": ["beta", "bee"], "A": ["alpha"]}
        assert alias_hits("alpha beta bee", aliases) == ["B", "A"]

    def test_folds_persian_and_arabic_spellings_and_zwnj(self):
        assert alias_hits("كارگزار", {"Broker": ["کارگزار"]}) == ["Broker"]
        assert alias_hits("تامینکننده", {"Supplier": ["تامین‌کننده"]}) == ["Supplier"]
        assert alias_hits("تأمین‌کننده", {"Supplier": ["تأمین کننده".replace(" ", "‌")]}) == ["Supplier"]

    def test_is_case_insensitive_and_substring(self):
        assert alias_hits("CUSTOMERS by city", {"Customer": ["customer"]}) == ["Customer"]

    def test_an_empty_alias_never_matches(self):
        assert alias_hits("anything", {"A": [""]}) == []

    def test_an_inflected_word_matches_where_a_substring_cannot(self):
        # "-ies" for "-y": "city" is not a substring of "cities"
        assert alias_hits("How many cities were visited?", {"Place": ["city"]}) == ["Place"]
        assert alias_hits("each category", {"Group": ["categories"]}) == ["Group"]
        assert alias_hits("every branch", {"Site": ["branches"]}) == ["Site"]

    def test_a_different_word_is_not_an_inflection(self):
        assert alias_hits("an ordinary day", {"Order": ["order"]}) == []
        assert alias_hits("citrus", {"Place": ["city"]}) == []

    def test_the_words_of_a_multi_word_alias_match_in_any_order(self):
        aliases = {"Part": ["gear tooth pitch"]}
        assert alias_hits("pitch of each tooth on the gear", aliases) == ["Part"]
        assert alias_hits("gear-pitch by tooth", aliases) == ["Part"]

    def test_a_multi_word_alias_needs_all_its_words(self):
        assert alias_hits("gear pitch only", {"Part": ["gear tooth pitch"]}) == []


class TestScoredExtras:
    def test_zero_limit_restores_the_old_behaviour(self):
        with cfg.override_settings(retrieval_extra_tables=0):
            assert scored_extras("customer purchase orders by date", fact=False) == []

    def test_only_the_requested_kind_is_returned(self):
        facts = scored_extras("customer purchase orders by date", fact=True)
        dims = scored_extras("customer purchase orders by date", fact=False)
        assert "Order" in facts
        assert "Order" not in dims
        assert "Date" in dims          # forced by always_include ("date")
        assert not set(facts) & set(dims)

    def test_excluded_tables_are_left_out(self):
        assert "Order" not in scored_extras("purchase orders", fact=True, exclude=["Order"])

    def test_limit_caps_the_unforced_extras(self):
        question = "customer broker supplier ring symbol currency"
        with cfg.override_settings(retrieval_extra_tables=1, retrieval_extra_score_ratio=0.0):
            extras = scored_extras(question, fact=False)
        assert len([t for t in extras if t not in forced_tables(question)]) <= 1

    def test_a_high_ratio_keeps_only_the_best(self):
        question = "customer broker supplier ring symbol currency"
        with cfg.override_settings(retrieval_extra_score_ratio=1.0):
            extras = scored_extras(question, fact=False)
        assert len([t for t in extras if t not in forced_tables(question)]) <= 1

    def test_result_is_deterministic(self):
        question = "customer broker supplier ring symbol"
        assert scored_extras(question, fact=False) == scored_extras(question, fact=False)


class TestRetrieversKeepSearchingAfterAnAliasHit:
    @pytest.fixture
    def entities(self):
        table = {"Customer": {"aliases": ["customer"], "table": "Customer", "label": None}}
        with patch("retrieval.entity_retriever.ENTITIES", table):
            yield

    def test_always_include_table_is_kept_beside_an_alias_hit(self, entities):
        result = EntityRetriever.retrieve("total amount per customer by month")
        assert result[0] == "Customer"
        assert "Date" in result

    def test_alias_hits_come_first_and_are_not_repeated(self, entities):
        result = EntityRetriever.retrieve("customer broker by month")
        assert result[0] == "Customer"
        assert len(result) == len(set(result))

    def test_old_behaviour_is_one_setting_away(self, entities):
        with cfg.override_settings(retrieval_extra_tables=0):
            assert EntityRetriever.retrieve("total amount per customer by month") == ["Customer"]

    def test_entity_order_is_the_configured_order(self):
        table = {
            "Ring": {"aliases": ["ring"], "table": "Ring", "label": None},
            "Customer": {"aliases": ["customer"], "table": "Customer", "label": None},
        }
        with patch("retrieval.entity_retriever.ENTITIES", table), \
                cfg.override_settings(retrieval_extra_tables=0):
            assert EntityRetriever.retrieve("customer and ring") == ["Ring", "Customer"]

    def test_fact_pattern_hit_keeps_ranking_facts_too(self):
        with patch("retrieval.fact_retriever.FACT_PATTERNS", {"Order": ["order"]}):
            assert FactRetriever.retrieve("purchase order totals") == ["Order"]
