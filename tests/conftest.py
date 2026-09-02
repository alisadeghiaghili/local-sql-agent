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
# Rate limit, raised for the suite before anything imports api.middleware
# ---------------------------------------------------------------------------
# api/middleware.py reads RATE_LIMIT_* from the environment at import time,
# and every authenticated test shares one principal (see auth_settings
# below), so the whole suite runs through a single token bucket. At the
# production default -- 60 requests per 60s plus a burst of 10 -- a suite
# issuing well over a thousand authenticated requests in ~15 seconds
# exhausts that bucket and starts getting 429s.
#
# Those 429s do not look like rate limiting when they land: a test reads
# resp.json()["session_id"] and gets a KeyError, because the body is an
# error envelope. It presented as an intermittent order-dependent flake
# (~1 in 10-30 local runs) and as a deterministic CI failure, CI running
# the suite about twice as fast and so giving the bucket less time to
# refill. It cost a long time to find, hence this comment.
#
# Set here rather than in the CI workflow so a local run and CI behave
# identically. Must run before api.middleware is first imported -- conftest
# loads before any test module, and nothing above imports api.
os.environ.setdefault("RATE_LIMIT_REQUESTS", "1000000")
os.environ.setdefault("RATE_LIMIT_BURST", "1000")

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
