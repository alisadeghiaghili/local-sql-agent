# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for schema_data/retriever.py (TF-IDF + synonym expansion).

A handful of cases below need a REAL deployment's real Persian retrieval
vocabulary and real fact-table names to mean anything -- e.g. a query only
resolves to a specific real table because that table's name or a real
``project_config/retrieval_hints.yaml`` trigger phrase matches it, which
``project_config.example/``'s generic config has no equivalent for. Those
cases are marked ``@pytest.mark.domain_data`` (auto-skipped whenever
``PROJECT_CONFIG_DIR`` points at ``project_config.example/`` -- see the
repo-root ``conftest.py``) and read their query/expectation pairs from an
optional, deployment-owned fixture file,
``<PROJECT_CONFIG_DIR>/_test_fixtures/retriever_expectations.json`` (see
``project_config.example/_test_fixtures/README.md`` for the format) via
:mod:`tests._domain_fixtures`, rather than hardcoding real values in this
tracked module.
"""

from __future__ import annotations

import pytest

from schema_data.retriever import retrieve_tables, _expand, _build_idf
from schema_data.tables import TABLE_DESCRIPTIONS as TABLES
from tests._domain_fixtures import load_json_fixture


@pytest.fixture(scope="module")
def expectations():
    return load_json_fixture("retriever_expectations.json")


class TestRetrieveTables:
    def test_returns_list(self):
        result = retrieve_tables("قرارداد")
        assert isinstance(result, list)
        assert len(result) > 0

    @pytest.mark.domain_data
    def test_returns_at_most_top_n(self, expectations):
        """Needs real Persian trigger phrases in
        project_config/retrieval_hints.yaml's always_include to force
        enough distinct matches that the ranked-and-sliced branch (capped
        at 6) runs instead of the fallback-to-all-tables branch; the
        generic example config's tables number fewer than 6 anyway."""
        case = expectations["returns_at_most_top_n"]
        result = retrieve_tables(case["query"])
        assert len(result) <= case["max"]

    @pytest.mark.domain_data
    def test_membership_cases(self, expectations):
        """Each case needs a real fact/dimension table name or a real
        retrieval-hint trigger phrase absent from
        project_config.example/'s generic schema."""
        for case in expectations["membership_cases"]:
            result = retrieve_tables(case["query"])
            assert case["expect_in"] in result, (
                f"query {case['query']!r}: expected {case['expect_in']!r} in {result!r}"
            )

    def test_relevant_table_for_customer(self):
        result = retrieve_tables("customer buyer")
        assert "Customer" in result

    def test_relevant_table_for_persian_date(self):
        result = retrieve_tables("تاریخ سال")
        assert "Date" in result

    def test_relevant_table_for_broker(self):
        result = retrieve_tables("کارگزار broker")
        assert "Broker" in result

    def test_fallback_on_no_match(self):
        result = retrieve_tables("xyzzy foobar nonexistent_word_12345")
        assert set(result) == set(TABLES.keys())

    def test_order_table_matched(self):
        result = retrieve_tables("سفارش خرید order")
        assert "Order" in result

    def test_ring_table_matched(self):
        result = retrieve_tables("تالار ring")
        assert "Ring" in result

    def test_trailing_punctuation_does_not_hide_a_word(self):
        """A question mark glued to the last word used to make it a
        different token ("customer?" != "customer")."""
        assert retrieve_tables("customer buyer?") == retrieve_tables("customer buyer")
        assert "Customer" in retrieve_tables("Which customer?")
        assert "Date" in retrieve_tables("تاریخ سال؟")

    def test_no_duplicates_in_result(self):
        result = retrieve_tables("مشتری کارگزار قرارداد")
        assert len(result) == len(set(result))

    def test_all_returned_names_are_valid_tables(self):
        """A neutral, no-match query (same as test_fallback_on_no_match)
        rather than real Persian vocabulary: schema_data/retriever.py's
        _ALWAYS_INCLUDE dict (a retrieval heuristic, not schema metadata --
        see its module docstring) can hardcode real table names
        independently of whichever schema.yaml is loaded, so a query that
        triggers a forced match can return a table name absent from a
        *different*, generic example schema. That is a property of
        _ALWAYS_INCLUDE, not something this test is about -- it exists to
        check the fallback-to-"all tables" path is internally consistent,
        which a neutral query exercises without that interaction."""
        result = retrieve_tables("xyzzy foobar nonexistent_word_12345")
        for name in result:
            assert name in TABLES

    def test_date_included_for_season_word_bahar(self):
        result = retrieve_tables("بیشترین حجم معامله در فصل بهار")
        assert "Date" in result

    def test_date_included_for_tabestan(self):
        result = retrieve_tables("حجم عرضه تابستان")
        assert "Date" in result

    def test_date_included_for_payiz(self):
        result = retrieve_tables("خرید مشتریان در پاییز")
        assert "Date" in result

    def test_date_included_for_zemestan(self):
        result = retrieve_tables("معاملات فصل زمستان")
        assert "Date" in result

    def test_date_included_via_always_include_signal(self):
        result = retrieve_tables("گزارش دورهای سه ماهه")
        assert "Date" in result

    def test_ring_included_via_petrochemical_synonym(self):
        result = retrieve_tables("حجم معامله در تالار پتروشیمی")
        assert "Ring" in result

    @pytest.mark.domain_data
    def test_complex_query_includes_expected_tables(self, expectations):
        """Needs a real fact-table name absent from
        project_config.example/schema.yaml (Date and Ring alone would pass
        under the example config too, via always_include's English
        trigger words and the fallback-to-all-tables path respectively)."""
        case = expectations["complex_query"]
        result = retrieve_tables(case["query"])
        for expected in case["expect_all_in"]:
            assert expected in result


class TestExpandSynonyms:
    @pytest.mark.domain_data
    def test_expands_single_word(self, expectations):
        """Real project_config/aliases.yaml synonym;
        project_config.example/aliases.yaml has no Persian synonyms."""
        case = expectations["expand_single"]
        expanded = _expand(case["word"])
        assert case["expect_in"] in expanded

    def test_expands_volume_to_trade(self):
        expanded = _expand("volume")
        assert "trade" in expanded.lower()

    def test_no_expansion_for_unknown_word(self):
        expanded = _expand("xyzzy")
        assert expanded.strip() == "xyzzy"

    @pytest.mark.domain_data
    def test_multiple_synonyms_expanded(self, expectations):
        """Real project_config/aliases.yaml synonyms;
        project_config.example/aliases.yaml has no Persian synonyms."""
        case = expectations["expand_multiple"]
        expanded = _expand(case["words"])
        for expected in case["expect_all_in"]:
            assert expected in expanded


class TestBuildIdf:
    def test_returns_dict(self):
        idf = _build_idf()
        assert isinstance(idf, dict)
        assert len(idf) > 0

    @pytest.mark.domain_data
    def test_rare_term_has_higher_idf(self, expectations):
        """Compares the real corpus-wide rarity of two specific real
        Persian words across the real table descriptions; meaningless
        against project_config.example/schema.yaml's different, generic
        descriptions."""
        case = expectations["idf_comparison"]
        idf = _build_idf()
        assert idf.get(case["rarer"], 0) > idf.get(case["commoner"], 0)

    def test_cached(self):
        idf1 = _build_idf()
        idf2 = _build_idf()
        assert idf1 is idf2
