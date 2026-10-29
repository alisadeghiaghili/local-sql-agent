# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``core.persian.strip_plural_suffix``: the stem of a word written with an attached plural.

The rules are conservative on purpose (see the function's docstring): the endings
``ها``, ``های``, ``هایی`` and ``ها`` with a possessive; a stem of at least three letters;
nothing in Latin script; and no ``ان`` / ``ات``, which end names and ordinary words.
The stem is only ever used in addition to the word, so what is checked here is both what
is reduced and, as important, what is not.
"""

from __future__ import annotations

import pytest

from core.persian import (
    NORMALIZER_VERSION,
    normalize_for_matching,
    strip_plural_suffix,
    strip_plural_suffixes,
)

HA = "ها"
HAYE = "های"


class TestWhatIsReduced:
    @pytest.mark.parametrize("suffix", [
        HA, HAYE, HAYE + "ی",
        HAYE + "م", HAYE + "ت", HAYE + "ش",
        HAYE + "مان", HAYE + "تان", HAYE + "شان",
    ])
    def test_every_ending_gives_the_stem(self, suffix):
        assert strip_plural_suffix("تالار" + suffix) == "تالار"

    def test_the_written_forms_of_one_word(self):
        # attached, with the half-space (the normaliser folds it to attached), with 'ye'
        assert strip_plural_suffix(normalize_for_matching("تالارها")) == "تالار"
        assert strip_plural_suffix(normalize_for_matching("تالار‌ها")) == "تالار"
        assert strip_plural_suffix(normalize_for_matching("تالارهای")) == "تالار"
        assert strip_plural_suffix(normalize_for_matching("تالار‌های")) == "تالار"
        assert strip_plural_suffix(normalize_for_matching("تالارهایی")) == "تالار"

    def test_a_stem_that_ends_in_heh_or_a_vowel(self):
        assert strip_plural_suffix("خانه" + HA) == "خانه"
        assert strip_plural_suffix("دانشجو" + HA) == "دانشجو"
        assert strip_plural_suffix("مشتری" + HAYE) == "مشتری"

    def test_the_longest_ending_wins(self):
        assert strip_plural_suffix("تالارهایشان") == "تالار"
        assert strip_plural_suffix("تالارهایی") == "تالار"


class TestWhatIsNot:
    @pytest.mark.parametrize("word", [
        "تهران",              # Tehran
        "ایران",              # Iran
        "مشتریان",  # the plural in -an is not stripped
        "حیات",                    # -at
        "امکانات",  # -at plural
        "تالار",              # a plain word
        "تالاری",        # with 'ye' only
    ])
    def test_a_word_that_is_not_a_plural_in_ha(self, word):
        assert strip_plural_suffix(word) == word

    @pytest.mark.parametrize("word", [
        HA, HAYE, HAYE + "ی",                     # the ending on its own
        "ب" + HA,                                  # price: one letter stem
        "ب" + HAYE,
        "ما" + HA,                            # two letters
        "ا" + HAYE + "ش",
    ])
    def test_a_stem_shorter_than_three_letters_is_not_a_stem(self, word):
        assert strip_plural_suffix(word) == word

    @pytest.mark.parametrize("word", ["tables", "aha", "cities", "SQL", "users_" + "ها", "ab1" + HA, "12" + HA])
    def test_latin_digits_and_underscores_are_left_alone(self, word):
        assert strip_plural_suffix(word) == word

    def test_a_word_that_ends_in_the_ending_but_is_no_plural_is_reduced_all_the_same(self):
        # documented: the stem is only ever an extra token, which matches nothing
        assert strip_plural_suffix("انت" + HA) == "انت"

    def test_the_empty_string(self):
        assert strip_plural_suffix("") == ""


class TestSeveralWords:
    def test_same_length_same_order(self):
        words = ["تالار" + HAYE, "صادراتی", "تهران", "x"]
        assert strip_plural_suffixes(words) == ["تالار", "صادراتی", "تهران", "x"]
        assert len(strip_plural_suffixes(words)) == len(words)

    def test_unchanged_when_no_word_has_a_plural(self):
        words = ["total", "sales", "تهران", HA]
        assert strip_plural_suffixes(words) == words

    def test_an_empty_sequence(self):
        assert strip_plural_suffixes([]) == []


class TestTheNormaliserIsNotTouched:
    def test_version_and_output(self):
        assert NORMALIZER_VERSION == "2"
        assert normalize_for_matching("تالار‌ها") == "تالارها"
        assert normalize_for_matching("تالار ها") == "تالار ها"
