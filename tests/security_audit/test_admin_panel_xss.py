# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Finding 2 — stored XSS in the admin panel, escalating operations to security.

Three separately-harmless facts compose into a privilege break.

1. ``escapeHtml`` (``web/admin/main.js``) is the ``textContent`` ->
   ``innerHTML`` idiom. It escapes ``&``, ``<`` and ``>`` because those are
   the characters that matter inside a *text node*. It does not escape
   ``"`` or ``'``, because inside a text node they mean nothing.

2. It is used in **attribute** position. ``renderKeys`` builds
   ``data-pid="${escapeHtml(row.principal_id)}"`` by string concatenation,
   where a quote means everything.

3. ``principal_id`` carried no character restriction -- only a length one.

Reproduced end to end against a running server during the audit::

    POST /admin/keys {"principal_id": "analyst-x\\" onmouseover=\\"...", ...}
    -> 200 OK
    GET  /admin/keys
    -> principal_id returned verbatim, quote intact

and in a browser, the same string interpolated at that line produced a real
``onmouseover`` attribute on the ``<tr>`` (``tr.hasAttribute("onmouseover")``
was ``true``, and ``dataset.pid`` had been truncated at the injected quote).

Why this is a privilege break and not just a defacement: the raw admin key
lives in ``localStorage`` and the analyst UI and admin panel are
same-origin, so the payload can exfiltrate it. An ``operations`` holder can
issue keys -- that is their job -- and a ``security`` holder triggers the
payload merely by opening the keys tab. ``security`` is the capability that
edits ACLs and grants roles, so this crosses the exact boundary the
two-role split exists to hold.

Three independent layers are asserted below, deliberately. Any one of them
stops this attack; the point of having all three is that the next sink
(``name`` is already a second one) does not reopen it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ADMIN_JS = _REPO_ROOT / "web" / "admin" / "main.js"


def _admin_js() -> str:
    return _ADMIN_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Layer 1 -- the escaper itself
# ---------------------------------------------------------------------------

class TestEscapeHtmlCoversQuotes:
    """The `textContent` -> `innerHTML` idiom is correct for body text and
    wrong for attributes. Whichever way it is fixed -- an explicit replace
    chain, or a library -- both quote characters must come out encoded."""

    def test_the_escaper_is_not_the_textcontent_idiom(self):
        js = _admin_js()
        match = re.search(r"function escapeHtml\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
        assert match, "escapeHtml is gone from web/admin/main.js"
        body = match.group(1)
        assert "textContent" not in body or "&quot;" in body or "&#39;" in body, (
            "escapeHtml still round-trips through textContent/innerHTML, which "
            "leaves \" and ' untouched. That is safe in a text node and unsafe "
            "in an attribute, and this function is used in both"
        )

    @pytest.mark.parametrize(
        "char, entity",
        [('"', "&quot;"), ("'", "&#39;"), ("<", "&lt;"), (">", "&gt;"), ("&", "&amp;")],
    )
    def test_every_html_significant_character_is_encoded(self, char, entity):
        """Asserted against the source rather than a JS runtime because the
        repo's other web_ui checks do the same, and because the absence of
        the quote branch *is* the defect."""
        body = re.search(r"function escapeHtml\s*\([^)]*\)\s*\{(.*?)\n\}", _admin_js(), re.S)
        assert body, "escapeHtml is gone from web/admin/main.js"
        assert entity in body.group(1), (
            f"escapeHtml does not encode {char!r} as {entity}. A principal_id "
            f"containing {char!r} escapes its attribute at renderKeys"
        )


# ---------------------------------------------------------------------------
# Layer 2 -- do not build attributes by string concatenation at all
# ---------------------------------------------------------------------------

class TestIdentifiersReachTheDomAsData:
    """Even a correct escaper is one refactor away from being bypassed.
    Setting `dataset` on a real element cannot produce an attribute
    injection at all, whatever the value contains."""

    def test_the_keys_row_does_not_interpolate_into_a_data_attribute(self):
        js = _admin_js()
        offenders = re.findall(r'data-(?:pid|hash)="\$\{[^}]*\}"', js)
        assert not offenders, (
            "web/admin/main.js still builds a data attribute by interpolating "
            f"into a quoted HTML string: {offenders!r}. Set el.dataset.* on a "
            "constructed element instead -- then no value can break out, "
            "regardless of how escapeHtml behaves"
        )

    def test_the_vocabulary_refresh_button_does_the_same(self):
        """The second attribute sink found in the audit."""
        js = _admin_js()
        assert not re.search(r'data-vocab-refresh="\$\{[^}]*\}"', js), (
            "data-vocab-refresh is still interpolated into a quoted attribute"
        )


# ---------------------------------------------------------------------------
# Layer 3 -- an identifier should never have contained a quote
# ---------------------------------------------------------------------------

class TestPrincipalIdIsConstrained:
    """Defence in depth, and independently worth having: a principal id is a
    token, and a token with a double quote in it is a mistake wherever it
    ends up -- an HTML attribute, a log line, a CSV cell."""

    def test_the_model_declares_a_character_pattern(self):
        from api.admin_write_routes import IssueKeyRequest

        field = IssueKeyRequest.model_fields["principal_id"]
        pattern = getattr(field, "pattern", None) or next(
            (getattr(m, "pattern", None) for m in getattr(field, "metadata", []) if getattr(m, "pattern", None)),
            None,
        )
        assert pattern, (
            "IssueKeyRequest.principal_id constrains only length. The audit "
            "issued a key whose id was 'analyst-x\" onmouseover=\"...' and the "
            "server returned 200"
        )

    @pytest.mark.parametrize(
        "hostile",
        [
            'analyst-x" onmouseover="alert(1)',
            "analyst-x' onfocus='alert(1)",
            "<script>alert(1)</script>",
            "id with spaces",
            "id\nwith-newline",
        ],
    )
    def test_hostile_identifiers_are_rejected(self, hostile):
        import pydantic

        from api.admin_write_routes import IssueKeyRequest

        with pytest.raises(pydantic.ValidationError):
            IssueKeyRequest(principal_id=hostile, name="probe")

    @pytest.mark.parametrize("ok", ["analyst-1", "ops.user", "a_b-c.d", "ADMIN2"])
    def test_ordinary_identifiers_still_pass(self, ok):
        """The constraint must not break the ids the docs tell people to use
        (``docs/fa/getting-started.md`` issues ``--id analyst-1``)."""
        from api.admin_write_routes import IssueKeyRequest

        assert IssueKeyRequest(principal_id=ok, name="x").principal_id == ok


# ---------------------------------------------------------------------------
# The page's own last line of defence
# ---------------------------------------------------------------------------

class TestThePagesCarryTheirOwnContentSecurityPolicy:
    """The API's response headers do not protect these pages: ``web/`` and
    ``web/admin/`` are served by a *different* origin (a static server, or
    nginx) than the FastAPI app. A ``<meta http-equiv>`` policy travels with
    the document, so the page is covered no matter what serves it."""

    @pytest.mark.parametrize("page", ["index.html", "admin/index.html"])
    def test_a_meta_csp_is_present(self, page):
        html = (_REPO_ROOT / "web" / page).read_text(encoding="utf-8")
        assert re.search(
            r'<meta[^>]+http-equiv=["\']Content-Security-Policy["\']', html, re.I
        ), (
            f"web/{page} declares no Content-Security-Policy. With one, the "
            "injected inline handler in this module's docstring does not run "
            "even if every other layer fails"
        )

    @pytest.mark.parametrize("page", ["index.html", "admin/index.html"])
    def test_the_policy_forbids_inline_script(self, page):
        html = (_REPO_ROOT / "web" / page).read_text(encoding="utf-8")
        meta = re.search(
            r'<meta[^>]+http-equiv=["\']Content-Security-Policy["\'][^>]*content=["\']([^"\']+)',
            html, re.I,
        )
        assert meta, f"web/{page} has no CSP meta tag"
        policy = meta.group(1)
        assert "unsafe-inline" not in policy.split("style-src")[0], (
            "script-src permits 'unsafe-inline', which is exactly the "
            "capability an injected onmouseover handler needs"
        )
