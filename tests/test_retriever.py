# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for schema_data/retriever.py (TF-IDF + synonym expansion)."""

from __future__ import annotations

from schema_data.retriever import retrieve_tables, _expand, _build_idf
from schema_data.tables import TABLE_DESCRIPTIONS as TABLES


class TestRetrieveTables:
    def test_returns_list(self):
        result = retrieve_tables("قرارداد")
        assert isinstance(result, list)
        assert len(result) > 0

    def test_returns_at_most_top_n(self):
        result = retrieve_tables("قرارداد مشتری خرید")
        assert len(result) <= 6

    def test_relevant_table_for_contract(self):
        result = retrieve_tables("contract trade")
        assert "Contract" in result

    def test_relevant_table_for_customer(self):
        result = retrieve_tables("customer buyer")
        assert "Customer" in result or "CustomerContract" in result

    def test_relevant_table_for_persian_date(self):
        result = retrieve_tables("تاریخ سال")
        assert "Date" in result

    def test_relevant_table_for_broker(self):
        result = retrieve_tables("کارگزار broker")
        assert "Broker" in result

    def test_fallback_on_no_match(self):
        result = retrieve_tables("xyzzy foobar nonexistent_word_12345")
        assert set(result) == set(TABLES.keys())

    def test_bigram_boosts_customer_contract(self):
        result = retrieve_tables("خرید مشتری")
        assert "CustomerContract" in result

    def test_offer_table_matched(self):
        result = retrieve_tables("عرضه کالا offer")
        assert "Offer" in result

    def test_order_table_matched(self):
        result = retrieve_tables("سفارش خرید order")
        assert "Order" in result

    def test_symbol_table_matched(self):
        result = retrieve_tables("نماد commodity")
        assert "Symbol" in result

    def test_ring_table_matched(self):
        result = retrieve_tables("تالار ring")
        assert "Ring" in result

    def test_no_duplicates_in_result(self):
        result = retrieve_tables("مشتری کارگزار قرارداد")
        assert len(result) == len(set(result))

    def test_all_returned_names_are_valid_tables(self):
        result = retrieve_tables("مشتری قرارداد")
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

    def test_contract_included_via_hacjm(self):
        result = retrieve_tables("حجم معاملات در تالار پتروشیمی")
        assert "Contract" in result

    def test_ring_included_via_petrochemical_synonym(self):
        result = retrieve_tables("حجم معامله در تالار پتروشیمی")
        assert "Ring" in result

    def test_customer_contract_included_for_purchase_question(self):
        result = retrieve_tables("خرید مشتری ارزش")
        assert "CustomerContract" in result

    def test_complex_query_includes_date_contract_ring(self):
        result = retrieve_tables("بیشترین حجم معامله در تالار پتروشیمی در فصل بهار")
        assert "Date" in result
        assert "Contract" in result
        assert "Ring" in result


class TestExpandSynonyms:
    def test_expands_bahar_to_fasl(self):
        expanded = _expand("بهار")
        assert "فصل" in expanded

    def test_expands_volume_to_trade(self):
        expanded = _expand("volume")
        assert "trade" in expanded.lower()

    def test_no_expansion_for_unknown_word(self):
        expanded = _expand("xyzzy")
        assert expanded.strip() == "xyzzy"

    def test_multiple_synonyms_expanded(self):
        expanded = _expand("بهار حجم")
        assert "فصل" in expanded
        assert "معامله" in expanded


class TestBuildIdf:
    def test_returns_dict(self):
        idf = _build_idf()
        assert isinstance(idf, dict)
        assert len(idf) > 0

    def test_rare_term_has_higher_idf(self):
        idf = _build_idf()
        assert idf.get("بسته", 0) > idf.get("معامله", 0)

    def test_cached(self):
        idf1 = _build_idf()
        idf2 = _build_idf()
        assert idf1 is idf2
