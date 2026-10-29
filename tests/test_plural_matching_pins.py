# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pins: how a question is matched to tables and data sources today.

Matching a question against table aliases, ``always_include`` trigger phrases,
table descriptions and data-source keywords is about to learn one more thing: a
word with an attached Persian plural suffix (``تالارها``) also counts as its
stem (``تالار``). That can only add matches. These tests describe the matching
as it is for every question that has no such word, and for the words that look
like a plural but are not (``تهران``, ``ایران``, ``ها`` on its own); they were
written against the code as it stood before the change and must keep passing,
byte for byte, after it. They also pin the helpers the new code sits next to
(``_stem``, ``_tokenize``) and the whole-word rule of the data-source keywords.

The corpus is a small Persian schema built in the tests: four tables with
descriptions and columns, trigger phrases and two synonyms, patched into
:mod:`schema_data.retriever`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from retrieval.source_selector import keyword_hits
from retrieval.table_evidence import alias_hits
from schema_data import retriever as r

TABLES = {
    "Hall": "ref.Hall — تالار معاملاتی کالا",
    "Customer": "sales.Customer — مشتری خریدار",
    "Order": "sales.Order — سفارش خرید",
    "City": "ref.City — شهر تهران ایران",
    "Broker": "ref.Broker — کارگزار فروشنده",
}
COLUMNS = {
    "Order": {"ID": "k", "TotalAmount": "مبلغ کل سفارش", "NetWeight": "وزن خالص"},
    "Hall": {"ID": "k", "Name": "نام", "Capacity": "ظرفیت تالار"},
    "City": {"ID": "k", "Name": "نام شهر"},
    "Broker": {"ID": "k", "Name": "نام کارگزار"},
}
INCLUDE = {"Hall": ["تالار", "سالن معاملات"], "City": ["شهر تهران"]}
SYNONYMS = {"خریدار": ["مشتری"], "بازار": ["تالار"]}

FORCED = 1000000000.0


@pytest.fixture(autouse=True)
def _persian_corpus():
    normalised = {t: [" ".join(r._tokenize(s)) for s in signals] for t, signals in INCLUDE.items()}
    with patch.object(r, "TABLES", TABLES), patch.object(r, "TABLE_COLUMNS", COLUMNS), patch.object(
        r, "_ALWAYS_INCLUDE_NORMALISED", normalised,
    ), patch.object(r, "SYNONYMS", SYNONYMS):
        r._build_idf.cache_clear()
        r._column_index.cache_clear()
        yield
    r._build_idf.cache_clear()
    r._column_index.cache_clear()


def ranked(question: str) -> list[tuple[str, float]]:
    return [(name, round(score, 6)) for name, score in r.rank_tables(question)]


class TestTheRankingOfAQuestionWithoutAnAttachedPlural:
    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("تعداد تالار", [("Hall", FORCED)]),
            ("تالار کالا", [("Hall", FORCED)]),
            ("مشتری خریدار", [("Customer", 2.229776)]),
            ("شهر تهران", [("City", FORCED)]),
            ("تهران شهر", [("City", FORCED)]),
            ("سفارش خرید در شهر", [("Order", 2.27559), ("City", 0.465537)]),
            ("مجموع ایران و تهران", [("City", 0.839445)]),
            ("انتها و ابتدا", []),
            ("مبلغ کل سفارش", [("Order", 0.753726)]),
            ("وزن خالص", [("Order", 0.137444)]),
            ("ظرفیت تالار", [("Hall", FORCED)]),
            ("بازار", [("Hall", FORCED)]),
            ("سالن معاملات کالا", [("Hall", FORCED)]),
            ("کارگزار فروشنده", [("Broker", 2.27559)]),
            ("xyzzy foobar", []),
            ("مشتری", [("Customer", 0.524653)]),
            ("سفارش", [("Order", 0.570468)]),
            ("کارگزار", [("Broker", 0.570468)]),
            ("شهر ایران", [("City", 0.885259)]),
            ("مبلغ سفارش مشتری", [("Order", 0.616282), ("Customer", 0.524653)]),
            ("نام کارگزار", [("Broker", 0.616282)]),
        ],
    )
    def test_the_ranked_tables_and_their_scores_are_unchanged(self, question, expected):
        assert ranked(question) == expected

    def test_the_forced_tables_are_unchanged(self):
        assert r.forced_tables("سالن معاملات کالا") == ["Hall"]
        assert r.forced_tables("تهران شهر") == ["City"]
        assert r.forced_tables("تالارکالا") == ["Hall"]
        assert r.forced_tables("سفارش خرید") == []

    def test_the_expansion_is_unchanged(self):
        assert r._expand("مشتری خریدار") == "مشتری خریدار مشتری"
        assert r._expand("بازار") == "بازار تالار"
        assert r._expand("تالار") == "تالار"


class TestWordsThatLookLikeAPluralAreNotOne:
    @pytest.mark.parametrize(
        ("word", "expected"),
        [
            ("تهران", [("City", 0.419722)]),
            ("ایران", [("City", 0.419722)]),
            ("انتها", []),
            ("ابتدا", []),
            ("بها", []),
            ("ها", []),
            ("های", []),
        ],
    )
    def test_a_lone_word_ranks_the_tables_it_did(self, word, expected):
        assert ranked(word) == expected

    def test_the_tokens_of_these_words_are_themselves(self):
        for word in ("تهران", "ایران", "انتها", "ها", "های"):
            assert r._tokenize(word) == [word]


class TestTheHelpersNextToThePluralMatching:
    def test_tokenize_keeps_an_attached_plural_as_one_word(self):
        assert r._tokenize("تالارها") == ["تالارها"]
        assert r._tokenize("تالار‌ها") == ["تالارها"]
        assert r._tokenize("تالار ها") == ["تالار", "ها"]

    def test_stem_strips_only_the_two_persian_suffixes_it_always_did(self):
        assert [r._stem(t) for t in ("تالارها", "تالارهای", "مشتریها", "مشتریهای")] == [
            "تالار", "تالار", "مشتری", "مشتری",
        ]
        assert [r._stem(t) for t in ("تالارهایی", "تالارهایش", "تهران", "ایران", "ها", "بها", "انتها")] == [
            "تالارهایی", "تالارهایش", "تهران", "ایران", "ها", "بها", "انت",
        ]
        assert [r._stem(t) for t in ("customers", "status", "address")] == ["customer", "status", "address"]


class TestAliasHitsToday:
    ALIASES = {"Hall": ["تالار"], "Pair": ["تالار کالا"], "Customer": ["مشتری"]}

    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("تالار", ["Hall"]),
            ("تالار ها", ["Hall"]),
            ("تالارها", ["Hall"]),
            ("تالار کالا", ["Hall", "Pair"]),
            ("کالای تالار", ["Hall"]),
            ("مشتریان", ["Customer"]),
            ("تهران", []),
            ("ایران و ها", []),
            ("", []),
        ],
    )
    def test_the_hits_are_unchanged(self, question, expected):
        assert alias_hits(question, self.ALIASES) == expected

    def test_a_multi_word_alias_with_a_plural_the_stem_rule_already_handles(self):
        assert alias_hits("تالارهای کالا", {"Pair": ["تالار کالا"]}) == ["Pair"]
        assert alias_hits("تالار‌ها کالا", {"Pair": ["تالار کالا"]}) == ["Pair"]


class TestDataSourceKeywordsToday:
    KEYWORDS = {"market": ["تالار"], "stock": ["انبار"], "phrase": ["تالار کالا"]}

    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("تالار", {"market": 1, "stock": 0, "phrase": 0}),
            ("در تالار کالا چقدر", {"market": 1, "stock": 0, "phrase": 1}),
            ("تالار ها", {"market": 1, "stock": 0, "phrase": 0}),
            ("تالار، انبار", {"market": 1, "stock": 1, "phrase": 0}),
            ("تهران", {"market": 0, "stock": 0, "phrase": 0}),
            ("تالاری", {"market": 0, "stock": 0, "phrase": 0}),
            ("", {"market": 0, "stock": 0, "phrase": 0}),
        ],
    )
    def test_the_whole_word_rule_is_what_it_was(self, question, expected):
        assert keyword_hits(question, self.KEYWORDS) == expected

    def test_a_latin_keyword_is_still_a_whole_word(self):
        assert keyword_hits("stockholders", {"inventory": ["stock"]}) == {"inventory": 0}
        assert keyword_hits("the stock level", {"inventory": ["stock"]}) == {"inventory": 1}
        assert keyword_hits("stocks", {"inventory": ["stock"]}) == {"inventory": 0}
