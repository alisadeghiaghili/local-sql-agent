# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test for the admin "Request access" queue render
(ADR-004 part 1) -- ``web/admin/access-requests.js``.

Drives the REAL source under Node (see ``run_admin_access_requests.mjs`` in
this directory for the full scenario list and its minimal DOM shim) and
asserts, at the actual boundary that matters:

* an empty queue renders the "no requests" message, a full row renders
  requester/column/question/created time, and only an OPEN row gets
  Approve/Deny controls;
* clicking Approve/Deny calls the right handler with the right arguments
  (including the reason text typed into the deny input);
* a denied row's reason is shown;
* an XSS payload in the column name, the joined audit question, AND a
  denial reason all render as literal text -- no ``<img>``/``<script>``
  element is ever created anywhere in the rendered tree. The admin panel
  had a stored XSS fixed in 4.12.1 (``web/admin/main.js``'s own
  ``escapeHtml`` docstring); this module follows the newer, structurally
  safer ``createElement``/``textContent`` pattern instead (no
  string-built HTML at all), and this test is what holds that.

Requires ``node`` on PATH. Skipped (not failed) when unavailable, mirroring
every other harness in this directory.
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MODULE_JS = _REPO_ROOT / "web" / "admin" / "access-requests.js"
_HARNESS = Path(__file__).resolve().parent / "run_admin_access_requests.mjs"

_NODE = shutil.which("node")


def _prepare_copy(tmp_path: Path) -> Path:
    """Copy access-requests.js into *tmp_path* as ESM (``.mjs``) --
    byte-identical, since the module has no internal ``./*.js`` imports of
    its own (mirrors run_feedback_control.mjs's own ``_prepare_copy``)."""
    module_mjs = tmp_path / "access-requests.mjs"
    module_mjs.write_text(_MODULE_JS.read_text(encoding="utf-8"), encoding="utf-8")
    return module_mjs


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_admin_access_requests_list_renders_safely_and_wires_approve_deny() -> None:
    assert _MODULE_JS.exists(), f"expected {_MODULE_JS} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        module_mjs = _prepare_copy(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(module_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        f"admin access-requests check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
