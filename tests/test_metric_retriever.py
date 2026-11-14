# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``retrieval.metric_retriever``: which metrics a question names, which tables they use.

Table names are taken from the loaded schema, not hardcoded, so the tests hold
under both ``project_config.example/`` and a real ``project_config/``.
"""

from __future__ import annotations

import logging

import pytest

from retrieval.metric_retriever import expression_tables, mentioned_metrics, metric_tables
from schema_data.columns import TABLE_COLUMNS
from schema_data.registry import bare_table_name

_FIRST, _SECOND = list(TABLE_COLUMNS)[:2]
_A, _B = bare_table_name(_FIRST), bare_table_name(_SECOND)

_METRICS = {
    "total_value": {
        "aliases": ["total value", "Sum Of Value", "مجموع ارزش"],
        "expression": f"SUM({_A}.Amount)",
    },
    "average_price": {
        "aliases": ["average price", "قیمت میانگین"],
        "expression": f"AVG({_B}.Price)",
    },
    "volume": {
        "aliases": [],
        "expression": f"SUM({_A}.Qty)",
    },
}


class TestExpressionTables:
    def test_a_simple_aggregate_names_its_table(self):
        assert expression_tables(f"SUM({_A}.Amount)") == [_FIRST]

    def test_a_ratio_over_two_columns_of_one_table_names_it_once(self):
        expression = f"SUM({_A}.Amount) / NULLIF(SUM({_A}.Qty), 0)"

        assert expression_tables(expression) == [_FIRST]

    def test_two_tables_are_listed_in_order_of_appearance(self):
        expression = f"SUM({_B}.Amount) / NULLIF(SUM({_A}.Qty), 0)"

        assert expression_tables(expression) == [_SECOND, _FIRST]

    def test_a_table_the_schema_does_not_know_is_ignored(self):
        expression = f"SUM(NoSuchTableAnywhere.Amount) + SUM({_A}.Qty)"

        assert expression_tables(expression) == [_FIRST]

    def test_only_unknown_tables_give_nothing(self):
        assert expression_tables("SUM(NoSuchTableAnywhere.Amount)") == []

    def test_bracketed_names_resolve_like_plain_ones(self):
        assert expression_tables(f"SUM([{_A}].[Amount])") == [_FIRST]

    def test_a_column_without_a_table_adds_nothing(self):
        assert expression_tables("SUM(Amount)") == []

    @pytest.mark.parametrize("expression", ["SUM(", "SUM(A.B))", "1 +"])
    def test_an_unparseable_expression_gives_nothing_and_warns(self, expression, caplog):
        with caplog.at_level(logging.WARNING, logger="retrieval.metric_retriever"):
            assert expression_tables(expression) == []

        assert any("could not be parsed" in r.getMessage() for r in caplog.records)


    def test_an_empty_expression_gives_nothing(self):
        assert expression_tables("") == []


class TestMentionedMetrics:
    def test_a_key_is_a_mention(self):
        assert mentioned_metrics("show the volume per month", _METRICS) == ["volume"]

    def test_an_underscored_key_matches_as_words(self):
        assert mentioned_metrics("average price by month", _METRICS) == ["average_price"]
        assert mentioned_metrics("average_price by month", _METRICS) == ["average_price"]

    def test_an_alias_is_a_mention(self):
        assert mentioned_metrics("what was the total value in 2023?", _METRICS) == ["total_value"]

    def test_matching_ignores_case_in_the_question_and_the_alias(self):
        assert mentioned_metrics("SUM OF VALUE please", _METRICS) == ["total_value"]
        assert mentioned_metrics("sum of value please", _METRICS) == ["total_value"]
        assert mentioned_metrics("TOTAL VALUE", _METRICS) == ["total_value"]

    def test_a_longer_word_containing_the_name_is_not_a_mention(self):
        assert mentioned_metrics("the volumetric survey", _METRICS) == []
        assert mentioned_metrics("revolume", _METRICS) == []
        assert mentioned_metrics("total values of the set", _METRICS) == []

    def test_a_persian_alias_matches_as_a_whole_word(self):
        assert mentioned_metrics("مجموع ارزش معاملات سال ۱۴۰۲", _METRICS) == ["total_value"]
        assert mentioned_metrics("مجموع ارزشمند", _METRICS) == []

    def test_arabic_letter_variants_match_their_persian_form(self):
        arabic_form = "قيمت ميانگين"  # Arabic yeh, where the alias has the Persian one

        assert arabic_form != "قیمت میانگین"
        assert mentioned_metrics(arabic_form + " معاملات", _METRICS) == ["average_price"]

    def test_several_metrics_come_back_in_configuration_order(self):
        question = "volume and average price and total value"

        assert mentioned_metrics(question, _METRICS) == ["total_value", "average_price", "volume"]

    def test_no_mention_gives_nothing(self):
        assert mentioned_metrics("how many customers are there?", _METRICS) == []
        assert mentioned_metrics("volume", {}) == []

    def test_the_configured_metrics_are_the_default(self, monkeypatch):
        import retrieval.metric_retriever as module

        monkeypatch.setattr(module, "METRICS", _METRICS)

        assert mentioned_metrics("average price") == ["average_price"]


class TestMetricTables:
    def test_the_tables_of_every_mentioned_metric_are_returned(self):
        assert metric_tables("average price", _METRICS) == [_SECOND]
        assert metric_tables("average price and total value", _METRICS) == [_FIRST, _SECOND]

    def test_a_table_shared_by_two_metrics_appears_once(self):
        assert metric_tables("total value and volume", _METRICS) == [_FIRST]

    def test_an_unmentioned_metric_contributes_nothing(self):
        assert metric_tables("how many customers are there?", _METRICS) == []

    def test_a_metric_with_a_broken_expression_does_not_hide_the_others(self):
        metrics = {
            **_METRICS,
            "broken": {"aliases": ["broken one"], "expression": "SUM("},
        }

        assert metric_tables("broken one and average price", metrics) == [_SECOND]
