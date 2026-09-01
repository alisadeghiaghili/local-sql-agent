"""Domain exception hierarchy and FastAPI exception handlers.

The handlers registered in ``register_handlers(app)`` translate these
typed exceptions into consistent JSON error responses with the correct
HTTP status code.

Exception map
-------------

The ``raised`` column is deliberate: this table used to read as though
every row were wired up, and five of them are not raised anywhere in
production code. They are reserved slots in the hierarchy, exercised by
``tests/test_errors.py`` and imported by ``api/runner.py``, kept so the
taxonomy stays complete — but a reader should not infer from this table
that the input-validation errors below are live. Pydantic currently
rejects short, long and out-of-range input before the pipeline sees it,
surfacing as ``RequestValidationError`` (422), not as a 400.

  Layer             Exception                        HTTP  raised
  ───────────────   ──────────────────────────────   ────  ──────
  Auth              UnauthenticatedError              401  yes
  Input validation  QuestionTooShortError             400  no
                    QuestionTooLongError              400  no
                    InvalidModeError                  400  no
  Security          ForbiddenSQLError                 400  yes
                    InjectionAttemptError             400  no
  Scope             OutOfScopeError                   422  yes
  LLM               ModelUnavailableError             503  yes
                    ModelTimeoutError                 504  yes
                    EmptySQLResponseError             502  yes
                    InvalidSQLResponseError           502  yes
  Database          DatabaseConnectionError           503  yes
                    QueryTimeoutError                 504  yes
                    QueryExecutionError               502  yes
  Overload          ServerOverloadError               503  no
  Catch-all         fastapi.RequestValidationError    422  yes  (Pydantic)
                    Exception                         500  yes

``ServerOverloadError`` is a special case worth recording. It cannot
simply be raised from the middleware: exceptions raised inside a
``BaseHTTPMiddleware.dispatch`` propagate above the router, so FastAPI's
``@app.exception_handler`` handlers never see them and the client gets a
500 instead of a 503. ``RateLimitMiddleware`` and
``ConcurrencyMiddleware`` therefore construct their JSON envelopes
directly. That duplicates the shape built by ``_error_response`` here,
which is a real drift risk — the two should share one builder.
"""

from __future__ import annotations

import logging
import traceback
import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class NLQError(Exception):
    """Root for all domain exceptions.  Always carries a user-facing message."""

    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail  # extra context for logs (never sent to client)


# ---------------------------------------------------------------------------
# 401 Unauthorized
# ---------------------------------------------------------------------------

class UnauthenticatedError(NLQError):
    """Missing or invalid credentials on a route that requires them.

    ``register_handlers``'s ``nlq_error_handler`` special-cases this
    exception to also attach a ``WWW-Authenticate: Bearer`` header — the
    one piece of the 401 response ``_error_response``'s generic envelope
    builder doesn't carry, since no other error in this hierarchy needs
    it.
    """
    http_status = status.HTTP_401_UNAUTHORIZED
    error_code = "UNAUTHENTICATED"


# ---------------------------------------------------------------------------
# 400 Bad Request
# ---------------------------------------------------------------------------

class QuestionTooShortError(NLQError):
    http_status = status.HTTP_400_BAD_REQUEST
    error_code = "QUESTION_TOO_SHORT"


class QuestionTooLongError(NLQError):
    http_status = status.HTTP_400_BAD_REQUEST
    error_code = "QUESTION_TOO_LONG"


class InvalidModeError(NLQError):
    http_status = status.HTTP_400_BAD_REQUEST
    error_code = "INVALID_MODE"


class ForbiddenSQLError(NLQError):
    """Generated SQL contains a forbidden keyword (DELETE, DROP, …)."""
    http_status = status.HTTP_400_BAD_REQUEST
    error_code = "FORBIDDEN_SQL"


class InjectionAttemptError(NLQError):
    """Question looks like a prompt-injection or SQL-injection attempt."""
    http_status = status.HTTP_400_BAD_REQUEST
    error_code = "INJECTION_ATTEMPT"


# ---------------------------------------------------------------------------
# 422 Unprocessable
# ---------------------------------------------------------------------------

class OutOfScopeError(NLQError):
    """Model signalled the question is outside the Auction domain."""
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_code = "OUT_OF_SCOPE"


# ---------------------------------------------------------------------------
# 502 Bad Gateway  (upstream returned garbage)
# ---------------------------------------------------------------------------

class EmptySQLResponseError(NLQError):
    """LLM returned an empty or whitespace-only response."""
    http_status = status.HTTP_502_BAD_GATEWAY
    error_code = "EMPTY_SQL_RESPONSE"


class InvalidSQLResponseError(NLQError):
    """LLM response could not be parsed into valid SQL."""
    http_status = status.HTTP_502_BAD_GATEWAY
    error_code = "INVALID_SQL_RESPONSE"


class QueryExecutionError(NLQError):
    """SQL executed but the database returned an error."""
    http_status = status.HTTP_502_BAD_GATEWAY
    error_code = "QUERY_EXECUTION_ERROR"


# ---------------------------------------------------------------------------
# 503 Service Unavailable  (dependency down)
# ---------------------------------------------------------------------------

class ModelUnavailableError(NLQError):
    """The configured LLM endpoint is not reachable."""
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "MODEL_UNAVAILABLE"


class DatabaseConnectionError(NLQError):
    """Cannot connect to the SQL Server database."""
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "DATABASE_UNAVAILABLE"


class ServerOverloadError(NLQError):
    """Too many concurrent requests — server is at capacity."""
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "SERVER_OVERLOAD"


# ---------------------------------------------------------------------------
# 504 Gateway Timeout
# ---------------------------------------------------------------------------

class ModelTimeoutError(NLQError):
    """LLM inference took longer than the configured timeout."""
    http_status = status.HTTP_504_GATEWAY_TIMEOUT
    error_code = "MODEL_TIMEOUT"


class QueryTimeoutError(NLQError):
    """SQL query exceeded the LOCK_TIMEOUT / query_timeout_seconds limit."""
    http_status = status.HTTP_504_GATEWAY_TIMEOUT
    error_code = "QUERY_TIMEOUT"


# ---------------------------------------------------------------------------
# JSON response builder
# ---------------------------------------------------------------------------

def _error_response(
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    request_id: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": error_code,
                "message": message,
                "request_id": request_id,
                "path": str(request.url.path),
            }
        },
    )


# ---------------------------------------------------------------------------
# FastAPI handler registration
# ---------------------------------------------------------------------------

def register_handlers(app: FastAPI) -> None:
    """Attach all exception handlers to *app*."""

    @app.exception_handler(NLQError)
    async def nlq_error_handler(request: Request, exc: NLQError) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.warning(
            "[%s] %s %s → %s(%s): %s",
            request_id,
            request.method,
            request.url.path,
            type(exc).__name__,
            exc.http_status,
            exc.message,
        )
        if exc.detail:
            logger.debug("[%s] detail: %s", request_id, exc.detail)
        response = _error_response(
            request,
            exc.http_status,
            exc.error_code,
            exc.message,
            request_id,
        )
        if isinstance(exc, UnauthenticatedError):
            response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        errors = exc.errors()
        first = errors[0] if errors else {}
        field = ".".join(str(loc) for loc in first.get("loc", []))
        msg = first.get("msg", "Invalid request")
        human = f"{field}: {msg}" if field else msg
        logger.info("[%s] Validation error: %s", request_id, errors)
        return _error_response(
            request,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "VALIDATION_ERROR",
            human,
            request_id,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.error(
            "[%s] Unhandled %s: %s\n%s",
            request_id,
            type(exc).__name__,
            exc,
            traceback.format_exc(),
        )
        return _error_response(
            request,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "INTERNAL_ERROR",
            "An unexpected error occurred. Please try again later.",
            request_id,
        )


def _get_request_id(request: Request) -> str:
    """Return this request's id, preferring the id RequestIDMiddleware already stamped.

    ``RequestIDMiddleware`` stores the id on ``request.state.request_id``
    (echoing the client's ``X-Request-ID`` header if it supplied one,
    otherwise minting a fresh one) *before* any route handler or
    exception path runs, and later copies that same value onto the
    ``X-Request-ID`` response header. Reading ``request.state`` first here
    — instead of re-reading the header and minting an independent id when
    absent — guarantees the id in an error body always matches the
    response's ``X-Request-ID`` header, which is what operators actually
    correlate against server logs.

    The header and fresh-uuid fallbacks below only matter when something
    bypasses ``RequestIDMiddleware`` entirely, e.g. a unit test that wires
    up ``register_handlers()`` without the middleware.
    """
    state_request_id = getattr(request.state, "request_id", None)
    if state_request_id:
        return state_request_id
    return request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
