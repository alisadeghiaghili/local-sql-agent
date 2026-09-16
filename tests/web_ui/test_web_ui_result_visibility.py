# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""D3-hidden-chart: a settled chart result is invisible behind the composer.

Measured live at a 910px viewport, RTL, after asking a sample question whose
result renders as a chart (``web/js/render/chart.js``):

==========================  ==========  ================
element                     y-range     height
==========================  ==========  ================
``.composer`` (sticky)      720 - 900   180px
chart ``svg``               873 - 1315  440px
==========================  ==========  ================

The composer's ``y`` range (720-900) fully contains the chart svg's top edge
(873) and then some, so the overlap is the entire visible portion of the
composer band: **0 of the chart's 440px was visible** above it, and nothing
in the page reserved space or re-scrolled to change that once the chart
finished rendering. An analyst had to know to scroll, and even a full scroll
to the bottom left the composer sitting on top of the last ~190px of the
result, because the composer is `position: sticky` (DESIGN.md decision D3 —
kept at the bottom, not reverted to the top) and nothing in the document's
flow ever reserved room for it: a sticky element does not push later
content, and there is no later content here for it to push against.

Why this is a source-text assertion rather than a rendering test
------------------------------------------------------------------
Same reasoning as ``test_web_ui_rtl_layout.py`` (see that module's own
docstring): a real occlusion measurement needs layout, fonts and a real
scroller, none of which the ``.mjs`` stub-DOM harnesses have. What this
module pins is that the *deliberate* declarations that prevent the
occlusion are present — with the measured numbers above recorded as the
justification — not a re-creation of the browser layout itself.

The real verification is the live 3-width browser check (375 / 768 / 1280,
``dir="rtl"``, simulated mode, sample-story chart) run manually against
``web/`` via a static preview server, confirming the chart svg's rect no
longer overlaps the composer's rect at any of the three widths after this
fix, where it overlapped completely (0px visible) before it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_STYLE = _REPO_ROOT / "web" / "styles" / "style.css"
_MAIN_JS = _REPO_ROOT / "web" / "js" / "main.js"


def _style() -> str:
    return _STYLE.read_text(encoding="utf-8")


def _main_js() -> str:
    return _MAIN_JS.read_text(encoding="utf-8")


def _rule(css: str, selector_pattern: str) -> str | None:
    """The declaration block for the first rule matching *selector_pattern*."""
    match = re.search(selector_pattern + r"\s*\{([^}]*)\}", css, re.S)
    return match.group(1) if match else None


# A rule value like "200px" or "var(--composer-clearance)" resolved through
# a single level of custom-property indirection, since --composer-clearance
# is itself declared as a plain px value in this stylesheet (see the
# assertion below that pins that declaration exists and is >= 150px).
_PX_OR_VAR = re.compile(r"(\d+(?:\.\d+)?)px|var\(\s*(--[\w-]+)\s*\)")


def _resolved_px(css: str, declaration_value: str) -> float | None:
    m = _PX_OR_VAR.search(declaration_value)
    if not m:
        return None
    if m.group(1) is not None:
        return float(m.group(1))
    var_name = m.group(2)
    var_rule = re.search(re.escape(var_name) + r"\s*:\s*([^;]+);", css)
    if not var_rule:
        return None
    inner = _PX_OR_VAR.search(var_rule.group(1))
    if inner and inner.group(1) is not None:
        return float(inner.group(1))
    return None


# The composer's measured resting height was 180px (720-900) plus its own
# 10px `bottom` sticky offset — 190px of footprint an analyst can't scroll
# through. Any reservation/clearance below that floor would still let a
# short scroll leave the tail of a result tucked under the composer, so
# this is the number every assertion below is checked against, not just
# "some positive value".
_MEASURED_COMPOSER_FOOTPRINT_PX = 190


class TestChartResultCanClearTheStickyComposer:
    """A sticky element does not participate in document flow the way a
    static one does: nothing after `.composer` in `.layout` pushes it out
    of the way, because there is nothing after it (D3 keeps it last/bottom
    on purpose). So the reservation has to be deliberate — either blank
    space reserved below the transcript's content, or a scroll target that
    refuses to call itself "in view" while the composer still covers part
    of it (or, ideally per the fix notes, both)."""

    def test_transcript_reserves_room_for_the_composer_below_its_content(self):
        """Without this, scrolling all the way down parks the sticky
        composer directly on top of the last turn's content instead of
        below it — there is no blank space at the end of the flow for the
        composer to occupy that isn't already occupied by a result."""
        css = _style()
        rule = _rule(css, r"#transcript")
        assert rule is not None, "#transcript has no rule at all in style.css"
        m = re.search(
            r"(?:padding|padding-bottom|padding-block-end)\s*:\s*([^;]+);", rule
        )
        assert m is not None, (
            "#transcript declares no bottom padding/padding-block-end. "
            "Measured: a 180px-tall sticky composer sat over the last "
            "440px chart with 0px of it visible once scrolled to the "
            "bottom, because nothing reserved space for the composer "
            "below the transcript's own content"
        )
        px = _resolved_px(css, m.group(1))
        assert px is not None and px >= _MEASURED_COMPOSER_FOOTPRINT_PX, (
            f"#transcript's bottom reservation resolves to {px!r}px, short "
            f"of the composer's measured ~{_MEASURED_COMPOSER_FOOTPRINT_PX}px "
            "resting footprint (180px card height + 10px sticky `bottom` "
            "offset) -- a reservation smaller than the composer itself "
            "still leaves part of it overlapping the last result"
        )

    def test_reservation_uses_a_logical_property_not_a_physical_side(self):
        """This is an RTL-first product (DESIGN.md; every other spacing
        rule in style.css uses logical properties so mirroring under
        `dir` changes nothing about which edge gets the space) -- but
        "bottom" here is block-axis, not inline-axis, so `padding-bottom`
        is not actually a physical-vs-logical bug the way `padding-right`
        would be. This test pins that the declaration is still written in
        the file's own logical-property style for consistency, and should
        be rewritten (not deleted) if the property genuinely changes."""
        css = _style()
        rule = _rule(css, r"#transcript") or ""
        assert re.search(r"padding-block-end\s*:", rule), (
            "#transcript's bottom reservation is not written as "
            "padding-block-end, unlike the rest of this logical-property "
            "stylesheet -- if a physical padding-bottom was substituted "
            "intentionally, update this test rather than silently drop it"
        )

    def test_composer_clearance_value_is_declared_and_clears_the_measured_footprint(self):
        """Pins the actual number, not just that *a* number exists --
        the whole point of recording 180px/190px in this module's
        docstring is that a future edit which shrinks the reservation
        below the composer's real footprint should fail loudly here."""
        css = _style()
        m = re.search(r"--composer-clearance\s*:\s*([^;]+);", css)
        assert m is not None, (
            "no --composer-clearance custom property in style.css -- if "
            "the reservation was rewritten to hardcode the value in every "
            "consumer instead of a shared token, update this test to check "
            "each declared value individually rather than deleting the check"
        )
        px = _resolved_px(css, m.group(1))
        assert px is not None and px >= _MEASURED_COMPOSER_FOOTPRINT_PX, (
            f"--composer-clearance resolves to {px!r}px, short of the "
            f"composer's measured ~{_MEASURED_COMPOSER_FOOTPRINT_PX}px "
            "resting footprint"
        )

    def test_turn_scroll_margin_leaves_composer_clearance(self):
        """`.turn` already carries `scroll-margin-top` (for the fixed
        topbar). Without a matching `scroll-margin-bottom`, a
        `scrollIntoView` call is free to consider a turn "in view" once
        its bottom edge merely touches the true viewport bottom -- which
        is exactly where the sticky composer sits, so "in view" and
        "covered by the composer" would be the same state."""
        css = _style()
        rule = _rule(css, r"\.turn\b")
        assert rule is not None, "no .turn rule in style.css"
        assert re.search(r"scroll-margin-top\s*:", rule), (
            "the premise of this test moved: .turn no longer declares "
            "scroll-margin-top at all. Re-check the topbar-clearance rule "
            "this test assumes still exists before rewriting"
        )
        m = re.search(r"scroll-margin-bottom\s*:\s*([^;]+);", rule)
        assert m is not None, (
            ".turn declares no scroll-margin-bottom, so scrollIntoView has "
            "no way to know the sticky composer occupies the bottom "
            "~190px of the viewport when deciding whether a turn (and any "
            "tall result inside it, like a chart) is already 'in view'"
        )
        px = _resolved_px(css, m.group(1))
        assert px is not None and px >= _MEASURED_COMPOSER_FOOTPRINT_PX, (
            f".turn's scroll-margin-bottom resolves to {px!r}px, short of "
            f"the composer's measured ~{_MEASURED_COMPOSER_FOOTPRINT_PX}px footprint"
        )

    def test_document_scroller_declares_scroll_padding_bottom(self):
        """The page's own scrolling box is `html` (body is a flex column
        with no height/overflow of its own -- see the comment beside this
        rule in style.css), so that is where `scroll-padding-bottom` has
        to live for `scrollIntoView`/keyboard/anchor scrolling to respect
        the composer's footprint at all; a rule on `body` or `#transcript`
        alone would have no effect on the real scroller."""
        css = _style()
        rule = _rule(css, r"html")
        assert rule is not None, "no `html` rule in style.css"
        assert re.search(r"scroll-padding-bottom\s*:", rule), (
            "html declares no scroll-padding-bottom -- scrollIntoView and "
            "native scrolling have no way to leave the sticky composer's "
            "footprint clear when bringing a target into view"
        )


class TestSettledResultReScrollsClearOfTheComposer:
    """Reserving space fixes the *reachable* case (a manual scroll to the
    bottom). It does not fix the *default* case: main.js's initial
    scrollIntoView fires the instant a turn card is appended, with
    `{ block: "start" }`, while the result is still `hidden` (turn.js's
    tagLate/revealLate -- a chart is late-revealed, not rendered
    synchronously visible). By the time the chart is unhidden and the card
    grows past the fold, nothing re-scrolls, so an analyst who never
    manually scrolls never sees it appear at all. These assertions pin
    that a second, settle-time scroll call exists on both paths that reveal
    late content (the simulated demo-script path and the live SSE path)."""

    def test_a_settle_scroll_helper_exists_and_respects_composer_clearance(self):
        js = _main_js()
        assert "function scrollSettledResultAboveComposer" in js, (
            "no dedicated settle-time scroll function in main.js -- the "
            "initial scrollIntoViewMaybeSmooth(card.el, { block: 'start' }) "
            "fires before a chart's late-revealed content exists (turn.js's "
            "revealLate), so it cannot account for a chart that grows the "
            "card past the viewport afterward"
        )

    def test_simulated_turn_settle_path_calls_the_settle_scroll_after_reveal_late(self):
        """appendTurnWithAnimation (the simulated/demo-script append path,
        also used by the sample-story buttons the browser check below
        drives) must call the settle scroll AFTER revealLate() -- calling
        it before would scroll based on the still-hidden (collapsed-height)
        result, reproducing the exact bug this module exists to catch."""
        js = _main_js()
        m = re.search(
            r"async function appendTurnWithAnimation\([^)]*\)\s*\{(.*?)\n\}",
            js, re.S,
        )
        assert m is not None, "appendTurnWithAnimation not found in main.js"
        body = m.group(1)
        reveal_late_pos = body.find("revealLate()")
        settle_scroll_pos = body.find("scrollSettledResultAboveComposer(")
        assert reveal_late_pos != -1, "appendTurnWithAnimation no longer calls revealLate()"
        assert settle_scroll_pos != -1, (
            "appendTurnWithAnimation never calls the settle-time scroll "
            "helper, so a chart revealed here (the sample-story/demo path) "
            "is never brought into view once it renders"
        )
        assert settle_scroll_pos > reveal_late_pos, (
            "the settle scroll in appendTurnWithAnimation runs before "
            "revealLate() -- it would measure the turn card's still-hidden "
            "(collapsed) result and scroll as if there were nothing to "
            "clear, reproducing the original bug"
        )

    def test_live_turn_settle_path_calls_the_settle_scroll_after_the_done_event(self):
        """askLive's SSE handler reveals late content on every `rebuild()`
        call (each streamed event), but the result only exists once the
        `"done"` event's rebuild has run. A settle scroll wired to an
        earlier event would fire before the chart exists; wiring it to
        `"done"` is what makes it see the finished card."""
        js = _main_js()
        m = re.search(r'case "done":(.*?)break;', js, re.S)
        assert m is not None, 'askLive has no case "done": branch'
        body = m.group(1)
        assert "rebuild()" in body, (
            'the "done" case no longer calls rebuild() -- re-check where '
            "the live path's final render (including a chart result) "
            "actually happens before rewriting this test"
        )
        assert "scrollSettledResultAboveComposer(" in body, (
            'askLive\'s "done" case never calls the settle-time scroll '
            "helper, so a chart result that lands via the live/SSE path "
            "is never brought into view once it finishes rendering"
        )
        assert body.index("scrollSettledResultAboveComposer(") > body.index("rebuild()"), (
            'the settle scroll in the "done" case runs before rebuild() -- '
            "it would target the DOM node from before the final result was "
            "rendered in"
        )
