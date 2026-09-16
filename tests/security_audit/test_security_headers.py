# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Finding 7 — the API sends no security headers at all.

Observed live against a running server during the audit. A normal
authenticated ``200`` carried exactly two headers of interest::

    server: uvicorn
    x-request-id: f2c85e95b4ec

and none of ``X-Content-Type-Options``, ``X-Frame-Options``,
``Content-Security-Policy``, ``Referrer-Policy`` or
``Permissions-Policy``. A grep of the whole repository for any of those
names returned zero hits, so this was absence rather than misconfiguration.

Two honest caveats, both of which shape what is asserted here.

*This is modest for a JSON API.* ``nosniff`` and ``X-Frame-Options`` earn
their keep mostly on the HTML the project also ships. The reason to add
them at the API anyway is that responses from this app do get rendered --
error bodies, the hand-registered ``/docs``, anything a future endpoint
returns as HTML -- and a blanket header costs nothing.

*HSTS does not belong here.* ``Strict-Transport-Security`` is a statement
about the TLS terminator, and this app does not terminate TLS. Emitting it
from the application would be a claim it cannot back, and would be wrong
the moment someone runs it on plain HTTP behind a proxy that does not.
So it is deliberately **not** required below; see
``TestHstsIsLeftToTheTlsTerminator``.

The pages under ``web/`` are covered separately, by a ``<meta>`` policy in
the documents themselves -- see ``test_admin_panel_xss.py``. They must be:
a header this app sets cannot reach a page a different origin serves.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


@pytest.fixture()
def client() -> TestClient:
    """No lifespan: headers are a middleware concern, and startup would
    demand a real database and LLM endpoint to say anything about them.
    Mirrors ``tests/test_cors_default.py``'s fixture for the same reason."""
    import api.server as server_module

    return TestClient(server_module.app, raise_server_exceptions=False)


def _any_response(client: TestClient):
    """``/health`` needs no credentials, so this works without wiring keys.
    The headers under test must not depend on the route or the status."""
    return client.get("/health")


class TestTheBaselineHeadersArePresent:
    @pytest.mark.parametrize(
        "header, expected",
        [
            ("x-content-type-options", "nosniff"),
            ("x-frame-options", "DENY"),
            ("referrer-policy", "no-referrer"),
        ],
    )
    def test_header_is_sent(self, client, header, expected):
        resp = _any_response(client)
        actual = resp.headers.get(header)
        assert actual is not None, (
            f"{header} is absent. The audit found every one of these missing "
            "from a live 200 response"
        )
        assert actual.lower() == expected.lower(), (
            f"{header} is {actual!r}, expected {expected!r}"
        )

    def test_headers_are_present_on_an_error_response_too(self, client):
        """A 401 body is still a body a browser may render, and the whole
        point of a blanket middleware is that it does not have to be
        remembered per route."""
        resp = client.get("/admin/summary")
        assert resp.status_code in (401, 403), (
            f"expected an auth rejection from an unauthenticated call, got "
            f"{resp.status_code}"
        )
        assert resp.headers.get("x-content-type-options") == "nosniff", (
            "security headers are applied on the success path only"
        )


class TestTheServerDoesNotAnnounceItself:
    """`server: uvicorn` tells an attacker which stack and, on some builds,
    which version to look up. It is free to remove and there is no reason
    to publish it."""

    def test_the_server_header_is_suppressed_or_generic(self, client):
        value = (_any_response(client).headers.get("server") or "").lower()
        assert "uvicorn" not in value, (
            f"the Server header still reports {value!r}. Suppressing it "
            "removes a free fingerprint"
        )


class TestHstsIsLeftToTheTlsTerminator:
    """Asserted as an explicit non-requirement so nobody 'completes' this
    finding by adding a header the app cannot honour. If the app ever does
    terminate TLS, delete this test rather than working around it."""

    def test_hsts_is_not_emitted_by_the_application(self, client):
        assert "strict-transport-security" not in {
            k.lower() for k in _any_response(client).headers.keys()
        }, (
            "the application emits HSTS. It does not terminate TLS, so this "
            "is a promise made by the wrong layer -- and actively wrong when "
            "the deployment is plain HTTP behind a proxy. Configure HSTS at "
            "the reverse proxy instead"
        )
