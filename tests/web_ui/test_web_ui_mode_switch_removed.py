# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The run-mode switch is gone from the topbar, and nothing still reaches for it.

Run mode decides whether the numbers on screen came from the warehouse or
from `web/js/data.js`'s synthetic fixtures. While that lived in the topbar
as a two-button switch, one mis-click sat between an analyst and a screen
full of invented figures that look exactly like real ones -- the UI offers
no other signal that a price or a tonnage is fabricated. Deploy/debug
concerns do not belong one pixel away from real controls, so the switch was
removed and the escape hatch moved to the URL (`?live=0`), where reaching
simulated mode takes intent rather than a slip.

Spec: `docs/design/IMPLEMENTATION.md`, Train B step B2 --
"Remove mode switch from DOM (keep `?live=0`)".

This module guards the removal three ways, because "delete the markup" has
two distinct failure modes that a markup-only assertion would miss:

* **Dangling lookups.** `main.js` called `$("mode-simulated")` in two places
  (`wireTopbar`, and `setMode` itself). `$` resolves by id and returns null
  for a node that no longer exists, so a leftover call throws on a null
  `.classList` / `.addEventListener` at boot. `setMode(state.mode)` runs as
  a top-level statement during module evaluation, so that throw kills the
  ENTIRE app before first paint -- the same class of silent, total failure
  the boot-order regression caused (`test_web_ui_boot_order.py`). Grepping
  the markup alone would call that fixed.

* **A gutted `setMode`.** The tempting way to kill the dangling lookups is
  to delete `setMode` with them. It is not dead code: it writes the footer
  provenance string, drives `setHealth`/`refreshHealth`, and is still called
  at boot, on language change, and by the `V2NotSupportedError` fallback.
  Removing it would silently drop the footer line that tells the analyst
  which mode produced what they are reading.

The `?live=0` behaviour itself stays covered by the existing
`tests/web_ui/test_web_ui_live_default.py` / `run_live_default.mjs`, which
B2 says to extend rather than replace. This module deliberately does not
duplicate them; it asserts only that the escape hatch they exercise is
still wired up at all.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WEB = _REPO_ROOT / "web"
_INDEX = _WEB / "index.html"
_MAIN_JS = _WEB / "js" / "main.js"
_STATE_JS = _WEB / "js" / "state.js"
_STYLE = _WEB / "styles" / "style.css"


def test_topbar_markup_no_longer_ships_the_mode_switch() -> None:
    html = _INDEX.read_text(encoding="utf-8")
    for needle in ('id="mode-switch"', 'id="mode-simulated"', 'id="mode-live"',
                   'class="mode-switch"', "mode-btn"):
        assert needle not in html, (
            f"web/index.html still ships {needle!r}: the run-mode switch is back in "
            "the analyst topbar. Run mode is reachable via ?live=0 only (B2)."
        )


def test_no_javascript_still_reaches_for_the_removed_nodes() -> None:
    """A leftover `$("mode-live")` throws at boot and takes the whole app down.

    Matched as the literal call shape rather than the bare word so that prose
    in a comment ("the topbar's mode-switch buttons") can never trip this --
    the assertion is about live DOM lookups, not about vocabulary.
    """
    lookup = re.compile(r"""(?:\$\(|getElementById\()\s*["']mode-(?:simulated|live|switch)["']""")
    offenders: list[str] = []
    for path in sorted((_WEB / "js").rglob("*.js")):
        if "vendor" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if lookup.search(line):
                offenders.append(f"{path.relative_to(_REPO_ROOT).as_posix()}:{lineno}: {line.strip()}")
    assert not offenders, (
        "JavaScript still looks up mode-switch nodes that no longer exist in the DOM. "
        "$() returns null for a missing id, so these throw during module evaluation "
        "and the app never paints:\n" + "\n".join(offenders)
    )


def test_set_mode_survived_the_removal_with_its_real_work_intact() -> None:
    """`setMode` outlived its buttons -- it still carries mode provenance."""
    src = _MAIN_JS.read_text(encoding="utf-8")
    assert re.search(r"function\s+setMode\s*\(", src), (
        "setMode() was deleted along with the mode switch. It is not dead code: it is "
        "still called at boot, on language change, and by the V2NotSupportedError "
        "fallback, and it owns the footer string that states which mode produced "
        "the numbers on screen."
    )
    for marker in ("foot-mode", "refreshSessionsForMode"):
        assert marker in src, (
            f"setMode()'s {marker!r} work is gone. Removing the switch must not remove "
            "the footer provenance line or the per-mode session refresh."
        )


def test_the_url_escape_hatch_is_still_wired() -> None:
    """`?live=0` is now the ONLY way into simulated mode; it must still exist."""
    state = _STATE_JS.read_text(encoding="utf-8")
    assert "export function resolveBootMode" in state, (
        "resolveBootMode() is gone from state.js. With the topbar switch removed, "
        "?live=0 is the only remaining route into simulated mode -- B2 removes the "
        "control, it does not remove the capability."
    )


def test_stylesheet_dropped_the_dead_mode_switch_rules() -> None:
    css = _STYLE.read_text(encoding="utf-8")
    for selector in (".mode-switch", ".mode-btn"):
        assert selector not in css, (
            f"style.css still carries {selector}, styling markup that no longer exists."
        )
