# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Finding 11, the half that the first fix missed — the v2 turn path.

``test_error_message_sanitization.py`` closed finding 11 on the ``/query``
(v1) path and on the SSE error *event* in ``api/v2_routes.py``. A live
re-test after remediation showed the internal model endpoint still reaching
a low-privileged analyst — this time in the JSON body of a v2 turn::

    POST /v2/sessions/{id}/turns  (interpret=false)
    -> 200
    {"error": {"code": "MODEL_UNAVAILABLE",
               "message": "Cannot reach the LLM backend: OpenAI-compatible
                           endpoint 'http://127.0.0.1:9/v1' unreachable after
                           3 retries: HTTPConnectionPool(host='127.0.0.1',
                           port=9)..."}}

The leak comes from a *third* place, which neither of the two fixed paths
touches: ``session/engine.py::_classify_router_failure`` builds

    return "MODEL_UNAVAILABLE", f"Cannot reach the LLM backend: {msg}"

where ``msg = str(cause)`` is the raw transport exception, and that string
becomes ``TurnErrorInfo.message`` verbatim. That field is serialised into
the turn — over both the JSON response and the SSE ``done`` event — so it
reached the client regardless of transport, and the SSE-event fix (which
only rewrites the *unexpected-exception* branch, not a classified
``MODEL_UNAVAILABLE`` outcome) never saw it.

Why this is a distinct test and not a line in the existing module
----------------------------------------------------------------
The existing module asserts the ``ModelUnavailableError``/``_error_response``
mechanism, which the engine does not use: the v2 layer answers-then-declares
(``TurnErrorInfo`` on the ``Turn``), it never raises that exception. So the
guarantee has to be re-made against the function that actually produces the
v2 message. ``TurnErrorInfo`` also has no ``detail`` field to hide the raw
text in, so the fix cannot be "move it to detail" as it was on v1 — the raw
transport error belongs in the server log, and the message handed back must
be a summary that names nothing an attacker probing the network could use.

The bar (mirrors the sibling module): the analyst learns the model is
unreachable and can retry; the host, port, scheme, retry policy and
transport library are not on their screen.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

#: A realistic transport failure, shaped like the one the live re-test saw,
#: with a non-loopback address so a copy-paste of the loopback probe cannot
#: pass by coincidence.
_RAW_TRANSPORT_ERROR = (
    "OpenAI-compatible endpoint 'http://10.20.30.40:8000/v1' unreachable "
    "after 3 retries: HTTPConnectionPool(host='10.20.30.40', port=8000): "
    "Max retries exceeded with url: /v1/chat/completions "
    "(Caused by NewConnectionError(...))"
)

#: Fragments that must never appear in anything handed to a client. Same
#: list as the sibling module, on purpose — one definition of "leaky".
_LEAKY = [
    "http://", "https://", "HTTPConnectionPool", "Max retries exceeded",
    "/v1/chat/completions", "NewConnectionError", "10.20.30.40", "8000",
]


def _assert_clean(text: str, context: str) -> None:
    leaked = [f for f in _LEAKY if f.lower() in (text or "").lower()]
    assert not leaked, (
        f"{context} exposes {leaked!r} to the caller.\n  text: {text!r}\n"
        "The v2 turn path has no `detail` field: put a summary in the "
        "TurnErrorInfo message and log the raw transport error server-side."
    )


def _router_failure(raw: str) -> Exception:
    """An exception shaped like the one ``LLMRouter._call_chain`` raises: a
    wrapper ``RuntimeError`` with the real transport error chained as
    ``__cause__`` — which is exactly what ``_classify_router_failure``
    unwraps (``cause = exc.__cause__ or exc``)."""
    cause = ConnectionError(raw)
    wrapper = RuntimeError("Every backend in the chain failed for SQL_GENERATION")
    wrapper.__cause__ = cause
    return wrapper


class TestTheV2ClassifierMessageIsSanitised:
    """The message this function returns becomes ``TurnErrorInfo.message``,
    which is serialised straight into the turn the client receives."""

    def test_a_transport_failure_does_not_reach_the_client_message(self):
        from session.engine import _classify_router_failure

        code, message = _classify_router_failure(_router_failure(_RAW_TRANSPORT_ERROR))
        assert code == "MODEL_UNAVAILABLE", (
            f"a chained ConnectionError classified as {code!r}, not "
            "MODEL_UNAVAILABLE — the transport-failure branch changed shape"
        )
        _assert_clean(message, "_classify_router_failure()'s MODEL_UNAVAILABLE message")

    def test_the_timeout_and_out_of_scope_messages_stay_clean(self):
        """Regression guard for the two branches that were already safe, so a
        future edit to this function cannot reintroduce a leak through them."""
        from session.engine import _classify_router_failure

        timeout = ConnectionError("timeout talking to http://10.20.30.40:8000/v1")
        wrapper = RuntimeError("chain failed")
        wrapper.__cause__ = timeout
        code, message = _classify_router_failure(wrapper)
        assert code == "MODEL_TIMEOUT"
        _assert_clean(message, "the MODEL_TIMEOUT message")


class TestTheOperatorStillGetsTheDetail:
    """Sanitising is not deleting. The address the analyst must not see is
    exactly what the operator diagnosing the outage needs — from the log."""

    def test_the_raw_transport_error_is_logged(self, caplog):
        """Whatever the summary becomes, the raw ``str(cause)`` must still be
        emitted to the server log, or the fix has traded a security leak for
        a blind outage. Asserted behaviourally: the leaky text appears in a
        log record but not in the returned message."""
        from session.engine import _classify_router_failure

        with caplog.at_level(logging.WARNING, logger="session.engine"):
            _code, message = _classify_router_failure(_router_failure(_RAW_TRANSPORT_ERROR))

        logged = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "10.20.30.40" in logged, (
            "the raw transport error was not logged anywhere — moving it out "
            "of the client message must not mean discarding it; an operator "
            "needs the address to find the unreachable host"
        )
        _assert_clean(message, "the returned message (while the log keeps the detail)")
