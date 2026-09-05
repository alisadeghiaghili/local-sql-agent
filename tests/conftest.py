# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Shared pytest configuration.

The suite had no ``conftest.py`` at all, which is how a single test came to
dominate its runtime. ``TestHealth::test_health_never_blocked_by_overload``
issued 20 requests to ``/health`` without patching ``check_health``, so each
one ran the real ``_ping_db()``. That resolves the default connection URL's
literal host ``server``, which does not exist, and blocks on DNS plus the
ODBC login timeout for roughly 21 seconds. Twenty of those cost ~420s — the
whole suite ran in ~433s, so that one test *was* the suite's runtime.

Nothing stopped it, because nothing was watching. The autouse fixture below
watches: any test that reaches real engine construction now fails
immediately with a message naming the fix, instead of hanging.

Tests that legitimately exercise the engine already patch
``database.connection.create_engine`` themselves (see
``tests/test_executor.py`` and ``TestDisposeEngine`` in
``tests/test_sql_guard.py``). Their patch is applied inside this one, so it
takes precedence and they are unaffected.
"""

from __future__ import annotations

import hashlib
import json
import os

# ---------------------------------------------------------------------------
# Rate limit, raised for the suite before config.Settings is ever built
# ---------------------------------------------------------------------------
# RATE_LIMIT_REQUESTS / RATE_LIMIT_BURST are config.Settings fields now
# (config.Settings.rate_limit_requests / rate_limit_burst -- deployment-
# readiness pass; they used to be read directly by api/middleware.py via
# its own os.getenv() calls at THAT module's import time). Moving them to
# Settings, read through cfg.settings at RateLimitMiddleware construction
# time, does NOT let this workaround go: it only changes *which* import
# has to see the env vars first. config.settings is a frozen singleton
# built exactly once, at config.py's own first import (see that module's
# ".env loading" comment) -- so the env vars still have to be in place
# before whichever happens first. And even that would not be late enough
# on its own: api.server.app is one shared module-level FastAPI instance
# reused by most of this suite's tests, and Starlette builds (and caches
# for the app's entire lifetime) its middleware stack lazily, on the
# first request it ever serves in the whole pytest session -- so whatever
# cfg.settings.rate_limit_requests/burst are AT THAT MOMENT is what every
# later test is stuck with, no matter how many later tests wrap a call in
# config.override_settings(). Setting the env vars here, before anything
# in this process has imported config OR sent api.server.app its first
# request, is what makes both of those "exactly once" events see the
# huge values from the start.
#
# Every authenticated test shares one principal (see auth_settings below),
# so the whole suite runs through a single token bucket. At the shipped
# production default -- 600 requests per 60s plus a burst of 40 -- a suite
# issuing well over a thousand authenticated requests in ~15 seconds would
# still exhaust that bucket and start getting 429s (this was true, worse,
# at the old 60/10 default).
#
# Those 429s do not look like rate limiting when they land: a test reads
# resp.json()["session_id"] and gets a KeyError, because the body is an
# error envelope. It presented as an intermittent order-dependent flake
# (~1 in 10-30 local runs) and as a deterministic CI failure, CI running
# the suite about twice as fast and so giving the bucket less time to
# refill. It cost a long time to find, hence this comment.
#
# Set here rather than in the CI workflow so a local run and CI behave
# identically. Must run before config.py is first imported -- conftest
# loads before any test module, and nothing above imports config or api.
os.environ.setdefault("RATE_LIMIT_REQUESTS", "1000000")
os.environ.setdefault("RATE_LIMIT_BURST", "1000")

# ---------------------------------------------------------------------------
# Session persistence, disabled by default for the suite
# ---------------------------------------------------------------------------
# config.Settings.session_store_path defaults to "logs/sessions.db" in
# production (persistence-on-by-default -- see that field's docstring).
# Left at that default here, any test that reaches
# api.v2_routes.get_session_store()'s lazy construction (every existing
# POST /v2/sessions test does) would open a REAL SQLite file under this
# repo's own logs/ directory -- state that would leak between test runs
# and pollute a git-ignored-but-real directory nobody asked this suite to
# write to. Empty string is the documented "disabled" value, restoring
# exactly the pre-Phase-9 in-memory-only behaviour every existing v2 test
# already assumes. Tests that specifically exercise persistence
# (tests/test_session_persistence.py, tests/test_v2_session_memory_endpoints.py)
# override this explicitly via config.override_settings(session_store_path=...)
# pointed at a pytest tmp_path, same pattern as RATE_LIMIT_* above: must be
# set before config.py is first imported.
os.environ.setdefault("SESSION_STORE_PATH", "")

from typing import Any, Iterator
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Phase 8: shared test API key
# ---------------------------------------------------------------------------
# AUTH_REQUIRED defaults to True (fail-closed -- see config.py), so any test
# that builds a TestClient against api.server.app now needs real credentials
# to reach a protected route, exactly like a real caller would. This is one
# principal, reused across every test that opts in via the `auth_settings`
# fixture below, rather than each test module inventing its own key.
TEST_PRINCIPAL_ID = "test-suite-principal"
#: >= security.auth.MIN_KEY_LENGTH (32) chars -- an ordinary, if fixed, token.
TEST_API_KEY_RAW = "test-suite-shared-api-key-0123456789abcdef"
TEST_API_KEY_SHA256 = hashlib.sha256(TEST_API_KEY_RAW.encode("utf-8")).hexdigest()
TEST_API_KEYS_JSON = json.dumps([
    {"id": TEST_PRINCIPAL_ID, "name": "Test Suite Principal", "key_sha256": TEST_API_KEY_SHA256},
])
#: Header a TestClient should send to authenticate as TEST_PRINCIPAL_ID.
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_KEY_RAW}"}


@pytest.fixture()
def auth_settings() -> Iterator[dict[str, str]]:
    """``AUTH_REQUIRED=true`` with one usable key, active for this test.

    The suite mostly exercises route *behaviour*, not the auth gate
    itself (that is ``tests/test_auth.py``'s job) -- so rather than
    disabling ``AUTH_REQUIRED`` for those tests, which would leave the
    gate completely untested on every one of those paths, this fixture
    gives them a real, valid principal to authenticate as, exactly like
    a real caller would. Yields the ``Authorization`` header a
    ``TestClient`` should send (pass it as ``TestClient(..., headers=...)``
    so every request the client makes carries it automatically).
    """
    from config import override_settings

    with override_settings(auth_required=True, api_keys_json=TEST_API_KEYS_JSON):
        yield dict(AUTH_HEADERS)


def _refuse_real_engine(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(
        "This test tried to build a real SQLAlchemy engine.\n"
        "\n"
        "Nothing in the suite should open a real database connection: the "
        "default DB_CONNECTION_URL points at the literal host 'server', so "
        "the attempt does not fail fast — it blocks on DNS and the ODBC "
        "login timeout for ~21s per call.\n"
        "\n"
        "Patch the seam your test actually needs:\n"
        "  - patch('api.health.check_health')            for /health routes\n"
        "  - patch('database.executor.execute_query')    for query execution\n"
        "  - patch('database.connection.create_engine')  to exercise the "
        "engine factory itself\n"
    )


@pytest.fixture(autouse=True)
def _no_real_database() -> Iterator[None]:
    """Fail fast, and loudly, if a test reaches real engine construction.

    ``get_engine`` is ``lru_cache``-backed, so a real engine built by an
    earlier test would be reused by later ones without ever calling
    ``create_engine`` again. The cache is therefore cleared on both sides of
    every test, which also removes a source of order-dependent behaviour.
    """
    from database.connection import get_engine

    get_engine.cache_clear()
    try:
        with patch(
            "database.connection.create_engine",
            side_effect=_refuse_real_engine,
        ):
            yield
    finally:
        get_engine.cache_clear()


@pytest.fixture(autouse=True)
def _no_background_dimension_refresh() -> Iterator[None]:
    """Disable ``retrieval.dimension_vocabulary``'s background self-healing
    refresh for every test, and restore it afterwards.

    Phase 5b's stale-while-revalidate redesign makes a cold or stale
    dimension-vocabulary lookup trigger a background refresh against the
    real database by default (see that module's docstring). Left enabled
    here, an ordinary route test that mentions any of ``Ring``/``Currency``/
    ``Broker``/``DeliveryPlace``/``Symbol`` -- ordinary questions do -- would
    spin up a background thread reaching ``database.connection.create_engine``
    on every single such test, since the vocabulary cache starts cold and
    nothing in this suite warms it. ``_no_real_database`` above turns that
    into a caught, logged ``AssertionError`` rather than a ~21s hang, but a
    background thread quietly doing that on every cold lookup during
    ordinary tests is still exactly the class of hidden-async-work problem
    that produced this phase's one real test flake elsewhere (a leaked
    ``time.sleep`` in a different module's shared thread pool -- see
    ``tests/test_value_resolver.py``). Disabling the trigger here keeps the
    whole suite's dimension-vocabulary reads synchronous and silent: a
    cold/stale lookup still returns immediately (no candidates, or stale
    candidates) but launches nothing.

    ``tests/test_dimension_vocabulary.py``'s background-refresh tests
    re-enable this locally, always with an injected ``execute_fn`` and
    always inside a ``try/finally`` that restores the disabled state
    before the test ends.

    Restores whatever value was in effect *before* this fixture ran
    (``is_background_refresh_enabled()``), not a hardcoded ``True``. A
    hardcoded restore was the actual, observed cause of a real-database
    connection attempt during a full-suite run: this fixture and the
    per-class fixture in ``TestBackgroundRefresh`` both run with function
    scope, so for a test in that class this one's setup runs first
    (leaving the flag ``False``), the class fixture's setup then flips it
    to ``True`` for the test body, and teardown unwinds in the opposite
    order -- the class fixture's teardown restores ``False`` first, and
    only THEN does this fixture's own teardown run. A hardcoded
    ``set_background_refresh_enabled(True)`` here would stomp that back to
    ``True`` regardless, leaving it wrong for every subsequent test until
    another ``TestBackgroundRefresh`` test happened to reset it -- a
    window in which an ordinary test's cold vocabulary lookup would
    launch a real background thread. Save/restore make each fixture
    responsible only for the value it actually changed.
    """
    from retrieval.dimension_vocabulary import (
        is_background_refresh_enabled, set_background_refresh_enabled,
    )

    previous = is_background_refresh_enabled()
    set_background_refresh_enabled(False)
    try:
        yield
    finally:
        set_background_refresh_enabled(previous)
