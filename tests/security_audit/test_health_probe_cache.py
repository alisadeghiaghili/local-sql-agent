# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Finding 3 — ``/health`` is an unauthenticated, unthrottled pool drain.

``api/middleware.py`` documents the problem against itself::

    # /health itself is a known DoS surface (tracked for Phase 8, not fixed
    # here): each call performs two real ~5s network probes (the LLM endpoint +
    # database) against the shared connection pool, so even a rate-limited
    # flood of /health requests can still exhaust pool capacity for real
    # /query traffic. It stays exempt for now because liveness checks (load
    # balancers, container orchestrators) must never be rate-limited away.

Measured live during the audit:

===============================  ==========
one unauthenticated ``/health``  2.09 s
five in a row                    all 200, no 429
fifteen concurrent               3.65 s wall
===============================  ==========

Fifteen concurrent finishing in 3.65 s rather than 30 s means they ran in
parallel -- each holding a connection from the same pool ``/query`` uses,
for about two seconds. No credentials, no rate limit.

The conflict in that comment is real: a liveness endpoint genuinely must
not be throttled away, or an orchestrator will kill a healthy container.
Rate-limiting is therefore the wrong lever. Caching is the right one --
a load balancer polling every second gets an answer from cache, and so
does a flood, and the pool sees at most one probe per TTL either way.

That is what these tests pin: not "how fast", which would be a flaky
benchmark, but "how many times the expensive probe actually ran".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts from a cold cache; a leftover entry would make a
    real regression look like a pass."""
    import api.health as health

    reset = getattr(health, "reset_health_cache", None)
    if reset:
        reset()
    yield
    if reset:
        reset()


class TestTheExpensiveProbesAreCached:
    def test_a_burst_runs_the_probes_once(self, monkeypatch):
        import api.health as health

        calls = {"db": 0, "llm": 0}

        def _db():
            calls["db"] += 1
            return True, "ok"

        def _llm():
            calls["llm"] += 1
            return True, "ok"

        monkeypatch.setattr(health, "_ping_db", _db)
        monkeypatch.setattr(health, "_ping_openai", _llm)

        for _ in range(10):
            health.check_health()

        assert calls["db"] == 1, (
            f"the database probe ran {calls['db']} times for 10 calls. Each one "
            "checks a connection out of the pool /query shares, and this "
            "endpoint needs no credentials"
        )
        assert calls["llm"] == 1, (
            f"the LLM probe ran {calls['llm']} times for 10 calls"
        )

    def test_the_cached_answer_is_the_same_answer(self, monkeypatch):
        """A cache that returns something different from what it cached
        would trade a DoS for a correctness bug."""
        import api.health as health

        monkeypatch.setattr(health, "_ping_db", lambda: (True, "db-ok"))
        monkeypatch.setattr(health, "_ping_openai", lambda: (True, "llm-ok"))

        first = health.check_health()
        second = health.check_health()

        assert second.database == first.database
        assert second.openai == first.openai
        assert second.status == first.status


class TestTheCacheExpires:
    """A health endpoint that never re-checks is not a health endpoint. The
    TTL has to be short enough that a real outage surfaces quickly."""

    def test_the_probe_reruns_after_the_ttl(self, monkeypatch):
        import api.health as health

        calls = {"n": 0}

        def _db():
            calls["n"] += 1
            return True, "ok"

        monkeypatch.setattr(health, "_ping_db", _db)
        monkeypatch.setattr(health, "_ping_openai", lambda: (True, "ok"))

        clock = {"t": 1000.0}
        monkeypatch.setattr(health.time, "monotonic", lambda: clock["t"])

        health.check_health()
        clock["t"] += health.HEALTH_CACHE_TTL_SECONDS + 1
        health.check_health()

        assert calls["n"] == 2, (
            "the health result never expires, so a database that came back "
            "up (or went down) is never noticed"
        )

    def test_the_ttl_is_short_enough_to_be_a_liveness_signal(self):
        import api.health as health

        assert 0 < health.HEALTH_CACHE_TTL_SECONDS <= 30, (
            f"HEALTH_CACHE_TTL_SECONDS is {health.HEALTH_CACHE_TTL_SECONDS}. "
            "Too long and an orchestrator acts on stale information; the "
            "point is to collapse a burst, not to stop checking"
        )


class TestTheEndpointStaysUnthrottled:
    """Guarding the fix's premise. If someone 'solves' this by rate-limiting
    /health instead, a load balancer starts getting 429s and kills healthy
    containers -- the exact outcome the original comment refused."""

    def test_health_is_still_exempt_from_rate_limiting(self):
        from api.middleware import _EXEMPT_PATHS

        assert "/health" in _EXEMPT_PATHS, (
            "/health was moved under the rate limiter. Liveness probes must "
            "not be throttled -- cache the probe result instead"
        )
