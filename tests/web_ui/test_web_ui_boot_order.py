# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""main.js must survive its own module-load in simulated mode.

`web/js/main.js` runs `setMode(state.mode)` as a top-level statement during
module evaluation (boot). In simulated mode that calls `setHealth(...)`
*synchronously* — the live path defers through the async `refreshHealth()`,
which is exactly why this defect was invisible in live mode and total in demo
mode.

`setHealth` reads the module-scope constant `HEALTH_STATE_LABEL_KEY`. When the
redundant-encoding fix landed, that constant was declared *below* the boot
line (line 289 vs the `setMode` call at line 84), so at boot the constant is
still in its temporal dead zone. The result is a hard

    ReferenceError: Cannot access 'HEALTH_STATE_LABEL_KEY' before initialization

thrown during module evaluation, which aborts the rest of `main.js` — every
listener registered after the boot call never binds, the sample-story chips
never render, and the whole simulated experience is dead. Observed live in a
browser at `?live=0`; the page loads to an empty shell and the console carries
the ReferenceError.

Why a source-order assertion and not a full boot test
-----------------------------------------------------
Faithfully reproducing the boot means executing `main.js`'s top level, which
pulls its entire import graph (state, data, api, every renderer, i18n) and a
real DOM/localStorage/fetch — far more machinery than this one-line ordering
bug warrants, and none of the existing `.mjs` harnesses drive `main.js` itself.
A real-browser smoke test would catch this and its siblings (the SQL-highlight
and hidden-chart defects) in one shot, and is worth adding as its own
infrastructure; this test guards the specific regression cheaply in the
meantime.

The rule this encodes: anything `setHealth` dereferences during a synchronous
boot must be initialized before the boot call. The fix is to declare
`HEALTH_STATE_LABEL_KEY` before `setMode(state.mode)` (with the other
module constants near the top), or to make it local to `setHealth`.
"""

from __future__ import annotations

import re
from pathlib import Path

_MAIN_JS = Path(__file__).resolve().parent.parent.parent / "web" / "js" / "main.js"


def _line_of(pattern: str, lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if re.search(pattern, line):
            return i
    return None


class TestSimulatedBootHasNoTemporalDeadZone:
    def test_health_label_map_is_initialized_before_the_boot_call(self):
        src = _MAIN_JS.read_text(encoding="utf-8")
        lines = src.splitlines()

        boot = _line_of(r"^\s*setMode\(\s*state\.mode\s*\)", lines)
        assert boot is not None, (
            "the top-level `setMode(state.mode)` boot call is gone from main.js; "
            "if boot changed shape, re-point this assertion at the new boot path"
        )

        # setHealth reads HEALTH_STATE_LABEL_KEY. Two safe shapes: a module
        # const declared before the boot line, or a const local to setHealth.
        module_decl = _line_of(r"^const HEALTH_STATE_LABEL_KEY\b", lines)
        local_decl = _line_of(r"^\s+const HEALTH_STATE_LABEL_KEY\b", lines)

        if module_decl is not None:
            assert module_decl < boot, (
                f"HEALTH_STATE_LABEL_KEY is declared at module scope on line "
                f"{module_decl + 1}, after the boot call on line {boot + 1}. "
                "In simulated mode setMode() calls setHealth() synchronously at "
                "boot, so reading this const throws a ReferenceError (temporal "
                "dead zone) and aborts the rest of main.js. Declare it before "
                "the boot call, or make it local to setHealth"
            )
        else:
            assert local_decl is not None, (
                "HEALTH_STATE_LABEL_KEY is neither a module const before the "
                "boot call nor local to setHealth — setHealth still reads it "
                f"(see below), and the boot call is on line {boot + 1}"
            )

    def test_set_health_still_depends_on_that_map(self):
        """Guard the premise: if setHealth stops using the const entirely, the
        ordering test above is guarding a dependency that no longer exists and
        should be rewritten rather than passing on absence."""
        src = _MAIN_JS.read_text(encoding="utf-8")
        body = src.split("function setHealth", 1)
        assert len(body) == 2, "setHealth is gone from main.js"
        nearby = body[1][:800]
        assert "HEALTH_STATE_LABEL_KEY" in nearby, (
            "setHealth no longer references HEALTH_STATE_LABEL_KEY; the boot-order "
            "test is now guarding a stale dependency and should be re-pointed at "
            "whatever setHealth reads at boot instead"
        )
