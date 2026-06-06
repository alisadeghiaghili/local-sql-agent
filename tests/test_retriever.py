"""Unit tests for schema/retriever.py."""

from __future__ import annotations

from schema.retriever import retrieve_tables
from schema.tables import TABLES


class TestRetrieveTables:
    def test_returns_list(self):
        result = retrieve_tables("قرارداد")
        assert isinstance(result, list)
        assert len(result) > 0

    def test_returns_at_most_top_n(self):
        result = retrieve_tables("قرارداد مشتری خرید")
        assert len(result) <= 6

    def test_relevant_table_included_for_contract(self):
        result = retrieve_tables("contract trade")
        assert "Contract" in result

    def test_relevant_table_included_for_customer(self):
        result = retrieve_tables("customer buyer purchase")
        assert "Customer" in result or "CustomerContract" in result

    def test_relevant_table_for_persian_date(self):
        result = retrieve_tables("تاریخ سال")
        assert "Date" in result

    def test_relevant_table_for_broker(self):
        result = retrieve_tables("کارگزار broker")
        assert "Broker" in result

    def test_fallback_returns_all_tables_on_no_match(self):
        result = retrieve_tables("xyzzy foobar nonexistent")
        assert set(result) == set(TABLES.keys())

    def test_bigram_scoring_boosts_exact_phrase(self):
        # "خرید مشتری" is a bigram in CustomerContract description
        result = retrieve_tables("خرید مشتری")
        assert "CustomerContract" in result
        # CustomerContract should rank higher than plain Customer
        assert result.index("CustomerContract") < result.index("Customer") \
            if "Customer" in result else True

    def test_offer_table_matched(self):
        result = retrieve_tables("عرضه کالا offer")
        assert "Offer" in result

    def test_order_table_matched(self):
        result = retrieve_tables("سفارش خرید order")
        assert "Order" in result

    def test_symbol_table_matched(self):
        result = retrieve_tables("نماد commodity symbol")
        assert "Symbol" in result

    def test_ring_table_matched(self):
        result = retrieve_tables("تالار رینگ ring")
        assert "Ring" in result

    def test_no_duplicates_in_result(self):
        result = retrieve_tables("مشتری کارگزار قرارداد")
        assert len(result) == len(set(result))

    def test_all_returned_names_are_valid_tables(self):
        result = retrieve_tables("مشتری قرارداد")
        for name in result:
            assert name in TABLES

    def test_empty_question_falls_back_to_all(self):
        result = retrieve_tables("")
        assert set(result) == set(TABLES.keys())
