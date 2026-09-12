# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Contrast is computed, and failure is designed.

Two policy sections that had been asserted for a year and never checked:
``DESIGN.md`` §8 sets an accessibility bar, and §2 names "Failure — honest
empty/error, with a next action" as a moment that matters. Nobody had measured
the first, and §5.2 specifies only a *successful* turn, so the second had no
design at all.

Measured against the shipping tokens when this module was written:

============================  ======  ======  ==========
check                         light   dark    WCAG
============================  ======  ======  ==========
body text on card              14.68   13.98  1.4.3 (4.5)
secondary text on page       **4.40**   7.30  1.4.3
primary action label         **3.74**   6.84  1.4.3
ask-field boundary           **1.23**   1.34  1.4.11 (3.0)
============================  ======  ======  ==========

The primary action is the most-pressed control in the product and it fails in
light mode. A 15px bold label is not "large text" — that exemption starts at
18.66px bold — so 4.5 applies and 3.74 does not reach it. ``--teal-d`` is
already in the palette and measures 5.21.

Note which column carries the failures. The theme decision (follow the system)
means both themes ship, so a light-only defect reaches roughly half the users,
not none.

Why the numbers live in this file
---------------------------------
A ratio is the only thing that tells a later reader whether a token change was
safe. The CSS shows a hex; only the computation shows whether it passes. So
the arithmetic is here rather than in a comment, and it runs on every change.

Policy: ``docs/design/DESIGN-INVARIANTS.md`` §8 and §11.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TOKENS = _REPO_ROOT / "web" / "styles" / "tokens.css"
_STYLE = _REPO_ROOT / "web" / "styles" / "style.css"


# ---------------------------------------------------------------------------
# WCAG relative luminance — the formula, not an approximation of it
# ---------------------------------------------------------------------------

def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG 2.1 contrast ratio between two opaque colours."""
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _palette(theme: str) -> dict[str, str]:
    """``{token: hex}`` for *theme*, read from the real stylesheet.

    Split on the dark media query rather than parsing the whole cascade: this
    file's own rule is that light and dark redefine the *same* properties, so
    two flat maps is a faithful model of it. If that rule is ever broken this
    lookup fails loudly, which is the right outcome.
    """
    css = _TOKENS.read_text(encoding="utf-8")
    if theme == "light":
        block = css.split("@media")[0]
    else:
        marker = "prefers-color-scheme: dark"
        assert marker in css, "the dark theme block is gone from tokens.css"
        block = css.split(marker, 1)[1]
    return dict(re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", block))


# ---------------------------------------------------------------------------
# 1.4.3 — text contrast
# ---------------------------------------------------------------------------

class TestTextMeetsContrast:
    @pytest.mark.parametrize("theme", ["light", "dark"])
    @pytest.mark.parametrize(
        "fg, bg, what",
        [
            ("ink", "card", "body text on a card"),
            ("ink", "bg", "body text on the page"),
            ("muted", "card", "secondary text on a card"),
            ("muted", "bg", "secondary text on the page"),
        ],
    )
    def test_text_pair_reaches_4_5(self, theme, fg, bg, what):
        pal = _palette(theme)
        if fg not in pal or bg not in pal:
            pytest.skip(f"--{fg} or --{bg} is not defined for {theme}")
        ratio = contrast(pal[fg], pal[bg])
        assert ratio >= 4.5, (
            f"{what} ({theme}) is {ratio:.2f}:1, below WCAG 1.4.3's 4.5. "
            f"{pal[fg]} on {pal[bg]}"
        )


class TestThePrimaryActionIsReadable:
    """Singled out because it is the control the analyst presses most, and
    because it failed: white on ``--teal`` measured 3.74 in light mode.

    15px bold is **not** "large text" — WCAG's exemption starts at 18.66px
    bold (14pt) or 24px regular. So 4.5 applies, not 3.0. That distinction is
    what makes this a defect rather than a judgement call.
    """

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_a_white_label_on_the_brand_fill_reaches_4_5(self, theme):
        pal = _palette(theme)
        assert "teal" in pal, "--teal is gone from tokens.css"
        ratio = contrast("#ffffff", pal["teal"])
        assert ratio >= 4.5, (
            f"a white label on --teal ({pal['teal']}, {theme}) is {ratio:.2f}:1. "
            "The Ask button fails WCAG 1.4.3. --teal-d is already in the "
            "palette and measures 5.21 in light mode"
        )


# ---------------------------------------------------------------------------
# 1.4.11 — non-text contrast for an interactive boundary
# ---------------------------------------------------------------------------

class TestTheAskFieldIsIdentifiable:
    """1.4.11 asks that what identifies a control reaches 3:1. The ask
    textarea failed on both available signals: its border measured 1.23
    against the card and its fill differed by 1.05, so nothing marked the
    field's edge at the required contrast.

    A decorative hairline between sections does not need 3:1. An interactive
    boundary does, which is why this needs a token of its own rather than
    darkening ``--border`` everywhere.
    """

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_something_marks_the_field_boundary_at_3_to_1(self, theme):
        pal = _palette(theme)
        surface = pal.get("card")
        assert surface, "--card is gone from tokens.css"

        candidates = {
            name: value for name, value in pal.items()
            if name in ("border-interactive", "field-border", "input-border", "border-strong")
        }
        best = max(
            (contrast(v, surface) for v in candidates.values()),
            default=contrast(pal.get("border", surface), surface),
        )
        assert best >= 3.0, (
            f"nothing identifies an input's boundary at 3:1 in {theme} "
            f"(best available is {best:.2f}). --border is for decorative "
            "hairlines; an interactive boundary needs its own token"
        )


# ---------------------------------------------------------------------------
# §8 — failure has an anatomy
# ---------------------------------------------------------------------------

class TestFailureIsRendered:
    """The rule: what happened, why, and a next action — as a control, not a
    sentence telling the user to go and do something.

    The load-bearing one is the guard rejection. "Did not run" and "returned
    nothing" must never look the same: an analyst who reads a denied column as
    an empty result concludes the data does not exist, and acts on that.
    """

    @staticmethod
    def _renderers() -> str:
        parts = []
        for name in ("turn.js", "table.js"):
            p = _REPO_ROOT / "web" / "js" / "render" / name
            if p.exists():
                parts.append(p.read_text(encoding="utf-8"))
        assert parts, "no turn/table renderer found"
        return "\n".join(parts)

    @pytest.mark.parametrize(
        "code",
        ["MODEL_UNAVAILABLE", "LLM_OUTPUT_TRUNCATED", "FORBIDDEN_SQL", "QUERY_EXECUTION_ERROR"],
    )
    def test_each_failure_code_has_its_own_rendering(self, code):
        src = self._renderers()
        assert code in src, (
            f"{code} has no branch in the turn renderer, so it falls through to "
            "a generic error. DESIGN.md §2 calls failure a moment that matters; "
            "§5.2 specifies only a successful turn"
        )

    def test_a_guard_rejection_is_not_shown_as_an_empty_result(self):
        """The single most consequential distinction in this module."""
        src = self._renderers()
        assert re.search(r"guard|FORBIDDEN_SQL", src), (
            "nothing in the renderer distinguishes a guard refusal from a "
            "zero-row result. They mean opposite things: one did not run, the "
            "other ran and matched nothing"
        )

    def test_a_failure_offers_an_action_not_just_a_code(self):
        src = self._renderers()
        assert re.search(r"(retry|تلاش|دوباره|createElement\(\"button\")", src, re.I), (
            "failure states render text only. A next action has to be a "
            "control the analyst can press"
        )


# ---------------------------------------------------------------------------
# §9 — what the user switched on is not hidden
# ---------------------------------------------------------------------------

class TestOptInContentStaysInTheMainFlow:
    """`DESIGN.md` §14b records burying assumptions in the drawer as a product
    error and corrects it. The same move was made one item later with the
    interpretation and not caught — and that case is worse, because the
    analyst ticked a box whose label states its cost to ask for it.
    """

    def test_the_interpretation_is_not_rendered_inside_the_details_drawer(self):
        turn_js = _REPO_ROOT / "web" / "js" / "render" / "turn.js"
        src = turn_js.read_text(encoding="utf-8")
        assert "interpretation" in src, "the interpretation is no longer rendered"

        drawer = re.search(r"(details|drawer)[\s\S]{0,1200}?interpretation", src, re.I)
        assert drawer is None, (
            "the interpretation renders inside the collapsed details drawer. "
            "Progressive disclosure applies to what the product chose to show, "
            "never to what the user explicitly requested"
        )


# ---------------------------------------------------------------------------
# §10 — chart emphasis by lightness, not hue
# ---------------------------------------------------------------------------

class TestChartEmphasisSeparatesByLightness:
    """Measured with the dataviz validator: focus↔context contrast was 1.27
    light and **1.03** dark — the same perceived brightness, leaving hue as
    the only signal, which is exactly what colour-blind vision cannot use
    (ΔE 2.2 deutan in dark mode).

    Focus versus context is emphasis, not category. Emphasis is lightness and
    weight; hue is identity.
    """

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_focus_and_context_differ_in_lightness(self, theme):
        css = _TOKENS.read_text(encoding="utf-8") + _STYLE.read_text(encoding="utf-8")
        pal = _palette(theme)

        def resolve(token: str) -> str | None:
            m = re.search(rf"--chart-{token}\s*:\s*([^;]+);", css)
            if not m:
                return None
            value = m.group(1).strip()
            if value.startswith("#"):
                return value
            ref = re.match(r"var\(\s*--([a-z0-9-]+)", value)
            return pal.get(ref.group(1)) if ref else None

        focus, context = resolve("focus"), resolve("context")
        if not focus or not context:
            pytest.skip("--chart-focus / --chart-context are not resolvable to hex")

        ratio = contrast(focus, context)
        assert ratio >= 1.8, (
            f"focus {focus} and context {context} are {ratio:.2f}:1 apart in "
            f"{theme} — effectively the same brightness, so only hue separates "
            "them and a colour-blind analyst sees one line"
        )

    def test_the_stroke_weights_differ_meaningfully(self):
        chart = (_REPO_ROOT / "web" / "js" / "render" / "chart.js").read_text(encoding="utf-8")
        widths = [float(w) for w in re.findall(r'"stroke-width":\s*"([\d.]+)"', chart)]
        assert widths, "no stroke widths found in chart.js"
        assert max(widths) / min(widths) >= 1.8, (
            f"stroke widths {sorted(set(widths))} differ by less than 1.8x. "
            "Weight is one of the redundant encodings that has to carry the "
            "focus/context distinction once hue no longer does"
        )
