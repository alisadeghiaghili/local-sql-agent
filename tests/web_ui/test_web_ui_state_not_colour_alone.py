# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The health rail says nothing unless you can see colour.

``web/index.html`` renders three indicators and ``web/js/main.js::setHealth``
rewrites them on every poll:

.. code-block:: javascript

    const dot = (ok) => (ok === null ? "unknown" : ok ? "ok" : "down");
    $("health").innerHTML = [
      ["API", api_], ["LLM", llm], ["DB", db],
    ].map(([name, ok]) => `<span class="pill"><span class="dot dot-${dot(ok)}"></span>${name}</span>`).join("");

Every state produces the **same text**. "API", "LLM", "DB" — up, down, or
unknown. The only thing that changes is a CSS class whose sole effect is a
background colour (``style.css``: ``.dot-ok{background:var(--status-good)}``,
``.dot-down{background:var(--status-critical)}``,
``.dot-unknown{background:var(--status-warn)}``). The dot element is empty: no
text, no glyph, no ``aria-label``.

Two populations get nothing from it.

**Colour-vision-deficient readers.** Measured with a Viénot dichromat
simulation over the real tokens, OKLab ΔE:

===========================  ==========  ===========  ===========
pair                          contrast    ΔE normal    ΔE deutan
===========================  ==========  ===========  ===========
good ↔ critical (light)          1.54        32.9        **8.6**
good ↔ critical (dark)           1.44        30.8        **7.1**
===========================  ==========  ===========  ===========

Under deuteranopia the "up" and "down" dots render ``#b7b79c`` and ``#a8a86b``
in dark mode — two olive dots 1.44:1 apart. ``DESIGN-INVARIANTS.md`` §10 sets
ΔE ≥ 8 as the floor; dark mode is below it. And because the contrast between
them is 1.44, lightness carries nothing either: there is no second signal to
fall back on.

**Screen-reader users.** ``#health`` carries ``role="status"
aria-live="polite"``, so it *is* announced — and what it announces is
"API LLM DB", in every state, forever. The status region is wired correctly
and has no status in it.

This is WCAG 2.1 **1.4.1 Use of Color**: colour is the only visual means of
conveying information. It is a Level A failure — the lowest bar the standard
sets — in the one component that tells an analyst whether the answers they are
reading came from a working database.

Why this module asserts three separate things
---------------------------------------------
A fix that only recolours the dots would satisfy a naive contrast check and
still fail 1.4.1, because the state would still be carried by colour alone —
just more distinguishable colour. A fix that only adds text would leave the
measured ΔE below §10's own floor. So the redundant encoding and the palette
separation are asserted independently, and neither substitutes for the other.

The third assertion is ``DESIGN-INVARIANTS.md`` §1.3: this is the last
``innerHTML`` string-interpolation site in the health path, and the rule is
"build DOM, not HTML strings, for anything carrying a value this process did
not author". ``h.llmDetail`` and ``h.dbDetail`` come off the wire.

What this module deliberately does **not** assert
--------------------------------------------------
A sweep of every state marker in ``web/`` found the health rail is the only
true colour-only case. Each of the following was measured or read before being
excluded, and none is a defect:

* the guard pill (``turn.js``) — ``✓ مجاز`` / ``✕ رد شد``: distinct glyph *and*
  distinct text;
* admin status pills — ``PASS``/``FAIL``/``SKIP``, ``تازه``/``کهنه``/``هرگز``,
  ``فعال``/``غیرفعال``/``ابطال‌شده``: distinct text per state;
* the cache badge — ``prefix cache HIT`` / ``prefix cache MISS``;
* the prefill/decode legend — measured ΔE deutan **16.2** light, **15.1** dark,
  above §10's floor and above its hard 15. Not a defect.

Recording the exclusions matters as much as the inclusion: a test module that
asserted all of them would have to be weakened later when someone measured
them, and a weakened test is worse than an absent one.

Policy: ``docs/design/DESIGN-INVARIANTS.md`` §1.3, §10; WCAG 2.1 SC 1.4.1.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MAIN_JS = _REPO_ROOT / "web" / "js" / "main.js"
_TOKENS = _REPO_ROOT / "web" / "styles" / "tokens.css"


# ---------------------------------------------------------------------------
# Colour science — the same arithmetic the contrast module keeps inline, and
# for the same reason: a hex tells a later reader nothing about whether a
# change was safe. Only the computation does.
# ---------------------------------------------------------------------------

def _srgb_to_linear(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _channels(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)


def contrast(a: str, b: str) -> float:
    """WCAG 2.1 contrast ratio between two opaque colours."""
    def lum(hex_colour: str) -> float:
        r, g, b = _channels(hex_colour)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def simulate_deutan(hex_colour: str) -> tuple[float, float, float]:
    """Viénot-Brettel-Mollon (1999) deuteranopia simulation, in linear RGB.

    Deuteranopia is the common case (~6% of men) and the one that collapses a
    green/red status pair, which is exactly the pair this module is about.
    Returned in linear RGB because the caller converts straight to OKLab;
    round-tripping through 8-bit sRGB would quantise away the difference the
    assertion is trying to measure.
    """
    r, g, b = _channels(hex_colour)
    long_ = 17.8824 * r + 43.5161 * g + 4.11935 * b
    medium = 3.45565 * r + 27.1554 * g + 3.86714 * b
    short = 0.0299566 * r + 0.184309 * g + 1.46709 * b

    # The dichromat projection: a deuteranope's M response is not independent,
    # it is reconstructed from L and S.
    medium = 0.494207 * long_ + 1.24827 * short

    return (
        0.080944 * long_ - 0.130504 * medium + 0.116721 * short,
        -0.0102485 * long_ + 0.0540194 * medium - 0.113615 * short,
        -0.000365294 * long_ - 0.00412163 * medium + 0.693513 * short,
    )


def _oklab(linear_rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = (max(0.0, min(1.0, c)) for c in linear_rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


def delta_e_deutan(a: str, b: str) -> float:
    """OKLab distance between two colours *as a deuteranope sees them*."""
    la, lb = _oklab(simulate_deutan(a)), _oklab(simulate_deutan(b))
    return sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5 * 100


def _palette(theme: str) -> dict[str, str]:
    css = _TOKENS.read_text(encoding="utf-8")
    if theme == "light":
        block = css.split("@media")[0]
    else:
        marker = "prefers-color-scheme: dark"
        assert marker in css, "the dark theme block is gone from tokens.css"
        block = css.split(marker, 1)[1]
    return dict(re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", block))


def _dot_rule(state: str) -> str:
    """The declaration block for ``.dot-<state>``, or ``""`` if there is none.

    Read from ``style.css`` rather than ``tokens.css``: the tokens file holds
    the colour *values*, and the question this module asks is whether the dot
    classes declare anything at all that is **not** a colour. At the time of
    writing each of the three rules is a single ``background`` declaration,
    which is precisely the defect.
    """
    css = (_REPO_ROOT / "web" / "styles" / "style.css").read_text(encoding="utf-8")
    match = re.search(rf"\.dot-{re.escape(state)}\b[^{{]*\{{([^}}]*)\}}", css)
    return match.group(1) if match else ""


def _set_health_source() -> str:
    """The body of ``setHealth``, which is the whole surface under test.

    Sliced from the function keyword to the next top-level ``function`` rather
    than regex-matching a brace-balanced block: the body contains template
    literals with braces in them, and a naive balance count reads those as
    structure. If the function is renamed or removed the slice fails loudly,
    which is the correct outcome — this module would otherwise pass by
    asserting nothing.
    """
    src = _MAIN_JS.read_text(encoding="utf-8")
    start = src.find("function setHealth")
    assert start != -1, (
        "setHealth is gone from web/js/main.js. If the health rail moved, "
        "re-point this module at its new home rather than deleting it"
    )
    nxt = src.find("\nfunction ", start + 1)
    nxt = nxt if nxt != -1 else len(src)
    return src[start:nxt]


# ---------------------------------------------------------------------------
# 1.4.1 — colour is never the only carrier
# ---------------------------------------------------------------------------

class TestTheHealthStateSurvivesWithoutColour:
    """An analyst who cannot distinguish the dots must still learn whether the
    database is reachable, and so must one who is listening rather than
    looking."""

    def test_something_other_than_colour_distinguishes_the_three_states(self):
        """Up, down and unknown must differ by something a colour-blind reader
        can perceive — a word, a glyph, or a shape.

        Two fix paths are legitimate and this accepts either:

        * **text** — ``setHealth`` gives each state its own human-readable
          label (the better fix, because it also reaches screen readers), or
        * **shape** — ``.dot-ok`` / ``.dot-down`` / ``.dot-unknown`` carry a
          distinguishing declaration that is not a colour (a glyph via
          ``content``, a different ``border-radius``, a ``clip-path``).

        What does **not** count, and why this test was rewritten twice:

        1. An early draft accepted ``title =`` as evidence, and so passed
           against the unfixed code — ``setHealth`` already ends with
           ``$("health").title = label``. That tooltip is hover-only
           (unreachable by touch and by keyboard) and it is one string for the
           whole rail rather than a state per indicator.
        2. The next draft counted "distinct string literals that are not class
           fragments", intending to spot a state→label map. The regex paired a
           *closing* quote with the next *opening* one, so in
           ``? "ok" : "down"`` it captured the ``" : "`` between them, and in
           ``["API", api_], ["LLM"`` it captured ``, api_], [``. Three pieces
           of punctuation cleared the bar and the test went green.

        Both drafts reported the opposite of the truth, which is worse than no
        test. So this asserts a **mechanism** rather than trying to recognise a
        label by shape: either the state reaches the accessible name/text, or
        the CSS gives the three classes a non-colour affordance. Those are the
        only two ways the information can arrive without colour, and neither
        can be satisfied by punctuation.
        """
        body = _set_health_source()

        has_state_text = bool(
            re.search(
                r'(aria-label|\.textContent\s*=|createTextNode|setAttribute\(\s*["\']aria-)',
                body,
            )
        )

        colour_props = {
            "background", "background-color", "color", "border-color",
            "outline-color", "fill", "stroke",
        }
        shaped = []
        for state in ("ok", "down", "unknown"):
            rule = _dot_rule(state)
            non_colour = {
                prop.strip().lower()
                for prop, _ in re.findall(r"([a-z-]+)\s*:\s*([^;]+);", rule)
                if prop.strip().lower() not in colour_props
            }
            shaped.append(non_colour)
        has_shape = all(s for s in shaped) and len({frozenset(s) for s in shaped}) > 1

        assert has_state_text or has_shape, (
            "nothing but colour distinguishes the three health states. Every "
            "pill renders the same text ('API', 'LLM', 'DB'), the dot span is "
            "empty, and .dot-ok/.dot-down/.dot-unknown differ only in "
            "background. WCAG 1.4.1 (Level A). Fix by giving each state a "
            "label in setHealth, or a non-colour affordance in the CSS — the "
            "title attribute is not either one: it is hover-only and covers "
            "the whole rail"
        )

    def test_the_live_region_announces_the_state_not_just_the_names(self):
        """``#health`` is ``role="status" aria-live="polite"``, so whatever it
        contains is read aloud on every poll. Today that is "API LLM DB",
        identically, whether the warehouse is reachable or unreachable — a
        correctly wired status region with no status in it.

        A shape-only fix satisfies the test above and still fails this one,
        which is why the two are separate: ``content: "✕"`` on a pseudo-element
        is not reliably announced, and a CSS-only cue reaches nobody who is
        listening. Something has to put the state into the DOM as text.
        """
        body = _set_health_source()
        mechanism = re.search(
            r'(aria-label|aria-describedby|\.textContent\s*=|createTextNode|setAttribute\(\s*["\']aria-)',
            body,
        )
        assert mechanism, (
            "nothing in setHealth puts the state into the accessible name or "
            "text of the live region. A screen reader announces 'API LLM DB' "
            "in every state, which is the same as announcing nothing. Note "
            "that `title =` does not satisfy this: an accessible name is not "
            "a tooltip"
        )

    def test_the_health_rail_is_built_as_dom_not_an_interpolated_string(self):
        """``DESIGN-INVARIANTS.md`` §1.3: build DOM, not HTML strings, for
        anything carrying a value this process did not author.

        ``refreshHealth`` passes ``h.llmDetail`` and ``h.dbDetail`` — server
        text — into this function. The rule is not conditional on whether
        today's particular interpolation happens to be exploitable; it exists
        so that the next edit to this line cannot introduce a sink.
        """
        body = _set_health_source()
        assert "innerHTML" not in body, (
            "setHealth still assembles the rail with innerHTML and template "
            "interpolation. Values from /health reach this function; build "
            "the pills with createElement/textContent per §1.3"
        )


# ---------------------------------------------------------------------------
# §10 — and when colour *is* used, it has to separate
# ---------------------------------------------------------------------------

class TestTheStatusPaletteSeparatesForColourBlindReaders:
    """Redundant encoding is the fix for 1.4.1; it is not a licence to leave
    the palette indistinguishable. ``DESIGN-INVARIANTS.md`` §10 sets ΔE ≥ 8.

    Measured at the time of writing: **8.6** light, **7.1** dark. Dark is
    already below the floor the project set for itself, which is why this is
    asserted rather than assumed.
    """

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_good_and_critical_are_distinguishable_under_deuteranopia(self, theme):
        pal = _palette(theme)
        for token in ("status-good", "status-critical"):
            assert token in pal, f"--{token} is gone from tokens.css"

        good, critical = pal["status-good"], pal["status-critical"]
        separation = delta_e_deutan(good, critical)
        assert separation >= 8.0, (
            f"'up' ({good}) and 'down' ({critical}) are ΔE {separation:.1f} "
            f"apart under deuteranopia in {theme} — below the ΔE >= 8 floor "
            "DESIGN-INVARIANTS.md §10 sets. These two colours are the entire "
            "difference between 'the warehouse answered' and 'it did not'"
        )

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_good_and_critical_also_differ_in_lightness(self, theme):
        """§10's deeper rule: hue is identity, lightness is emphasis. Two
        status colours at the same brightness leave hue as the sole signal,
        which is the condition that makes the ΔE above collapse in the first
        place. Measured 1.54 light / 1.44 dark — effectively identical
        brightness.
        """
        pal = _palette(theme)
        ratio = contrast(pal["status-good"], pal["status-critical"])
        assert ratio >= 1.8, (
            f"--status-good and --status-critical are {ratio:.2f}:1 apart in "
            f"{theme}: the same perceived brightness, so hue is the only "
            "thing separating them and a dichromat sees one colour"
        )
