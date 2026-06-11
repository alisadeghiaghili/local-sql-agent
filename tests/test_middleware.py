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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middleware import RequestIDMiddleware, ConcurrencyMiddleware


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
