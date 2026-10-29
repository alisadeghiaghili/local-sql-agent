# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Canonical Persian text normalisation for matching and cache-key equality.

Why this module exists
-----------------------
Persian text reaches this codebase through several input methods, and two
spellings of the same word are common:

* **Digits**: Persian (۰-۹, U+06F0-06F9), Arabic-Indic (٠-٩, U+0660-0669),
  and ASCII (0-9) digits all appear depending on keyboard/IME.
* **ي/ك vs ی/ک**: the Arabic-form YEH (ي, U+064A) and KAF (ك, U+0643) are
  frequently typed in place of their Persian equivalents (ی U+06CC, ک
  U+06A9) -- both are valid input, neither is "wrong".
* **ZWNJ** (U+200C, zero-width non-joiner): used inside Persian compound
  words (می‌خواهم) but often omitted or replaced with a plain space
  (می خواهم / میخواهم) depending on typing habits.
* **Whitespace**: run-length and placement varies with no change in
  meaning.
* **Unicode form**: the same visible word can arrive as different
  codepoint sequences -- a composed letter (U+0622 ALEF WITH MADDA ABOVE)
  vs. its decomposed form (ALEF + COMBINING MADDA ABOVE), or as an Arabic
  *presentation form* / ligature (U+FE8D ALEF ISOLATED FORM, U+FDF2 the
  ALLAH ligature) rather than the plain base letters. Presentation forms
  and ligatures are not exotic here -- they are exactly what text copied
  out of a PDF or an older Windows application (this project's actual
  input environment) tends to contain.

Before this module existed, each call site that needed to compare Persian
text for equality (the query cache's key, the value retriever's filter
extractors, the schema retriever's TF-IDF tokenizer) carried its own
partial translation table, and no two of them agreed. Concretely: the
cache folded ي/ك and ZWNJ so two spellings hashed to the *same* cache key,
while the value retriever did not fold them when extracting filters --
so the *same* cached answer could be served for two spellings that the
retriever itself would have turned into two different SQL queries. This
module is the single place that decides what "the same Persian text"
means, so every call site agrees.

Two entry points, not one
--------------------------
:func:`normalize_for_matching` collapses whitespace to single spaces --
it is for comparing text where word *boundaries* still matter (a
multi-word cache key, a multi-word alias such as "تالار پتروشیمی" checked
as a substring of a question).

:func:`normalize_compact` goes one step further and removes whitespace
entirely. It exists for matching a *fixed, known* vocabulary token
(a month, weekday, or season name) that a user may type as one word, two
words, or ZWNJ-joined -- "پنج شنبه" / "پنج‌شنبه" / "پنجشنبه" must all
resolve to the same weekday, and none of the three spellings is any more
"correct" than the others. Using the whitespace-collapsing form here
would leave "پنج شنبه" unmatched against the canonical "پنجشنبه".

Both build on the same folding rules; :func:`normalize_compact` simply
strips what :func:`normalize_for_matching` leaves as a single space.

Note on scope: this module folds only the rules already present
somewhere in this codebase before unification, plus Unicode
NFKC normalisation (added after it was measured that plain NFC misses
Arabic presentation forms and ligatures -- see the ordering note on
:func:`normalize_for_matching`). It is deliberately not the place to add
further coverage (e.g. diacritic stripping) -- that is a separate
decision with its own tradeoffs.

Helpers that are not normalisation
-----------------------------------
Two later helpers live here because they are about Persian text, but they are
**not** part of :func:`normalize_for_matching` and change none of its output
(so :data:`NORMALIZER_VERSION` and the query cache's keys are untouched):

* :func:`letter_form_key` folds only the letter variants a stored value can
  differ from a typed one by (Arabic ي/ى/ك, presentation forms, ZWNJ), for
  deciding which stored value a filter literal was meant to be
  (:mod:`retrieval.filter_spelling`). It never folds case, digits or spaces.
* :func:`strip_plural_suffix` reduces a word written with an attached plural
  (تالارها) to its stem, for matching a question against aliases and trigger
  phrases. Callers use the stem *in addition to* the word, never instead.

Cache-key version
------------------
:data:`NORMALIZER_VERSION` is bumped whenever this module's OUTPUT changes
for any input (not merely its API surface). Any consumer that builds a
persistent or cross-run key from :func:`normalize_for_matching`'s output
must fold this into that key, so a version bump can never let a stale
entry, produced under the old folding rules, collide with a new one --
see ``api.query_cache.QueryCache._question_key``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

#: Persian digits (U+06F0-06F9) and Arabic-Indic digits (U+0660-0669)
#: mapped to ASCII 0-9, so "۱۴۰۲", "١٤٠٢", and "1402" all fold alike.
#: Unaffected by NFKC (verified: NFKC leaves both digit scripts alone),
#: so this table's correctness does not depend on running before or
#: after the NFKC step below.
_DIGIT_MAP = {
    **{chr(0x06F0 + i): str(i) for i in range(10)},  # Persian
    **{chr(0x0660 + i): str(i) for i in range(10)},   # Arabic-Indic
}

#: Arabic-form letters folded to their Persian equivalents: ي (U+064A) ->
#: ی (U+06CC), ك (U+0643) -> ک (U+06A9). Both spellings are common in
#: Persian text depending on input method/keyboard.
_LETTER_MAP = {"ي": "ی", "ك": "ک"}

#: The one and only translation table in this codebase (digit folding and
#: letter folding combined into a single `str.translate` pass -- the two
#: maps share no keys, so combining them is equivalent to applying either
#: order separately).
_TRANSLATION_TABLE = str.maketrans({**_DIGIT_MAP, **_LETTER_MAP})

#: Zero-width non-joiner -- common inside Persian compound words
#: (می‌خواهم) but irrelevant to text equality; stripped outright.
#: Unaffected by NFKC (verified by execution), so, like the digit map,
#: its position relative to the NFKC step does not matter.
_ZWNJ = "‌"

_WHITESPACE_RE = re.compile(r"\s+")

#: Bumped whenever normalize_for_matching's OUTPUT changes for any input.
#: See the module docstring's "Cache-key version" section.
NORMALIZER_VERSION = "2"


def normalize_for_matching(text: str) -> str:
    """Fold *text* to a canonical form for equality/substring comparison.

    Applies, in order: Unicode NFKC normalisation, Persian/Arabic-Indic
    digit folding, ي/ك -> ی/ک letter folding, ZWNJ removal, whitespace
    collapsing (runs of whitespace become a single space), and ASCII
    lowercasing. None of this changes the *meaning* of the text -- it
    only removes differences a reader would never notice between two
    spellings of the same question or name.

    This is the form used for: cache-key equality, substring/alias
    matching (e.g. ring names), and any other comparison where word
    boundaries (single spaces) still matter.

    Ordering is load-bearing: NFKC MUST run before the ي/ك letter fold,
    not after. NFKC maps an Arabic *presentation form* of YEH (e.g.
    U+FEF1 ARABIC LETTER YEH ISOLATED FORM) to the plain *Arabic* base
    letter ي (U+064A) -- never directly to the Persian ی. Only a fold
    applied AFTER NFKC reaches ی. Folding first and running NFKC second
    leaves a presentation-form YEH as Arabic ي, permanently unmatched
    against Persian-letter text. Verified by execution: with the correct
    order, U+FEF1 folds to U+06CC (ی); reversed, it lands on U+064A (ي).
    Do not reorder these two steps.

    Examples
    --------
    >>> normalize_for_matching("  خرید   در ۱۴۰۲  ")
    'خرید در 1402'
    >>> normalize_for_matching("خرید در ١٤٠٢") == normalize_for_matching("خرید در 1402")
    True
    >>> normalize_for_matching("علي") == normalize_for_matching("علی")
    True
    >>> normalize_for_matching("كارگزار") == normalize_for_matching("کارگزار")
    True
    >>> normalize_for_matching("می‌خواهم") == normalize_for_matching("میخواهم")
    True
    >>> normalize_for_matching("Top Customers") == normalize_for_matching("top customers")
    True

    NFKC folds a composed letter and its decomposed (base + combining
    mark) spelling to the same form -- e.g. ALEF WITH MADDA ABOVE
    (U+0622) vs. ALEF (U+0627) + COMBINING MADDA ABOVE (U+0653):

    >>> normalize_for_matching("\u0622") == normalize_for_matching("\u0627\u0653")
    True

    It also folds an Arabic presentation form / ligature -- text pasted
    from a PDF or an older Windows application often arrives this way --
    to its plain spelled-out equivalent, e.g. the isolated ALEF
    presentation form (U+FE8D) vs. plain ALEF (U+0627), and the ALLAH
    ligature (U+FDF2) vs. "الله" spelled out with four base letters:

    >>> normalize_for_matching("\ufe8d") == normalize_for_matching("\u0627")
    True
    >>> normalize_for_matching("\ufdf2") == normalize_for_matching("الله")
    True
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_TRANSLATION_TABLE)
    text = text.replace(_ZWNJ, "")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.lower()


def normalize_compact(text: str) -> str:
    """Like :func:`normalize_for_matching`, but removes ALL whitespace.

    For matching a fixed vocabulary token (month/weekday/season name)
    that may be typed as one word, two words, or ZWNJ-joined -- spacing
    is not meaningful for these tokens, only the letters are.

    Examples
    --------
    >>> normalize_compact("پنج شنبه") == normalize_compact("پنجشنبه")
    True
    >>> normalize_compact("پنج‌شنبه") == normalize_compact("پنجشنبه")
    True
    """
    return _WHITESPACE_RE.sub("", normalize_for_matching(text))


#: The letters a stored value and a typed one can differ by, one tuple per
#: letter, the Persian form first. ي (U+064A) and ى (U+0649, alef maksura) for
#: ی (U+06CC); ك (U+0643) for ک (U+06A9). Deliberately short: ة/ه, ۀ/هٔ and the
#: alefs are different letters or optional marks, not two spellings of one
#: letter, and a digit script is a separate decision
#: (:mod:`retrieval.filter_spelling` explains each exclusion).
LETTER_FORM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("\u06cc", "\u064a", "\u0649"),
    ("\u06a9", "\u0643"),
)

_LETTER_FORM_TABLE = str.maketrans({
    variant: group[0] for group in LETTER_FORM_GROUPS for variant in group[1:]
})

#: Arabic Presentation Forms-A (U+FB50-FDFF) and -B (U+FE70-FEFF): what text
#: pasted out of a PDF or an old Windows application contains.
PRESENTATION_FORMS_RE = re.compile("[\ufb50-\ufdff\ufe70-\ufeff]")


def letter_form_key(text: str) -> str:
    """The key under which two spellings of one stored value are equal.

    Folds, and only folds: Arabic presentation forms and ligatures to their
    base letters (NFKC, applied to those characters alone, so nothing else in
    the text is rewritten), canonical composition (NFC), ي/ى -> ی and ك -> ک
    (:data:`LETTER_FORM_GROUPS`), and ZWNJ removed. Case, digits, whitespace and
    every other character are kept as they are, which is what makes this
    narrower than :func:`normalize_for_matching`: two texts with the same key
    are the same words spelled with other letter forms, nothing more.

    Parameters
    ----------
    text:
        A stored value or a typed literal.

    Returns
    -------
    str
        The key. Equal keys mean "the same text up to letter form".

    Examples
    --------
    >>> letter_form_key("علي") == letter_form_key("علی")
    True
    >>> letter_form_key("كتاب") == letter_form_key("کتاب")
    True
    >>> letter_form_key("می\u200cخواهم") == letter_form_key("میخواهم")
    True
    >>> letter_form_key("\ufef1") == letter_form_key("ی")
    True

    Nothing else is folded:

    >>> letter_form_key("Ali") == letter_form_key("ali")
    False
    >>> letter_form_key("۱۴۰۲") == letter_form_key("1402")
    False
    >>> letter_form_key("تالار ها") == letter_form_key("تالارها")
    False
    >>> letter_form_key("ة") == letter_form_key("ه")
    False
    """
    text = PRESENTATION_FORMS_RE.sub(lambda m: unicodedata.normalize("NFKC", m.group()), text)
    text = unicodedata.normalize("NFC", text)
    return text.translate(_LETTER_FORM_TABLE).replace(_ZWNJ, "")


#: Attached plural endings, longest first: the plural alone, the plural with
#: the indefinite or the ezafe ی, and the plural with a possessive pronoun.
_PLURAL_SUFFIXES = (
    "هایشان", "هایمان", "هایتان", "هایش", "هایم", "هایت", "هایی", "های", "ها",
)

#: A stem shorter than this is not a word the suffix was attached to.
_MIN_STEM = 3


def strip_plural_suffix(token: str) -> str:
    """The stem of a word written with an attached Persian plural, else *token* itself.

    For a word of the **normalised** text (the half-space is already gone, so
    ``تالار‌ها`` arrives as ``تالارها``). Strips one ending,
    ``ها``, ``های``, ``هایی`` or ``ها`` with a possessive pronoun (``هایم``,
    ``هایت``, ``هایش``, ``هایمان``, ``هایتان``, ``هایشان``), and only when
    what is left is at least three characters, all of them letters. Not stripped, on purpose:
    ``ان`` and ``ات``, which end names and ordinary words that are no plural
    (``تهران``, ``ایران``, ``حیات``), and anything in Latin script. A word that
    ends in ``ها`` without being a plural but has a long enough stem is
    reduced all the same (``انتها`` -> ``انت``); that is why the stem is only
    ever used **in addition to** the word, where an extra token that matches
    nothing costs nothing.

    Parameters
    ----------
    token:
        One word, already normalised (:func:`normalize_for_matching`).

    Returns
    -------
    str
        The stem, or *token* unchanged when no rule applies.

    Examples
    --------
    >>> [strip_plural_suffix(t) for t in ("تالارها", "تالارهای", "تالارهایی", "تالارهایش", "تالارهایشان")]
    ['تالار', 'تالار', 'تالار', 'تالار', 'تالار']

    The half-space form is the same word once normalised:

    >>> strip_plural_suffix(normalize_for_matching("تالار\u200cها"))
    'تالار'

    A word that is not a plural, a stem too short to be a word, and Latin script
    are left alone:

    >>> [strip_plural_suffix(t) for t in ("تالار", "تهران", "ایران", "حیات", "ها", "بها", "های", "tables", "ها1")]
    ['تالار', 'تهران', 'ایران', 'حیات', 'ها', 'بها', 'های', 'tables', 'ها1']
    """
    if token.isascii():
        return token
    for suffix in _PLURAL_SUFFIXES:
        if token.endswith(suffix):
            stem = token[: -len(suffix)]
            return stem if len(stem) >= _MIN_STEM and stem.isalpha() else token
    return token


def strip_plural_suffixes(tokens: Iterable[str]) -> list[str]:
    """Each of *tokens* reduced by :func:`strip_plural_suffix`, in order.

    The result has as many words as *tokens* and equals it when no word
    carries an attached plural, so a caller can compare the two and add the
    second only when they differ. Phrases of several words keep their
    adjacency (``تالارهای کالا`` -> ``تالار کالا``), which is what a bigram or a
    phrase match needs.

    Parameters
    ----------
    tokens:
        Words of the normalised text.

    Returns
    -------
    list[str]
        The stems, same length and order.

    Examples
    --------
    >>> strip_plural_suffixes(["تالارهای", "صادراتی", "تهران"])
    ['تالار', 'صادراتی', 'تهران']
    >>> strip_plural_suffixes(["total", "sales"])
    ['total', 'sales']
    """
    return [strip_plural_suffix(token) for token in tokens]
