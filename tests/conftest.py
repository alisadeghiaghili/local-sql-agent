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

from typing import Any, Iterator
from unittest.mock import patch

import pytest


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
