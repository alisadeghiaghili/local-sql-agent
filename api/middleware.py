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

Rate-limit tuning (env vars)
-----------------------------
RATE_LIMIT_REQUESTS   — number of requests allowed per window  (default: 60)
RATE_LIMIT_WINDOW_SEC — sliding window size in seconds          (default: 60)
RATE_LIMIT_BURST      — max instantaneous burst above the base  (default: 10)
TRUSTED_PROXY_IPS     — comma-separated IPs allowed to supply X-Forwarded-For
                        / X-Real-IP for rate-limiting            (default: empty)
RATE_LIMIT_MAX_TRACKED_IPS — max distinct client buckets kept in memory at
                        once, LRU-evicted beyond this            (default: 10000)

Bucket identity (Phase 8): a request with a principal resolved by
``AuthMiddleware`` buckets on ``principal:<id>`` instead of IP, so callers
behind a shared proxy (or NAT) each get their own allowance instead of
exhausting one shared-IP bucket for the whole organisation. An
unauthenticated request still buckets on IP, exactly as before this phase.

Example: RATE_LIMIT_REQUESTS=30 RATE_LIMIT_WINDOW_SEC=60 RATE_LIMIT_BURST=5
  → allows 30 req/min sustained, with a burst of up to 5 extra tokens.

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
_RATE_REQUESTS: int = int(os.getenv("RATE_LIMIT_REQUESTS", "60"))
_RATE_WINDOW: float = float(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
_RATE_BURST: int = int(os.getenv("RATE_LIMIT_BURST", "10"))
_MAX_TRACKED_IPS: int = int(os.getenv("RATE_LIMIT_MAX_TRACKED_IPS", "10000"))

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
    window_seconds:
        Length of the refill window in seconds.
    burst:
        Extra tokens above the sustained rate that a client can spend
        instantly (capacity = requests_per_window + burst).
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
        requests_per_window: int = _RATE_REQUESTS,
        window_seconds: float = _RATE_WINDOW,
        burst: int = _RATE_BURST,
        trusted_proxies: frozenset[str] = _TRUSTED_PROXIES,
        max_tracked_ips: int = _MAX_TRACKED_IPS,
    ) -> None:
        super().__init__(app)
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

        Behind a shared proxy, every caller's ``request.client.host`` is
        the same proxy address — bucketing on IP alone would then put the
        whole organisation in one bucket. When ``api.auth.AuthMiddleware``
        (which must run before this middleware — see ``api/server.py``'s
        middleware ordering) has resolved a principal for this request,
        key on that principal's id instead; an unauthenticated request
        (missing/invalid key, or ``AUTH_REQUIRED=false`` with no key
        presented) falls back to the IP-based key exactly as before this
        phase.
        """
        principal = getattr(request.state, "principal", None)
        if principal is not None:
            return f"principal:{principal.id}"
        return f"ip:{self._client_ip(request)}"

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
                            f"Too many requests. "
                            f"Allowed {self._requests_per_window} requests per "
                            f"{int(self._window)}s window. "
                            f"Retry in {retry_after:.1f}s."
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
