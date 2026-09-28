# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test for the admin panel's 30-second auto-refresh no longer
including the warehouse-touching "expensive" cards -- ``web/admin/main.js``.

2026 warehouse-load audit, Required item 1: with an admin tab open all day,
``refreshAll()`` re-running on a 30s timer used to re-run
``scripts.verify_deployment``'s checks and a full schema-catalogue
reflection every cycle. Those two cards ("health", "schemaDrift") must
still load once at page-open and on their own refresh button, but must be
excluded from the recurring timer.

Drives the REAL ``web/admin/main.js`` (see ``run_admin_auto_refresh.mjs`` in
this directory for the full scenario list and its DOM/fetch/timer shim)
together with its real import graph -- ``admin.js``, ``access-requests.js``,
``../js/apikey.js`` and ``../js/state.js`` -- via caller-supplied copies with
only their internal relative import specifiers rewritten from ``.js`` to
``.mjs`` (Node's ESM loader needs an extension it recognizes; the sources
ship as plain ``.js`` for the browser, which resolves extensionless-friendly
specifiers itself). The directory layout is mirrored one level (``admin/``
and ``js/`` siblings under the temp root) so the existing ``../js/...``
relative specifiers keep resolving without rewriting their path, only their
extension.

Requires ``node`` on PATH. Skipped (not failed) when unavailable, mirroring
every other harness in this directory.
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
_ADMIN_DIR = _REPO_ROOT / "web" / "admin"
_JS_DIR = _REPO_ROOT / "web" / "js"
_MAIN_JS = _ADMIN_DIR / "main.js"
_HARNESS = Path(__file__).resolve().parent / "run_admin_auto_refresh.mjs"

_NODE = shutil.which("node")

# Every source file in main.js's real import graph, and where it lands
# relative to the temp root -- mirroring the repo's own web/admin + web/js
# layout so relative specifiers such as "../js/apikey.js" still resolve to
# a sibling directory after the .js -> .mjs rewrite below.
_GRAPH = {
    "admin/main.mjs": _MAIN_JS,
    "admin/admin.mjs": _ADMIN_DIR / "admin.js",
    "admin/access-requests.mjs": _ADMIN_DIR / "access-requests.js",
    "js/apikey.mjs": _JS_DIR / "apikey.js",
    "js/state.mjs": _JS_DIR / "state.js",
    # main.js now also resolves its backend-address default from
    # config.js (DEFAULT_BASE_URL/DEFAULT_API_PORT), same as web/'s own
    # main.js; admin.js imports describeTransportFailure from api.js for
    # the shared "unreachable host or CORS" hint (web/admin/admin.js's
    # own import comment) -- api.js in turn imports apikey.js, already
    # above, so no further additions are needed once it is here.
    "js/config.mjs": _JS_DIR / "config.js",
    "js/api.mjs": _JS_DIR / "api.js",
}

# Matches a relative import/export specifier ending in .js, e.g.
# `from "./admin.js"` or `from "../js/apikey.js"` -- rewritten to .mjs so
# Node's ESM loader (which, unlike a browser, needs a recognized extension)
# resolves it to the copy this test just wrote.
_RELATIVE_JS_SPECIFIER = re.compile(r'(from\s+["\'])(\.\./[^"\']+?|\./[^"\']+?)\.js(["\'])')


def _rewrite_specifiers(src: str) -> str:
    return _RELATIVE_JS_SPECIFIER.sub(r"\1\2.mjs\3", src)


def _prepare_copy(tmp_path: Path) -> Path:
    """Materialize the whole import graph under *tmp_path*, each file
    renamed .js -> .mjs with its own relative specifiers rewritten to
    match. Returns the path to the copied main.mjs."""
    for rel_path, source in _GRAPH.items():
        dest = tmp_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_rewrite_specifiers(source.read_text(encoding="utf-8")), encoding="utf-8")
    return tmp_path / "admin" / "main.mjs"


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_expensive_cards_load_once_but_are_excluded_from_auto_refresh() -> None:
    assert _MAIN_JS.exists(), f"expected {_MAIN_JS} to exist"
    for rel_path, source in _GRAPH.items():
        assert source.exists(), f"expected {source} (part of main.js's import graph) to exist"

    with tempfile.TemporaryDirectory() as tmp:
        main_mjs = _prepare_copy(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(main_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        f"admin auto-refresh check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
