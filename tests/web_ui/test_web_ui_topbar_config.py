# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test for the topbar change that removes the backend base-URL
control (``#live-base-row`` / ``#live-base-input`` / ``#live-base-connect``)
from ``web/index.html`` while explicitly KEEPING the API-key field
(``#live-key-row`` / ``#live-key-input`` / ``#live-key-save`` /
``#live-key-clear``).

Those two facts are easy to get backwards during a refactor that touches
the same block of markup -- removing the base-URL row is required, but the
API-key field is the analyst's own identity (``observability/audit.py``
attributes by ``principal_id``; ``api/middleware.py``'s rate limiter
buckets on ``(principal, ip)``) and must never be collapsed into it or
removed alongside it. This is a plain-text structural check against the
real ``web/index.html`` -- no Node needed, since this is markup, not
behaviour -- run BEFORE the base-row markup was removed (observed failing:
the row was present) and AFTER (observed passing), giving the same
red -> green evidence as the Node-backed suites in this directory.

This also checks that ``web/js/config.js`` -- the "one file a deployment
edits" for the backend base URL that used to live in that now-removed
top-bar row -- exists and actually exports a usable default.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INDEX_HTML = _REPO_ROOT / "web" / "index.html"
_CONFIG_JS = _REPO_ROOT / "web" / "js" / "config.js"


def test_base_url_row_removed_from_topbar() -> None:
    html = _INDEX_HTML.read_text(encoding="utf-8")
    for needle in ("id=\"live-base-row\"", "id=\"live-base-input\"", "id=\"live-base-connect\""):
        assert needle not in html, (
            f"expected {needle} to be gone from web/index.html's top bar -- the backend "
            "base URL is deployment configuration (web/js/config.js), not an "
            "analyst-facing control"
        )


def test_api_key_field_still_present_in_topbar() -> None:
    html = _INDEX_HTML.read_text(encoding="utf-8")
    for needle in (
        "id=\"live-key-row\"", "id=\"live-key-status\"", "id=\"live-key-input\"",
        "id=\"live-key-save\"", "id=\"live-key-clear\"",
    ):
        assert needle in html, (
            f"expected {needle} to still be present in web/index.html -- the API-key field "
            "is the analyst's own identity (principal_id / rate-limit bucket), not "
            "deployment config, and must never be removed"
        )


def test_config_js_exists_and_exports_a_usable_default_base_url() -> None:
    assert _CONFIG_JS.exists(), (
        "expected web/js/config.js to exist -- the one file a deployment edits for the "
        "default backend base URL now that it is no longer a visible top-bar control"
    )
    src = _CONFIG_JS.read_text(encoding="utf-8")
    m = re.search(r'export\s+const\s+DEFAULT_BASE_URL\s*=\s*"([^"]+)"', src)
    assert m, "expected web/js/config.js to `export const DEFAULT_BASE_URL = \"...\";`"
    assert m.group(1), "DEFAULT_BASE_URL must not be an empty string"


if __name__ == "__main__":  # pragma: no cover
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
