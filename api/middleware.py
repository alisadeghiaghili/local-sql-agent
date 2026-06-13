"""ASGI middleware for the API.

Middleware stack (outer → inner)
--------------------------------
1. RequestIDMiddleware   — stamps every request with X-Request-ID
2. RateLimitMiddleware   — per-IP token-bucket rate limiter (429)
3. ConcurrencyMiddleware — rejects requests when MAX_CONCURRENT_REQUESTS
                            active requests are already in flight (503)

Rate-limit tuning (env vars)
-----------------------------
RATE_LIMIT_REQUESTS   — number of requests allowed per window  (default: 60)
RATE_LIMIT_WINDOW_SEC — sliding window size in seconds          (default: 60)
RATE_LIMIT_BURST      — max instantaneous burst above the base  (default: 10)

Example: RATE_LIMIT_REQUESTS=30 RATE_LIMIT_WINDOW_SEC=60 RATE_LIMIT_BURST=5
  → allows 30 req/min sustained, with a burst of up to 5 extra tokens.

Concurrency tuning
------------------
MAX_CONCURRENT_REQUESTS — max parallel /query requests before 503 (default: 10)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import MutableMapping

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

# Paths that are exempt from rate limiting (health, docs, cache admin)
_EXEMPT_PATHS: frozenset[str] = frozenset({
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/cache/stats",
    "/cache/clear",
    "/cache/invalidate",
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
    """Token-bucket state for a single IP."""
    tokens: float
    last_refill: float = field(default_factory=time.monotonic)


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
    """

    def __init__(
        self,
        app: ASGIApp,
        requests_per_window: int = _RATE_REQUESTS,
        window_seconds: float = _RATE_WINDOW,
        burst: int = _RATE_BURST,
    ) -> None:
        super().__init__(app)
        self._capacity: float = requests_per_window + burst
        self._refill_rate: float = requests_per_window / window_seconds  # tokens / second
        self._window: float = window_seconds
        self._buckets: MutableMapping[str, _Bucket] = defaultdict(
            lambda: _Bucket(tokens=self._capacity)
        )
        self._lock = Lock()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _client_ip(self, request: Request) -> str:
        """Best-effort client IP extraction (handles reverse-proxy headers)."""
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
        if request.client:
            return request.client.host
        return "unknown"

    def _consume(self, ip: str) -> tuple[bool, float]:
        """Try to consume one token for *ip*.

        Returns
        -------
        (allowed, retry_after)
            *allowed* — True if the request is permitted.
            *retry_after* — seconds until next token is available (0 if allowed).
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets[ip]
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

        ip = self._client_ip(request)
        allowed, retry_after = self._consume(ip)

        if not allowed:
            request_id = getattr(request.state, "request_id", "")
            logger.warning(
                "[%s] Rate limit exceeded for IP %s — retry in %.1fs",
                request_id,
                ip,
                retry_after,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMIT_EXCEEDED",
                        "message": (
                            f"Too many requests. "
                            f"Allowed {_RATE_REQUESTS} requests per "
                            f"{int(_RATE_WINDOW)}s window. "
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
        response.headers["X-RateLimit-Limit"] = str(_RATE_REQUESTS)
        response.headers["X-RateLimit-Window"] = str(int(_RATE_WINDOW))
        return response


# ---------------------------------------------------------------------------
# 3. Concurrency limiter
# ---------------------------------------------------------------------------

class ConcurrencyMiddleware(BaseHTTPMiddleware):
    """Return 503 when more than *max_concurrent* requests are in-flight.

    Only applies to ``POST /query`` — health checks and docs are always served.
    """

    def __init__(self, app: ASGIApp, max_concurrent: int = _MAX_CONCURRENT) -> None:
        super().__init__(app)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max = max_concurrent

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path != "/query":
            return await call_next(request)

        acquired = self._semaphore._value > 0
        if not acquired:
            request_id = getattr(request.state, "request_id", "")
            logger.warning(
                "[%s] Server overload — %d/%d slots used",
                request_id,
                self._max - self._semaphore._value,
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

        async with self._semaphore:
            return await call_next(request)
