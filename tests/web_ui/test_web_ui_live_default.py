# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test for the live-by-default change: ``web/js/state.js``'s
``state.mode`` now initializes to ``"live"`` instead of ``"simulated"``, and
the backend base URL now resolves through a deploy-time default
(``web/js/config.js``'s ``DEFAULT_BASE_URL``) with the same override
precedence as before (a persisted ``localStorage`` value, then ``?base=``
on top of that).

Before this change, a fresh load of a LIVE deployment showed the
simulated-mode demo data by default -- synthetic, made-up numbers, rendered
in the exact same UI as real ones -- until an analyst noticed and clicked
the "زندهٔ API" toggle themselves. There was also no supported way to force
simulated mode via the URL (``?live=0``), since simulated was already the
default and nothing needed overriding.

This test drives the REAL ``web/js/state.js`` and ``web/js/config.js``
source under Node (see ``run_live_default.mjs`` in this directory for the
full scenario list) and asserts, at the actual boundary that changed:

* a fresh import of ``state.js`` has ``state.mode === "live"`` -- the
  default itself, not merely that some helper function CAN return "live";
* ``config.js`` exports a real, non-empty ``DEFAULT_BASE_URL`` -- the "one
  file a deployment edits" this change adds for the backend base URL that
  used to be a visible, analyst-facing top-bar control;
* ``resolveBootMode``'s precedence: ``?live=1`` -> "live", ``?live=0`` ->
  "simulated" (this exact override is new), anything else -> the given
  default, unchanged;
* ``resolveBootBaseUrl``'s precedence, proven through a REAL
  ``loadPersisted()`` round-trip against a mocked ``localStorage`` (not a
  hand-built string): a value already persisted to this browser survives
  into the resolved boot base URL when no ``?base=`` is given, and
  ``?base=`` still overrides it when one is -- the exact two guarantees
  the brief requires to keep working once the visible base-URL row is
  removed from the top bar (see ``web/index.html`` / ``web/js/main.js``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WEB_JS = _REPO_ROOT / "web" / "js"
_STATE_JS = _WEB_JS / "state.js"
_CONFIG_JS = _WEB_JS / "config.js"
_HARNESS = Path(__file__).resolve().parent / "run_live_default.mjs"

_NODE = shutil.which("node")


def _prepare_copies(tmp_path: Path) -> tuple[Path, Path]:
    """Copy state.js/config.js into *tmp_path* as ESM (``.mjs``). Neither
    file has an internal import specifier to rewrite (config.js has no
    imports at all; state.js does not import config.js -- see
    web/js/main.js, which is where DEFAULT_BASE_URL actually gets wired
    in), so this is a byte-identical copy, extension aside.
    """
    state_src = _STATE_JS.read_text(encoding="utf-8")
    config_src = _CONFIG_JS.read_text(encoding="utf-8")
    assert "import" not in state_src.split("\n")[0:20] or "./config.js" not in state_src, (
        "state.js now imports config.js -- update this test (and "
        "run_live_default.mjs's copy step) to rewrite that import path to "
        "./config.mjs the same way the other harnesses in this directory do, "
        "instead of silently shipping a copy that fails to resolve under Node."
    )
    state_mjs = tmp_path / "state.mjs"
    config_mjs = tmp_path / "config.mjs"
    state_mjs.write_text(state_src, encoding="utf-8")
    config_mjs.write_text(config_src, encoding="utf-8")
    return state_mjs, config_mjs


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_live_is_the_default_mode_and_base_url_precedence_still_works() -> None:
    assert _STATE_JS.exists(), f"expected {_STATE_JS} to exist"
    assert _CONFIG_JS.exists(), f"expected {_CONFIG_JS} to exist (the one file a deployment edits)"

    with tempfile.TemporaryDirectory() as tmp:
        state_mjs, config_mjs = _prepare_copies(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(state_mjs), str(config_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    assert result.returncode == 0, (
        f"live-default / base-url precedence check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
