# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""TDD tests for api/middleware.py.

Contracts tested
----------------
RequestIDMiddleware
  - Adds X-Request-ID to every response.
  - Echoes the client-supplied X-Request-ID instead of generating one.
  - Adds X-Response-Time header.
  - X-Response-Time is a non-negative float string.

ConcurrencyMiddleware
  - Requests below the limit are served normally.
  - The (limit+1)-th concurrent request gets 503 with SERVER_OVERLOAD.
  - 503 body carries Retry-After header.
  - Non-/query paths are not affected by the concurrency limiter.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middleware import RequestIDMiddleware, ConcurrencyMiddleware, RateLimitMiddleware


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_app(max_concurrent: int = 2) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ConcurrencyMiddleware, max_concurrent=max_concurrent)
    app.add_middleware(RequestIDMiddleware)

    @app.post("/query")
    def slow_query():
        import time
        time.sleep(0.05)
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_make_app(max_concurrent=2), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# RequestIDMiddleware
# ---------------------------------------------------------------------------

class TestRequestIDMiddleware:
    def test_response_has_request_id_header(self, client):
        resp = client.get("/health")
        assert "x-request-id" in resp.headers

    def test_generated_request_id_is_nonempty(self, client):
        resp = client.get("/health")
        assert resp.headers["x-request-id"]

    def test_client_supplied_id_is_echoed(self, client):
        resp = client.get("/health", headers={"x-request-id": "abc123"})
        assert resp.headers["x-request-id"] == "abc123"

    def test_response_has_response_time_header(self, client):
        resp = client.get("/health")
        assert "x-response-time" in resp.headers

    def test_response_time_is_numeric(self, client):
        val = resp.headers["x-response-time"] if (resp := client.get("/health")) else "0"
        assert float(val.rstrip("s")) >= 0


# ---------------------------------------------------------------------------
# ConcurrencyMiddleware
# ---------------------------------------------------------------------------

class TestConcurrencyMiddleware:
    def test_single_request_is_served(self, client):
        resp = client.post("/query")
        assert resp.status_code == 200

    def test_non_query_path_always_served(self, client):
        """Health endpoint must never be blocked regardless of concurrency."""
        for _ in range(10):
            assert client.get("/health").status_code == 200

    def test_overload_returns_503(self):
        """Fill all slots then confirm the extra request gets 503."""
        MAX = 2
        app = _make_app(max_concurrent=MAX)
        # Use requests.Session directly so we can fire truly concurrent calls
        import requests
        from starlette.testclient import TestClient as TC

        results: list[int] = []
        barrier = threading.Barrier(MAX + 1)

        def call():
            with TC(app, raise_server_exceptions=False) as c:
                barrier.wait()  # synchronise thread start
                results.append(c.post("/query").status_code)

        threads = [threading.Thread(target=call) for _ in range(MAX + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert 503 in results

    def test_overload_body_has_correct_code(self):
        """503 body must contain SERVER_OVERLOAD error code."""
        import threading
        from starlette.testclient import TestClient as TC

        MAX = 1
        app = _make_app(max_concurrent=MAX)
        bodies: list[dict] = []
        barrier = threading.Barrier(MAX + 1)

        def call():
            with TC(app, raise_server_exceptions=False) as c:
                barrier.wait()
                resp = c.post("/query")
                if resp.status_code == 503:
                    bodies.append(resp.json())

        threads = [threading.Thread(target=call) for _ in range(MAX + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert bodies
        assert bodies[0]["error"]["code"] == "SERVER_OVERLOAD"

    def test_excess_request_rejected_not_queued_under_contended_acquire(self):
        """The accept/reject decision must be atomic: a request that
        arrives while the middleware is already at capacity must get 503
        immediately, never wait in line for a slot to free up.

        Historical note — this test used to reproduce the bug by
        monkeypatching ``asyncio.Semaphore.acquire`` to yield once before
        deciding. The old implementation read ``asyncio.Semaphore``'s
        private ``_value`` attribute and only *afterwards* performed a
        completely separate ``async with self._semaphore:`` to actually
        acquire it — a check-then-act (TOCTOU) pattern whose safety
        depended entirely on the undocumented CPython detail that
        ``Semaphore.acquire()``'s fast path never suspends. With that
        patch in place, both of two requests against ``max_concurrent=1``
        came back successful instead of one getting 503 — proving the
        check and the act were not atomic (see the commit introducing
        this test for the captured red output).

        The fix removes ``asyncio.Semaphore`` entirely in favour of a
        plain counter guarded by ``threading.Lock`` (``_try_acquire`` /
        ``_release`` below), so there is no separate "acquire" coroutine
        left to make yield — the accept-or-reject decision is a single
        synchronous, lock-held statement. This test now exercises that
        directly: heavy *real* multi-threaded contention (not just
        asyncio-task interleaving) against a capacity of 1 must never let
        more than one caller "in" at a time.
        """
        import threading

        from api.middleware import ConcurrencyMiddleware

        middleware = ConcurrencyMiddleware(app=MagicMock(), max_concurrent=1)
        current_inside = 0
        max_observed_inside = 0
        accepted = 0
        observed_lock = threading.Lock()
        barrier = threading.Barrier(50)

        def worker():
            nonlocal current_inside, max_observed_inside, accepted
            barrier.wait()
            if middleware._try_acquire():
                with observed_lock:
                    accepted += 1
                    current_inside += 1
                    max_observed_inside = max(max_observed_inside, current_inside)
                with observed_lock:
                    current_inside -= 1
                middleware._release()

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max_observed_inside == 1  # never oversubscribed, even under
        assert accepted >= 1             # real thread-level contention
        assert middleware._active == 0   # every acquire was released

    def test_overload_response_has_retry_after_header(self):
        import threading
        from starlette.testclient import TestClient as TC

        MAX = 1
        app = _make_app(max_concurrent=MAX)
        headers_list: list[dict] = []
        barrier = threading.Barrier(MAX + 1)

        def call():
            with TC(app, raise_server_exceptions=False) as c:
                barrier.wait()
                resp = c.post("/query")
                if resp.status_code == 503:
                    headers_list.append(dict(resp.headers))

        threads = [threading.Thread(target=call) for _ in range(MAX + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert headers_list
        assert "retry-after" in headers_list[0]

# ---------------------------------------------------------------------------
# RateLimitMiddleware — trusted-proxy header spoofing (item 5)
# ---------------------------------------------------------------------------

def _rate_limited_app(**middleware_kwargs) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, **middleware_kwargs)

    @app.get("/limited")
    def limited():
        return {"ok": True}

    return app


class TestRateLimitTrustedProxy:
    """RateLimitMiddleware must key its per-client bucket on the actual
    TCP peer unless that peer is an explicitly trusted reverse proxy —
    otherwise any direct client gets unlimited throughput by sending a
    fresh, made-up X-Forwarded-For value on every request."""

    def test_untrusted_client_forwarded_for_header_is_ignored(self):
        """Starlette's TestClient always connects as peer 'testclient',
        which is NOT in the (default-empty) trusted-proxy allowlist, so a
        spoofed X-Forwarded-For must not grant a fresh bucket.

        Capacity 2 (not 1) is incidental here — the fresh-bucket rounding
        edge case that once made capacity 1 flaky is fixed (see
        :class:`TestRateLimitFreshBucketNoRounding`).
        """
        app = _rate_limited_app(requests_per_window=2, window_seconds=60, burst=0)
        client = TestClient(app)

        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 200  # bucket now empty

        resp = client.get("/limited", headers={"x-forwarded-for": "1.2.3.4"})
        assert resp.status_code == 429, (
            "a direct (untrusted) client spoofing X-Forwarded-For must not "
            "bypass the rate limit by claiming a different IP"
        )

    def test_untrusted_client_real_ip_header_is_ignored(self):
        app = _rate_limited_app(requests_per_window=2, window_seconds=60, burst=0)
        client = TestClient(app)

        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 200
        resp = client.get("/limited", headers={"x-real-ip": "9.9.9.9"})
        assert resp.status_code == 429

    def test_trusted_proxy_forwarded_for_header_is_honoured(self):
        """When the direct peer *is* a configured trusted proxy, distinct
        X-Forwarded-For values must get distinct buckets, and a repeated
        value must share the same (now exhausted) bucket."""
        app = _rate_limited_app(
            requests_per_window=2,
            window_seconds=60,
            burst=0,
            trusted_proxies=frozenset({"testclient"}),
        )
        client = TestClient(app)

        assert client.get("/limited", headers={"x-forwarded-for": "5.5.5.5"}).status_code == 200
        assert client.get("/limited", headers={"x-forwarded-for": "5.5.5.5"}).status_code == 200

        resp2 = client.get("/limited", headers={"x-forwarded-for": "6.6.6.6"})
        assert resp2.status_code == 200, "a different forwarded IP must get its own bucket"

        resp3 = client.get("/limited", headers={"x-forwarded-for": "5.5.5.5"})
        assert resp3.status_code == 429, "the same forwarded IP must share its earlier bucket"

    def test_default_trusted_proxies_is_empty(self):
        """Default configuration trusts nothing — the safest default for
        a server exposed directly to the internet."""
        import api.middleware as mw_module

        assert mw_module._TRUSTED_PROXIES == frozenset()


# ---------------------------------------------------------------------------
# RateLimitMiddleware — unbounded bucket growth (item 6)
# ---------------------------------------------------------------------------

class TestRateLimitBucketCap:
    """The bucket store is keyed by (spoofable) client IP and was never
    pruned, so a flood of distinct IPs (or spoofed X-Forwarded-For values,
    per item 5) grows it forever -- a memory-exhaustion vector. It must be
    bounded to a configurable maximum, evicting old entries to make room
    for new ones."""

    def test_bucket_count_never_exceeds_configured_cap(self):
        from api.middleware import RateLimitMiddleware

        middleware = RateLimitMiddleware(
            app=MagicMock(),
            requests_per_window=5,
            window_seconds=60,
            burst=0,
            max_tracked_ips=3,
        )

        for i in range(200):
            middleware._consume(f"10.0.0.{i}")
            assert len(middleware._buckets) <= 3, (
                f"tracked {len(middleware._buckets)} buckets after "
                f"{i + 1} distinct IPs, cap is 3"
            )

        assert len(middleware._buckets) == 3

    def test_default_max_tracked_ips_is_bounded(self):
        import api.middleware as mw_module

        assert mw_module._MAX_TRACKED_IPS > 0
        assert mw_module._MAX_TRACKED_IPS < 10_000_000  # sanity: not "unbounded"


# ---------------------------------------------------------------------------
# RateLimitMiddleware — 429 body must report the INSTANCE's limits (item 7)
# ---------------------------------------------------------------------------

class TestRateLimit429ReportsInstanceConfig:
    """The 429 body and X-RateLimit-* headers must describe the limits
    THIS middleware instance was actually constructed with, not
    config.Settings' own defaults -- otherwise a middleware constructed
    with custom limits (as tests do, and as any deployment overriding the
    defaults would) reports a number that has nothing to do with what
    just happened."""

    def test_429_message_reports_custom_requests_per_window(self):
        import config as cfg

        custom_requests = 3
        assert custom_requests != cfg.settings.rate_limit_requests
        # A long window keeps refill-during-the-loop negligible so this
        # stays deterministic regardless of how long the requests take.
        app = _rate_limited_app(
            requests_per_window=custom_requests, window_seconds=3600, burst=0,
        )
        client = TestClient(app)

        for _ in range(custom_requests):
            assert client.get("/limited").status_code == 200
        resp = client.get("/limited")

        assert resp.status_code == 429
        assert str(custom_requests) in resp.json()["error"]["message"], (
            f"429 message did not mention the configured limit "
            f"({custom_requests}): {resp.json()['error']['message']!r}"
        )

    def test_429_message_reports_custom_window(self):
        import config as cfg

        custom_window = cfg.settings.rate_limit_window_seconds + 999  # guaranteed to differ
        app = _rate_limited_app(
            # capacity 2, not 1: sidesteps the unrelated pre-existing
            # first-request rounding edge case noted in item 5's tests.
            requests_per_window=2, window_seconds=custom_window, burst=0,
        )
        client = TestClient(app)

        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 200
        resp = client.get("/limited")

        assert resp.status_code == 429
        assert str(int(custom_window)) in resp.json()["error"]["message"]

    def test_ratelimit_headers_report_custom_config(self):
        import config as cfg

        custom_requests = cfg.settings.rate_limit_requests + 42
        custom_window = cfg.settings.rate_limit_window_seconds + 42
        app = _rate_limited_app(
            requests_per_window=custom_requests,
            window_seconds=custom_window,
            burst=0,
        )
        client = TestClient(app)

        resp = client.get("/limited")
        assert resp.status_code == 200
        assert resp.headers["x-ratelimit-limit"] == str(custom_requests)
        assert resp.headers["x-ratelimit-window"] == str(int(custom_window))


# ---------------------------------------------------------------------------
# RateLimitMiddleware — cache admin endpoints must NOT be exempt (item 8)
# ---------------------------------------------------------------------------

class TestRateLimitExemptPaths:
    """/cache/clear and /cache/invalidate are unauthenticated,
    state-mutating POST endpoints (see api/server.py) -- exempting them
    from rate limiting means anyone can flush or evict the shared query
    cache as fast as the network allows. /health and the docs endpoints
    are read-only and legitimately exempt."""

    def test_cache_clear_is_rate_limited(self):
        app = FastAPI()
        app.add_middleware(
            RateLimitMiddleware, requests_per_window=2, window_seconds=3600, burst=0
        )

        @app.post("/cache/clear")
        def clear():
            return {"ok": True}

        client = TestClient(app)
        assert client.post("/cache/clear").status_code == 200
        assert client.post("/cache/clear").status_code == 200
        resp = client.post("/cache/clear")
        assert resp.status_code == 429, (
            "/cache/clear mutates shared state and must be rate-limited"
        )

    def test_cache_invalidate_is_rate_limited(self):
        app = FastAPI()
        app.add_middleware(
            RateLimitMiddleware, requests_per_window=2, window_seconds=3600, burst=0
        )

        @app.post("/cache/invalidate")
        def invalidate():
            return {"ok": True}

        client = TestClient(app)
        assert client.post("/cache/invalidate").status_code == 200
        assert client.post("/cache/invalidate").status_code == 200
        resp = client.post("/cache/invalidate")
        assert resp.status_code == 429, (
            "/cache/invalidate mutates shared state and must be rate-limited"
        )

    def test_health_remains_exempt(self):
        app = FastAPI()
        app.add_middleware(
            RateLimitMiddleware, requests_per_window=1, window_seconds=3600, burst=0
        )

        @app.get("/health")
        def health():
            return {"ok": True}

        client = TestClient(app)
        for _ in range(10):
            assert client.get("/health").status_code == 200

    def test_docs_and_cache_stats_remain_exempt(self):
        """/cache/stats is read-only (GET) -- unlike clear/invalidate, it
        never mutates shared state, so it stays exempt alongside the docs
        endpoints."""
        app = FastAPI()
        app.add_middleware(
            RateLimitMiddleware, requests_per_window=1, window_seconds=3600, burst=0
        )

        @app.get("/cache/stats")
        def stats():
            return {"ok": True}

        client = TestClient(app)
        for _ in range(10):
            assert client.get("/cache/stats").status_code == 200


# ---------------------------------------------------------------------------
# RateLimitMiddleware — fresh bucket must not lose a token to clock ordering
# ---------------------------------------------------------------------------

class TestRateLimitFreshBucketNoRounding:
    """``_consume`` reads ``time.monotonic()`` into *now* and then creates
    the bucket. When the bucket stamped its own ``last_refill`` from a
    second, later clock reading, ``elapsed = now - last_refill`` came out
    slightly NEGATIVE and shaved an epsilon off the starting tokens.
    Invisible at capacity 70, fatal at capacity 1: the first-ever request
    from a brand-new client got a 429."""

    def test_first_request_succeeds_at_capacity_one(self):
        from api.middleware import RateLimitMiddleware

        middleware = RateLimitMiddleware(
            app=MagicMock(), requests_per_window=1, window_seconds=60, burst=0
        )

        allowed, retry_after = middleware._consume("1.2.3.4")
        assert allowed is True, (
            "a brand-new client's very first request must be allowed when "
            "capacity is exactly 1"
        )
        assert retry_after == 0.0

        # ...and the bucket is genuinely spent afterwards.
        assert middleware._consume("1.2.3.4")[0] is False

    def test_fresh_bucket_elapsed_is_never_negative(self):
        """Directly assert the invariant behind the bug: a new bucket's
        ``last_refill`` must never be later than the ``now`` used to
        compute ``elapsed`` in the same call."""
        from api.middleware import RateLimitMiddleware

        middleware = RateLimitMiddleware(
            app=MagicMock(), requests_per_window=5, window_seconds=60, burst=0
        )

        before = time.monotonic()
        middleware._consume("5.6.7.8")
        after = time.monotonic()

        bucket = middleware._buckets["5.6.7.8"]
        assert before <= bucket.last_refill <= after
        assert bucket.tokens == pytest.approx(4.0, abs=0.0), (
            "a fresh bucket must spend exactly one whole token, with no "
            "epsilon lost to a negative elapsed"
        )

    def test_first_request_succeeds_at_capacity_one_end_to_end(self):
        """Same guarantee through the real ASGI stack."""
        app = _rate_limited_app(requests_per_window=1, window_seconds=60, burst=0)
        client = TestClient(app)

        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 429


class TestRateLimitBucketPairsPrincipalAndIp:
    """The bucket key must combine principal and IP, not use either alone.

    Phase 8 moved this limiter from per-IP to per-principal to stop a
    shared proxy putting a whole organisation in one bucket. That traded
    one collapse for another: an organisation fronting this API with a
    single web UI gives that UI one service key, so every user of it lands
    in one bucket again.

    The failure mode is nasty because a 429 does not look like rate
    limiting downstream -- callers reading ``resp.json()["session_id"]``
    get a KeyError from an error envelope. It surfaced as an intermittent
    "flaky test" and as a deterministic CI failure, and took a long time
    to trace.
    """

    @staticmethod
    def _key(mw, *, principal_id: str | None, ip: str) -> str:
        from unittest.mock import MagicMock

        request = MagicMock()
        request.client.host = ip
        request.headers = {}
        request.state.principal = (
            None if principal_id is None else SimpleNamespace(id=principal_id)
        )
        return mw._bucket_key(request)

    def test_same_key_different_ips_do_not_share_a_bucket(self):
        """One service key used by many clients: the common deployment."""
        mw = RateLimitMiddleware(app=MagicMock())
        a = self._key(mw, principal_id="web-ui", ip="10.0.0.7")
        b = self._key(mw, principal_id="web-ui", ip="10.0.0.8")
        assert a != b

    def test_same_ip_different_principals_do_not_share_a_bucket(self):
        """Per-user keys behind one proxy: the case Phase 8 set out to fix."""
        mw = RateLimitMiddleware(app=MagicMock())
        a = self._key(mw, principal_id="analyst-a", ip="10.0.0.1")
        b = self._key(mw, principal_id="analyst-b", ip="10.0.0.1")
        assert a != b

    def test_identical_principal_and_ip_share_one_bucket(self):
        """Genuinely indistinguishable traffic still shares a bucket."""
        mw = RateLimitMiddleware(app=MagicMock())
        a = self._key(mw, principal_id="web-ui", ip="10.0.0.7")
        b = self._key(mw, principal_id="web-ui", ip="10.0.0.7")
        assert a == b

    def test_unauthenticated_falls_back_to_ip_only(self):
        mw = RateLimitMiddleware(app=MagicMock())
        key = self._key(mw, principal_id=None, ip="10.0.0.9")
        assert key == "ip:10.0.0.9"


# ---------------------------------------------------------------------------
# RateLimitMiddleware — RATE_LIMIT_* moved to config.Settings, read at
# construction time (deployment-readiness pass)
# ---------------------------------------------------------------------------

class TestRateLimitSettingsReadAtCallTime:
    """RATE_LIMIT_REQUESTS / RATE_LIMIT_WINDOW_SEC / RATE_LIMIT_BURST used to
    be read once, as os.getenv() calls evaluated at api.middleware's own
    *import* time -- so config.override_settings() never reached a
    RateLimitMiddleware built with no explicit constructor kwargs, only a
    real environment-variable change made before the very first import of
    this module ever could (see tests/conftest.py's RATE_LIMIT_REQUESTS /
    RATE_LIMIT_BURST workaround, needed for exactly that reason).

    Now they live on config.Settings and RateLimitMiddleware.__init__
    resolves them itself, through cfg.settings, at construction time --
    these tests pin that directly: constructing the middleware with NO
    explicit kwargs inside an override_settings() block must pick up the
    override, proving the read genuinely happens at construction time
    rather than being captured once at import."""

    def test_no_kwargs_picks_up_overridden_requests_and_burst(self):
        from config import override_settings

        with override_settings(rate_limit_requests=2, rate_limit_burst=0, rate_limit_window_seconds=3600):
            mw = RateLimitMiddleware(app=MagicMock())
            assert mw._requests_per_window == 2
            assert mw._capacity == 2

    def test_no_kwargs_picks_up_overridden_window(self):
        from config import override_settings

        with override_settings(rate_limit_window_seconds=123.0):
            mw = RateLimitMiddleware(app=MagicMock())
            assert mw._window == 123.0

    def test_explicit_kwarg_still_wins_over_settings(self):
        """An explicit constructor argument (what every other test in this
        module passes) must never be silently overridden by cfg.settings --
        only an omitted (None) argument falls back to it."""
        from config import override_settings

        with override_settings(rate_limit_requests=999):
            mw = RateLimitMiddleware(app=MagicMock(), requests_per_window=5)
            assert mw._requests_per_window == 5

    def test_end_to_end_override_without_constructor_kwargs(self):
        """Same guarantee through a real ASGI app built with no kwargs at
        all -- the shape api.server.py's own app.add_middleware(RateLimitMiddleware)
        call uses."""
        from config import override_settings

        with override_settings(
            rate_limit_requests=1, rate_limit_burst=0, rate_limit_window_seconds=3600,
        ):
            app = _rate_limited_app()
            client = TestClient(app)
            assert client.get("/limited").status_code == 200
            assert client.get("/limited").status_code == 429
