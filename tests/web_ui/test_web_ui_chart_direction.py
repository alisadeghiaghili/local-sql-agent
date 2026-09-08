# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The chart SVG must pin its own text direction.

``web/js/render/chart.js`` computes every label position left-to-right: x
grows rightward, and ``edgeSafeLabel`` flips ``text-anchor`` between
``"start"`` and ``"end"`` to keep a label inside ``[0, W]``. But
``text-anchor`` resolves against the element's **inline base direction**,
and both pages that host a chart are ``dir="rtl"``. Without an explicit
``direction: ltr`` on the SVG, every one of those anchor decisions meant
its opposite.

Measured in a browser before the fix: a label clamped to ``x=2`` with
anchor ``"start"`` rendered at ``x=-140`` — 140 units outside the box, on
the exact side the clamp existed to protect. Every label the function
tried to rescue was precisely the one it threw out. Paired with
``overflow: visible``, those labels did not even get clipped: they drew
over the card around the chart.

Why this is a CSS text check rather than a rendering test
---------------------------------------------------------
``tests/web_ui/run_result_shapes.mjs`` already drives the real chart.js,
but against a hand-written stub DOM — "only the handful of DOM primitives
table.js/chart.js actually call". A stub has no layout, no fonts and no
bidi algorithm, so it cannot observe this class of bug at all, and adding
a headless browser for one declaration would be a large amount of
machinery to assert something a two-line check states directly.

What matters is that the declaration is *present and deliberate*, because
its absence is the whole defect. If chart geometry is ever rewritten to be
direction-aware, this test should be deleted along with the CSS — not
worked around.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_STYLE_CSS = _REPO_ROOT / "web" / "styles" / "style.css"
_CHART_JS = _REPO_ROOT / "web" / "js" / "render" / "chart.js"


def _chart_svg_rule() -> str:
    css = _STYLE_CSS.read_text(encoding="utf-8")
    match = re.search(r"\.chart-block\s+svg\s*\{(.*?)\}", css, re.S)
    assert match, ".chart-block svg rule is gone from web/styles/style.css"
    return match.group(1)


def test_the_chart_svg_declares_ltr():
    rule = _chart_svg_rule()
    assert re.search(r"direction\s*:\s*ltr", rule), (
        "web/styles/style.css no longer pins `direction: ltr` on .chart-block svg. "
        "Both host pages are dir=\"rtl\", and chart.js's edgeSafeLabel decides "
        "text-anchor start/end against LTR geometry -- without this the anchors "
        "invert and labels land outside the viewBox"
    )


def test_the_chart_svg_clips_rather_than_spilling():
    rule = _chart_svg_rule()
    assert not re.search(r"overflow\s*:\s*visible", rule), (
        "overflow: visible lets a mispositioned label escape the SVG and draw "
        "over the surrounding card. Clipping keeps a geometry mistake inside "
        "the chart, where it reads as a cut-off label rather than as a broken page"
    )
    assert re.search(r"overflow\s*:\s*hidden", rule)


def test_the_geometry_this_protects_is_still_direction_naive():
    """If chart.js ever became direction-aware, the CSS pin would be the
    wrong fix rather than the right one — so this test's own premise is
    checked instead of assumed."""
    js = _CHART_JS.read_text(encoding="utf-8")
    assert 'anchor: "start"' in js and 'anchor: "end"' in js, (
        "edgeSafeLabel no longer emits literal start/end anchors. If the "
        "chart now resolves anchors against the document's direction, delete "
        "this module and the CSS pin together rather than keeping both"
    )


@pytest.mark.parametrize("page", ["index.html", "admin/index.html"])
def test_the_host_pages_are_still_rtl(page):
    """The reason the pin is needed. If a page ever stops being RTL this
    test says so, rather than leaving a declaration nobody can justify."""
    html = (_REPO_ROOT / "web" / page).read_text(encoding="utf-8")
    assert 'dir="rtl"' in html
