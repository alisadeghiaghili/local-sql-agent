# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Chrome i18n and user-menu contract for the analyst UI.

Language is fa by default with en secondary (DESIGN.md). The user menu
owns theme, language, and API-key entry so the topbar stays readable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parent.parent.parent / "web"
_I18N = _WEB / "js" / "i18n.js"
_INDEX = _WEB / "index.html"
_MAIN = _WEB / "js" / "main.js"


def test_i18n_module_defines_fa_and_en() -> None:
    src = _I18N.read_text(encoding="utf-8")
    assert 'fa: {' in src
    assert 'en: {' in src
    for key in ("apiKey", "theme", "language", "userMenu", "ask"):
        assert f"{key}:" in src, f"i18n must define chrome string {key}"
    assert "export function t" in src
    assert "export function setLang" in src
    assert "export function applyLang" in src


def test_index_has_user_menu_holding_theme_lang_and_api_key() -> None:
    html = _INDEX.read_text(encoding="utf-8")
    assert 'id="user-menu"' in html
    assert 'id="user-menu-panel"' in html
    assert 'id="theme-segment"' in html
    assert 'id="lang-segment"' in html
    assert 'id="live-key-input"' in html
    assert 'id="live-key-save"' in html
    # Theme is no longer a standalone topbar cycle button
    assert 'id="theme-toggle"' not in html
    assert "data-i18n=" in html


def test_main_wires_user_menu_theme_and_lang() -> None:
    src = _MAIN.read_text(encoding="utf-8")
    assert "wireUserMenu" in src
    assert "theme-segment" in src
    assert "lang-segment" in src
    assert "from \"./i18n.js\"" in src
    # API key prompt opens the user menu, not a topbar row toggle
    assert "user-menu-panel" in src
