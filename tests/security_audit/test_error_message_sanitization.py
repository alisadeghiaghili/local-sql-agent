# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Finding 11 — internal infrastructure addresses reach the client.

Rated Low from reading the code, then raised to Medium after seeing what
comes back. A turn whose model call fails returns this to *any*
authenticated caller, including the lowest-privileged analyst::

    {"error": {"code": "MODEL_UNAVAILABLE",
               "message": "Cannot reach the LLM backend: OpenAI-compatible
                           endpoint 'http://127.0.0.1:9/v1' unreachable after
                           3 retries: HTTPConnectionPool(host='127.0.0.1',
                           port=9): Max retries exceeded with url:
                           /v1/chat/completions ..."}}

The internal model endpoint's scheme, host and port, the retry policy and
the transport library -- handed to a user whose job is asking questions
about auction data. On a real deployment that host is not loopback; it is
an address on the internal network.

The fix needs no new architecture, because ``api/errors.py`` already drew
exactly this line::

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        self.message = message
        self.detail = detail  # extra context for logs (never sent to client)

The mechanism was there and unused. ``api/runner.py`` raises
``ModelUnavailableError(str(exc))`` -- putting the whole transport
traceback into the field that *is* sent to the client and leaving the
field built for it empty. The correction is to swap which side of that
constructor the raw text goes on.

The distinction being drawn: "the language model is unavailable, try
again" tells the analyst everything they can act on. The host and port
tell them nothing they can act on, and tell someone probing the network
quite a lot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

#: A realistic transport failure, shaped like the one the audit captured.
_RAW_TRANSPORT_ERROR = (
    "OpenAI-compatible endpoint 'http://10.20.30.40:8000/v1' unreachable "
    "after 3 retries: HTTPConnectionPool(host='10.20.30.40', port=8000): "
    "Max retries exceeded with url: /v1/chat/completions "
    "(Caused by NewConnectionError(...))"
)

#: Fragments that must never appear in anything handed to a client.
_LEAKY = [
    "http://", "https://", "HTTPConnectionPool", "Max retries exceeded",
    "/v1/chat/completions", "NewConnectionError", "10.20.30.40",
]


def _assert_clean(text: str, context: str) -> None:
    leaked = [f for f in _LEAKY if f.lower() in (text or "").lower()]
    assert not leaked, (
        f"{context} exposes {leaked!r} to the caller.\n  text: {text!r}\n"
        "Put the raw transport error in `detail` (logs) and a summary in "
        "`message` (client) -- the constructor already separates them"
    )


class TestTheClientFacingMessageIsSanitised:
    """`message` is the field that reaches the client. It must be a summary."""

    def test_a_transport_failure_summarises_rather_than_quotes(self):
        from api.runner import _safe_generate_sql_only  # noqa: F401  (import guard)
        from api.errors import ModelUnavailableError

        err = ModelUnavailableError(
            "The language model is currently unreachable. Please try again.",
            detail=_RAW_TRANSPORT_ERROR,
        )
        _assert_clean(err.message, "ModelUnavailableError.message")

    def test_the_runner_does_not_put_raw_exception_text_in_message(self):
        """The defect in one line. ``raise ModelUnavailableError(str(exc))``
        makes the raw text the client-facing message; the fix is
        ``ModelUnavailableError(<summary>, detail=str(exc))``."""
        src = (_REPO_ROOT / "api" / "runner.py").read_text(encoding="utf-8")
        assert "ModelUnavailableError(str(exc))" not in src.replace(" ", ""), (
            "api/runner.py still raises ModelUnavailableError(str(exc)), which "
            "sends the whole transport error to the analyst"
        )


class TestTheOperatorStillGetsTheDetail:
    """Sanitising must not mean deleting. Whoever diagnoses the outage needs
    the address -- from the log, not from the user's screen."""

    def test_the_raw_text_survives_in_detail(self):
        from api.errors import ModelUnavailableError

        err = ModelUnavailableError("summary", detail=_RAW_TRANSPORT_ERROR)
        assert err.detail == _RAW_TRANSPORT_ERROR, (
            "the internal detail was discarded rather than moved"
        )

    def test_detail_is_documented_as_never_leaving_the_process(self):
        src = (_REPO_ROOT / "api" / "errors.py").read_text(encoding="utf-8")
        assert "never sent to client" in src, (
            "the contract that `detail` stays server-side is no longer stated "
            "where the field is defined -- which is how it drifts back"
        )


class TestTheSerialisedEnvelopeIsClean:
    """What actually leaves the process, which is where the leak was seen."""

    @staticmethod
    def _envelope(message: str) -> str:
        """Build the real envelope through the real helper. Its signature
        takes the Request (for the path), so a minimal stub stands in."""
        from types import SimpleNamespace

        from api.errors import ModelUnavailableError, _error_response

        request = SimpleNamespace(url=SimpleNamespace(path="/v2/sessions/x/turns"))
        resp = _error_response(
            request=request,  # type: ignore[arg-type]
            status_code=502,
            error_code=ModelUnavailableError.error_code,
            message=message,
            request_id="r_abc123",
        )
        return resp.body.decode("utf-8")

    def test_the_error_body_carries_no_internal_address(self):
        from api.errors import ModelUnavailableError

        err = ModelUnavailableError(
            "The language model is currently unreachable. Please try again.",
            detail=_RAW_TRANSPORT_ERROR,
        )
        _assert_clean(self._envelope(err.message), "the serialised error envelope")

    def test_the_request_id_is_still_present_to_correlate_with(self):
        """Removing the address only works if the user can still quote
        something that leads an operator to the full record."""
        assert "r_abc123" in self._envelope("unavailable"), (
            "the client-facing error carries no request_id, so a user "
            "reporting a failure has nothing an operator can look up"
        )


class TestTheStreamingPathIsCoveredToo:
    """The audit saw the leak on the SSE turn endpoint, which builds its own
    error event instead of going through the HTTP error handler -- a
    separate path, and the one that regressed."""

    def test_the_sse_error_event_does_not_pass_raw_exception_text(self):
        src = (_REPO_ROOT / "api" / "v2_routes.py").read_text(encoding="utf-8")
        normalised = src.replace("'", '"').replace(" ", "")
        assert '"message":str(exc)' not in normalised, (
            "api/v2_routes.py still yields the raw exception as the SSE error "
            "message. That is the line the leaked HTTPConnectionPool string "
            "came out of"
        )
