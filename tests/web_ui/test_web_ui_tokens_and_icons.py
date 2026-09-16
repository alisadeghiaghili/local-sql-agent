# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Contract tests for the extracted design token file and chrome icons.

``web/styles/tokens.css`` is the only place product colours are defined.
``style.css`` imports it and must not re-declare brand hex values on
``:root`` (a second source of truth is how light and dark drift apart).

Product chrome (``web/index.html``, ``web/admin/index.html``) must not
ship emoji as UI icons — see DESIGN.md §7.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parent.parent.parent / "web"
_TOKENS = _WEB / "styles" / "tokens.css"
_STYLE = _WEB / "styles" / "style.css"
_INDEX = _WEB / "index.html"
_ADMIN = _WEB / "admin" / "index.html"

# Emoji / pictographs that must not appear in chrome markup.
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # pictographs, transport, supplemental
    "\U00002600-\U000027BF"  # misc symbols, dingbats
    "\U0001F900-\U0001F9FF"
    "]+",
    flags=re.UNICODE,
)

# Brand hexes that must live only in tokens.css on :root.
_BRAND_HEX = re.compile(
    r"--(?:navy|teal|bg|card|ink|muted)\s*:\s*#[0-9a-fA-F]{3,8}",
)


def test_tokens_css_exists_and_defines_core_palette() -> None:
    text = _TOKENS.read_text(encoding="utf-8")
    for name in ("--navy", "--teal", "--bg", "--card", "--ink", "--sql-bg", "--status-good"):
        assert f"{name}:" in text, f"tokens.css must define {name}"
    assert "@media (prefers-color-scheme: dark)" in text
    assert '[data-theme="dark"]' in text


def test_style_css_imports_tokens_and_does_not_redeclare_brand_on_root() -> None:
    style = _STYLE.read_text(encoding="utf-8")
    assert '@import url("tokens.css")' in style
    # Any :root { --navy: ... } block in style.css would be a second source.
    root_blocks = re.findall(r":root\s*\{[^}]+\}", style)
    for block in root_blocks:
        assert not _BRAND_HEX.search(block), (
            "style.css must not redefine brand colours on :root; "
            "they belong in tokens.css only"
        )


@pytest.mark.parametrize("path", [_INDEX, _ADMIN])
def test_product_chrome_html_links_tokens_and_has_no_emoji(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "tokens.css" in text, f"{path.name} must link tokens.css"
    # Allow ✓/✕/↻/▾/▴ used as status marks in rendered content (not chrome
    # pictographs). Strip those ASCII-ish symbols before the emoji scan.
    #
    # 📌 is deliberately NOT stripped. It used to be, and that single entry
    # made this assertion unable to fail for the one true pictograph the
    # chrome actually shipped: index.html quoted a button as «📌 به‌خاطر
    # بسپار», the strip deleted it, and the test then reported no emoji. A
    # scan that removes the thing it searches for is not a check, and it let
    # "No emoji in product chrome" sit ticked while the emoji was on screen.
    stripped = text.replace("✓", "").replace("✕", "").replace("↻", "")
    stripped = stripped.replace("▾", "").replace("▴", "")
    emoji = _EMOJI.findall(stripped)
    assert not emoji, f"{path.name} chrome must not use emoji as icons: {emoji!r}"
    # Topbar must use inline SVG and a user menu (theme is inside it).
    if path == _INDEX:
        assert "<svg" in text
        assert 'id="user-menu"' in text
        assert 'id="theme-toggle"' not in text


def test_icons_module_exports_expected_names() -> None:
    src = (_WEB / "js" / "icons.js").read_text(encoding="utf-8")
    for name in ("menu", "close", "theme", "memory", "refresh", "check", "alert", "pin"):
        assert f"{name}:" in src, f"icons.js must define {name}"
    assert "export function icon" in src
