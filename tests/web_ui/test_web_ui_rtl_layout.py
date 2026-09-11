# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Three layout defects a Persian analyst sees and an LTR reviewer cannot.

All three were found the same way: by loading the real pages in a browser at
``dir="rtl"`` and *measuring* the result, never by reading the CSS. Each looked
fine in source. Two of them look fine in an LTR browser as well, which is why
they survived every prior review.

Why these are source-text assertions rather than rendering tests
----------------------------------------------------------------
The same reasoning ``test_web_ui_chart_direction.py`` records: the ``.mjs``
harnesses drive real modules against a hand-written stub DOM, and a stub has no
layout, no fonts and no bidi algorithm, so it cannot observe this class of bug
at all. A headless browser for three declarations is a large amount of
machinery to assert what a short check states directly.

What each test therefore pins is that a *deliberate* declaration is present,
with the measurement that justifies it recorded in the docstring — because in
six months the number is what tells a reader whether the rule still applies,
and the CSS alone never will.

Policy: ``docs/design/DESIGN-INVARIANTS.md`` §2 and §3.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_STYLE = _REPO_ROOT / "web" / "styles" / "style.css"
_TOKENS = _REPO_ROOT / "web" / "styles" / "tokens.css"


def _stylesheet() -> str:
    """Every product stylesheet, concatenated.

    Train A of the 5.0 redesign splits colours out into ``tokens.css``; until
    that lands the file does not exist. Reading whichever are present keeps
    these tests meaningful on both sides of that move instead of pinning them
    to a layout of the CSS that is already scheduled to change.
    """
    return "\n".join(
        p.read_text(encoding="utf-8") for p in (_STYLE, _TOKENS) if p.exists()
    )


def _rule(selector_pattern: str) -> str | None:
    """The declaration block for the first rule matching *selector_pattern*."""
    match = re.search(selector_pattern + r"\s*\{([^}]*)\}", _stylesheet(), re.S)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# §2.2 — an off-canvas panel hides toward the edge it lives on
# ---------------------------------------------------------------------------

class TestTheConversationDrawerFullyLeavesTheScreen:
    """Measured at a 375px viewport with the real page:

    ==========================  ====  =====  =======
    transform                   left  right  visible
    ==========================  ====  =====  =======
    none (drawer open)            75    375      300
    ``translateX(-100%)``       -225     75   **75**
    ``translateX(100%)``         375    675        0
    ==========================  ====  =====  =======

    In RTL the drawer is flush against the **right** edge, so hiding it means
    pushing it right. ``-100%`` is the element's own width, not the distance to
    the far edge: a 300px panel in a 375px viewport lands 75px short. Those
    75px sat on top of the header and the Ask button, which is why the page
    looked like it had a dozen clipping bugs when it had exactly one.
    """

    def test_the_rtl_drawer_does_not_translate_toward_the_far_edge(self):
        rule = _rule(r'\[dir="rtl"\]\s*\.sidebar')
        if rule is None:
            pytest.skip("no RTL-specific .sidebar rule; check the direction-agnostic test")
        assert not re.search(r"translateX\(\s*-\s*100%\s*\)", rule), (
            "the RTL drawer still hides with translateX(-100%). Measured at "
            "375px that leaves 75px of a 300px panel on screen, covering the "
            "header and the Ask button. A panel anchored to the inline-end "
            "edge hides with translateX(100%) under RTL"
        )

    def test_some_rule_hides_the_drawer_in_rtl(self):
        """Guarding the premise: if the drawer stops being transform-hidden
        entirely (a display/visibility approach, or a direction-agnostic
        logical property), this module's assumption is stale and the test
        should be rewritten rather than silently passing on absence."""
        css = _stylesheet()
        assert ".sidebar" in css, "the conversation drawer is gone from the stylesheet"
        assert re.search(r"\.sidebar[^{]*\{[^}]*(transform|inset-inline|inline-size|display)", css, re.S), (
            "nothing in the stylesheet positions or hides .sidebar"
        )


# ---------------------------------------------------------------------------
# §2.1 — a forced direction applies to the value, never to its prose
# ---------------------------------------------------------------------------

class TestAnLtrFieldDoesNotReorderItsPersianPlaceholder:
    """The API-key input is ``direction: ltr``, which is correct: an API key is
    ASCII and must not reorder. Its placeholder is Persian prose containing a
    Latin run, and that is what breaks.

    Measured, reading right-to-left as a Persian reader does:

    ==========  ==========================================
    under LTR   ``خود را وارد کنید API کلید``
    under RTL   ``کلید API خود را وارد کنید``
    ==========  ==========================================

    The Latin ``API`` inside Persian prose is the trigger. The admin panel's
    pure-Persian ``کلید مدیریتی`` was measured too and does **not** reorder —
    it only misaligns. Different severities; the fix below covers both without
    pretending they are the same bug.
    """

    def test_the_placeholder_is_styled_back_to_the_document_direction(self):
        css = _stylesheet()
        has_placeholder_rule = re.search(
            r"::placeholder\s*\{[^}]*direction\s*:\s*rtl", css, re.S
        )
        flips_on_content = re.search(
            r":not\(\s*:placeholder-shown\s*\)\s*\{[^}]*direction\s*:\s*ltr", css, re.S
        )
        assert has_placeholder_rule or flips_on_content, (
            "an input forced to direction: ltr shows its Persian placeholder "
            "reordered ('خود را وارد کنید API کلید'). Either style "
            "::placeholder back to rtl, or start the field rtl and flip to ltr "
            "with :not(:placeholder-shown) — which also fixes alignment while "
            "the analyst types"
        )

    def test_the_key_field_still_forces_ltr_for_its_value(self):
        """The fix must not be 'make the whole field RTL'. An API key rendered
        right-to-left is unreadable and unverifiable against what was issued."""
        css = _stylesheet()
        assert re.search(r"direction\s*:\s*ltr", css), (
            "no element forces direction: ltr any more. The key's *value* is "
            "ASCII and must stay left-to-right; only its placeholder moves"
        )


# ---------------------------------------------------------------------------
# §3 — tap targets
# ---------------------------------------------------------------------------

class TestControlsAreLargeEnoughToHit:
    """`DESIGN.md` §8 asks for 44x44 on touch. The shipping interpretation
    toggle measured **81x20** — the native checkbox is 13x13 and the wrapping
    label adds almost nothing vertically.

    The floor asserted here is 24px (WCAG 2.5.8 conformance) rather than the
    policy's single 44 figure: 44 is the comfort line inside a touch
    breakpoint, and a rule nobody meets at desktop density gets dropped, after
    which neither number holds.
    """

    def test_the_interpret_toggle_has_an_explicit_hit_area(self):
        css = _stylesheet()
        rule = _rule(r"\.ask-toggle") or _rule(r"\.check")
        assert rule is not None, (
            "the interpretation toggle's label has no style rule at all, so "
            "its hit area is whatever a 13px native checkbox happens to be"
        )
        assert re.search(r"min-(height|block-size)\s*:\s*(2[4-9]|[3-9]\d)px", rule), (
            "the toggle label declares no min-height >= 24px. Measured 81x20 "
            "on the shipping UI; the wrapping label must carry the padding "
            "because a 13px native checkbox is never the target on its own"
        )


# ---------------------------------------------------------------------------
# §5 — the product ships at more than one width
# ---------------------------------------------------------------------------

class TestTheLayoutIsDefinedAtMoreThanOneWidth:
    """One breakpoint at 640px means 768-1024 tablets inherit the desktop
    chrome that `DESIGN.md` §5.1 already calls a junk drawer."""

    def test_there_is_a_breakpoint_above_the_phone_one(self):
        """Only ``@media`` widths count.

        A first draft of this test matched every ``max-width`` declaration in
        the file and passed against a stylesheet with a single 640px
        breakpoint — because containers like the transcript carry
        ``max-width: 880px``. A layout constraint on one element says nothing
        about which viewports the layout was designed for, and a test that
        cannot tell them apart reports the opposite of the truth.
        """
        media_queries = re.findall(r"@media([^{]*)\{", _stylesheet())
        widths = {
            int(w)
            for q in media_queries
            for w in re.findall(r"max-width\s*:\s*(\d+)px", q)
        }
        assert any(w >= 768 for w in widths), (
            f"the only @media max-width breakpoints are {sorted(widths) or 'none'}. "
            "Tablet widths inherit the desktop topbar, which the design policy "
            "documents as already overflowing below ~1400px"
        )
