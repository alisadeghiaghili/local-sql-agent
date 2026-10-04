# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for ``retrieval.source_selector``.

The selector is pure: the configuration (sources, keywords, table-to-source
sets) is passed in, so these tests need no ``datasources.yaml`` and no
schema. The tables are generic: ``sales_fact`` lives in ``sales``,
``stock_dim`` in ``inventory``, ``old_fact`` in ``archive`` and
``shared_dim`` (a replicated dimension) in ``sales`` and ``inventory``.
"""

from __future__ import annotations

import pytest

from core.models import RetrievalContext
from retrieval.source_selector import (
    REASON_DEFAULT,
    REASON_KEYWORD,
    REASON_RETRIEVAL,
    REASON_SESSION,
    SourceSelection,
    keyword_hits,
    retrieval_scores,
    select_source,
    select_source_for_question,
)
from tests._source_fixtures import configured_sources

SOURCES = ("sales", "inventory", "archive")

TABLE_SETS = {
    "sales_fact": ("sales",),
    "stock_dim": ("inventory",),
    "old_fact": ("archive",),
    "shared_dim": ("sales", "inventory"),
}


def _select(question="plain question", context=None, *, keywords=None, previous=None):
    return select_source(
        question,
        context if context is not None else RetrievalContext(),
        sources=SOURCES,
        keywords=keywords or {},
        table_sets=TABLE_SETS,
        previous_source=previous,
    )


class TestKeywordSignal:
    def test_a_keyword_of_one_source_picks_it(self):
        sel = _select("show the stock level", keywords={"inventory": ["stock level"]})
        assert (sel.chosen, sel.reason) == ("inventory", REASON_KEYWORD)

    def test_the_source_with_more_distinct_matches_wins(self):
        kw = {"sales": ["revenue"], "inventory": ["stock", "bin"]}
        sel = _select("revenue by stock bin", keywords=kw)
        assert (sel.chosen, sel.reason) == ("inventory", REASON_KEYWORD)

    def test_a_phrase_counts_once_however_often_it_occurs(self):
        kw = {"sales": ["revenue"], "inventory": ["stock", "bin"]}
        sel = _select("revenue revenue revenue and stock", keywords=kw)
        # sales has 1 distinct hit, inventory 1: a tie, not a sales win.
        assert sel.reason != REASON_KEYWORD

    def test_keywords_beat_retrieval_evidence(self):
        ctx = RetrievalContext(facts=["sales_fact"])
        sel = _select("stock level", ctx, keywords={"inventory": ["stock level"]})
        assert (sel.chosen, sel.reason) == ("inventory", REASON_KEYWORD)

    def test_keywords_beat_session_continuity(self):
        sel = _select("stock level", keywords={"inventory": ["stock level"]}, previous="sales")
        assert (sel.chosen, sel.reason) == ("inventory", REASON_KEYWORD)

    def test_no_match_falls_through(self):
        sel = _select("nothing relevant", keywords={"inventory": ["stock level"]})
        assert sel.reason == REASON_DEFAULT

    def test_a_source_missing_from_the_keyword_map_has_no_keywords(self):
        sel = _select("revenue", keywords={"sales": ["revenue"]})
        assert (sel.chosen, sel.reason) == ("sales", REASON_KEYWORD)


class TestKeywordTies:
    KW = {"sales": ["revenue"], "inventory": ["stock"]}

    def test_a_tie_is_not_a_keyword_decision(self):
        sel = _select("revenue and stock", keywords=self.KW)
        assert sel.reason != REASON_KEYWORD

    def test_a_tie_goes_to_the_previous_source_if_it_is_among_the_leaders(self):
        sel = _select("revenue and stock", keywords=self.KW, previous="inventory")
        assert (sel.chosen, sel.reason) == ("inventory", REASON_SESSION)

    def test_a_previous_source_outside_the_tie_does_not_win_it(self):
        sel = _select("revenue and stock", keywords=self.KW, previous="archive")
        assert sel.chosen in ("sales", "inventory")
        assert sel.reason != REASON_SESSION

    def test_a_tie_is_broken_by_retrieval_among_the_leaders_only(self):
        # archive has the strongest retrieval evidence but no keyword: it
        # is not among the tied leaders and must not win.
        ctx = RetrievalContext(entities=["old_fact", "stock_dim"])
        sel = _select("revenue and stock", ctx, keywords=self.KW)
        assert (sel.chosen, sel.reason) == ("inventory", REASON_RETRIEVAL)

    def test_a_tie_with_no_evidence_takes_the_first_leader_in_configuration_order(self):
        sel = _select("revenue and stock", keywords=self.KW)
        assert (sel.chosen, sel.reason) == ("sales", REASON_DEFAULT)


class TestSessionSignal:
    def test_a_follow_up_keeps_the_previous_source(self):
        sel = _select("and last year?", previous="inventory")
        assert (sel.chosen, sel.reason) == ("inventory", REASON_SESSION)

    def test_session_beats_retrieval(self):
        ctx = RetrievalContext(facts=["sales_fact"])
        sel = _select("and last year?", ctx, previous="inventory")
        assert (sel.chosen, sel.reason) == ("inventory", REASON_SESSION)

    def test_an_unknown_previous_source_is_ignored(self):
        ctx = RetrievalContext(facts=["sales_fact"])
        sel = _select("question", ctx, previous="retired")
        assert (sel.chosen, sel.reason) == ("sales", REASON_RETRIEVAL)

    def test_no_previous_turn_means_no_session_signal(self):
        assert _select("question", previous=None).reason == REASON_DEFAULT


class TestRetrievalSignal:
    def test_selected_tables_score_for_their_source(self):
        sel = _select(context=RetrievalContext(entities=["stock_dim"]))
        assert (sel.chosen, sel.reason) == ("inventory", REASON_RETRIEVAL)

    def test_facts_count_like_entities(self):
        sel = _select(context=RetrievalContext(facts=["old_fact"]))
        assert (sel.chosen, sel.reason) == ("archive", REASON_RETRIEVAL)

    def test_more_evidence_wins(self):
        # One table for each of three sources: nothing to separate them.
        ctx = RetrievalContext(entities=["stock_dim"], facts=["sales_fact", "old_fact"])
        assert retrieval_scores(ctx, TABLE_SETS, SOURCES) == {
            "sales": 1.0, "inventory": 1.0, "archive": 1.0,
        }
        # A matched warehouse value in a table of one of them breaks the tie.
        ctx = RetrievalContext(
            entities=["stock_dim"], facts=["sales_fact"],
            resolved_values={"stock_dim.Name": ["x"]},
        )
        sel = _select(context=ctx)
        assert (sel.chosen, sel.reason) == ("inventory", REASON_RETRIEVAL)

    def test_resolved_values_score_through_their_table(self):
        ctx = RetrievalContext(resolved_values={"old_fact.Name": ["x"]})
        sel = _select(context=ctx)
        assert (sel.chosen, sel.reason) == ("archive", REASON_RETRIEVAL)

    def test_a_resolved_key_without_a_column_part_is_a_table(self):
        ctx = RetrievalContext(resolved_values={"old_fact": ["x"]})
        assert retrieval_scores(ctx, TABLE_SETS, SOURCES)["archive"] == 1.0

    def test_a_qualified_table_key_is_found_before_the_column_is_split_off(self):
        sets = {"ref.Region": ("inventory",)}
        ctx = RetrievalContext(resolved_values={"ref.Region.Name": ["x"], "ref.Region": ["y"]})
        assert retrieval_scores(ctx, sets, SOURCES)["inventory"] == 2.0

    def test_a_shared_table_counts_for_each_of_its_sources_with_lower_weight(self):
        scores = retrieval_scores(RetrievalContext(entities=["shared_dim"]), TABLE_SETS, SOURCES)
        assert scores == {"sales": 0.5, "inventory": 0.5, "archive": 0.0}

    def test_a_shared_table_does_not_outweigh_an_exclusive_one(self):
        ctx = RetrievalContext(entities=["shared_dim"], facts=["sales_fact"])
        sel = _select(context=ctx)
        assert (sel.chosen, sel.reason) == ("sales", REASON_RETRIEVAL)

    def test_only_shared_tables_is_a_tie_and_the_default_decides(self):
        sel = _select(context=RetrievalContext(entities=["shared_dim"]))
        assert (sel.chosen, sel.reason) == ("sales", REASON_DEFAULT)

    def test_a_tie_between_exclusive_tables_takes_the_first_in_configuration_order(self):
        ctx = RetrievalContext(entities=["stock_dim"], facts=["old_fact"])
        sel = _select(context=ctx)
        assert (sel.chosen, sel.reason) == ("inventory", REASON_DEFAULT)

    def test_unknown_tables_are_ignored(self):
        ctx = RetrievalContext(entities=["nowhere"], resolved_values={"nowhere.Col": ["x"]})
        assert retrieval_scores(ctx, TABLE_SETS, SOURCES) == {s: 0.0 for s in SOURCES}

    def test_a_context_without_the_attributes_scores_nothing(self):
        assert retrieval_scores(object(), TABLE_SETS, SOURCES) == {s: 0.0 for s in SOURCES}


class TestDefaultAndCandidates:
    def test_nothing_decides_so_the_default_source_is_chosen(self):
        sel = _select()
        assert (sel.chosen, sel.reason) == ("sales", REASON_DEFAULT)

    def test_candidates_list_every_source_chosen_first(self):
        sel = _select(context=RetrievalContext(facts=["old_fact"]))
        assert sel.candidates[0] == "archive"
        assert sorted(sel.candidates) == sorted(SOURCES)

    def test_the_rest_follow_by_strength_of_evidence_then_configuration_order(self):
        ctx = RetrievalContext(entities=["stock_dim", "shared_dim"], facts=["sales_fact"])
        # inventory 1.5, sales 1.5, archive 0 -> inventory/sales tie, config order.
        sel = _select(context=ctx)
        assert sel.candidates == ("sales", "inventory", "archive")
        sel = _select("revenue", ctx, keywords={"archive": ["revenue"]})
        assert sel.candidates == ("archive", "sales", "inventory")

    def test_the_selection_is_deterministic(self):
        ctx = RetrievalContext(entities=["stock_dim"], facts=["sales_fact"])
        assert _select("q", ctx) == _select("q", ctx)

    def test_no_sources_is_refused(self):
        with pytest.raises(ValueError, match="no data source"):
            select_source("q", RetrievalContext(), sources=(), keywords={}, table_sets={})

    def test_one_source_selects_it(self):
        sel = select_source("q", RetrievalContext(), sources=("sales",), keywords={}, table_sets={})
        assert (sel.chosen, sel.candidates) == ("sales", ("sales",))


class TestSelectionObject:
    def test_after_walks_the_candidates(self):
        sel = SourceSelection("a", "default", ("a", "b", "c"))
        assert sel.after("a") == "b"
        assert sel.after("b") == "c"
        assert sel.after("c") is None
        assert sel.after("unknown") is None

    def test_audit_block_shape(self):
        sel = SourceSelection("a", "keyword", ("a", "b"))
        assert sel.audit() == {
            "chosen": "a", "reason": "keyword", "candidates": ["a", "b"], "fallback_from": None,
        }
        assert sel.audit(chosen="b", fallback_from="a") == {
            "chosen": "b", "reason": "keyword", "candidates": ["a", "b"], "fallback_from": "a",
        }


class TestKeywordNormalisation:
    def test_arabic_yeh_and_kaf_fold_to_the_persian_letters(self):
        # keyword typed with Arabic ي / ك, question with Persian ی / ک, and back.
        assert keyword_hits("آمار کارگزار", {"s": ["كارگزار"]}) == {"s": 1}
        assert keyword_hits("آمار كارگزار", {"s": ["کارگزار"]}) == {"s": 1}
        assert keyword_hits("وضعيت", {"s": ["وضعیت"]}) == {"s": 1}
        assert keyword_hits("وضعیت", {"s": ["وضعيت"]}) == {"s": 1}

    def test_zwnj_is_removed_on_both_sides(self):
        assert keyword_hits("می‌خواهم", {"s": ["میخواهم"]}) == {"s": 1}
        assert keyword_hits("میخواهم", {"s": ["می‌خواهم"]}) == {"s": 1}

    def test_digits_in_any_script_fold_to_ascii(self):
        assert keyword_hits("گزارش ۱۴۰۲", {"s": ["1402"]}) == {"s": 1}
        assert keyword_hits("گزارش 1402", {"s": ["۱۴۰۲"]}) == {"s": 1}
        assert keyword_hits("گزارش ١٤٠٢", {"s": ["1402"]}) == {"s": 1}

    def test_case_and_spacing_do_not_matter(self):
        assert keyword_hits("Show   the  STOCK   Level", {"s": ["stock level"]}) == {"s": 1}
        assert keyword_hits("stock level", {"s": ["  Stock   LEVEL "]}) == {"s": 1}

    def test_a_ligature_folds_like_its_letters(self):
        assert keyword_hits("ﷲ", {"s": ["الله"]}) == {"s": 1}

    def test_the_selector_applies_the_folding_end_to_end(self):
        sel = _select("آمار كارگزار", keywords={"inventory": ["کارگزار"]})
        assert (sel.chosen, sel.reason) == ("inventory", REASON_KEYWORD)


class TestWholePhraseMatching:
    def test_a_keyword_inside_a_longer_word_does_not_match(self):
        assert keyword_hits("stockholders", {"s": ["stock"]}) == {"s": 0}
        assert keyword_hits("restock", {"s": ["stock"]}) == {"s": 0}
        assert keyword_hits("stock_level", {"s": ["stock"]}) == {"s": 0}

    def test_punctuation_and_edges_are_boundaries(self):
        for question in ("stock", "stock?", "(stock)", "the stock, today", "stock-level", "a/stock/b"):
            assert keyword_hits(question, {"s": ["stock"]}) == {"s": 1}, question

    def test_a_persian_keyword_does_not_match_inside_a_longer_persian_word(self):
        assert keyword_hits("فروشنده", {"s": ["فروش"]}) == {"s": 0}
        assert keyword_hits("پیش فروش", {"s": ["فروش"]}) == {"s": 1}
        assert keyword_hits("فروش؟", {"s": ["فروش"]}) == {"s": 1}
        assert keyword_hits("فروش،", {"s": ["فروش"]}) == {"s": 1}

    def test_a_multi_word_phrase_must_match_all_its_words_in_order(self):
        assert keyword_hits("current stock level", {"s": ["stock level"]}) == {"s": 1}
        assert keyword_hits("level of stock", {"s": ["stock level"]}) == {"s": 0}
        assert keyword_hits("stock levels", {"s": ["stock level"]}) == {"s": 0}

    def test_a_phrase_with_regex_characters_is_literal(self):
        assert keyword_hits("what is c++ usage", {"s": ["c++"]}) == {"s": 1}
        assert keyword_hits("what is cc usage", {"s": ["c++"]}) == {"s": 0}
        assert keyword_hits("a.b", {"s": ["a.b"]}) == {"s": 1}
        assert keyword_hits("axb", {"s": ["a.b"]}) == {"s": 0}

    def test_every_source_gets_an_entry(self):
        assert keyword_hits("q", {"a": [], "b": ["q"]}) == {"a": 0, "b": 1}


class TestSelectForQuestionUsesTheConfiguration:
    def test_one_source_means_no_selection(self):
        # The suite's own configuration (no datasources.yaml) has one source.
        assert select_source_for_question("anything", RetrievalContext()) is None

    def test_several_sources_read_keywords_and_tables_from_the_configuration(self):
        with configured_sources(
            TABLE_SETS, keywords={"archive": ["old orders"]},
        ):
            sel = select_source_for_question("show old orders", RetrievalContext())
            assert sel is not None
            assert (sel.chosen, sel.reason) == ("archive", REASON_KEYWORD)

            sel = select_source_for_question(
                "q", RetrievalContext(entities=["stock_dim"]),
            )
            assert (sel.chosen, sel.reason) == ("inventory", REASON_RETRIEVAL)

            sel = select_source_for_question("q", RetrievalContext(), previous_source="archive")
            assert (sel.chosen, sel.reason) == ("archive", REASON_SESSION)
