# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for concurrency capping across all pipeline routes.

Contracts tested
----------------
``_is_capped_request``
  - Exactly the pipeline-running routes are capped; everything else is not.

``ConcurrencyMiddleware`` (pure ASGI)
  - A streaming response holds its slot for the WHOLE body, not just until
    headers are ready (the regression BaseHTTPMiddleware could not fix).
  - An exception from the wrapped app, or a client disconnect surfaced as
    an exception (a ``send()`` that raises ``OSError``, or a ``receive()``
    that yields ``http.disconnect``), always releases the slot.
  - Through the real ``api.server.app``, with the counter saturated,
    every capped route answers 503 SERVER_OVERLOAD with ``Retry-After: 5``,
    and every uncapped route is unaffected.
  - The 503 body's ``request_id`` and the overload warning log line carry
    the caller's ``X-Request-ID`` (regression: ``scope`` is a plain dict,
    not an object with a ``.state`` attribute).
  - The 503 response has a real ``Content-Length`` and JSON content type
    (regression: a hand-assembled ASGI response left it out).

``api.concurrency.run_bounded``
  - A limit of 1 genuinely serializes concurrent callers; the default
    limit allows them to overlap.
  - POST /query's ``_run_query_bounded`` and the v2 turn helpers wait on
    the exact same semaphore object.
  - A real v2 streaming turn (?stream=1) completes even when the shared
    semaphore is reduced to one slot -- the SSE loop's queue reads must
    never compete with the pipeline worker for that one slot.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

import api.concurrency as concurrency
from api.concurrency import run_bounded
from api.middleware import ConcurrencyMiddleware, _is_capped_request
from api.models import QueryResponse


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _scope(path: str, method: str = "POST") -> dict:
    """A minimal, realistic ASGI HTTP scope for driving middleware directly.

    ``asgi.spec_version: "2.4"`` steers Starlette's ``StreamingResponse``
    onto its newer, ``receive``-free body-send path (``await
    self.stream_response(send)``) instead of the older path that also
    spawns a disconnect-listening task -- irrelevant to what these tests
    check, and it would otherwise need a ``receive`` that keeps yielding
    something sensible after the first call.
    """
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
    }


async def _empty_receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _stub_query_response() -> QueryResponse:
    return QueryResponse(question="q", sql="SELECT 1")


def _live_concurrency_middleware(app) -> ConcurrencyMiddleware:
    """Return the ``ConcurrencyMiddleware`` instance actually wired into *app*.

    Starlette builds and caches its middleware stack lazily, on the first
    request the app ever serves (``Starlette.__call__``), so this only
    finds anything once at least one request has already gone through --
    callers send a throwaway request (e.g. ``client.get("/health")``)
    first. Every middleware in the stack, this codebase's included,
    stores the app it wraps as ``self.app`` (a deliberate hygiene choice
    in ``ConcurrencyMiddleware``, matching Starlette's own
    ``BaseHTTPMiddleware``), so walking that attribute from the built
    stack's outermost layer finds whichever instance is genuinely
    receiving requests -- as opposed to constructing a fresh
    ``ConcurrencyMiddleware()`` and testing that, which would prove
    nothing about the app the rest of this suite, and a real deployment,
    actually serve.
    """
    node = app.middleware_stack
    for _ in range(50):
        if isinstance(node, ConcurrencyMiddleware):
            return node
        node = getattr(node, "app", None)
        if node is None:
            break
    raise RuntimeError(
        "ConcurrencyMiddleware not found in the app's built middleware "
        "stack -- send a request through the app first so Starlette "
        "builds and caches it."
    )


@contextlib.contextmanager
def _saturated(middleware: ConcurrencyMiddleware):
    """Fill every concurrency slot directly (no real concurrent requests
    needed), yield, then release exactly as many slots as were acquired."""
    acquired = 0
    while middleware._try_acquire():
        acquired += 1
    assert acquired > 0, "middleware was already at/above capacity before this context"
    try:
        yield
    finally:
        for _ in range(acquired):
            middleware._release()


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

    def test_get_query_is_not_capped(self):
        """Only POST /query is capped -- a GET (which doesn't exist as a
        route, but the check is on method+path alone) must not match."""
        assert not _is_capped_request("/query", "GET")

    def test_get_v2_turns_is_not_capped(self):
        """Only POST .../turns is capped, not GET (there is no such
        route, but this pins the method check)."""
        assert not _is_capped_request("/v2/sessions/abc123/turns", "GET")

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
# (a) Streaming responses hold the slot for the WHOLE body
# ---------------------------------------------------------------------------

class TestStreamingHoldsSlotForWholeBody:
    """A pure-ASGI streaming response must hold its concurrency slot until
    the full body has been sent, not just until headers are ready. This is
    the core regression BaseHTTPMiddleware could not fix: its call_next()
    returns as soon as the response's headers are ready (long before a
    StreamingResponse's body generator has produced a single chunk), so
    the old ConcurrencyMiddleware.dispatch() released the slot in its own
    finally block before any body streaming had even started.
    """

    def test_slot_held_until_generator_completes(self):
        async def _run() -> tuple[int, int]:
            resume = asyncio.Event()
            first_chunk_sent = asyncio.Event()

            async def gen():
                yield b"chunk-one"
                first_chunk_sent.set()
                await resume.wait()
                yield b"chunk-two"

            async def inner_app(scope, receive, send):
                response = StreamingResponse(gen())
                await response(scope, receive, send)

            middleware = ConcurrencyMiddleware(inner_app, max_concurrent=2)
            scope = _scope("/query/stream")

            async def send(message):
                pass

            call_task = asyncio.create_task(middleware(scope, _empty_receive, send))

            await asyncio.wait_for(first_chunk_sent.wait(), timeout=5)
            active_during_stream = middleware._active

            resume.set()
            await asyncio.wait_for(call_task, timeout=5)
            active_after_complete = middleware._active
            return active_during_stream, active_after_complete

        during, after = asyncio.run(_run())
        assert during == 1, (
            "the slot must still be held after only the first body chunk "
            "has streamed, not released as soon as headers were ready"
        )
        assert after == 0, "the slot must be released once the whole body (and the call) completes"


# ---------------------------------------------------------------------------
# (b) Exception / disconnect always releases the slot
# ---------------------------------------------------------------------------

class TestSlotReleasedOnFailureOrDisconnect:
    def test_exception_from_app_releases_slot(self):
        async def _run() -> int:
            async def failing_app(scope, receive, send):
                raise RuntimeError("boom")

            middleware = ConcurrencyMiddleware(failing_app, max_concurrent=1)

            async def send(message):
                pass

            with pytest.raises(RuntimeError):
                await middleware(_scope("/query"), _empty_receive, send)
            return middleware._active

        assert asyncio.run(_run()) == 0

    def test_send_raising_oserror_mid_stream_releases_slot(self):
        """A client disconnect surfaces to this process as ``send()``
        raising ``OSError`` (a broken pipe) partway through a response."""

        async def _run() -> int:
            async def app(scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"partial", "more_body": True})
                await send({"type": "http.response.body", "body": b"more", "more_body": False})

            middleware = ConcurrencyMiddleware(app, max_concurrent=1)
            calls = {"n": 0}

            async def flaky_send(message):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("Broken pipe")

            with pytest.raises(OSError):
                await middleware(_scope("/query"), _empty_receive, flaky_send)
            return middleware._active

        assert asyncio.run(_run()) == 0

    def test_receive_yielding_disconnect_releases_slot(self):
        """A disconnect that the wrapped app detects via ``receive()``
        yielding ``http.disconnect`` (rather than a failed ``send()``)
        must release the slot exactly the same way once it propagates as
        an exception out of the wrapped app."""

        async def _run() -> int:
            async def app(scope, receive, send):
                message = await receive()
                if message["type"] == "http.disconnect":
                    raise ConnectionResetError("client disconnected mid-request")
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok", "more_body": False})

            middleware = ConcurrencyMiddleware(app, max_concurrent=1)

            async def disconnect_receive():
                return {"type": "http.disconnect"}

            async def send(message):
                pass

            with pytest.raises(ConnectionResetError):
                await middleware(_scope("/query"), disconnect_receive, send)
            return middleware._active

        assert asyncio.run(_run()) == 0


# ---------------------------------------------------------------------------
# (1) BLOCKER regression: request_id must survive into the 503 body and log
# (2) The 503 must carry a real Content-Length / JSON content type
# (c) Through the real app: every capped route is refused when saturated,
#     every uncapped route is not
# ---------------------------------------------------------------------------

class TestSaturatedRealApp:
    """Drives the concurrency cap through the actual, fully-wired
    ``api.server.app`` -- the same middleware stack, in the same order, a
    real deployment serves -- rather than a synthetic app built just for
    this test module."""

    @pytest.fixture()
    def warmed_client(self, auth_settings):
        import api.server as server_module

        client = TestClient(
            server_module.app, raise_server_exceptions=False, headers=auth_settings,
        )
        client.get("/health")  # forces Starlette to build + cache the middleware stack
        return client, server_module.app

    def test_503_body_and_log_line_carry_the_caller_supplied_request_id(
        self, warmed_client, caplog,
    ):
        client, app = warmed_client
        middleware = _live_concurrency_middleware(app)
        with _saturated(middleware):
            with caplog.at_level("WARNING", logger="api.middleware"):
                resp = client.post(
                    "/query",
                    json={"question": "q"},
                    headers={"X-Request-ID": "caller-supplied-id-123"},
                )

        assert resp.status_code == 503
        assert resp.json()["error"]["request_id"] == "caller-supplied-id-123"
        assert any(
            "caller-supplied-id-123" in record.getMessage() for record in caplog.records
        ), "the overload warning log line must carry the caller's request id"

    def test_503_response_has_content_length_and_json_content_type(self, warmed_client):
        client, app = warmed_client
        middleware = _live_concurrency_middleware(app)
        with _saturated(middleware):
            resp = client.post("/query", json={"question": "q"})

        assert resp.status_code == 503
        assert resp.headers.get("content-type", "").startswith("application/json")
        assert "content-length" in resp.headers
        assert int(resp.headers["content-length"]) == len(resp.content)

    def test_every_capped_route_is_refused_when_saturated(self, warmed_client):
        client, app = warmed_client
        middleware = _live_concurrency_middleware(app)
        cases = [
            ("POST", "/query"),
            ("POST", "/query/stream"),
            ("POST", "/v2/sessions/sid/turns"),
            ("POST", "/v2/sessions/sid/turns?stream=1"),
            ("PATCH", "/v2/sessions/sid/turns/tid/assumptions"),
        ]
        with _saturated(middleware):
            for method, path in cases:
                body = {"question": "q"} if "turns" in path and "assumptions" not in path else (
                    {"assumptions": []} if "assumptions" in path else {"question": "q"}
                )
                resp = client.request(method, path, json=body)
                assert resp.status_code == 503, f"{method} {path} -> {resp.status_code}, expected 503"
                assert resp.json()["error"]["code"] == "SERVER_OVERLOAD"
                assert resp.headers.get("retry-after") == "5"

    def test_uncapped_routes_are_not_refused_when_saturated(self, warmed_client):
        client, app = warmed_client
        middleware = _live_concurrency_middleware(app)
        with _saturated(middleware):
            assert client.get("/health").status_code == 200
            assert client.get("/docs").status_code == 200
            assert client.post("/v2/sessions").status_code == 201
            assert client.get("/v2/sessions/nope").status_code == 404
            assert client.delete("/v2/sessions/nope").status_code == 204
            assert client.get("/v2/sessions/nope/turns/nope/feedback").status_code != 503


# ---------------------------------------------------------------------------
# (d) No deadlock between the pipeline worker and the SSE queue-draining loop
# ---------------------------------------------------------------------------

class TestNoDeadlockBetweenPipelineWorkerAndQueueReads:
    """With the shared semaphore reduced to a single slot, a real v2
    streaming turn (?stream=1) must still complete: the pipeline worker
    (``run_bounded(run)`` in ``_ask_turn_streaming_stages``) holds the one
    slot for the whole turn, while the SSE loop's queue reads
    (``asyncio.to_thread(events.get)``, deliberately NOT ``run_bounded``)
    must never need a slot of their own -- routing them through
    ``run_bounded`` too would have them compete with their own producer
    for the one slot it already holds.

    Run on a daemon thread and joined with a timeout, so a real deadlock
    fails this test in bounded time instead of hanging the whole suite.
    """

    def test_streaming_turn_completes_with_semaphore_of_one(self, monkeypatch, auth_settings):
        import pandas as pd

        import api.server as server_module
        import api.v2_routes as v2_routes
        from llm.providers import MockBackend
        from llm.router import LLMRouter
        from session.engine import TurnEngine

        monkeypatch.setattr(concurrency, "_semaphore", asyncio.Semaphore(1))

        server_module._system_prompt = "stub system prompt"
        v2_routes._system_prompt = "stub system prompt"
        v2_routes._reset_for_testing()

        engine = TurnEngine(
            router=LLMRouter(
                default_chain=[MockBackend(response="SELECT TOP 10 c.Name FROM Customer c")]
            ),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A", "B"]}),
        )
        v2_routes._turn_engine = engine

        client = TestClient(
            server_module.app, raise_server_exceptions=False, headers=auth_settings,
        )
        sid = client.post("/v2/sessions").json()["session_id"]

        result: dict = {}

        def worker() -> None:
            try:
                with client.stream(
                    "POST", f"/v2/sessions/{sid}/turns?stream=1",
                    json={"question": "لیست مشتریان"},
                ) as resp:
                    result["status"] = resp.status_code
                    result["body"] = "".join(resp.iter_text())
            except Exception as exc:  # noqa: BLE001 - surfaced via the assertions below
                result["exception"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=15)

        v2_routes._reset_for_testing()

        assert not thread.is_alive(), (
            "the streaming turn did not complete within 15s -- almost "
            "certainly a deadlock between the pipeline worker and the "
            "queue-draining loop both waiting on the same single-slot "
            "semaphore"
        )
        assert "exception" not in result, result.get("exception")
        assert result.get("status") == 200
        assert "event: done" in result.get("body", "")


# ---------------------------------------------------------------------------
# (e) run_bounded's shared semaphore
# ---------------------------------------------------------------------------

class TestRunBoundedSemaphoreBehavior:
    def test_limit_of_one_serializes_three_concurrent_calls(self, monkeypatch):
        monkeypatch.setattr(concurrency, "_semaphore", asyncio.Semaphore(1))
        lock = threading.Lock()
        state = {"current": 0, "max_seen": 0}

        def work():
            with lock:
                state["current"] += 1
                state["max_seen"] = max(state["max_seen"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1

        async def _test():
            await asyncio.gather(*(run_bounded(work) for _ in range(3)))

        asyncio.run(_test())
        assert state["max_seen"] == 1, "a limit of 1 must never let two calls run at once"

    def test_default_limit_allows_three_concurrent_calls_to_overlap(self):
        lock = threading.Lock()
        state = {"current": 0, "max_seen": 0}

        def work():
            with lock:
                state["current"] += 1
                state["max_seen"] = max(state["max_seen"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1

        async def _test():
            await asyncio.gather(*(run_bounded(work) for _ in range(3)))

        asyncio.run(_test())
        assert state["max_seen"] > 1, (
            "the default (16) limit must allow more than one call to run "
            "at once -- if this is 1, the default bound is far too tight "
            "or something reset the shared semaphore to a smaller one"
        )

    def test_run_query_bounded_and_v2_helper_share_the_same_semaphore(
        self, monkeypatch, auth_settings,
    ):
        """Prove POST /query's ``_run_query_bounded`` and the v2 turn
        helpers wait on the exact same semaphore object: hold its only
        slot from outside, then confirm a call into EACH path blocks
        until that slot is released, and both proceed once it is."""
        import api.runner as runner_module
        import api.server as server_module
        from unittest.mock import patch as mock_patch

        monkeypatch.setattr(concurrency, "_semaphore", asyncio.Semaphore(1))
        server_module._system_prompt = "stub system prompt"

        async def _test():
            with mock_patch.object(runner_module, "run_query", return_value=_stub_query_response()):
                await concurrency._semaphore.acquire()  # hold the only slot from outside
                try:
                    query_task = asyncio.create_task(
                        server_module._run_query_bounded(
                            question="q", system_prompt="sp", mode="full",
                            interpret=False, request_id="r1", principal=None,
                        )
                    )
                    v2_task = asyncio.create_task(concurrency.run_bounded(lambda: "v2-side"))
                    await asyncio.sleep(0.1)
                    assert not query_task.done(), (
                        "POST /query's _run_query_bounded did not wait for "
                        "the already-held shared slot -- it is not sharing "
                        "the semaphore the v2 helpers use"
                    )
                    assert not v2_task.done(), (
                        "the v2 helper path did not wait for the "
                        "already-held shared slot"
                    )
                finally:
                    concurrency._semaphore.release()

                query_result, v2_result = await asyncio.gather(query_task, v2_task)
                return query_result, v2_result

        query_result, v2_result = asyncio.run(_test())
        assert query_result.question == "q"
        assert v2_result == "v2-side"
