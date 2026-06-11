"""Domain exception hierarchy and FastAPI exception handlers.

Every failure in the pipeline raises one of the typed exceptions below.
The handlers registered in ``register_handlers(app)`` translate them to
consistent JSON error responses with the correct HTTP status code.

Exception map
-------------

  Layer            Exception                         HTTP
  ───────────────  ────────────────────────────────  ────
  Input validation  QuestionTooShortError             400
                    QuestionTooLongError              400
                    InvalidModeError                  400
  Security         ForbiddenSQLError                 400
                    InjectionAttemptError             400
  Scope            OutOfScopeError                   422
  LLM              ModelUnavailableError             503
                    ModelTimeoutError                 504
                    EmptySQLResponseError             502
                    InvalidSQLResponseError           502
  Database         DatabaseConnectionError           503
                    QueryTimeoutError                 504
                    QueryExecutionError               502
  Overload         ServerOverloadError               503  (rate-limit / semaphore)
  Catch-all        fastapi.RequestValidationError    422  (Pydantic)
                    Exception                         500
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
    """Ollama / LLM backend is not reachable."""
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
        return _error_response(
            request,
            exc.http_status,
            exc.error_code,
            exc.message,
            request_id,
        )

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
    """Return existing X-Request-ID header or generate a new one."""
    return request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
