# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""ASGI middleware for the API.

Middleware stack (outer → inner)
--------------------------------
1. RequestIDMiddleware   — stamps every request with X-Request-ID
2. AuthMiddleware         — resolves ``request.state.principal`` from the
                            ``Authorization`` header (``api/auth.py`` — Phase 8).
                            Runs before RateLimitMiddleware so the limiter can
                            bucket on principal id instead of IP.
3. RateLimitMiddleware   — per-IP (or per-principal — see below) token-bucket
                            rate limiter (429)
4. ConcurrencyMiddleware — rejects requests when MAX_CONCURRENT_REQUESTS
                            active requests are already in flight (503)

See ``api/server.py``'s own middleware-registration comment for the exact
``add_middleware()`` call order that produces this stack (Starlette
applies it in reverse).

Rate-limit tuning
------------------
``RATE_LIMIT_REQUESTS`` / ``RATE_LIMIT_WINDOW_SEC`` / ``RATE_LIMIT_BURST``
are ``config.Settings`` fields (:attr:`~config.Settings.rate_limit_requests`,
:attr:`~config.Settings.rate_limit_window_seconds`,
:attr:`~config.Settings.rate_limit_burst`) as of the deployment-readiness
pass — see that module for their current defaults and the reasoning behind
them (the short version: the old ``60``/``60``/``10`` defaults meant one
whole organisation sharing one service key got 60 requests per minute
*total*, not per analyst). ``RateLimitMiddleware.__init__`` resolves them
itself, read through ``cfg.settings`` **at construction time** (not at this
module's import time, and not baked into the class's own default-parameter
values) — see the docstring on that class for exactly why, and see
``config.Settings.rate_limit_requests``'s own docstring for why the
*shared* ``api.server.app`` instance still needs the test-suite override in
place before the whole pytest session's first request, not just "before
some test runs".

TRUSTED_PROXY_IPS          — comma-separated IPs allowed to supply
                             X-Forwarded-For / X-Real-IP for rate-limiting
                             (env var, not a Settings field; default: empty)
RATE_LIMIT_MAX_TRACKED_IPS — max distinct client buckets kept in memory at
                             once, LRU-evicted beyond this (env var, not a
                             Settings field; default: 10000)

Bucket identity (Phase 8): a request with a principal resolved by
``AuthMiddleware`` buckets on ``principal:<id>`` instead of IP, so callers
behind a shared proxy (or NAT) each get their own allowance instead of
exhausting one shared-IP bucket for the whole organisation. An
unauthenticated request still buckets on IP, exactly as before this phase.

Example: RATE_LIMIT_REQUESTS=600 RATE_LIMIT_WINDOW_SEC=60 RATE_LIMIT_BURST=40
  → allows 600 req/min sustained per (principal, ip) bucket, with a burst
  of up to 40 extra tokens — see config.Settings.rate_limit_requests for
  the reasoning behind these numbers.

TRUSTED_PROXY_IPS is empty by default, meaning X-Forwarded-For / X-Real-IP
are never trusted and the rate limiter always keys on the raw TCP peer
address. Set it only when this server sits behind a known reverse proxy
(so the proxy's own address is what request.client.host reports) —
otherwise any direct client can bypass its per-IP limit by sending a
different made-up X-Forwarded-For value on every request.

Concurrency tuning
------------------
MAX_CONCURRENT_REQUESTS — max parallel /query requests before 503 (default: 10)
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

import config as cfg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env-var configuration
# ---------------------------------------------------------------------------

_MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "10"))
_MAX_TRACKED_IPS: int = int(os.getenv("RATE_LIMIT_MAX_TRACKED_IPS", "10000"))

# RATE_LIMIT_REQUESTS / RATE_LIMIT_WINDOW_SEC / RATE_LIMIT_BURST are
# deliberately NOT read here as module-level constants (contrast
# _MAX_CONCURRENT / _MAX_TRACKED_IPS just above, which are out of this
# change's scope) -- they now live on config.Settings
# (rate_limit_requests / rate_limit_window_seconds / rate_limit_burst) and
# are read through cfg.settings at RateLimitMiddleware construction time
# instead, so config.override_settings() reaches a middleware instance
# built with no explicit constructor kwargs. See that class's __init__ and
# config.Settings' own fields for the full reasoning.

# IPs allowed to supply X-Forwarded-For / X-Real-IP for rate-limit purposes
# — i.e. known reverse proxies sitting in front of this server. Empty by
# default: trust nothing, since a server exposed directly to the internet
# must never let a client set its own rate-limit identity via a header.
_TRUSTED_PROXIES: frozenset[str] = frozenset(
    ip.strip() for ip in os.getenv("TRUSTED_PROXY_IPS", "").split(",") if ip.strip()
)

# Paths that are exempt from rate limiting. Only read-only endpoints
# belong here: /cache/clear and /cache/invalidate are unauthenticated,
# state-mutating POST endpoints (see api/server.py) and must NOT be
# exempt, or anyone can flush/evict the shared query cache as fast as
# the network allows.
#
# /health itself is a known DoS surface (tracked for Phase 8, not fixed
# here): each call performs two real ~5s network probes (the LLM endpoint +
# database) against the shared connection pool, so even a rate-limited
# flood of /health requests can still exhaust pool capacity for real
# /query traffic. It stays exempt for now because liveness checks (load
# balancers, container orchestrators) must never be rate-limited away.
_EXEMPT_PATHS: frozenset[str] = frozenset({
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/cache/stats",
})


# ---------------------------------------------------------------------------
# 1. Request ID
# ---------------------------------------------------------------------------

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Ensure every request and response carries an X-Request-ID header."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id

        start = time.perf_counter()
        response: Response = await call_next(request)
        elapsed = time.perf_counter() - start

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{elapsed:.3f}s"
        return response


# ---------------------------------------------------------------------------
# 2. Rate limiter  (token-bucket per IP)
# ---------------------------------------------------------------------------

@dataclass
class _Bucket:
    """Token-bucket state for a single IP.

    ``last_refill`` is deliberately required rather than defaulting to
    ``time.monotonic()``: the caller must stamp it with the very same
    *now* it uses to compute ``elapsed``. A factory reading the clock
    independently is called *after* the caller's reading, which makes a
    brand-new bucket's first ``elapsed`` slightly negative and shaves an
    epsilon off its starting tokens -- enough to spuriously reject the
    very first request when capacity is exactly 1.
    """
    tokens: float
    last_refill: float


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP token-bucket rate limiter.

    Parameters
    ----------
    requests_per_window:
        Sustained request allowance (tokens refilled over *window_seconds*).
        ``None`` (the default) reads :attr:`config.Settings.rate_limit_requests`
        through ``cfg.settings`` **at construction time** — i.e. when this
        middleware is actually instantiated (Starlette builds
        ``api.server.app``'s middleware stack lazily, on its first request,
        and caches the result for the app's lifetime — see
        :attr:`config.Settings.rate_limit_requests`'s own docstring for why
        that matters for the shared test-suite app). Passing an explicit
        int (as most of this module's own tests do, and as any deployment
        overriding the env-derived default in code rather than via
        ``RATE_LIMIT_REQUESTS`` would) always wins over ``cfg.settings``.
    window_seconds:
        Length of the refill window in seconds. ``None`` reads
        :attr:`config.Settings.rate_limit_window_seconds` the same way.
    burst:
        Extra tokens above the sustained rate that a client can spend
        instantly (capacity = requests_per_window + burst). ``None`` reads
        :attr:`config.Settings.rate_limit_burst` the same way.
    trusted_proxies:
        IPs whose ``X-Forwarded-For`` / ``X-Real-IP`` headers are honoured
        for rate-limiting. Any direct client whose TCP peer address is
        *not* in this set has its headers ignored outright — otherwise a
        direct client could bypass its own per-IP limit forever simply by
        sending a different made-up ``X-Forwarded-For`` value on every
        request. Defaults to :data:`_TRUSTED_PROXIES` (empty unless
        ``TRUSTED_PROXY_IPS`` is set), i.e. trust nothing.
    max_tracked_ips:
        Maximum number of distinct client buckets kept in memory at once.
        The store is keyed by client IP, which is spoofable by anyone
        whose header isn't trusted (see trusted_proxies above) and, at a
        minimum, always attacker-controllable in the sense that any
        client can make requests from many source addresses -- so left
        unbounded it is a memory-exhaustion vector. Beyond this many
        distinct clients, the least-recently-seen bucket is evicted to
        make room for a new one -- the same LRU policy QueryCache already
        uses for cached responses.
    """

    def __init__(
        self,
        app: ASGIApp,
        requests_per_window: int | None = None,
        window_seconds: float | None = None,
        burst: int | None = None,
        trusted_proxies: frozenset[str] = _TRUSTED_PROXIES,
        max_tracked_ips: int = _MAX_TRACKED_IPS,
    ) -> None:
        super().__init__(app)
        # Resolved through cfg.settings HERE, at construction time, rather
        # than via Python default-parameter values (which would be baked
        # in once, at this module's own import time -- exactly the
        # import-time-capture problem this whole change moves away from).
        # A caller passing an explicit value always wins; None means "ask
        # cfg.settings right now".
        if requests_per_window is None or window_seconds is None or burst is None:
            if requests_per_window is None:
                requests_per_window = cfg.settings.rate_limit_requests
            if window_seconds is None:
                window_seconds = cfg.settings.rate_limit_window_seconds
            if burst is None:
                burst = cfg.settings.rate_limit_burst
        self._requests_per_window: int = requests_per_window
        self._capacity: float = requests_per_window + burst
        self._refill_rate: float = requests_per_window / window_seconds  # tokens / second
        self._window: float = window_seconds
        self._trusted_proxies: frozenset[str] = trusted_proxies
        self._max_tracked_ips: int = max_tracked_ips
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = Lock()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _client_ip(self, request: Request) -> str:
        """Best-effort client IP extraction.

        ``X-Forwarded-For`` / ``X-Real-IP`` are only honoured when the
        request's actual TCP peer is a configured trusted proxy — anyone
        else's copy of these headers is attacker-controlled and ignored,
        falling back straight to the real socket address.
        """
        direct_ip = request.client.host if request.client else "unknown"
        if direct_ip not in self._trusted_proxies:
            return direct_ip

        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
        return direct_ip

    def _bucket_key(self, request: Request) -> str:
        """The rate-limit bucket identity for *request* (Phase 8).

        Keyed on the **pair** (principal, ip) when the caller is
        authenticated, because either half alone collapses in a real
        deployment:

        * **IP alone.** Behind a shared proxy every caller's
          ``request.client.host`` is the same proxy address, so the whole
          organisation shares one bucket.
        * **Principal alone.** An organisation that fronts this API with
          one web UI issues that UI a single service key — so every user
          of it shares one bucket. This was the shape shipped in Phase 8,
          and it traded the first collapse for the second rather than
          fixing it. The test suite made it visible: 1800+ requests under
          a single test principal exhausted one bucket and produced
          intermittent 429s that looked like unrelated test flakiness.

        The pair separates per-user keys behind one proxy *and* distinct
        clients sharing one service key. One shared key behind one proxy
        still collapses, but that traffic is genuinely indistinguishable.

        ``api.auth.AuthMiddleware`` must run before this middleware — see
        ``api/server.py``'s middleware ordering. An unauthenticated
        request (missing/invalid key, or ``AUTH_REQUIRED=false`` with no
        key presented) falls back to the IP-only key.
        """
        ip = self._client_ip(request)
        principal = getattr(request.state, "principal", None)
        if principal is not None:
            return f"principal:{principal.id}|ip:{ip}"
        return f"ip:{ip}"

    def _consume(self, bucket_key: str) -> tuple[bool, float]:
        """Try to consume one token for *bucket_key* (an IP- or principal-keyed identity).

        Returns
        -------
        (allowed, retry_after)
            *allowed* — True if the request is permitted.
            *retry_after* — seconds until next token is available (0 if allowed).
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                if len(self._buckets) >= self._max_tracked_ips:
                    self._buckets.popitem(last=False)  # evict least-recently-seen
                # Stamp last_refill with *this* call's `now` so a fresh
                # bucket's first `elapsed` is exactly 0.0, never negative.
                bucket = _Bucket(tokens=self._capacity, last_refill=now)
                self._buckets[bucket_key] = bucket
            else:
                self._buckets.move_to_end(bucket_key)

            # Refill tokens proportional to elapsed time
            elapsed = now - bucket.last_refill
            bucket.tokens = min(
                self._capacity,
                bucket.tokens + elapsed * self._refill_rate,
            )
            bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            else:
                retry_after = (1.0 - bucket.tokens) / self._refill_rate
                return False, retry_after

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        key = self._bucket_key(request)
        allowed, retry_after = self._consume(key)

        if not allowed:
            request_id = getattr(request.state, "request_id", "")
            logger.warning(
                "[%s] Rate limit exceeded for %s — retry in %.1fs",
                request_id,
                key,
                retry_after,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMIT_EXCEEDED",
                        "message": (
                            f"Rate limit exceeded (this is a client throttling "
                            f"response, not a query or model failure). "
                            f"Allowed {self._requests_per_window} requests per "
                            f"{int(self._window)}s window for this caller. "
                            f"Retry after {retry_after:.1f}s."
                        ),
                        "request_id": request_id,
                        "path": str(request.url.path),
                        "retry_after_seconds": round(retry_after, 2),
                    }
                },
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

        response = await call_next(request)
        # Informational headers so clients can self-throttle
        response.headers["X-RateLimit-Limit"] = str(self._requests_per_window)
        response.headers["X-RateLimit-Window"] = str(int(self._window))
        return response


# ---------------------------------------------------------------------------
# 3. Concurrency limiter
# ---------------------------------------------------------------------------

class ConcurrencyMiddleware(BaseHTTPMiddleware):
    """Return 503 when more than *max_concurrent* requests are in-flight.

    Only applies to ``POST /query`` — health checks and docs are always served.

    Implementation note
    --------------------
    The previous implementation decided whether to accept a request by
    reading ``asyncio.Semaphore``'s private, undocumented ``_value``
    attribute, then performed a *separate* ``async with self._semaphore:``
    to actually acquire it — a check-then-act pattern whose safety relied
    entirely on the undocumented fact that ``Semaphore.acquire()``'s fast
    path never suspends, rather than on anything asyncio's public contract
    guarantees. If acquisition ever needs to yield control for real (a
    perfectly legitimate thing for a semaphore implementation to do), an
    excess request's stale "a slot is free" check would let it fall
    through into the ``async with`` block anyway, where it would then
    silently **queue** for a slot instead of being rejected outright —
    defeating the whole point of a hard concurrency cap.

    (The obvious-looking replacement, ``asyncio.wait_for(sem.acquire(),
    timeout=0)``, does not work either: asyncio special-cases
    ``timeout<=0`` to cancel the wrapped coroutine *before it ever runs*
    — see ``asyncio.tasks.wait_for``'s own docstring, which documents
    this as intentional — so it raises ``TimeoutError`` unconditionally,
    even when a slot is free. Confirmed against this interpreter: it
    rejects 100% of requests, making it strictly worse than the bug it
    would "fix".)

    Instead, in-flight requests are tracked with a plain counter guarded
    by a ``threading.Lock`` (the same primitive ``RateLimitMiddleware``
    already uses for its bucket state, just below). The accept-or-reject
    decision and the matching increment happen inside the lock with no
    ``await`` in between, so the decision is genuinely atomic — both
    against other asyncio tasks on the same event loop and against real
    OS-thread concurrency touching this middleware instance (which is
    exactly how the test suite exercises it: multiple ``TestClient``
    instances, each with its own event loop, hitting the same app).
    """

    def __init__(self, app: ASGIApp, max_concurrent: int = _MAX_CONCURRENT) -> None:
        super().__init__(app)
        self._max = max_concurrent
        self._active = 0
        self._lock = Lock()

    def _try_acquire(self) -> bool:
        """Atomically accept-and-count, or refuse. Never blocks."""
        with self._lock:
            if self._active >= self._max:
                return False
            self._active += 1
            return True

    def _release(self) -> None:
        with self._lock:
            self._active -= 1

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path != "/query":
            return await call_next(request)

        if not self._try_acquire():
            request_id = getattr(request.state, "request_id", "")
            logger.warning(
                "[%s] Server overload — %d/%d slots used",
                request_id,
                self._max,
                self._max,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "SERVER_OVERLOAD",
                        "message": (
                            f"Server is at capacity ({self._max} concurrent requests). "
                            "Please retry in a few seconds."
                        ),
                        "request_id": request_id,
                        "path": str(request.url.path),
                    }
                },
                headers={"Retry-After": "5"},
            )

        try:
            return await call_next(request)
        finally:
            self._release()
