# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test for the backend API-address resolution and
normalisation shared by both pages (``web/`` and ``web/admin/``).

Real incident this covers: a deployment moved the API and the static UI
to a new host. The main UI reads its API address from
``web/js/config.js``'s ``DEFAULT_BASE_URL`` (overridable by ``?base=`` and
a saved ``localStorage`` value); the admin panel did not use that file at
all and fell back to a hardcoded ``"http://localhost:8000"`` in
``web/js/state.js``, so it had to be typed into the admin top bar by
hand. The operator typed ``"172.16.101.42:8076"`` (no scheme) into that
top bar, and ``fetch`` silently treated it as a RELATIVE path: every
request went to ``GET /admin/172.16.101.42:8076/admin/maintenance`` on
the static file server (404/501) instead of the API.

This drives the REAL ``web/js/state.js`` (``resolveDefaultBaseUrl``,
``normalizeBaseUrl``, ``loadPersisted``), ``web/js/config.js``
(``DEFAULT_BASE_URL``/``DEFAULT_API_PORT``), ``web/js/api.js`` (``Api`` --
the main UI's request layer) and ``web/admin/admin.js`` (``AdminApi`` --
the admin panel's) together under Node (see
``run_base_url_resolution.mjs`` in this directory for the full scenario
list) and asserts, at the actual boundary that changed:

* ``resolveDefaultBaseUrl`` derives ``<page protocol>//<page hostname>:
  <DEFAULT_API_PORT>`` only when ``DEFAULT_BASE_URL`` is empty, and
  otherwise returns the configured default unchanged -- the "one source
  of truth for both pages" this exists for;
* ``normalizeBaseUrl`` accepts every valid-but-messy address a deployment
  or an operator's typo could produce (no scheme, surrounding
  whitespace, a trailing slash, a pasted path, an upper-case scheme, a
  bare ``host:port``) and normalises every one of them down to a bare
  origin -- and REJECTS every invalid one (an accidental leading slash in
  front of an embedded scheme, a non-http(s) scheme, empty, garbage),
  each with a stated reason rather than a silent wrong guess;
* ``loadPersisted()`` repairs a fixable bad saved value in place (and
  writes the repair back to storage) and discards an unfixable one
  instead of loading either verbatim;
* end-to-end, for BOTH ``Api`` (web/) and ``AdminApi`` (web/admin/): the
  real request URL built from a normalised, messy input is exactly
  ``<origin>`` + ``<path>`` -- never a doubled or relative path like the
  ``/admin/172.16.101.42:8076/admin/maintenance`` the real incident
  produced.

Requires ``node`` on PATH. Skipped (not failed) when it is unavailable,
matching every other harness in this directory.
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
_JS_DIR = _REPO_ROOT / "web" / "js"
_ADMIN_DIR = _REPO_ROOT / "web" / "admin"
_STATE_JS = _JS_DIR / "state.js"
_CONFIG_JS = _JS_DIR / "config.js"
_APIKEY_JS = _JS_DIR / "apikey.js"
_API_JS = _JS_DIR / "api.js"
_ADMIN_JS = _ADMIN_DIR / "admin.js"
_HARNESS = Path(__file__).resolve().parent / "run_base_url_resolution.mjs"

_NODE = shutil.which("node")

# Mirrors test_web_ui_admin_auto_refresh.py's technique: every source file
# involved, copied .js -> .mjs with its own relative import specifiers
# rewritten the same way, into a temp root that mirrors the repo's
# web/js + web/admin layout so "../js/..." specifiers keep resolving.
_GRAPH = {
    "js/state.mjs": _STATE_JS,
    "js/config.mjs": _CONFIG_JS,
    "js/apikey.mjs": _APIKEY_JS,
    "js/api.mjs": _API_JS,
    "admin/admin.mjs": _ADMIN_JS,
}

_RELATIVE_JS_SPECIFIER = re.compile(r'(from\s+["\'])(\.\./[^"\']+?|\./[^"\']+?)\.js(["\'])')


def _rewrite_specifiers(src: str) -> str:
    return _RELATIVE_JS_SPECIFIER.sub(r"\1\2.mjs\3", src)


def _prepare_copies(tmp_path: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for rel_path, source in _GRAPH.items():
        dest = tmp_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_rewrite_specifiers(source.read_text(encoding="utf-8")), encoding="utf-8")
        out[rel_path] = dest
    return out


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_base_url_resolution_and_normalisation_agree_across_both_pages() -> None:
    for source in _GRAPH.values():
        assert source.exists(), f"expected {source} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        copies = _prepare_copies(Path(tmp))
        result = subprocess.run(
            [
                _NODE, str(_HARNESS),
                str(copies["js/state.mjs"]), str(copies["js/config.mjs"]),
                str(copies["js/api.mjs"]), str(copies["admin/admin.mjs"]),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        f"base-URL resolution/normalisation check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
