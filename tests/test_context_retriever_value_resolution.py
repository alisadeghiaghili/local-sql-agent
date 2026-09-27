# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for retrieval/context_retriever.py's Phase 5b wiring.

Covers the redesigned "Wiring" section: ``ContextRetriever.retrieve``
extends ``RetrievalContext.filters`` from the *prefetched-vocabulary* match
(``retrieval.dimension_vocabulary``), but only for entity tables the static
``ValueRetriever`` pass left unresolved (the static path always wins when
it matches), and a tied match surfaces as a ``value_clarifications`` entry
rather than ever being folded into ``filters``.

``EntityRetriever``/``ValueRetriever`` are patched to fixed return values so
these tests exercise only the merge/precedence logic in
``ContextRetriever.retrieve`` itself, not real alias-file content. The
vocabulary cache is warmed directly via ``refresh_vocabulary`` with an
injected ``execute_fn`` — no live database anywhere in this file.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from retrieval.context_retriever import ContextRetriever
from retrieval.dimension_vocabulary import clear_vocabulary_cache, refresh_vocabulary


@pytest.fixture(autouse=True)
def _clean_vocabulary_cache():
    clear_vocabulary_cache()
    yield
    clear_vocabulary_cache()


def _patch_retrievers(*, entities, static_filters):
    return (
        patch("retrieval.context_retriever.EntityRetriever.retrieve", return_value=entities),
        patch("retrieval.context_retriever.FactRetriever.retrieve", return_value=[]),
        patch("retrieval.context_retriever.ValueRetriever.retrieve", return_value=dict(static_filters)),
    )


def _warm(table: str, column: str, values: list[str]) -> None:
    def execute_fn(sql, params):
        return pd.DataFrame({column: values})

    refresh_vocabulary(table, column, execute_fn=execute_fn)


class TestValueResolutionWiring:
    def test_matched_vocabulary_extends_filters(self):
        _warm("Ring", "Name", ["تالار محصولات صنعتی", "تالار پتروشیمی"])

        patches = _patch_retrievers(entities=["Ring"], static_filters={})
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve("قیمت در تالار محصولات صنعتی چقدر بود")

        assert ctx.filters == {"Ring": "تالار محصولات صنعتی"}
        assert ctx.value_clarifications == []

    def test_static_alias_wins_and_vocabulary_is_never_consulted_for_that_table(self):
        # A deliberately WRONG cached value proves the static filter's
        # precedence: if the vocabulary path were consulted for "Ring" at
        # all, this wrong value would win and the assertion would fail.
        _warm("Ring", "Name", ["should never be reached"])

        patches = _patch_retrievers(
            entities=["Ring"], static_filters={"Ring": "تالار پتروشیمی"},
        )
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve("تالار پتروشیمی")

        assert ctx.filters == {"Ring": "تالار پتروشیمی"}

    def test_tied_vocabulary_match_populates_value_clarifications_not_filters(self):
        _warm("Currency", "PersianName", ["دلار آمریکا", "دلار کانادا"])

        patches = _patch_retrievers(entities=["Currency"], static_filters={})
        with patches[0], patches[1], patches[2]:
            # Neither cached value is a substring of the other, so both
            # match at their own (equal) length -- a genuine tie.
            ctx = ContextRetriever.retrieve("نرخ دلار آمریکا و دلار کانادا")

        assert ctx.filters == {}
        assert len(ctx.value_clarifications) == 1
        assert set(ctx.value_clarifications[0].options) == {"دلار آمریکا", "دلار کانادا"}

    def test_no_entities_never_consults_the_vocabulary_at_all(self):
        _warm("Ring", "Name", ["تالار پتروشیمی"])

        patches = _patch_retrievers(entities=[], static_filters={})
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve("سلام")

        assert ctx.filters == {}
        assert ctx.value_clarifications == []

    def test_cold_cache_leaves_the_pipeline_unaffected(self):
        """A table never warmed (or a table not in PREFETCH_COLUMNS at
        all, e.g. Customer) contributes nothing -- the pipeline still
        produces a context, no exception, no block."""
        patches = _patch_retrievers(entities=["Customer"], static_filters={})
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve("مشتری فولاد مبارکه چند قرارداد دارد؟")

        assert ctx.filters == {}
        assert ctx.value_clarifications == []
        assert ctx.entities == ["Customer"]

    def test_two_dimensions_in_one_question_both_resolve(self):
        _warm("Ring", "Name", ["تالار محصولات صنعتی", "تالار پتروشیمی"])
        _warm("Symbol", "Commodity_PersianName", ["فولاد مبارکه"])
        _warm("Symbol", "Commodity_Symbol", [])

        patches = _patch_retrievers(entities=["Ring", "Symbol"], static_filters={})
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve(
                "گرانترین معامله فولاد مبارکه در تالار محصولات صنعتی چقدر بود"
            )

        assert ctx.filters == {
            "Ring": "تالار محصولات صنعتی",
            "Symbol": "فولاد مبارکه",
        }

    def test_denied_column_excludes_that_dimension_from_matching(self):
        from security.auth import Principal

        _warm("Ring", "Name", ["تالار محصولات صنعتی"])
        principal = Principal(id="p1", name="P1", denied_columns=("Name",))

        patches = _patch_retrievers(entities=["Ring"], static_filters={})
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve(
                "قیمت در تالار محصولات صنعتی چقدر بود", principal=principal,
            )

        assert ctx.filters == {}


class TestVocabularyUnavailableWarnings:
    """Unavailable-vocabulary warning: a table the question named (entity
    detection matched it) whose vocabulary is entirely unavailable this
    request -- never cached, or a background refresh stuck failing -- and
    that got no filter from ANY source must add the exact
    ``warning_texts.json`` Persian sentence to ``RetrievalContext.warnings``,
    never just silently answer as if the question had named nothing."""

    def test_never_cached_entity_table_adds_the_generic_warning(self):
        # Never refreshed at all -- a genuinely cold entry, distinct from
        # "searched and found nothing".
        patches = _patch_retrievers(entities=["Ring"], static_filters={})
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve("تالار محصولات صنعتی")

        assert ctx.filters == {}
        assert len(ctx.warnings) == 1
        assert "«" not in ctx.warnings[0]  # the generic text names no dimension

    def test_labeled_entity_uses_the_with_label_text(self):
        patches = _patch_retrievers(entities=["Ring"], static_filters={})
        with patches[0], patches[1], patches[2]:
            with patch(
                "retrieval.context_retriever.ENTITIES",
                {"Ring": {"aliases": [], "table": "Ring", "label": "تالار"}},
            ):
                ctx = ContextRetriever.retrieve("تالار محصولات صنعتی")

        assert ctx.filters == {}
        assert len(ctx.warnings) == 1
        assert "تالار" in ctx.warnings[0]

    def test_a_table_already_resolved_by_the_static_pass_gets_no_warning(self):
        # ValueRetriever already resolved "Ring" -- it is never even a
        # db_candidate_table, so a cold vocabulary cache for it is
        # irrelevant and must not warn (a filter WAS resolved, from
        # another source).
        patches = _patch_retrievers(
            entities=["Ring"], static_filters={"Ring": "تالار پتروشیمی"},
        )
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve("تالار پتروشیمی")

        assert ctx.filters == {"Ring": "تالار پتروشیمی"}
        assert ctx.warnings == []

    def test_a_warm_cache_that_resolves_produces_no_warning(self):
        _warm("Ring", "Name", ["تالار محصولات صنعتی"])
        patches = _patch_retrievers(entities=["Ring"], static_filters={})
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve("قیمت در تالار محصولات صنعتی چقدر بود")

        assert ctx.filters == {"Ring": "تالار محصولات صنعتی"}
        assert ctx.warnings == []

    def test_a_warm_cache_that_searches_and_finds_nothing_is_not_unavailable(self):
        """Cached-but-no-match is a different case than never-searched --
        only the latter warns (see VocabularyMatchResult.unavailable_tables's
        own docstring: "searched and not found" is unremarkable)."""
        _warm("Ring", "Name", ["تالار محصولات صنعتی"])
        patches = _patch_retrievers(entities=["Ring"], static_filters={})
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve("چیزی نامرتبط با هیچ تالاری")

        assert ctx.filters == {}
        assert ctx.warnings == []

    def test_no_entities_at_all_produces_no_warning(self):
        patches = _patch_retrievers(entities=[], static_filters={})
        with patches[0], patches[1], patches[2]:
            ctx = ContextRetriever.retrieve("سلام")

        assert ctx.warnings == []
