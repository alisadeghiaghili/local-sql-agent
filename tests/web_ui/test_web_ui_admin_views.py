# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The admin panel's grouped views (``web/admin/views.js``).

Thirteen sections and the summary rail on one page made the admin panel about 6,800 px tall. The
sticky nav now opens with one button per view (status, quality and use,
access and keys, data and configuration, all) and only the chosen view's
sections are shown; «همه» is the page as it was. Nothing moves in the DOM:
sections are hidden, and every ``#sec-*`` link still works by switching to
the view that holds it.

The static checks read the real page and module: every section of
``web/admin/index.html`` belongs to exactly one named view, and ``main.js``
wires the switch, the hash and the summary rail. The behavioural check
drives the real ``views.js`` under Node against a fake built from the
page's own ids and layout (see ``run_admin_views.mjs``).

Requires ``node`` on PATH for the behavioural test. Skipped (not failed)
when unavailable.
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import json
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parent.parent.parent / "web"
_ADMIN_HTML = _WEB / "admin" / "index.html"
_ADMIN_JS = _WEB / "admin" / "main.js"
_ADMIN_CSS = _WEB / "admin" / "admin.css"
_VIEWS_JS = _WEB / "admin" / "views.js"
_HARNESS = Path(__file__).resolve().parent / "run_admin_views.mjs"
_NODE = shutil.which("node")


class _Page(HTMLParser):
    """Collects the jump links and the admin sections, grouping the ones
    inside one ``.admin-columns`` wrapper."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.layout: list[list[str]] = []
        self._in_nav = False
        self._columns_depth: int | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div":
            self._depth += 1
            if "admin-columns" in classes:
                self._columns_depth = self._depth
                self.layout.append([])
        if tag == "nav" and a.get("id") == "admin-jump":
            self._in_nav = True
        if self._in_nav and tag == "a" and (a.get("href") or "").startswith("#"):
            self.links.append(a["href"] or "")
        if tag == "section" and "admin-section" in classes:
            if self._columns_depth is not None:
                self.layout[-1].append(a.get("id") or "")
            else:
                self.layout.append([a.get("id") or ""])

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav":
            self._in_nav = False
        if tag == "div":
            if self._columns_depth == self._depth:
                self._columns_depth = None
            self._depth -= 1


def _page() -> _Page:
    parser = _Page()
    parser.feed(_ADMIN_HTML.read_text(encoding="utf-8"))
    return parser


def _named_views() -> dict[str, list[str]]:
    src = _VIEWS_JS.read_text(encoding="utf-8")
    views = {}
    for m in re.finditer(r'\{ id: "(\w+)", label: "[^"]*", sections: (\[[^\]]*\]|null) \}', src):
        if m.group(2) != "null":
            views[m.group(1)] = re.findall(r'"([\w-]+)"', m.group(2))
    return views


def test_every_section_is_in_exactly_one_named_view() -> None:
    page = _page()
    section_ids = [i for group in page.layout for i in group]
    assert len(section_ids) == 13, section_ids
    views = _named_views()
    assert set(views) == {"status", "quality", "access", "data"}
    owners: dict[str, list[str]] = {}
    for view, ids in views.items():
        for i in ids:
            owners.setdefault(i, []).append(view)
    for i in section_ids:
        assert len(owners.get(i, [])) == 1, f"{i} is in {owners.get(i)} -- it must be in exactly one view"
    assert set(owners) == set(section_ids), "a view names a section the page does not have"
    for href in page.links:
        target = href[1:]
        assert target == "overview" or target in owners, f"jump link {href} has no view"


def test_main_js_wires_the_views_the_hash_and_the_rail() -> None:
    src = _ADMIN_JS.read_text(encoding="utf-8")
    assert 'from "./views.js"' in src
    block = src[src.index("(function wireViews()"):src.index("/* Scroll-spy for the sticky jump nav")]
    assert 'applyView(document, "all")' in block, "a failure while wiring leaves every section shown"
    assert "renderViewSwitch(" in block and "applyView(document, initialView(hash()" in block
    assert 'addEventListener("hashchange"' in block
    assert "rememberView(" in block
    for rail, sec in (
        ("rail-maintenance", "sec-maintenance"), ("rail-checks", "sec-health"),
        ("rail-feedback", "sec-feedback"), ("rail-cache", "sec-cache"),
    ):
        assert f'"{rail}": "{sec}"' in src
    rail = src[src.index("function updateSummaryRail()"):src.index("function setRailItem(")]
    assert "markAttention(document" in rail


def test_the_switch_is_styled_with_tokens_and_logical_properties() -> None:
    css = _ADMIN_CSS.read_text(encoding="utf-8")
    block = css[css.index("/* Grouped views (views.js)"):css.index("/* A summary-rail item opens its section")]
    assert '.admin-view-btn[aria-pressed="true"]' in block, "the pressed view is styled from aria-pressed"
    assert ".admin-view-btn:focus-visible" in block
    assert not re.search(r"\b(margin|padding|border)-(left|right)\b|\b(left|right)\s*:", block)
    assert "var(--status-critical)" in block
    assert ".admin-section[hidden]" in block and ".admin-columns[hidden]" in block


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/admin/*.js under test")
def test_views_show_hide_and_open_as_specified() -> None:
    page = _page()
    with tempfile.TemporaryDirectory() as tmp:
        views_mjs = Path(tmp) / "views.mjs"
        views_mjs.write_text(_VIEWS_JS.read_text(encoding="utf-8"), encoding="utf-8")
        page_json = Path(tmp) / "page.json"
        page_json.write_text(json.dumps({"links": page.links, "layout": page.layout}), encoding="utf-8")
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(views_mjs), str(page_json)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        f"admin views check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
