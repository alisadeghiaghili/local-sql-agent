# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression coverage for the unified Persian-text normaliser (core.persian).

Phase 5a unification: api/query_cache.py, retrieval/value_retriever.py, and
schema_data/retriever.py each carried an independent, partial Persian
normaliser. Two spellings of the same question -- differing only by input
method (Arabic vs Persian YEH/KAF, ZWNJ presence, digit script) -- hashed to
the SAME cache key under the cache's (complete) normaliser but produced
DIFFERENT extracted filters, and therefore different SQL, under the
retriever's (digits-only) one. Whichever spelling was asked first silently
served its cached answer to both.

This module pins:

1. The cache's view of "same question" and the retriever's view of "same
   filters" now agree on the four spellings that used to disagree
   (TestCacheAndRetrieverAgree). Confirmed by running this test against a
   ``git stash`` of the unification: it fails on all four cases without it.
2. ``ValueRetriever.extract_ring`` specifically now matches through
   Arabic-form letters and ZWNJ (TestExtractRingArabicFormAndZwnj), using an
   injected alias table so the test never depends on
   project_config/aliases.yaml (git-ignored, may be absent).
3. Exactly one ``maketrans`` translation table exists in first-party source
   (TestSingleTranslationTable) -- a second one reappearing unnoticed is
   exactly the bug this task fixes.
4. ``Settings.cache_normalize_questions=False`` still disables normalisation
   for the cache's key only, not globally (TestCacheSwitchIsCacheOnly).
5. Composed/decomposed spellings and Arabic presentation forms / ligatures
   -- what text pasted from a PDF or an older Windows application tends to
   contain -- now fold alike too (TestUnicodeFormFolding), with a control
   case so the test cannot pass by degenerating into "everything matches".
6. NFKC must run BEFORE the letter fold, not after -- reversed, a
   presentation-form YEH lands on the ARABIC base letter and never
   reaches the Persian one (TestNfkcMustRunBeforeLetterFold).
7. The question-keyed cache store's key now also carries
   ``core.persian.NORMALIZER_VERSION`` (TestQuestionKeyEmbedsNormalizerVersion)
   -- required because adding NFKC changed normalize_for_matching's output,
   so an old entry must not be reachable under the new folding rules.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import retrieval.value_retriever as value_retriever_module
from api.models import QueryResponse
from api.query_cache import QueryCache, _normalize_question
from config import override_settings
from core.persian import normalize_for_matching
from retrieval.value_retriever import ValueRetriever


def _resp(question: str) -> QueryResponse:
    return QueryResponse(question=question, sql="SELECT 1", result=[{"n": 1}], row_count=1, model="test")


#: A tiny, self-contained ring-alias table so these tests never depend on
#: project_config/aliases.yaml (git-ignored, may be absent in this checkout).
_TEST_RING_ALIASES = {
    "تالار الف": ["علی"],
    "تالار ب": ["کارگزار"],
    "تالار ج": ["می‌خواهم"],
    "تالار د": ["1402"],
}


@pytest.fixture(autouse=True)
def _small_alias_table(monkeypatch):
    monkeypatch.setattr(value_retriever_module, "RING_ALIASES", _TEST_RING_ALIASES)


# ---------------------------------------------------------------------------
# 1. Cache view vs retriever view must agree (the regression measured in
#    the task write-up: cache said "same", retriever said "different").
# ---------------------------------------------------------------------------

_PAIRS = [
    pytest.param("معاملات علی در تالار", "معاملات علي در تالار", id="arabic_yeh_vs_persian_yeh"),
    pytest.param("کارگزار برتر امسال", "كارگزار برتر امسال", id="persian_kaf_vs_arabic_kaf"),
    pytest.param("می‌خواهم گزارش را ببینم", "میخواهم گزارش را ببینم", id="with_vs_without_zwnj"),
    pytest.param("رکورد سال ۱۴۰۲", "رکورد سال 1402", id="persian_vs_ascii_digits"),
]


class TestCacheAndRetrieverAgree:
    @pytest.mark.parametrize("question_a, question_b", _PAIRS)
    def test_cache_and_retriever_now_agree(self, question_a, question_b):
        cache_says_same = _normalize_question(question_a) == _normalize_question(question_b)

        ring_a = ValueRetriever.extract_ring(question_a)
        ring_b = ValueRetriever.extract_ring(question_b)
        retriever_says_same = ring_a is not None and ring_a == ring_b

        # Both views must actually agree on "same question" -- and that
        # agreement must be a genuine match, not two Nones.
        assert cache_says_same is True
        assert retriever_says_same is True


# ---------------------------------------------------------------------------
# 2. extract_ring specifically matches through Arabic-form letters and ZWNJ.
# ---------------------------------------------------------------------------

class TestExtractRingArabicFormAndZwnj:
    """extract_ring previously did a plain, unnormalised substring check --
    an alias spelled with a Persian letter would never match a question
    spelled with the Arabic-form equivalent, and vice versa."""

    def test_arabic_yeh_question_matches_persian_yeh_alias(self):
        assert ValueRetriever.extract_ring("معاملات علي در تالار") == "تالار الف"

    def test_arabic_kaf_question_matches_persian_kaf_alias(self):
        assert ValueRetriever.extract_ring("كارگزار برتر امسال") == "تالار ب"

    def test_zwnj_free_question_matches_zwnj_joined_alias(self):
        assert ValueRetriever.extract_ring("میخواهم گزارش را ببینم") == "تالار ج"


# ---------------------------------------------------------------------------
# 3. One place defines the translation table.
# ---------------------------------------------------------------------------

class TestSingleTranslationTable:
    """A fifth maketrans-based table must not reappear unnoticed -- that
    kind of drift is exactly why four normalisers disagreed in the first
    place."""

    #: Mirrors the package list in the --doctest-modules CI invocation --
    #: the first-party source, as opposed to tests/ or third-party code.
    _FIRST_PARTY_DIRS = (
        "api", "llm", "security", "session", "database", "core", "knowledge",
        "prompt_engine", "retrieval", "schema_data", "logs", "exporters",
        "observability", "eval",
    )
    _FIRST_PARTY_FILES = ("config.py",)

    def test_maketrans_appears_exactly_once(self):
        repo_root = Path(__file__).resolve().parent.parent
        pattern = re.compile(r"\bmaketrans\b")
        hits: list[str] = []

        candidate_paths: list[Path] = [repo_root / f for f in self._FIRST_PARTY_FILES]
        for d in self._FIRST_PARTY_DIRS:
            for path in sorted((repo_root / d).rglob("*.py")):
                rel_parts = path.relative_to(repo_root).parts
                if "tests" in rel_parts:
                    continue
                candidate_paths.append(path)

        for path in candidate_paths:
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                hits.append(path.relative_to(repo_root).as_posix())

        assert hits == ["core/persian.py"], hits


# ---------------------------------------------------------------------------
# 4. cache_normalize_questions=False stays scoped to the cache's key only.
# ---------------------------------------------------------------------------

class TestCacheSwitchIsCacheOnly:
    def test_switch_still_disables_cache_key_folding(self):
        cache = QueryCache(ttl_seconds=60, max_size=10)
        with override_settings(cache_normalize_questions=False):
            cache.set("خرید در ۱۴۰۲", "full", _resp("خرید در ۱۴۰۲"))
            assert cache.get("خرید در 1402", "full") is None

    def test_switch_does_not_affect_core_persian_directly(self):
        with override_settings(cache_normalize_questions=False):
            assert normalize_for_matching("علي") == normalize_for_matching("علی")

    def test_switch_does_not_affect_value_retriever(self):
        with override_settings(cache_normalize_questions=False):
            assert ValueRetriever.extract_ring("كارگزار برتر امسال") == "تالار ب"


# ---------------------------------------------------------------------------
# 5. NFKC unicode-form folding: composed vs decomposed spellings, and
#    Arabic presentation forms / ligatures (text pasted from a PDF or an
#    older Windows application) fold to the same form as the plain spelling.
# ---------------------------------------------------------------------------

_UNICODE_FORM_PAIRS = [
    pytest.param("آ", "آ", id="alef_madda_composed_vs_decomposed"),
    pytest.param("أ", "أ", id="alef_hamza_above_composed_vs_decomposed"),
    pytest.param("إ", "إ", id="alef_hamza_below_composed_vs_decomposed"),
    pytest.param("ﺍ", "ا", id="alef_isolated_presentation_form_vs_plain"),
    pytest.param("ﻟ", "ل", id="lam_initial_presentation_form_vs_plain"),
    pytest.param("ﷲ", "الله", id="allah_ligature_vs_spelled_out"),
]


class TestUnicodeFormFolding:
    @pytest.mark.parametrize("form_a, form_b", _UNICODE_FORM_PAIRS)
    def test_presentation_and_composed_forms_fold_alike(self, form_a, form_b):
        assert normalize_for_matching(form_a) == normalize_for_matching(form_b)

    def test_control_two_different_words_stay_unequal(self):
        # Without a control like this, the six cases above could pass even
        # if normalize_for_matching degenerated into mapping everything to
        # the same output -- this pins that it still discriminates between
        # two genuinely different Persian words.
        assert normalize_for_matching("گزارش") != normalize_for_matching("فروش")


# ---------------------------------------------------------------------------
# 6. Ordering is load-bearing: NFKC must run before the letter fold.
# ---------------------------------------------------------------------------

class TestNfkcMustRunBeforeLetterFold:
    """NFKC maps an Arabic presentation-form YEH to the ARABIC base letter
    (ي, U+064A), never directly to the Persian one (ی, U+06CC) -- only a
    fold applied AFTER NFKC reaches ی. Reversing the two steps -- fold,
    then NFKC -- leaves a presentation-form YEH permanently unmatched
    against Persian-letter text. This is a mutation check: it proves the
    real implementation's order is the one that works, by showing the
    reversed order fails on the exact same input."""

    def test_correct_order_matches_presentation_form_yeh_to_persian(self):
        # U+FEF1 ARABIC LETTER YEH ISOLATED FORM, through the real
        # (correctly-ordered) normalize_for_matching.
        assert normalize_for_matching("ﻱ") == normalize_for_matching("ی")  # Persian yeh

    def test_reversed_order_would_reintroduce_the_bug(self):
        import unicodedata

        fold_table = str.maketrans({"ي": "ی", "ك": "ک"})

        def wrong_order(text: str) -> str:
            text = text.translate(fold_table)            # fold FIRST (wrong)
            return unicodedata.normalize("NFKC", text)    # NFKC SECOND (wrong)

        # Under the reversed order, the presentation-form YEH lands on the
        # ARABIC base letter, not the Persian one -- so it no longer
        # matches. This is the failure the real implementation avoids by
        # running NFKC first.
        assert wrong_order("ﻱ") == "ي"
        assert wrong_order("ﻱ") != "ی"


# ---------------------------------------------------------------------------
# 7. The question-keyed cache store's key carries the normaliser version.
# ---------------------------------------------------------------------------

class TestQuestionKeyEmbedsNormalizerVersion:
    def test_question_key_carries_normalizer_version(self):
        from core.persian import NORMALIZER_VERSION

        key = QueryCache._question_key("سوال", "full", "prefix1")
        assert NORMALIZER_VERSION in key[0]
