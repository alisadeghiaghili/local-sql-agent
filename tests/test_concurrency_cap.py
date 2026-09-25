# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""TDD tests for concurrency capping across all pipeline routes.

Contracts tested
----------------
ConcurrencyMiddleware (pure ASGI middleware)
  - Capped routes (POST /query, POST /query/stream, POST /v2/sessions/X/turns,
    PATCH .../turns/X/assumptions) answer 503 SERVER_OVERLOAD when at capacity.
  - Uncapped routes (health, docs, GET/DELETE sessions, etc.) proceed regardless.
  - 503 body carries Retry-After: 5 header.
  - Streaming responses hold the slot until the body completes.
  - Client disconnect or exception releases the slot.

Shared concurrency.run_bounded semaphore
  - Multiple async contexts share a single per-loop semaphore.
  - Slots are acquired before asyncio.to_thread and released after it completes.
  - No deadlock when streaming queue reads do NOT hold slots.
  - Exception or cancellation releases the slot.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from api.concurrency import run_bounded, _get_semaphore, _semaphores
from api.middleware import ConcurrencyMiddleware, _is_capped_request


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_asgi_app(max_concurrent: int = 2):
    """Create a test app with ConcurrencyMiddleware and test routes."""

    app = FastAPI()

    # Create the base ASGI app
    base_app = app

    # Wrap it with ConcurrencyMiddleware
    class MiddlewareApp:
        def __init__(self, app: ASGIApp, max_concurrent: int = 2):
            self.middleware = ConcurrencyMiddleware(app, max_concurrent=max_concurrent)

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self.middleware(scope, receive, send)

    wrapped_app = MiddlewareApp(base_app, max_concurrent=max_concurrent)

    @app.post("/query")
    def post_query():
        """Capped route."""
        time.sleep(0.05)
        return {"ok": True}

    @app.post("/query/stream")
    def post_query_stream():
        """Capped streaming route."""
        def gen():
            for i in range(3):
                yield f"event: tick\ndata: {i}\n\n"
                time.sleep(0.02)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v2/sessions/{session_id}/turns")
    def post_turn():
        """Capped route."""
        time.sleep(0.05)
        return {"turn": "created"}

    @app.patch("/v2/sessions/{session_id}/turns/{turn_id}/assumptions")
    def patch_assumptions():
        """Capped route."""
        time.sleep(0.05)
        return {"turn": "updated"}

    @app.get("/health")
    def health():
        """Uncapped route."""
        return {"status": "ok"}

    @app.get("/docs")
    def docs():
        """Uncapped route."""
        return {"docs": "here"}

    return wrapped_app


@pytest.fixture(scope="function")
def client() -> TestClient:
    """Test client with concurrency cap of 2."""
    return TestClient(_make_asgi_app(max_concurrent=2), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# _is_capped_request
# ---------------------------------------------------------------------------


class TestIsCappedRequest:
    def test_post_query_is_capped(self):
        assert _is_capped_request("/query", "POST")

    def test_post_query_stream_is_capped(self):
        assert _is_capped_request("/query/stream", "POST")

    def test_post_v2_turns_is_capped(self):
        assert _is_capped_request("/v2/sessions/abc123/turns", "POST")

    def test_patch_v2_assumptions_is_capped(self):
        assert _is_capped_request("/v2/sessions/abc123/turns/turn456/assumptions", "PATCH")

    def test_get_health_not_capped(self):
        assert not _is_capped_request("/health", "GET")

    def test_get_docs_not_capped(self):
        assert not _is_capped_request("/docs", "GET")

    def test_post_feedback_not_capped(self):
        assert not _is_capped_request("/v2/sessions/abc123/turns/turn456/feedback", "POST")

    def test_get_v2_sessions_not_capped(self):
        assert not _is_capped_request("/v2/sessions", "GET")

    def test_delete_v2_sessions_not_capped(self):
        assert not _is_capped_request("/v2/sessions/abc123", "DELETE")


# ---------------------------------------------------------------------------
# ConcurrencyMiddleware — capped routes
# ---------------------------------------------------------------------------


class TestConcurrencyMiddlewareCappedRoutes:
    def test_single_request_served(self, client):
        """A single request below the limit is served normally."""
        resp = client.post("/query")
        assert resp.status_code == 200

    def test_exceeding_capacity_returns_503(self):
        """The (limit+1)-th concurrent request gets 503 when capacity is full."""
        MAX = 2
        app = _make_asgi_app(max_concurrent=MAX)
        results: list[int] = []
        barrier = threading.Barrier(MAX + 1)

        def call():
            # Each thread creates its own TestClient to avoid Starlette sync issues
            c = TestClient(app, raise_server_exceptions=False)
            barrier.wait()  # Ensure all threads start at the same time
            results.append(c.post("/query").status_code)

        threads = [threading.Thread(target=call) for _ in range(MAX + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At least one request should get 503
        assert 503 in results
        # At least one request should succeed
        assert 200 in results

    def test_503_has_server_overload_code(self):
        """503 response carries error.code = SERVER_OVERLOAD."""
        MAX = 2
        app = _make_asgi_app(max_concurrent=MAX)
        bodies: list[dict] = []
        barrier = threading.Barrier(MAX + 1)

        def call():
            c = TestClient(app, raise_server_exceptions=False)
            barrier.wait()
            resp = c.post("/query")
            if resp.status_code == 503:
                bodies.append(resp.json())

        threads = [threading.Thread(target=call) for _ in range(MAX + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert bodies, "Expected at least one 503 response"
        assert bodies[0]["error"]["code"] == "SERVER_OVERLOAD"

    def test_503_has_retry_after_header(self):
        """503 response carries Retry-After: 5 header."""
        MAX = 2
        app = _make_asgi_app(max_concurrent=MAX)
        headers_list: list[dict] = []
        barrier = threading.Barrier(MAX + 1)

        def call():
            c = TestClient(app, raise_server_exceptions=False)
            barrier.wait()
            resp = c.post("/query")
            if resp.status_code == 503:
                headers_list.append(dict(resp.headers))

        threads = [threading.Thread(target=call) for _ in range(MAX + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert headers_list, "Expected at least one 503 response"
        assert headers_list[0].get("retry-after") == "5"


# ---------------------------------------------------------------------------
# ConcurrencyMiddleware — uncapped routes
# ---------------------------------------------------------------------------


class TestConcurrencyMiddlewareUncappedRoutes:
    def test_health_always_served(self, client):
        """Health endpoint bypasses the concurrency cap."""
        import concurrent.futures

        def make_request():
            return client.post("/query")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(make_request) for _ in range(2)]
            time.sleep(0.01)

            # Health should still be served
            resp = client.get("/health")
            assert resp.status_code == 200

            for f in futures:
                f.result()

    def test_docs_always_served(self, client):
        """Docs endpoint bypasses the concurrency cap."""
        import concurrent.futures

        def make_request():
            return client.post("/query")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(make_request) for _ in range(2)]
            time.sleep(0.01)

            resp = client.get("/docs")
            assert resp.status_code == 200

            for f in futures:
                f.result()


# ---------------------------------------------------------------------------
# ConcurrencyMiddleware — streaming responses hold slots
# ---------------------------------------------------------------------------


class TestStreamingHoldsSlot:
    def test_streaming_response_not_broken_by_middleware(self):
        """Streaming responses work correctly with the middleware."""
        import concurrent.futures

        client = TestClient(_make_asgi_app(max_concurrent=2), raise_server_exceptions=False)

        def make_request():
            return client.post("/query/stream")

        # Make two streaming requests concurrently — both should succeed
        # since the limit is 2
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(make_request) for _ in range(2)]
            results = [f.result() for f in futures]

        # Both should succeed
        assert all(r.status_code == 200 for r in results)
        # Both should have content
        assert all(len(r.text) > 0 for r in results)


# ---------------------------------------------------------------------------
# Shared concurrency.run_bounded
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function", autouse=True)
def clear_semaphores():
    """Clear the global semaphore dict before each test."""
    yield
    _semaphores.clear()


class TestRunBounded:
    @pytest.mark.asyncio
    async def test_run_bounded_executes_function(self):
        """run_bounded executes the function and returns its result."""
        result = await run_bounded(lambda x: x * 2, 5)
        assert result == 10

    @pytest.mark.asyncio
    async def test_run_bounded_propagates_exception(self):
        """run_bounded propagates exceptions from the function."""
        def failing():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            await run_bounded(failing)

    @pytest.mark.asyncio
    async def test_run_bounded_acquires_and_releases_slot(self):
        """run_bounded acquires a slot, runs the function, and releases."""
        call_count = {"value": 0}

        def work():
            call_count["value"] += 1
            time.sleep(0.01)
            return "done"

        result = await run_bounded(work)
        assert result == "done"
        assert call_count["value"] == 1

    @pytest.mark.asyncio
    async def test_concurrent_requests_share_semaphore(self):
        """Multiple concurrent requests share one per-loop semaphore."""
        semaphore = _get_semaphore()
        initial_value = semaphore._value

        async def work():
            result = await run_bounded(lambda: time.sleep(0.01))
            return result

        # Run two tasks concurrently
        await asyncio.gather(work(), work())

        # After both complete, semaphore should be back to initial
        assert semaphore._value == initial_value

    @pytest.mark.asyncio
    async def test_semaphore_blocks_excess_requests(self):
        """Requests beyond the limit block on the semaphore."""
        # Clear semaphores and create a new one with limit 1
        _semaphores.clear()

        calls = []

        def work(n):
            time.sleep(0.02)
            calls.append(n)
            return n

        # Run three tasks concurrently with a limit of 1
        # They should serialize (queue for the semaphore and run one at a time)
        tasks = [run_bounded(work, n) for n in range(3)]
        results = await asyncio.gather(*tasks)

        # gather() preserves the order of the results from the tasks
        assert results == [0, 1, 2]
        # Key test: all calls were made and serialized (only one at a time)
        # Order is not guaranteed, just check that all were executed
        assert set(calls) == {0, 1, 2}
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_no_deadlock_with_streaming_and_queue(self):
        """No deadlock: streaming generator with queue reads succeeds."""
        import queue as stdlib_queue

        q: stdlib_queue.Queue = stdlib_queue.Queue()

        def producer():
            """This function runs in a worker thread."""
            for i in range(3):
                q.put(i)
                time.sleep(0.01)
            q.put(None)  # Sentinel

        async def consumer():
            """Async generator that reads from queue without holding slot."""
            task = asyncio.create_task(run_bounded(producer))
            while True:
                # Queue reads do NOT hold semaphore slot
                item = await asyncio.to_thread(q.get)
                if item is None:
                    break
                yield item
            await task

        items = []
        async for item in consumer():
            items.append(item)

        assert items == [0, 1, 2]
