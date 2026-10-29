# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A word with an attached Persian plural is matched as its stem, too.

``تالارها``, ``تالار‌های`` (with the half-space) and ``تالارهایی`` did not match what
``تالار`` or ``تالار ها`` (with a space) matched, wherever the question's tokens are
compared with something configured: the TF-IDF scorer, the ``always_include`` trigger
phrases, the synonyms, the column evidence, an alias of several words and the data-source
keywords. The stem is now an extra term next to the word. These tests cover that gain, and
the guarantees that come with it: a question without such a word is matched as it was
(``tests/test_plural_matching_pins.py`` pins the numbers), the stem is only ever added,
``ها`` alone and words that merely end like a plural gain nothing, and the matching
without the stems is recreated here (``without_stems``) to compare against.

The corpus is a small Persian schema patched into :mod:`schema_data.retriever`; the
generic example configuration has no Persian aliases.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest

from core.models import RetrievalContext
from retrieval import source_selector, table_evidence
from retrieval.source_selector import keyword_hits, select_source
from retrieval.table_evidence import alias_hits
from schema_data import retriever as r

HA = "ها"
HAYE = "های"
ZWNJ = "‌"

TABLES = {
    "Hall": "ref.Hall — تالار معاملاتی کالا",
    "Customer": "sales.Customer — مشتری خریدار",
    "Order": "sales.Order — سفارش خرید",
    "City": "ref.City — شهر تهران ایران",
    "Broker": "ref.Broker — کارگزار فروشنده",
    "Plain": "ref.Plain — مشتریها",
}
COLUMNS = {
    "Order": {"ID": "k", "TotalAmount": "مبلغ کل سفارش", "NetWeight": "وزن خالص"},
    "Hall": {"ID": "k", "Name": "نام", "Capacity": "ظرفیت تالار"},
    "City": {"ID": "k", "Name": "نام شهر"},
    "Broker": {"ID": "k", "Name": "نام کارگزار"},
}
INCLUDE = {"Hall": ["سالن معاملات"], "City": ["شهر تهران"]}
SYNONYMS = {"بازار": ["تالار"], "خریدار": ["مشتری"]}


@pytest.fixture(autouse=True)
def _persian_corpus():
    normalised = {t: [" ".join(r._tokenize(s)) for s in signals] for t, signals in INCLUDE.items()}
    with patch.object(r, "TABLES", TABLES), patch.object(r, "TABLE_COLUMNS", COLUMNS), patch.object(
        r, "_ALWAYS_INCLUDE_NORMALISED", normalised,
    ), patch.object(r, "SYNONYMS", SYNONYMS):
        r._build_idf.cache_clear()
        r._column_index.cache_clear()
        table_evidence._alias_form.cache_clear()
        yield
    r._build_idf.cache_clear()
    r._column_index.cache_clear()
    table_evidence._alias_form.cache_clear()


@contextlib.contextmanager
def without_stems():
    """The matching as it was before the stems: no word is ever reduced."""
    table_evidence._alias_form.cache_clear()
    with patch.multiple(
        r, strip_plural_suffix=lambda token: token, strip_plural_suffixes=lambda tokens: list(tokens),
    ), patch.multiple(
        table_evidence, strip_plural_suffixes=lambda tokens: list(tokens),
    ), patch.multiple(
        source_selector, strip_plural_suffix=lambda token: token,
    ):
        yield
    table_evidence._alias_form.cache_clear()


def names(question: str) -> list[str]:
    return [name for name, _score in r.rank_tables(question)]


class TestTheTfIdfScorer:
    @pytest.mark.parametrize("suffix", [HA, HAYE, HAYE + "ی", HAYE + "ش", HAYE + "شان"])
    def test_every_way_of_writing_the_plural_finds_the_table_the_stem_does(self, suffix):
        questions = {
            "attached": "مشتری" + suffix,
            "half-space": "مشتری" + ZWNJ + suffix,
            "space": "مشتری " + suffix,
            "stem": "مشتری",
        }
        for how, question in questions.items():
            assert "Customer" in names(question), how

    def test_it_was_the_attached_forms_that_missed(self):
        with without_stems():
            assert "Customer" in names("مشتری ها")
            assert "Customer" not in names("مشتریها")
            assert "Customer" not in names("مشتری" + ZWNJ + "های")
            assert "Customer" not in names("مشتریهایی")

    def test_ha_alone_matches_nothing(self):
        assert r.rank_tables(HA) == []
        assert r.rank_tables(HAYE) == []
        assert r.rank_tables(HA + " " + HA) == []

    def test_a_bigram_of_the_description_is_found_with_a_plural_inside_it(self):
        assert "Order" in names("سفارشهای خرید")
        assert dict(r.rank_tables("سفارشهای خرید"))["Order"] == pytest.approx(dict(r.rank_tables("سفارش خرید"))["Order"])

    def test_the_word_itself_still_counts(self):
        # the Plain table's description holds the attached plural itself
        with without_stems():
            before = dict(r.rank_tables("مشتریها"))
        after = dict(r.rank_tables("مشتریها"))
        assert after["Plain"] >= before["Plain"] > 0
        assert "Customer" in after and "Customer" not in before

    def test_the_stem_is_only_added(self):
        for question in (
            "مشتریها", "سفارشهای خرید", "تعداد کارگزارهایی", "شهرهای ایران", "تالارها و مشتریها",
            "مبلغ سفارشها", "ظرفیت تالارها", "خریدارها", "تهران و شهرهایش",
        ):
            with without_stems():
                before = dict(r.rank_tables(question))
            after = dict(r.rank_tables(question))
            assert set(before) <= set(after), question
            for table, score in before.items():
                assert after[table] >= score - 1e-12, (question, table)

    def test_a_question_without_an_attached_plural_is_scored_as_it_was(self):
        for question in (
            "مشتری خریدار", "سفارش خرید در شهر", "مجموع ایران و تهران", "انتها و ابتدا", "تالار ها",
            "customers and suppliers", "xyzzy foobar", "شهر تهران", "سالن معاملات کالا",
        ):
            with without_stems():
                before = r.rank_tables(question)
            assert r.rank_tables(question) == before, question

    @pytest.mark.parametrize("word", ["تهران", "ایران", "ها", "بها", "حیات", "امکانات", "مشتریان"])
    def test_a_word_that_only_looks_like_a_plural_gains_nothing(self, word):
        with without_stems():
            before = r.rank_tables(word)
        assert r.rank_tables(word) == before


class TestTheTriggerPhrases:
    @pytest.mark.parametrize(
        ("question", "forced"),
        [
            ("سالنهای معاملات", True),
            ("سالن" + ZWNJ + "های معاملات", True),
            ("سالنهایی معاملات", True),
            ("سالن ها معاملات", True),
            ("سالن معاملات", True),
            ("معاملات در سالنهای بزرگ", True),
            ("سالنها و معاملاتی", False),
            ("سالن", False),
            ("تهران", False),
            ("معاملات " + HA, False),
        ],
    )
    def test_a_phrase_of_several_words_is_found_with_a_plural_in_it(self, question, forced):
        assert ("Hall" in r.forced_tables(question)) is forced

    def test_it_was_the_attached_forms_that_missed(self):
        with without_stems():
            assert "Hall" in r.forced_tables("سالن ها معاملات")
            assert "Hall" not in r.forced_tables("سالنهای معاملات")

    def test_a_word_that_only_looks_like_a_plural_forces_nothing(self):
        assert r.forced_tables("تهران") == []
        assert r.forced_tables("ایران شهر") == []

    def test_a_question_without_a_plural_forces_what_it_did(self):
        assert r.forced_tables("شهر تهران") == ["City"]
        assert r.forced_tables("سالن معاملات کالا") == ["Hall"]
        assert r.forced_tables("سفارش خرید") == []


class TestTheSynonyms:
    def test_a_synonym_key_is_found_under_the_stem(self):
        assert r._expand("بازارها") == "بازارها تالار"
        assert r._expand("بازار" + ZWNJ + "های") == "بازار" + ZWNJ + "های تالار"
        assert r._expand("بازار") == "بازار تالار"
        assert r._expand("بازار ها") == "بازار ها تالار"

    def test_the_expansion_makes_the_table_a_candidate(self):
        assert "Hall" in names("بازارها")
        with without_stems():
            assert "Hall" not in names("بازارها")

    def test_a_question_without_an_attached_plural_is_expanded_as_it_was(self):
        questions = ("خریدار", "تعداد تالار", "بازار و خریدار", "ایران", "totals in 2025")
        with without_stems():
            before = [r._expand(q) for q in questions]
        assert [r._expand(q) for q in questions] == before


class TestTheColumnEvidence:
    def test_a_plural_the_old_stem_rule_does_not_know_still_finds_the_column(self):
        # "مبلغ" is only in Order's column description; the old rule knew ها and های, not هایی
        with without_stems():
            assert "Order" not in r._column_scores(["مبلغهایی"])
        assert "Order" in r._column_scores(["مبلغهایی"])

    def test_the_old_forms_keep_their_score(self):
        for tokens in (["مبلغ"], ["مبلغها"], ["مبلغهای"], ["وزن"], ["xyzzy"], ["name"], ["amounts"]):
            with without_stems():
                before = r._column_scores(tokens)
            assert r._column_scores(tokens) == before, tokens


class TestAnAliasOfSeveralWords:
    ALIASES = {"Pair": ["تالار کالا"], "One": ["مشتری"]}

    @pytest.mark.parametrize(
        "question",
        [
            "تالارها کالا", "تالار" + ZWNJ + "های کالا", "تالارهایی کالا", "تالارهایش کالا", "تالارهایشان کالا",
            "کالا و تالارهایی", "تالار ها کالا", "تالار کالا",
        ],
    )
    def test_every_way_of_writing_the_plural_is_a_hit(self, question):
        assert "Pair" in alias_hits(question, self.ALIASES)

    @pytest.mark.parametrize("question", ["تالاری کالا", "تهران", "ایران و کالا", "تالار", "کالا", "ها", "کالاها"])
    def test_what_is_not_the_alias_is_not_a_hit(self, question):
        assert "Pair" not in alias_hits(question, self.ALIASES)

    def test_it_was_the_unfamiliar_endings_that_missed(self):
        with without_stems():
            assert "Pair" in alias_hits("تالارهای کالا", self.ALIASES)
            assert "Pair" not in alias_hits("تالارهایی کالا", self.ALIASES)
            assert "Pair" not in alias_hits("تالارهایش کالا", self.ALIASES)

    def test_an_alias_written_as_a_plural_is_found_by_the_singular_and_the_reverse(self):
        assert alias_hits("تالار کالا", {"Pair": ["تالارهایی کالا"]}) == ["Pair"]
        assert alias_hits("تالارها کالا", {"Pair": ["تالارهایی کالا"]}) == ["Pair"]

    def test_the_hits_of_a_question_without_a_plural_are_unchanged(self):
        for question in ("تالار کالا", "مشتریان", "تهران", "تالار", "customer", "کالای تالار"):
            with without_stems():
                before = alias_hits(question, self.ALIASES)
            assert alias_hits(question, self.ALIASES) == before, question

    def test_the_entity_retriever_names_the_table(self):
        from retrieval.entity_retriever import EntityRetriever

        entities = {"Ring": {"aliases": ["تالار کالا"]}}
        for question in ("حجم تالارهایی کالا", "حجم تالار" + ZWNJ + "های کالا", "حجم تالار کالا", "حجم تالار ها کالا"):
            with patch("retrieval.entity_retriever.ENTITIES", entities):
                assert EntityRetriever.retrieve(question)[0] == "Ring", question


class TestDataSourceKeywords:
    KEYWORDS = {"market": ["تالار"], "stock": ["انبار"], "phrase": ["تالار کالا"]}

    @pytest.mark.parametrize("suffix", [HA, HAYE, HAYE + "ی", HAYE + "ش", HAYE + "شان"])
    def test_a_keyword_matches_its_word_with_an_attached_plural(self, suffix):
        assert keyword_hits("تالار" + suffix, self.KEYWORDS) == {"market": 1, "stock": 0, "phrase": 0}
        assert keyword_hits("تالار" + ZWNJ + suffix, self.KEYWORDS) == {"market": 1, "stock": 0, "phrase": 0}
        assert keyword_hits("حجم انبار" + suffix + " امروز", self.KEYWORDS) == {"market": 0, "stock": 1, "phrase": 0}

    def test_it_was_the_attached_forms_that_missed(self):
        with without_stems():
            assert keyword_hits("تالار ها", self.KEYWORDS)["market"] == 1
            assert keyword_hits("تالارها", self.KEYWORDS)["market"] == 0
            assert keyword_hits("تالار" + ZWNJ + "ها", self.KEYWORDS)["market"] == 0

    def test_a_phrase_matches_with_a_plural_in_it(self):
        assert keyword_hits("تالارهای کالا", self.KEYWORDS) == {"market": 1, "stock": 0, "phrase": 1}
        assert keyword_hits("تالار کالاها", self.KEYWORDS) == {"market": 1, "stock": 0, "phrase": 1}

    def test_the_whole_word_rule_still_holds(self):
        assert keyword_hits("تالاری", self.KEYWORDS) == {"market": 0, "stock": 0, "phrase": 0}
        assert keyword_hits("تهران", self.KEYWORDS) == {"market": 0, "stock": 0, "phrase": 0}
        assert keyword_hits("ها", self.KEYWORDS) == {"market": 0, "stock": 0, "phrase": 0}
        assert keyword_hits("stockholders stocks", {"inventory": ["stock"]}) == {"inventory": 0}

    def test_a_keyword_counts_once_however_it_is_written(self):
        assert keyword_hits("تالار تالارها تالارهای", self.KEYWORDS)["market"] == 1

    def test_the_hits_of_a_question_without_a_plural_are_unchanged(self):
        for question in ("تالار", "در تالار کالا", "تالار ها", "تالار، انبار", "تهران", "stock level", ""):
            with without_stems():
                before = keyword_hits(question, self.KEYWORDS)
            assert keyword_hits(question, self.KEYWORDS) == before, question

    def test_the_selector_picks_the_source_for_a_plural_question(self):
        selection = select_source(
            "حجم تالارها", RetrievalContext(), sources=("sales", "inventory"),
            keywords={"inventory": ["تالار"], "sales": ["فروش"]},
            table_sets={},
        )
        assert (selection.chosen, selection.reason) == ("inventory", "keyword")
        with without_stems():
            selection = select_source(
                "حجم تالارها", RetrievalContext(), sources=("sales", "inventory"),
                keywords={"inventory": ["تالار"], "sales": ["فروش"]},
                table_sets={},
            )
        assert selection.reason == "default"
