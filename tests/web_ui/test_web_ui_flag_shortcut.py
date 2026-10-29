# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The «پاسخ نادرست است» button on the answer (#223).

The wrong-answer flag lived only in the collapsed «جزئیات» drawer, worded
«این عدد درست نیست؟», so analysts who used «پاسخ درست است» under the result
did not find its negative twin. ``web/js/render/turn.js`` now puts a
«پاسخ نادرست است» button in the same ``.result-footer``. It sends nothing
itself: it opens the drawer's flag form (category and note, as before) and
focuses it. The drawer's control stays where it was.

Drives the REAL ``turn.js`` (staged with its real render dependency chain,
as ``test_web_ui_confirmation_placement.py`` does) and the real
``confirmation.js``; see ``run_flag_shortcut.mjs`` for the scenarios.

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

from tests.web_ui.test_web_ui_request_access import _NODE, _prepare_copies

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HARNESS = Path(__file__).resolve().parent / "run_flag_shortcut.mjs"
_CONFIRMATION_JS = _REPO_ROOT / "web" / "js" / "render" / "confirmation.js"
_STYLE = _REPO_ROOT / "web" / "styles" / "style.css"
_TOKENS = _REPO_ROOT / "web" / "styles" / "tokens.css"


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_the_button_opens_the_flag_form_from_the_answer() -> None:
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
        f"flag shortcut check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def _rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css, re.S)
    assert match, f"no rule for {selector}"
    return match.group(1)


def test_the_button_has_the_toggle_box_and_a_flagged_state_in_both_themes() -> None:
    css = _STYLE.read_text(encoding="utf-8")
    tokens = _TOKENS.read_text(encoding="utf-8")
    button = _rule(css, ".flag-shortcut")
    for prop in ("min-inline-size", "box-sizing: border-box", "min-block-size"):
        assert prop in button, f".flag-shortcut lacks {prop}: the check mark must not resize it"
    flagged = _rule(css, ".flag-shortcut.is-flagged")
    assert "border-width: 2px" in flagged and "padding-block" in flagged and "padding-inline" in flagged
    for token in ("--status-critical", "--status-critical-bg", "--ink"):
        assert f"var({token})" in flagged
        assert len(re.findall(re.escape(token) + r"\s*:", tokens)) >= 2, f"{token} has no dark value"
    assert "flex-wrap: wrap" in _rule(css, ".result-footer"), "on a narrow screen the two buttons wrap"


def test_no_physical_left_or_right_in_the_footer_rules() -> None:
    css = _STYLE.read_text(encoding="utf-8")
    block = css[css.index('/* ── "This answer is correct" confirmation'):css.index('/* ── "Request access" (ADR-004 part 1)')]
    assert not re.search(r"\b(margin|padding|border)-(left|right)\b|\b(left|right)\s*:", block)
