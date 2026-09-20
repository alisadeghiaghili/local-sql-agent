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

Nothing stopped it, because nothing was watching. The root ``conftest.py``'s
``_no_real_database`` autouse fixture watches: any test that reaches real
engine construction now fails immediately with a message naming the fix,
instead of hanging. That fixture (and the related
``_no_background_dimension_refresh`` guard) used to live in this file, but
were moved to the repository-root ``conftest.py`` -- a plain ``pytest``
conftest applies only to the directory tree rooted where it lives, and this
file's tree is ``tests/`` alone, so a combined ``pytest tests/ eval/tests``
run collected and ran ``eval/tests`` with neither guard active. See the
root ``conftest.py`` for both fixtures now.

Tests that legitimately exercise the engine already patch
``database.connection.create_engine`` themselves (see
``tests/test_executor.py`` and ``TestDisposeEngine`` in
``tests/test_sql_guard.py``). Their patch is applied inside the root
conftest's ``_no_real_database``, so it takes precedence and they are
unaffected.
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

# ---------------------------------------------------------------------------
# Application database (admin panel, phase 2), in-memory by default
# ---------------------------------------------------------------------------
# config.Settings.app_db_url defaults to "" in production, which falls back
# to a SQLite FILE at app_db_sqlite_path ("logs/app.db") -- the same "would
# write a real file under this repo's own logs/ directory" problem
# SESSION_STORE_PATH="" above already exists to avoid, for the same reason:
# api.auth.AuthMiddleware now resolves a principal by consulting the
# application database on EVERY request (security.auth.load_all_principals
# -> appdb.key_store.get_active_principals), regardless of whether a given
# test cares about the admin panel at all. An in-memory SQLite database
# (fresh per process, never touching disk) is therefore the right default
# for the whole suite. Must be set before config.py is first imported, same
# as RATE_LIMIT_*/SESSION_STORE_PATH above.
os.environ.setdefault("APP_DB_URL", "sqlite://")

from typing import Iterator

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


@pytest.fixture(autouse=True)
def _fresh_app_db() -> Iterator[None]:
    """Give every test a fresh, empty application database.

    ``appdb.engine.get_app_engine`` is ``lru_cache``-backed exactly like
    the warehouse ``get_engine`` above, and the default in-memory SQLite
    URL (``sqlite://``, set by this conftest above) is held open as a
    single shared connection (``StaticPool``) for as long as the cached
    engine lives — so without disposing it between tests, an issued key or
    granted role from one test would still be visible to the next,
    exactly the order-dependent leakage the root ``conftest.py``'s
    ``_no_real_database`` exists to prevent for the warehouse engine.
    Disposing before AND after each
    test (not just after) also protects the first test in a run against
    any engine another fixture or import happened to build first.

    The key-set cache (``appdb.key_store``'s module-level
    ``_cached_principals``) is invalidated for the same reason: it is a
    process-wide cache keyed on nothing test-specific, so a cached merge
    from one test would otherwise leak into the next until its TTL
    happened to elapse.
    """
    from appdb.engine import dispose_app_engine
    from appdb.key_store import invalidate_cache

    dispose_app_engine()
    invalidate_cache()
    try:
        yield
    finally:
        dispose_app_engine()
        invalidate_cache()
