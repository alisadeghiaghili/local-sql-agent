# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Findings 16 and 17 — the Flask app's own authentication surface.

``webapp/`` is a second, separately-authored web application sharing this
project's warehouse and model. It has its own username/password login, its
own SQLite user table, and a security posture that was never brought
level with the FastAPI service beside it. Three gaps.

**A real person's username shipped as a default, in a public repository.**

    ADMIN_USER = os.getenv("ADMIN_USER", "bahmanabadi.m")

That account is the only one permitted to create users. Publishing which
username holds the privilege is free reconnaissance, and the repository
was public at audit time (finding 1).

**No throttling on ``/login``.** The FastAPI side buckets authentication
failures separately and returns ``429`` with ``Retry-After`` -- measured
live at roughly thirty bad keys. Flask has nothing, so the named account
above can be guessed at indefinitely.

**No CSRF token and no cookie hardening.** Three POST forms, a cookie
session, no token, and none of ``SESSION_COOKIE_SECURE``,
``SAMESITE`` or an explicit ``HTTPONLY``.

On that last one, honestly: modern browsers default an unset ``SameSite``
to ``Lax``, which blocks most cross-site form posts on its own. This is a
missing layer rather than an open door, and it is worth closing anyway
because ``/register`` -- the endpoint that creates accounts -- sits behind
it, and because relying on a browser default is relying on a decision
someone else can change. The FastAPI service is structurally immune here
(bearer tokens, not cookies), which is exactly why the difference went
unnoticed: the two apps have different answers to the same question.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "webapp"))

_APP_PY = _REPO_ROOT / "webapp" / "app.py"
_TEMPLATES = _REPO_ROOT / "webapp" / "templates"


@pytest.fixture()
def configured_admin_user(monkeypatch):
    """Supply the variable finding 16 makes mandatory.

    Once ``ADMIN_USER`` has no default, building an app without it is a
    startup failure -- which is the point, and which every test that wants
    a *working* app therefore has to satisfy. It is deliberately a
    per-test fixture rather than anything session-wide: setting it once
    for the whole run would also satisfy
    ``test_a_missing_admin_user_is_a_startup_failure_not_a_guess``
    accidentally, and that test exists precisely to prove the variable is
    not optional.
    """
    monkeypatch.setenv("ADMIN_USER", "pentest-admin")
    return "pentest-admin"


class TestNoRealIdentityIsHardcoded:
    def test_admin_user_has_no_baked_in_default(self):
        src = _APP_PY.read_text(encoding="utf-8")
        match = re.search(r'ADMIN_USER\s*=\s*os\.getenv\(\s*["\']ADMIN_USER["\']\s*,\s*([^)]*)\)', src)
        if match and match.group(1).strip() not in ("", "None", '""', "''"):
            pytest.fail(
                f"ADMIN_USER falls back to {match.group(1).strip()} -- a literal "
                "username committed to a public repository, and the only "
                "account allowed to create users. Require the variable instead"
            )

    def test_a_missing_admin_user_is_a_startup_failure_not_a_guess(self, monkeypatch):
        """Silently picking some default would reintroduce the finding. An
        unset privileged account is a misconfiguration and should say so.

        Both the import and the reload sit inside ``pytest.raises`` on
        purpose. Whether ``webapp.app`` is already cached from an earlier
        test decides *which* of the two raises, and the guarantee under
        test is the same either way: with the variable unset, this module
        must refuse to come up. Putting only the reload inside the context
        manager would make the test pass or error depending on collection
        order -- and would tempt a reader into pre-importing the module
        somewhere global just to stabilise it, which quietly sets
        ``ADMIN_USER`` for the whole session and hides the very failure
        this asserts.
        """
        monkeypatch.delenv("ADMIN_USER", raising=False)
        import importlib

        with pytest.raises((RuntimeError, ValueError, SystemExit)):
            importlib.reload(importlib.import_module("webapp.app"))

    def test_no_persian_or_personal_username_literal_remains(self):
        """Blunt sweep for the specific value and its shape."""
        src = _APP_PY.read_text(encoding="utf-8")
        assert "bahmanabadi" not in src, (
            "a real employee's username is still present in webapp/app.py"
        )


class TestLoginIsThrottled:
    """Parity with the FastAPI side, which the audit measured returning 429
    with Retry-After after roughly thirty failures."""

    def test_repeated_failures_are_eventually_refused(self, configured_admin_user):
        import webapp.app as webapp_app

        app = webapp_app.create_app()
        app.config.update(TESTING=True)
        client = app.test_client()

        codes = [
            client.post("/login", data={"username": "victim", "password": f"guess{i}"}).status_code
            for i in range(40)
        ]
        assert 429 in codes, (
            "forty consecutive failed logins were all accepted for another "
            f"attempt (status codes seen: {sorted(set(codes))}). The username "
            "with create-user rights is published in the repo; unlimited "
            "guessing against it is the whole attack"
        )


class TestSessionCookiesAreHardened:
    @pytest.mark.parametrize(
        "setting, expected",
        [
            ("SESSION_COOKIE_HTTPONLY", True),
            ("SESSION_COOKIE_SECURE", True),
            ("SESSION_COOKIE_SAMESITE", "Strict"),
        ],
    )
    def test_cookie_flag_is_set_explicitly(self, setting, expected, configured_admin_user):
        import webapp.app as webapp_app

        app = webapp_app.create_app()
        assert app.config.get(setting) == expected, (
            f"{setting} is {app.config.get(setting)!r}, expected {expected!r}. "
            "Relying on the framework or browser default leaves the decision "
            "to someone else"
        )


class TestPostFormsCarryACsrfToken:
    """`/register` creates accounts. It is the one that must not be
    submittable from a page the admin merely visits."""

    @pytest.mark.parametrize("template", ["login.html", "register.html", "index.html"])
    def test_the_form_includes_a_token_field(self, template):
        html = (_TEMPLATES / template).read_text(encoding="utf-8")
        assert re.search(r"csrf", html, re.I), (
            f"webapp/templates/{template} posts with no CSRF token"
        )

    def test_a_post_without_a_token_is_rejected(self, configured_admin_user):
        import webapp.app as webapp_app

        app = webapp_app.create_app()
        app.config.update(TESTING=True)
        resp = app.test_client().post(
            "/register", data={"username": "attacker", "password": "p", "confirm": "p"}
        )
        assert resp.status_code in (400, 403), (
            f"a token-less POST to /register returned {resp.status_code}. "
            "Enforcement, not just a hidden field in the template, is what "
            "makes the token mean anything"
        )
