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
