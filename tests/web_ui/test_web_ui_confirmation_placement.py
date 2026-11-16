# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Where the «پاسخ درست است» toggle is: on the answer, not in the drawer.

The confirmation only grows the golden set if analysts click it during normal
use. Inside the collapsed «جزئیات» drawer, behind a disclosure, it would
barely be used, so ``web/js/render/turn.js`` puts it in a footer under the
result table or chart and leaves only the wrong-answer flag in the drawer.

Drives the REAL ``turn.js`` (staged with its real render dependency chain,
as ``test_web_ui_request_access.py`` does) and the real ``confirmation.js``
wired the way ``web/js/main.js`` wires it, and asserts, without opening the
drawer:

* the toggle is present exactly once, in the result card's ``.result-footer``,
  after the result and outside ``<details>``;
* it is absent on out-of-scope, guard-rejected and failed turns;
* flagging and confirming still reset each other.

The stylesheet checks pin what a stub DOM cannot observe: the toggle keeps
the same box while it toggles (a fixed minimum size, a pressed state that
sets nothing that changes the box, a status slot that is always one line
tall), the pressed thumb has 3:1 against the button in both themes, and a
check badge, not only the colour, marks the pressed state.

Requires ``node`` on PATH for the behavioural test. Skipped (not failed)
when unavailable.
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.web_ui.css_rules import base_layer, box_properties, declarations, resolve_colour
from tests.web_ui.test_web_ui_contrast_and_failure import _palette, contrast
from tests.web_ui.test_web_ui_request_access import _NODE, _prepare_copies

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HARNESS = Path(__file__).resolve().parent / "run_confirmation_placement.mjs"
_CONFIRMATION_JS = _REPO_ROOT / "web" / "js" / "render" / "confirmation.js"
_STYLE = _REPO_ROOT / "web" / "styles" / "style.css"


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_the_toggle_is_on_the_answer_outside_the_drawer_and_only_for_successful_answers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        turn_mjs = _prepare_copies(Path(tmp))
        confirmation_mjs = Path(tmp) / "confirmation.mjs"
        confirmation_mjs.write_text(_CONFIRMATION_JS.read_text(encoding="utf-8"), encoding="utf-8")
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(turn_mjs), str(confirmation_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        f"confirmation placement check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_turn_js_builds_the_footer_in_the_result_card_and_leaves_only_the_flag_in_the_drawer() -> None:
    source = (_REPO_ROOT / "web" / "js" / "render" / "turn.js").read_text(encoding="utf-8")
    assert 'from "./confirmation.js"' not in source, "the page supplies the control (ctx.renderConfirmation)"

    result_block = source[source.index("let resultEl = null;"):source.index("// 8.5 Interpretation")]
    assert "ctx.renderConfirmation(" in result_block
    assert 'el("div", "result-footer")' in result_block
    assert "resultCard.appendChild(footer)" in result_block

    drawer_block = source[source.index("if (turn.result && ctx.onFlag)"):source.index("drawer.appendChild(drawerBody)")]
    assert "renderConfirmation" not in drawer_block, "no second copy of the toggle in the drawer"
    assert "renderFeedbackControl(" in drawer_block, "the flag control stays in the drawer"
    assert "confirmation.setConfirmed(false)" in drawer_block


def test_main_js_builds_the_control_only_for_a_successful_answer() -> None:
    main = (_REPO_ROOT / "web" / "js" / "main.js").read_text(encoding="utf-8")
    block = main[main.index("renderConfirmation: (turn"):]
    block = block[: block.index("onConfirmed:")]
    assert "if (!canConfirm(turn)) return null;" in block
    assert "api.confirmAnswer(" in block and "api.withdrawConfirmation(" in block


_PRESSED = '.confirm-toggle[aria-pressed="true"]'


def test_the_toggle_does_not_shift_the_answer_when_it_changes_state() -> None:
    css = _STYLE.read_text(encoding="utf-8")
    button = declarations(css, ".confirm-toggle")
    assert "min-inline-size" in button, "the check mark must not change the button's width"
    assert button.get("box-sizing") == "border-box"
    assert "min-block-size" in button
    changed = box_properties(css, _PRESSED)
    assert not changed, (
        f"the pressed state sets {changed}. A state may change colour, shadow and the "
        "badge; anything that changes the box moves the answer beside it"
    )
    status = declarations(css, ".confirmation-status")
    assert "min-block-size" in status, "the message slot is always reserved"
    assert status.get("white-space") == "nowrap" and status.get("text-overflow") == "ellipsis", (
        "a long message cannot make the row taller"
    )
    assert "min-block-size" in declarations(css, ".confirmation-control")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_pressed_state_is_visible_in_both_themes(theme: str) -> None:
    """WCAG 1.4.11: the thumb and its badge are the only visible sign that an
    answer is confirmed, so they need 3:1 against the button they sit on.
    Measured against the flat layer of the pressed background; the gradient
    over it is tinted with the same colour and can only lower the ratio."""
    css = _STYLE.read_text(encoding="utf-8")
    palette = _palette(theme)
    pressed = declarations(css, _PRESSED)
    glyph = resolve_colour(pressed["color"], palette)
    ground = resolve_colour(base_layer(pressed["background"]), palette)
    ratio = contrast(glyph, ground)
    assert ratio >= 3.0, (
        f"{theme}: the pressed thumb is {glyph} on {ground}, {ratio:.2f}:1. "
        "Non-text contrast needs 3:1 (WCAG 1.4.11); draw it with a darker token, "
        "as the status line does with --status-good-ink"
    )


def test_the_pressed_state_is_not_shown_by_colour_alone() -> None:
    css = _STYLE.read_text(encoding="utf-8")
    idle = declarations(css, ".confirm-toggle::after")
    pressed = declarations(css, f"{_PRESSED}::after")
    assert idle.get("opacity") == "0" and pressed.get("opacity") == "1", (
        "the check badge is the shape that says 'confirmed': hidden until pressed, shown after"
    )
    # ...and the check mark is in the text (see test_web_ui_confirmation_control.py).
    assert "\u2713" in _CONFIRMATION_JS.read_text(encoding="utf-8")


def test_no_physical_left_or_right_in_the_new_rules() -> None:
    css = _STYLE.read_text(encoding="utf-8")
    block = css[css.index('/* ── "This answer is correct" confirmation'):css.index('/* ── "Request access" (ADR-004 part 1)')]
    assert not re.search(r"\b(margin|padding|border)-(left|right)\b|\b(left|right)\s*:", block)
