# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The analyst-facing "this answer is correct" control (پاسخ درست است) --
``web/js/render/confirmation.js``.

Drives the REAL source under Node (see ``run_confirmation_control.mjs`` in
this directory for the scenario list and its minimal DOM shim) and asserts,
at the boundary that matters:

* the control is a real, keyboard-reachable ``<button>`` with a Persian
  label and ``aria-pressed`` carrying its state; the pressed state is also a
  check mark, not colour alone;
* pressing it confirms and pressing it again withdraws; an already
  confirmed turn renders pressed;
* it is offered only on a successful answer (``canConfirm``);
* a rejected request (the server's 409, a network error) keeps the previous
  state, says why in a ``role="status"`` line and leaves the control usable;
* confirming a turn the analyst had flagged reports the replaced flag, and
  the flag control's ``reopen()`` makes it usable again.

Where the control sits (on the answer, outside the details drawer) is pinned
by ``test_web_ui_confirmation_placement.py``. One source-level check here pins
what the shim cannot observe: the stylesheet styles the pressed state with
design tokens and logical properties only (RTL).

Requires ``node`` on PATH. Skipped (not failed) when unavailable.
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RENDER_DIR = _REPO_ROOT / "web" / "js" / "render"
_HARNESS = Path(__file__).resolve().parent / "run_confirmation_control.mjs"
_STYLE = _REPO_ROOT / "web" / "styles" / "style.css"

_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_confirmation_control_is_an_accessible_toggle_that_recovers_from_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        copies = []
        for name in ("confirmation", "feedback"):
            dest = Path(tmp) / f"{name}.mjs"
            # Neither module has internal ./*.js imports, so a byte-identical copy suffices.
            dest.write_text((_RENDER_DIR / f"{name}.js").read_text(encoding="utf-8"), encoding="utf-8")
            copies.append(str(dest))
        result = subprocess.run(
            [_NODE, str(_HARNESS), *copies],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        f"confirmation control check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_confirmation_styles_use_tokens_and_logical_properties_only() -> None:
    css = _STYLE.read_text(encoding="utf-8")
    start = css.index('/* ── "This answer is correct" confirmation')
    end = css.index('/* ── "Request access" (ADR-004 part 1)')
    block = css[start:end]
    assert '.confirm-toggle[aria-pressed="true"]' in block, "the pressed state is styled from aria-pressed"
    assert ".confirm-toggle:focus-visible" in block, "keyboard focus must be visible"
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block), "colours must come from design tokens"
    assert not re.search(r"\b(margin|padding)-(left|right)\b|\b(left|right)\s*:", block), (
        "use logical properties so the control flips with dir=rtl"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
