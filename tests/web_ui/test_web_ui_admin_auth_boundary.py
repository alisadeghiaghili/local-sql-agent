# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel, phase 1 — the fifth required test: the panel attaches
``Authorization`` on every call.

Mirrors ``test_web_ui_auth_boundary.py`` exactly, for ``web/admin/admin.js``
instead of ``web/js/api.js``: the request-building code lives in browser
JS, not Python, so this drives the REAL ``web/admin/admin.js`` /
``web/js/apikey.js`` source under Node with a mocked ``fetch``/
``localStorage`` (see ``run_admin_auth_boundary.mjs`` in this directory)
and asserts, at the actual request-building boundary, that:

* every admin call site (summary, healthChecks, cache, config) attaches
  ``Authorization: Bearer <key>`` when a key is stored;
* a 401 (no/invalid credential) and a 403 (a real, non-admin key) are
  promoted to distinguishable typed errors, not one generic error the
  panel cannot usefully act on -- the phase-1 spec's own requirement that
  a 403 says plainly "this key is not an admin key" instead of showing an
  empty dashboard depends on being able to tell the two apart.

Requires ``node`` on PATH. Skipped (not failed) when it is unavailable,
same as ``test_web_ui_auth_boundary.py``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ADMIN_JS = _REPO_ROOT / "web" / "admin" / "admin.js"
_APIKEY_JS = _REPO_ROOT / "web" / "js" / "apikey.js"
_HARNESS = Path(__file__).resolve().parent / "run_admin_auth_boundary.mjs"

_NODE = shutil.which("node")

# The only change made to admin.js's real source before handing it to
# Node: apikey.js is copied alongside it as apikey.mjs (Node's ESM loader
# needs an .mjs extension or a controlling package.json -- web/ ships
# neither, see web/README.md), so the one import statement naming it has
# to point at the renamed file too. Anything else in admin.js reaching
# this test is byte-identical to the real source.
_IMPORT_LINE = re.compile(r'^import \{ getApiKey \} from "\.\./js/apikey\.js";$', re.MULTILINE)


def _prepare_copy(tmp_path: Path) -> Path:
    """Copy admin.js/apikey.js into *tmp_path* as ESM (.mjs), import-path
    fixed up. Returns the path to the copied ``admin.mjs``."""
    admin_src = _ADMIN_JS.read_text(encoding="utf-8")
    apikey_src = _APIKEY_JS.read_text(encoding="utf-8")

    fixed_admin_src, n = _IMPORT_LINE.subn('import { getApiKey } from "./apikey.mjs";', admin_src)
    assert n == 1, (
        "web/admin/admin.js no longer imports apikey.js the way this test "
        "expects (`import { getApiKey } from \"../js/apikey.js\";`) -- "
        "update _IMPORT_LINE to match the real source instead of silently "
        "testing stale code."
    )

    admin_mjs = tmp_path / "admin.mjs"
    apikey_mjs = tmp_path / "apikey.mjs"
    admin_mjs.write_text(fixed_admin_src, encoding="utf-8")
    apikey_mjs.write_text(apikey_src, encoding="utf-8")
    return admin_mjs


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/admin/admin.js under test")
def test_admin_panel_attaches_bearer_token_on_every_call() -> None:
    assert _ADMIN_JS.exists(), f"expected {_ADMIN_JS} to exist"
    assert _APIKEY_JS.exists(), f"expected {_APIKEY_JS} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        admin_mjs = _prepare_copy(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(admin_mjs)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert result.returncode == 0, (
        f"web/admin/admin.js auth boundary check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
