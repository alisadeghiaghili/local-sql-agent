# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin IA contract (DESIGN.md §5.3 / Train C).

Health summary rail first, sticky section jump nav, global auto-refresh
with last-updated — not a wall of equal sections each with its own ↻.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parent.parent.parent / "web"
_ADMIN_HTML = _WEB / "admin" / "index.html"
_ADMIN_JS = _WEB / "admin" / "main.js"
_ADMIN_CSS = _WEB / "admin" / "admin.css"


def test_admin_html_has_summary_rail_and_jump_nav() -> None:
    html = _ADMIN_HTML.read_text(encoding="utf-8")
    assert 'id="overview"' in html
    assert "admin-summary-rail" in html
    for rail in ("rail-maintenance", "rail-checks", "rail-feedback", "rail-cache"):
        assert f'id="{rail}"' in html
    assert "admin-jump" in html
    for sec in (
        "sec-maintenance", "sec-health", "sec-stats", "sec-feedback",
        "sec-cache", "sec-config", "sec-keys", "sec-schema",
        "sec-vocab", "sec-usage", "sec-auth",
    ):
        assert f'id="{sec}"' in html, f"jump nav target {sec} missing"
        assert f'href="#{sec}"' in html, f"jump nav link to #{sec} missing"
    assert 'id="admin-last-updated"' in html
    # No emoji refresh glyphs left in chrome
    assert "↻" not in html


def test_admin_js_auto_refreshes_and_fills_rail() -> None:
    src = _ADMIN_JS.read_text(encoding="utf-8")
    assert "AUTO_REFRESH_MS" in src
    assert "updateSummaryRail" in src
    assert "markLastUpdated" in src
    assert "document.hidden" in src


def test_admin_css_makes_jump_nav_sticky() -> None:
    css = _ADMIN_CSS.read_text(encoding="utf-8")
    assert ".admin-jump" in css
    assert "position: sticky" in css
    assert ".admin-summary-rail" in css
    assert ".rail-item" in css
