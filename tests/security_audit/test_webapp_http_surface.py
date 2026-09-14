# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Findings 16/17, Task 4 -- drive the controls over the wire, not the config.

``tests/security_audit/test_flask_hardening.py`` proved that the three
controls from the round-1 and round-2 remediation exist: cookie flags are
set in ``app.config``, a throttle counter exists and eventually returns
429, and a token-less POST is refused. None of that is the same claim as
"a real browser talking to this app over HTTP gets the protection". A
config dict can say ``SESSION_COOKIE_SECURE=True`` while a reverse proxy,
a stale Flask version, or a typo in the key name still ships a cookie the
browser accepts over plain HTTP -- the only way to know is to read the
``Set-Cookie`` header Flask actually produced. A CSRF check that accepts
"any non-empty string" instead of "the string this session was issued"
passes every test in that file (a token is present, and it round-trips
for the client that minted it) while doing nothing against the one thing
CSRF tokens exist to stop: a token lifted from a *different* session or
forged by an attacker who cannot read the victim's cookie jar. And a
throttle that trips but never lifts is, per ``webapp/app.py``'s own
comment on ``_LOGIN_FAILURE_WINDOW_SECONDS``, a worse outcome than the
attack it defends against -- a permanent denial of service inflicted with
thirty wrong guesses against a shared IP -- and no existing test drives
the clock forward far enough to observe whether it actually recovers.

Four scenarios, each driven through ``app.test_client()`` against a fresh
``create_app()``:

* T1 -- the ``Set-Cookie`` header on the wire, not ``app.config``, carries
  ``Secure``, ``HttpOnly`` and ``SameSite=Strict``.
* T2 -- a CSRF token minted for one session is refused on another, and the
  refusal does not collaterally break the session it *was* minted for.
* T3 -- CSRF enforcement on ``/register`` does not break account creation
  for the admin, and a non-admin's own valid token is not mistaken for
  admin authorization (the two checks are independent, and a naive
  reading of the code could conflate them).
* T4 -- the failure counter both trips at the threshold and releases the
  IP once the window set by ``_LOGIN_FAILURE_WINDOW_SECONDS`` has fully
  elapsed, holding one second short of the window as a boundary check.
"""

from __future__ import annotations

import re
import sys
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "webapp"))


@pytest.fixture()
def configured_admin_user(monkeypatch):
    """Same per-test shape as test_flask_hardening.py's fixture of the same
    name -- ADMIN_USER has no default (finding 16), so every test that
    wants a working app has to supply it, and setting it session-wide
    would silently defeat that module's own startup-failure test."""
    monkeypatch.setenv("ADMIN_USER", "pentest-admin")
    return "pentest-admin"


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf_token hidden field found in the rendered page"
    return match.group(1)


def _session_cookie_morsel(response):
    """Parse the real ``Set-Cookie`` header(s) for the ``session`` cookie.

    Deliberately not a substring check on the raw header text: a cookie
    *value* could itself contain the word "Secure" (it is base64-ish
    signed session data, not free text, but nothing guarantees that stays
    true), and attribute names are case-insensitive per RFC 6265 -- a
    server that emitted ``secure`` in lowercase would still be safe and a
    blind ``"Secure" in header`` check would wrongly fail it.
    ``http.cookies.SimpleCookie`` parses the header into named attributes
    the same way a browser's cookie jar does, which is the actual claim
    T1 is making.
    """
    cookie = SimpleCookie()
    for header_value in response.headers.get_all("Set-Cookie"):
        cookie.load(header_value)
    assert "session" in cookie, (
        f"no 'session' cookie in Set-Cookie headers: "
        f"{response.headers.get_all('Set-Cookie')!r}"
    )
    return cookie["session"]


class TestCookieFlagsOnTheWire:
    """TestSessionCookiesAreHardened (test_flask_hardening.py) asserts
    app.config values. This asserts what a browser actually receives."""

    def test_login_response_sets_a_hardened_session_cookie(self, configured_admin_user):
        import webapp.app as webapp_app

        app = webapp_app.create_app()
        app.config.update(TESTING=True)
        client = app.test_client()

        response = client.get("/login")
        morsel = _session_cookie_morsel(response)

        assert morsel["secure"], (
            "Set-Cookie for 'session' has no Secure attribute -- a browser "
            "would send this cookie over plain HTTP, where anyone on the "
            "network path can read it"
        )
        assert morsel["httponly"], (
            "Set-Cookie for 'session' has no HttpOnly attribute -- a "
            "script injected via any XSS elsewhere on the page could read "
            "the session cookie directly"
        )
        assert morsel["samesite"].lower() == "strict", (
            f"Set-Cookie SameSite is {morsel['samesite']!r}, expected "
            "'Strict' -- the exact browser-default reliance the app's own "
            "docstring (test_flask_hardening.py) says not to lean on"
        )


class TestCsrfTokenIsSessionBound:
    """A token that merely has to be present (rather than match the
    session it claims to belong to) stops nothing: an attacker's page
    cannot read the victim's cookie jar, but it can read its *own* --
    mint a token for a session the attacker controls, embed it in a form
    that silently posts to the victim's browser, and if the server checks
    only "is csrf_token non-empty" the forged submission sails through
    with the victim's actual session cookie doing the authenticating.
    This is exactly the "login CSRF" scenario named in webapp/app.py's
    own comment above _CSRF_EXEMPT_ENDPOINTS."""

    def test_a_token_minted_for_another_session_is_rejected(self, configured_admin_user):
        import webapp.app as webapp_app

        app = webapp_app.create_app()
        app.config.update(TESTING=True)

        client_a = app.test_client()
        client_b = app.test_client()

        token_a = _extract_csrf_token(client_a.get("/login").get_data(as_text=True))
        token_b = _extract_csrf_token(client_b.get("/login").get_data(as_text=True))
        assert token_a != token_b, (
            "both sessions minted the same csrf token -- the test setup "
            "cannot distinguish 'session-bound' from 'always accepted'"
        )

        cross_session_response = client_a.post(
            "/login",
            data={"username": "victim", "password": "guess", "csrf_token": token_b},
        )
        assert cross_session_response.status_code == 400, (
            f"client A's POST carrying client B's token returned "
            f"{cross_session_response.status_code}, not 400 -- a token "
            "minted for a different session was accepted, which is what "
            "CSRF protection exists to prevent"
        )

    def test_the_owning_session_s_own_token_still_works_after_a_forged_attempt(
        self, configured_admin_user,
    ):
        """Rejecting the wrong token must not be a side effect that also
        breaks the right one -- e.g. an implementation that clears or
        rotates the session token as part of refusing a bad submission
        would lock the legitimate user out too."""
        import webapp.app as webapp_app

        app = webapp_app.create_app()
        app.config.update(TESTING=True)

        client_a = app.test_client()
        client_b = app.test_client()

        token_a = _extract_csrf_token(client_a.get("/login").get_data(as_text=True))
        token_b = _extract_csrf_token(client_b.get("/login").get_data(as_text=True))

        rejected = client_a.post(
            "/login",
            data={"username": "victim", "password": "guess", "csrf_token": token_b},
        )
        assert rejected.status_code == 400

        # Same client, its own real token: must reach the view (a login
        # form re-render on bad credentials is 200; only a 400/403 from
        # _check_csrf's abort() would mean the token stopped working too.
        own_token_response = client_a.post(
            "/login",
            data={"username": "victim", "password": "guess", "csrf_token": token_a},
        )
        assert own_token_response.status_code != 400, (
            f"client A's own token was refused ({own_token_response.status_code}) "
            "after a forged submission using someone else's token -- rejecting "
            "the attack must not also break the legitimate session"
        )


class TestRegisterAuthorizationIsSeparateFromCsrfValidity:
    """A valid CSRF token proves only "this POST came from a form this
    server rendered for this session" -- it says nothing about who the
    session belongs to. /register's admin check (webapp/app.py's
    ``if username != admin_user`` in the register view) is a second,
    independent gate. Collapsing the two -- e.g. treating "has a good
    token" as "is authorized" -- would let any logged-in user create
    accounts."""

    def test_admin_can_register_a_new_account_with_a_valid_token(
        self, configured_admin_user, tmp_path, monkeypatch,
    ):
        import db as webapp_db
        import webapp.app as webapp_app

        monkeypatch.setattr(webapp_db, "DB_PATH", tmp_path / "webapp_app.db")
        webapp_db.init_db()
        webapp_db.create_user(configured_admin_user, "adminpassword")

        app = webapp_app.create_app()
        app.config.update(TESTING=True)
        client = app.test_client()

        login_token = _extract_csrf_token(client.get("/login").get_data(as_text=True))
        login_response = client.post(
            "/login",
            data={
                "username": configured_admin_user,
                "password": "adminpassword",
                "csrf_token": login_token,
            },
        )
        assert login_response.status_code == 302, "admin login itself failed"

        register_token = _extract_csrf_token(
            client.get("/register").get_data(as_text=True)
        )
        register_response = client.post(
            "/register",
            data={
                "username": "new_analyst",
                "password": "new_analyst_pw",
                "confirm": "new_analyst_pw",
                "csrf_token": register_token,
            },
        )
        assert register_response.status_code == 302, (
            f"admin POST to /register with a valid token returned "
            f"{register_response.status_code}, expected a 302 redirect back "
            "to /register -- CSRF enforcement must not break the feature "
            "it protects"
        )
        assert register_response.headers.get("Location", "").endswith("/register")

        assert webapp_db.verify_user("new_analyst", "new_analyst_pw") is not None, (
            "the account /register claimed to create does not actually "
            "verify with the password submitted"
        )

    def test_non_admin_with_a_valid_token_cannot_register_an_account(
        self, configured_admin_user, tmp_path, monkeypatch,
    ):
        import db as webapp_db
        import webapp.app as webapp_app

        monkeypatch.setattr(webapp_db, "DB_PATH", tmp_path / "webapp_app.db")
        webapp_db.init_db()
        webapp_db.create_user("regular_user", "regularpassword")

        app = webapp_app.create_app()
        app.config.update(TESTING=True)
        client = app.test_client()

        # This token is a real, session-bound token -- minted by the same
        # GET /login this session's own subsequent POST uses -- not a
        # missing or forged one. The point is that its validity is not
        # sufficient authorization on its own.
        login_token = _extract_csrf_token(client.get("/login").get_data(as_text=True))
        login_response = client.post(
            "/login",
            data={
                "username": "regular_user",
                "password": "regularpassword",
                "csrf_token": login_token,
            },
        )
        assert login_response.status_code == 302, "regular user login itself failed"

        register_response = client.post(
            "/register",
            data={
                "username": "sneaked_in",
                "password": "whatever",
                "confirm": "whatever",
                "csrf_token": login_token,
            },
        )
        assert register_response.status_code == 302, (
            f"non-admin POST to /register returned "
            f"{register_response.status_code}, expected a 302 (redirected "
            "away, never reaching account creation)"
        )
        redirect_location = register_response.headers.get("Location", "")
        assert not redirect_location.endswith("/register"), (
            f"non-admin register redirect went to {redirect_location!r} -- "
            "expected the index page, not back to /register (which is where "
            "the *admin* success path redirects)"
        )

        assert webapp_db.verify_user("sneaked_in", "whatever") is None, (
            "a non-admin session with a technically-valid CSRF token was "
            "able to create an account -- a valid token was mistaken for "
            "authorization"
        )


class _FakeMonotonicClock:
    """Exposes only ``monotonic()``, deliberately -- swapping in a full
    fake ``time`` module (or patching the real one globally) risks
    breaking anything else in the process that reads wall-clock time
    during the same test run. ``webapp/app.py`` reads only
    ``time.monotonic()`` (lines 192 and 208 at the time this test was
    written), so that is the only surface this needs to control."""

    def __init__(self, start: float) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestLoginThrottleTripsAndLifts:
    """TestLoginIsThrottled (test_flask_hardening.py) proves a 429 can
    happen. It never proves the 429 goes away -- and per the round-2
    remediation comment on _LOGIN_FAILURE_WINDOW_SECONDS in
    webapp/app.py, a throttle with no expiry is a worse outcome than the
    guessing attack it defends against: thirty wrong guesses is enough
    to inflict a standing denial of service on every legitimate user
    behind the same address, indefinitely, with no further access
    required from the attacker."""

    def test_threshold_trips_then_window_expiry_lifts_it(
        self, configured_admin_user, tmp_path, monkeypatch,
    ):
        import db as webapp_db
        import webapp.app as webapp_app

        monkeypatch.setattr(webapp_db, "DB_PATH", tmp_path / "webapp_app.db")
        webapp_db.init_db()
        webapp_db.create_user("victim", "correct horse battery staple")

        threshold = webapp_app._LOGIN_FAILURE_THRESHOLD
        window_seconds = webapp_app._LOGIN_FAILURE_WINDOW_SECONDS

        clock = _FakeMonotonicClock(start=0.0)
        monkeypatch.setattr(webapp_app, "time", clock)

        app = webapp_app.create_app()
        app.config.update(TESTING=True)
        client = app.test_client()

        login_token = _extract_csrf_token(client.get("/login").get_data(as_text=True))

        # Exactly _LOGIN_FAILURE_THRESHOLD failures, one shared token (a
        # real browser -- or any attacker worth defending against -- GETs
        # the form once and reuses the token, matching
        # TestLoginIsThrottled's own reasoning in test_flask_hardening.py).
        failure_codes = [
            client.post(
                "/login",
                data={
                    "username": "victim",
                    "password": f"wrong-{i}",
                    "csrf_token": login_token,
                },
            ).status_code
            for i in range(threshold)
        ]
        assert 429 not in failure_codes, (
            "a 429 appeared before the threshold-th failure -- the trip "
            f"point does not match _LOGIN_FAILURE_THRESHOLD ({threshold})"
        )

        # The threshold-th failure above should already have armed the
        # throttle for the *next* attempt (checked before the DB lookup,
        # per _login()'s own comment) -- even the correct password is now
        # refused without ever being checked.
        tripped_response = client.post(
            "/login",
            data={
                "username": "victim",
                "password": "correct horse battery staple",
                "csrf_token": login_token,
            },
        )
        assert tripped_response.status_code == 429, (
            f"after {threshold} failures, the correct password was not "
            f"refused (got {tripped_response.status_code}) -- the "
            "threshold from webapp.app._LOGIN_FAILURE_THRESHOLD did not "
            "actually trip the throttle"
        )

        # Boundary: _login_throttled's own condition is
        # `now - window_started_at >= _LOGIN_FAILURE_WINDOW_SECONDS`, so
        # one second short of the window must still be throttled. The
        # window started at the first failure above (clock was at 0.0
        # then and has not moved since), so advancing by window - 1 here
        # lands exactly one second short of expiry.
        clock.advance(window_seconds - 1)
        still_throttled_response = client.post(
            "/login",
            data={
                "username": "victim",
                "password": "correct horse battery staple",
                "csrf_token": login_token,
            },
        )
        assert still_throttled_response.status_code == 429, (
            f"{window_seconds - 1} seconds into a {window_seconds}-second "
            f"window (one second short of expiry), the correct password "
            f"was accepted (status {still_throttled_response.status_code}) "
            "-- the window expired early"
        )

        # Advance the final second: now - window_started_at == window_seconds,
        # which satisfies _login_throttled's `>=` and must discard the
        # stale entry, lifting the throttle with no successful login ever
        # having occurred in between.
        clock.advance(1)
        lifted_response = client.post(
            "/login",
            data={
                "username": "victim",
                "password": "correct horse battery staple",
                "csrf_token": login_token,
            },
        )
        assert lifted_response.status_code == 302, (
            f"exactly {window_seconds} seconds after the failure window "
            f"started, the correct password was still refused (status "
            f"{lifted_response.status_code}) -- the throttle never lifts "
            "on its own, which is the permanent-denial-of-service defect "
            "round 2 remediation was supposed to fix"
        )
