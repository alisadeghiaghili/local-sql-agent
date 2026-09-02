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
