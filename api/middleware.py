"""ASGI middleware for the API.

Middleware stack (outer → inner)
--------------------------------
1. RequestIDMiddleware   — stamps every request with X-Request-ID
2. ConcurrencyMiddleware — rejects requests when MAX_CONCURRENT_REQUESTS
                            active requests are already in flight (503)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

import config as cfg

logger = logging.getLogger(__name__)

# Maximum concurrent /query requests before 503 is returned.
# Tune via MAX_CONCURRENT_REQUESTS env var (default: 10).
import os
_MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "10"))


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
# 2. Concurrency limiter
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
        # Skip limiter for non-query paths
        if request.url.path != "/query":
            return await call_next(request)

        acquired = self._semaphore._value > 0  # non-blocking peek
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
                        "request_id": getattr(request.state, "request_id", ""),
                        "path": str(request.url.path),
                    }
                },
                headers={"Retry-After": "5"},
            )

        async with self._semaphore:
            return await call_next(request)
