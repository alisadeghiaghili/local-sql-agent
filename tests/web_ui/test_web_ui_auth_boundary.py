# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test for the web/ auth bug: the static UI in web/ never sent
`Authorization: Bearer <key>` on any call, so once Phase 8 gated every
route except `GET /health` behind an API key, every request the UI made
against a real server 401'd. Proven against a live server before this fix:

    POST /query        -> 401
    POST /v2/sessions   -> 401
    GET  /health         -> 200   (the only thing the UI could reach)

The bug lived entirely in the browser-side request-building code
(``web/js/api.js``'s ``_fetchV2``), which is not Python and has no server
behind it in this suite. A test that only asserted "some function related
to auth exists" would not have caught the original bug -- the original
code *had* working-looking fetch calls, they just never attached a header.
This test instead drives the REAL ``web/js/api.js`` / ``web/js/apikey.js``
source under Node with a mocked ``fetch``/``localStorage`` (see
``run_auth_boundary.mjs`` in this directory) and asserts, at the actual
request-building boundary, that:

* every authenticated v2 call site (createSession, askTurn,
  askTurnStreaming, patchAssumptions) attaches
  ``Authorization: Bearer <key>`` when a key is stored;
* ``GET /health`` never requires one (Phase 8 left it open on purpose);
* a 401 is promoted to a distinguishable ``UnauthorizedError`` and a 429
  to a ``RateLimitError`` carrying ``retry_after_seconds``, rather than
  both collapsing into one generic error the UI cannot usefully act on.

Requires ``node`` on PATH. Skipped (not failed) when it is unavailable, the
same way the rest of this suite skips on an unreachable real database --
this is calling out to the actual runtime the code under test executes in,
which is not the Python interpreter running the rest of the suite.
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
_WEB_JS = _REPO_ROOT / "web" / "js"
_API_JS = _WEB_JS / "api.js"
_APIKEY_JS = _WEB_JS / "apikey.js"
_HARNESS = Path(__file__).resolve().parent / "run_auth_boundary.mjs"

_NODE = shutil.which("node")

# The only change made to api.js's real source before handing it to Node:
# apikey.js is copied alongside it as apikey.mjs (Node's ESM loader needs
# an .mjs extension or a controlling package.json to parse `export`/
# `import` syntax, and web/ deliberately ships neither -- see
# web/README.md's "no build step, no package.json"), so the one import
# statement naming it has to point at the renamed file too. Anything else
# in api.js reaching this test is byte-identical to the real source.
_IMPORT_LINE = re.compile(r'^import \{ getApiKey \} from "\./apikey\.js";$', re.MULTILINE)


def _prepare_copy(tmp_path: Path) -> Path:
    """Copy api.js/apikey.js into *tmp_path* as ESM (.mjs), import-path fixed up.

    Returns the path to the copied ``api.mjs``.
    """
    api_src = _API_JS.read_text(encoding="utf-8")
    apikey_src = _APIKEY_JS.read_text(encoding="utf-8")

    fixed_api_src, n = _IMPORT_LINE.subn('import { getApiKey } from "./apikey.mjs";', api_src)
    assert n == 1, (
        "web/js/api.js no longer imports apikey.js the way this test expects "
        "(`import { getApiKey } from \"./apikey.js\";`) -- update _IMPORT_LINE "
        "to match the real source instead of silently testing stale code."
    )

    api_mjs = tmp_path / "api.mjs"
    apikey_mjs = tmp_path / "apikey.mjs"
    api_mjs.write_text(fixed_api_src, encoding="utf-8")
    apikey_mjs.write_text(apikey_src, encoding="utf-8")
    return api_mjs


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_web_ui_attaches_bearer_token_on_every_authenticated_call_and_omits_it_for_health() -> None:
    assert _API_JS.exists(), f"expected {_API_JS} to exist"
    assert _APIKEY_JS.exists(), f"expected {_APIKEY_JS} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        api_mjs = _prepare_copy(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(api_mjs)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert result.returncode == 0, (
        f"web/js/api.js auth boundary check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
