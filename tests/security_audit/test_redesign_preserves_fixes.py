# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The 5.0 redesign may not pay for its layout with a reverted security fix.

Two efforts ran in parallel against this repository and neither could see the
other. A security audit landed nineteen fixes on `main`; a UI redesign branched
from `main` *before* those fixes and rewrote the same files —
`web/admin/main.js`, `web/index.html`, `web/admin/index.html`,
`api/admin_write_routes.py`.

Checked on that branch at the time this module was written, all four layers of
the stored-XSS fix (audit finding 2, High — `operations` escalating to
`security`) were absent:

* `escapeHtml` was still the ``textContent``->``innerHTML`` idiom, which does
  not encode ``"``
* both attribute sinks still interpolated into ``data-*="${…}"``
* ``principal_id`` still had length limits and no character pattern
* neither page carried a Content-Security-Policy

None of that is a criticism of the redesign. It branched from a commit where
the defect was the status quo. It is a statement about merges: resolving one
conflicted hunk in favour of the newer-looking file silently reopens a
privilege-escalation path, and nothing in a diff review makes that obvious.

`tests/security_audit/test_admin_panel_xss.py` asserts each layer in detail.
This module is the blunter instrument beside it — the one that fails loudly
during an integration, names the audit finding, and says which side of the
merge to keep. It duplicates coverage on purpose: the sharp test explains the
bug, this one survives someone reaching for "theirs" at 2am.

Policy: `docs/design/DESIGN-INVARIANTS.md` §1.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_ADMIN_JS = _REPO_ROOT / "web" / "admin" / "main.js"
_PAGES = ("index.html", "admin/index.html")


def _fail(layer: str, detail: str) -> str:
    return (
        f"REDESIGN REGRESSION — the {layer} layer of audit finding 2 is gone.\n"
        f"  {detail}\n"
        "  This is the stored-XSS chain that let an operations-capability "
        "holder run script in a security holder's browser. If this appeared "
        "during a merge with feature/ui-design-system, keep the security "
        "branch's version of this file and re-apply the redesign on top."
    )


class TestTheEscaperStillCoversQuotes:
    def test_escape_html_is_not_the_textcontent_idiom(self):
        body = re.search(
            r"function escapeHtml\s*\([^)]*\)\s*\{(.*?)\n\}", _ADMIN_JS.read_text(encoding="utf-8"), re.S
        )
        assert body, "escapeHtml is gone from web/admin/main.js"
        src = body.group(1)
        assert "&quot;" in src or "&#39;" in src, _fail(
            "escaper",
            "escapeHtml no longer encodes quote characters. The "
            "textContent->innerHTML idiom is correct in a text node and unsafe "
            "in an attribute, and this function is used in both.",
        )


class TestIdentityStillReachesTheDomAsData:
    @pytest.mark.parametrize("attr", ["data-pid", "data-hash", "data-vocab-refresh"])
    def test_no_attribute_is_built_by_interpolation(self, attr):
        js = _ADMIN_JS.read_text(encoding="utf-8")
        assert not re.search(re.escape(attr) + r'="\$\{', js), _fail(
            "DOM-construction",
            f"{attr} is being interpolated into a quoted HTML string again. "
            "Set it through el.dataset on a constructed element — then no "
            "value can break out, whatever the escaper does.",
        )


class TestTheIdentifierIsStillConstrained:
    def test_principal_id_declares_a_character_pattern(self):
        from api.admin_write_routes import IssueKeyRequest

        field = IssueKeyRequest.model_fields["principal_id"]
        pattern = getattr(field, "pattern", None) or next(
            (
                getattr(m, "pattern", None)
                for m in getattr(field, "metadata", [])
                if getattr(m, "pattern", None)
            ),
            None,
        )
        assert pattern, _fail(
            "input-validation",
            "IssueKeyRequest.principal_id constrains length only. The audit "
            "issued a key whose id was 'analyst-x\" onmouseover=\"…' and the "
            "server answered 200.",
        )


class TestThePagesStillCarryTheirOwnPolicy:
    """A static server delivers these files, not FastAPI, so
    `SecurityHeadersMiddleware` never sees them. The policy has to travel with
    the document. `DESIGN.md` §9.2 requires this too — it is the one rule the
    redesign branch states and does not yet implement."""

    @pytest.mark.parametrize("page", _PAGES)
    def test_the_page_has_a_meta_csp(self, page):
        html = (_REPO_ROOT / "web" / page).read_text(encoding="utf-8")
        assert re.search(
            r'<meta[^>]+http-equiv=["\']Content-Security-Policy["\']', html, re.I
        ), _fail("content-security-policy", f"web/{page} carries no <meta> CSP.")

    @pytest.mark.parametrize("page", _PAGES)
    def test_the_policy_still_forbids_inline_script(self, page):
        html = (_REPO_ROOT / "web" / page).read_text(encoding="utf-8")
        meta = re.search(
            r'<meta[^>]+http-equiv=["\']Content-Security-Policy["\'][^>]*content=["\']([^"\']+)',
            html, re.I,
        )
        assert meta, f"web/{page} carries no <meta> CSP"
        assert "unsafe-inline" not in meta.group(1).split("style-src")[0], _fail(
            "content-security-policy",
            f"web/{page}'s script-src permits 'unsafe-inline' — exactly the "
            "capability an injected handler needs.",
        )

    @pytest.mark.parametrize("page", _PAGES)
    def test_the_page_introduces_no_inline_handler(self, page):
        """The redesign was verified CSP-compatible: zero ``on*=`` handlers in
        either page. Adding one would not merely violate the policy, it would
        stop working the moment the CSP is honoured — a silently dead button."""
        html = (_REPO_ROOT / "web" / page).read_text(encoding="utf-8")
        handlers = re.findall(r"\son[a-z]+\s*=\s*[\"']", html)
        assert not handlers, (
            f"web/{page} gained {len(handlers)} inline event handler(s). Under "
            "script-src 'self' they never run. Bind in the module instead."
        )


class TestTheAnalystResultPathStaysDomBuilt:
    """`web/js/render/table.js` renders warehouse rows and was the one surface
    the audit could not exploit, because it builds elements instead of HTML.
    It is the pattern the redesign should spread, not a detail to refactor
    away while reorganising the turn card."""

    def test_the_result_table_does_not_render_rows_as_html_strings(self):
        table_js = _REPO_ROOT / "web" / "js" / "render" / "table.js"
        if not table_js.exists():
            pytest.skip("table.js has been restructured; re-point this assertion")
        src = table_js.read_text(encoding="utf-8")
        assert "createElement" in src, (
            "table.js no longer constructs elements. Warehouse cell values are "
            "the least trusted strings in this product"
        )
        assert not re.search(r"innerHTML\s*=\s*[`'\"].*\$\{", src), _fail(
            "rendering",
            "table.js now interpolates values into an HTML string. Database "
            "content reaches this function verbatim.",
        )
