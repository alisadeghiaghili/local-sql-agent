# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test for the analyst-facing wrong-answer flag control
(admin panel phase 4, spec §2) -- ``web/js/render/feedback.js``.

Drives the REAL source under Node (see ``run_feedback_control.mjs`` in
this directory for the full scenario list and its minimal DOM shim) and
asserts, at the actual boundary that matters:

* the control renders collapsed (spec §2.1's "low-key... never a
  prominent button") and the toggle reveals exactly one form;
* submitting calls back with the selected category and a TRIMMED note,
  never the question or the SQL (this module has no way to send either --
  its callback signature is ``(category, note)``, nothing else);
* a successful submission disables the control (one flag per turn) and
  shows a success message;
* a rejected submission (already flagged, a network error) shows the
  failure and leaves the control usable again, rather than a permanently
  broken UI.

Requires ``node`` on PATH. Skipped (not failed) when unavailable, mirroring
every other harness in this directory.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FEEDBACK_JS = _REPO_ROOT / "web" / "js" / "render" / "feedback.js"
_HARNESS = Path(__file__).resolve().parent / "run_feedback_control.mjs"

_NODE = shutil.which("node")


def _prepare_copy(tmp_path: Path) -> Path:
    """Copy feedback.js into *tmp_path* as ESM (``.mjs``) -- byte-identical,
    since the module has no internal ``./*.js`` imports to rewrite."""
    feedback_mjs = tmp_path / "feedback.mjs"
    feedback_mjs.write_text(_FEEDBACK_JS.read_text(encoding="utf-8"), encoding="utf-8")
    return feedback_mjs


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_feedback_control_is_low_key_one_interaction_and_recovers_from_failure() -> None:
    assert _FEEDBACK_JS.exists(), f"expected {_FEEDBACK_JS} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        feedback_mjs = _prepare_copy(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(feedback_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    assert result.returncode == 0, (
        f"feedback control check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
