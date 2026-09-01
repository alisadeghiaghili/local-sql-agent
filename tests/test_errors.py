# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""TDD tests for api/errors.py — exception hierarchy and HTTP mapping.

Philosophy
----------
Every NLQError subclass has three contracts:
  1. It IS-A NLQError (Liskov substitution).
  2. It carries the correct http_status and error_code.
  3. The registered FastAPI handler turns it into the expected JSON shape.

These tests use a minimal FastAPI app with a single /boom route that
raises a requested exception class by name.  No real DB or LLM needed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.errors import (
    NLQError,
    QuestionTooShortError,
    QuestionTooLongError,
    ForbiddenSQLError,
    InjectionAttemptError,
    OutOfScopeError,
    EmptySQLResponseError,
    InvalidSQLResponseError,
    QueryExecutionError,
    ModelUnavailableError,
    DatabaseConnectionError,
    ServerOverloadError,
    ModelTimeoutError,
    QueryTimeoutError,
    register_handlers,
)


# ---------------------------------------------------------------------------
# Fixture: minimal app with one configurable-error route
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[NLQError]] = {
    cls.__name__: cls
    for cls in [
        QuestionTooShortError,
        QuestionTooLongError,
        ForbiddenSQLError,
        InjectionAttemptError,
        OutOfScopeError,
        EmptySQLResponseError,
        InvalidSQLResponseError,
        QueryExecutionError,
        ModelUnavailableError,
        DatabaseConnectionError,
        ServerOverloadError,
        ModelTimeoutError,
        QueryTimeoutError,
    ]
}


@pytest.fixture(scope="module")
def error_client() -> TestClient:
    app = FastAPI()
    register_handlers(app)

    @app.get("/boom/{exc_name}")
    def boom(exc_name: str):
        exc_cls = _REGISTRY[exc_name]
        raise exc_cls(f"test message for {exc_name}")

    @app.get("/unhandled")
    def unhandled():
        raise RuntimeError("surprise")

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. Inheritance
# ---------------------------------------------------------------------------

class TestInheritance:
    @pytest.mark.parametrize("exc_cls", list(_REGISTRY.values()))
    def test_is_nlq_error(self, exc_cls):
        assert issubclass(exc_cls, NLQError)

    def test_base_carries_message(self):
        exc = QuestionTooShortError("too short")
        assert str(exc) == "too short"
        assert exc.message == "too short"

    def test_base_carries_optional_detail(self):
        exc = QueryExecutionError("public msg", detail="internal trace")
        assert exc.detail == "internal trace"
        assert exc.message == "public msg"


# ---------------------------------------------------------------------------
# 2. HTTP status code mapping
# ---------------------------------------------------------------------------

class TestStatusCodes:
    @pytest.mark.parametrize("exc_cls,expected_status", [
        (QuestionTooShortError,    400),
        (QuestionTooLongError,     400),
        (ForbiddenSQLError,        400),
        (InjectionAttemptError,    400),
        (OutOfScopeError,          422),
        (EmptySQLResponseError,    502),
        (InvalidSQLResponseError,  502),
        (QueryExecutionError,      502),
        (ModelUnavailableError,    503),
        (DatabaseConnectionError,  503),
        (ServerOverloadError,      503),
        (ModelTimeoutError,        504),
        (QueryTimeoutError,        504),
    ])
    def test_http_status(self, exc_cls, expected_status, error_client):
        resp = error_client.get(f"/boom/{exc_cls.__name__}")
        assert resp.status_code == expected_status

    @pytest.mark.parametrize("exc_cls", list(_REGISTRY.values()))
    def test_has_correct_error_code(self, exc_cls, error_client):
        resp = error_client.get(f"/boom/{exc_cls.__name__}")
        body = resp.json()
        assert body["error"]["code"] == exc_cls.error_code


# ---------------------------------------------------------------------------
# 3. Response envelope shape
# ---------------------------------------------------------------------------

class TestResponseShape:
    def test_envelope_has_error_key(self, error_client):
        resp = error_client.get("/boom/OutOfScopeError")
        assert "error" in resp.json()

    def test_error_has_required_fields(self, error_client):
        body = error_client.get("/boom/ModelUnavailableError").json()["error"]
        assert {"code", "message", "request_id", "path"} <= body.keys()

    def test_message_is_not_empty(self, error_client):
        body = error_client.get("/boom/ForbiddenSQLError").json()["error"]
        assert body["message"]

    def test_path_is_correct(self, error_client):
        body = error_client.get("/boom/OutOfScopeError").json()["error"]
        assert "/boom/" in body["path"]

    def test_request_id_is_present(self, error_client):
        body = error_client.get("/boom/ModelTimeoutError").json()["error"]
        assert body["request_id"]

    def test_custom_request_id_echoed(self, error_client):
        resp = error_client.get(
            "/boom/QueryTimeoutError",
            headers={"x-request-id": "myid123"},
        )
        assert resp.json()["error"]["request_id"] == "myid123"

    def test_unhandled_exception_returns_500(self, error_client):
        resp = error_client.get("/unhandled")
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "INTERNAL_ERROR"

    def test_unhandled_exception_does_not_leak_traceback(self, error_client):
        body = error_client.get("/unhandled").json()["error"]["message"]
        assert "Traceback" not in body
        assert "RuntimeError" not in body


# ---------------------------------------------------------------------------
# 4. Pydantic validation error → 422
# ---------------------------------------------------------------------------

class TestRequestIdCorrelation:
    """The X-Request-ID response header (stamped by RequestIDMiddleware)
    and the error body's request_id field must be the SAME id for the
    SAME request, so an operator can correlate a client-visible error
    against server logs keyed by X-Request-ID."""

    @pytest.fixture()
    def client_with_request_id_middleware(self) -> TestClient:
        from api.middleware import RequestIDMiddleware

        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)
        register_handlers(app)

        @app.get("/boom")
        def boom():
            raise ModelUnavailableError("down")

        return TestClient(app, raise_server_exceptions=False)

    def test_error_body_request_id_matches_response_header(
        self, client_with_request_id_middleware
    ):
        """No client-supplied X-Request-ID: RequestIDMiddleware mints one
        and stores it on request.state *before* the handler runs. The
        error handler must reuse that same id, not mint a second one."""
        resp = client_with_request_id_middleware.get("/boom")
        assert resp.json()["error"]["request_id"] == resp.headers["x-request-id"]

    def test_error_body_request_id_matches_header_when_client_supplies_one(
        self, client_with_request_id_middleware
    ):
        resp = client_with_request_id_middleware.get(
            "/boom", headers={"x-request-id": "client-supplied-id"}
        )
        assert resp.json()["error"]["request_id"] == "client-supplied-id"
        assert resp.headers["x-request-id"] == "client-supplied-id"


class TestValidationError:
    def test_pydantic_validation_returns_422(self, error_client):
        app2 = FastAPI()
        register_handlers(app2)

        from pydantic import BaseModel

        class Body(BaseModel):
            value: int

        @app2.post("/typed")
        def typed(body: Body):
            return {"ok": True}

        client2 = TestClient(app2, raise_server_exceptions=False)
        resp = client2.post("/typed", json={"value": "not-an-int"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
