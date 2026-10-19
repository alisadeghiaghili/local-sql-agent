# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A refused CORS origin is named in the server log, once.

The incident: the web UI showed every light red while the server log held only
``OPTIONS /health``, ``OPTIONS /v2/sessions`` and ``OPTIONS /v2/access-requests``,
each ``400 Bad Request``. The allowlist held ``http://172.16.101.42:8077`` and
the page had been opened as ``http://localhost:8077``; nothing said so.

``RejectedOriginLogMiddleware`` names the origin. These tests drive it through
Starlette's real ``CORSMiddleware`` with the ASGI test client, because the point
is where it sits relative to that layer, and they assert that the CORS policy
itself is untouched.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from api.cors_diagnostics import MAX_REMEMBERED_ORIGINS, RejectedOriginLogMiddleware

ALLOWED = ["http://172.16.101.42:8077"]
REJECTED = "http://localhost:8077"
LOGGER = "api.cors_diagnostics"


def _build(allowed: list[str], **kwargs) -> TestClient:
    """A tiny app wired like ``api/server.py``: the logger outside CORS."""

    async def health(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/health", health, methods=["GET", "OPTIONS"])])
    app.add_middleware(
        CORSMiddleware, allow_origins=allowed, allow_credentials=False,
        allow_methods=["*"], allow_headers=["*"],
    )
    app.add_middleware(RejectedOriginLogMiddleware, allowed_origins=allowed, **kwargs)
    return TestClient(app)


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


@pytest.fixture()
def client() -> TestClient:
    return _build(list(ALLOWED))


class TestAllowedOrigin:
    def test_an_allowed_origin_logs_nothing(self, client, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            pre = _preflight(client, ALLOWED[0])
            ordinary = client.get("/health", headers={"Origin": ALLOWED[0]})

        assert pre.status_code == 200
        assert ordinary.headers["access-control-allow-origin"] == ALLOWED[0]
        assert _warnings(caplog) == []

    def test_a_request_without_an_origin_logs_nothing(self, client, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert client.get("/health").status_code == 200
        assert _warnings(caplog) == []

    def test_the_wildcard_allows_everything_and_logs_nothing(self, caplog):
        client = _build(["*"])
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert _preflight(client, REJECTED).status_code == 200
        assert _warnings(caplog) == []

    def test_a_request_to_the_servers_own_address_is_same_origin_and_logs_nothing(self, caplog):
        client = _build(list(ALLOWED))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            client.get("/health", headers={"Origin": "http://testserver"})  # Host is testserver
        assert _warnings(caplog) == []


class TestRejectedOrigin:
    def test_a_rejected_preflight_logs_once_and_is_still_a_400(self, client, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            resp = _preflight(client, REJECTED)

        assert resp.status_code == 400
        assert resp.text == "Disallowed CORS origin"
        assert "access-control-allow-origin" not in resp.headers
        assert len(_warnings(caplog)) == 1

    def test_the_line_names_everything_an_operator_needs(self, client, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _preflight(client, REJECTED)

        (line,) = _warnings(caplog)
        assert f"origin '{REJECTED}' is not allowed" in line
        assert f"(allowed: {ALLOWED[0]})" in line
        assert f"CORS_ALLOWED_ORIGINS={ALLOWED[0]},{REJECTED}" in line
        assert "exactly an allowed address (scheme, host and port)" in line

    def test_a_second_request_from_the_same_origin_logs_nothing(self, client, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _preflight(client, REJECTED)
            _preflight(client, REJECTED)
            client.get("/health", headers={"Origin": REJECTED})

        assert len(_warnings(caplog)) == 1

    def test_a_different_rejected_origin_logs_again(self, client, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _preflight(client, REJECTED)
            _preflight(client, "http://nas.example:8077")

        lines = _warnings(caplog)
        assert len(lines) == 2
        assert "http://nas.example:8077" in lines[1]

    def test_an_ordinary_request_from_a_rejected_origin_logs(self, client, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            resp = client.get("/health", headers={"Origin": REJECTED})

        # CORS lets the request through without its headers; the browser
        # discards the response. The policy is unchanged.
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers
        assert len(_warnings(caplog)) == 1

    def test_an_empty_allowlist_says_none_and_the_line_is_just_the_origin(self, caplog):
        client = _build([])
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _preflight(client, REJECTED)

        (line,) = _warnings(caplog)
        assert "(allowed: (none))" in line
        assert f"CORS_ALLOWED_ORIGINS={REJECTED} " in line


class TestBounds:
    def test_at_most_n_origins_are_logged_then_one_line_says_so(self, caplog):
        client = _build(list(ALLOWED), max_origins=3)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            for i in range(10):
                _preflight(client, f"http://host{i}.example:8077")
            _preflight(client, "http://host0.example:8077")  # remembered: silent

        lines = _warnings(caplog)
        assert len(lines) == 4
        assert all("is not allowed" in line for line in lines[:3])
        assert lines[3] == (
            "CORS: more than 3 distinct rejected origins seen; "
            "further rejected origins are not logged."
        )

    def test_the_default_cap_is_64(self):
        assert MAX_REMEMBERED_ORIGINS == 64

    def test_control_characters_are_stripped_from_the_logged_origin(self, caplog):
        # An HTTP client library refuses such a header, a raw socket does not:
        # drive the ASGI app directly with the bytes the server would hand it.
        allowed = list(ALLOWED)
        mw = RejectedOriginLogMiddleware(_noop_app, allowed_origins=allowed)
        evil = b"http://evil.example\x00\x1b[31m\r\nWARNING forged\x85\xad"
        scope = {"type": "http", "method": "OPTIONS", "path": "/health", "headers": [(b"origin", evil)]}
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            asyncio.run(mw(scope, None, None))

        (line,) = _warnings(caplog)
        for bad in ("\x00", "\x1b", "\r", "\n", "\x85", "\xad"):
            assert bad not in line
        assert "origin 'http://evil.example[31mWARNING forged' is not allowed" in line

    def test_a_long_origin_is_truncated(self, caplog):
        client = _build(list(ALLOWED))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _preflight(client, "a" * 5000)

        (line,) = _warnings(caplog)
        assert len(line) < 800
        assert "a" * 200 in line and "a" * 201 not in line
        assert "..." in line

    def test_the_origin_is_never_echoed_into_the_response(self, client):
        marker = "http://reflected-marker.example"
        pre = _preflight(client, marker)
        ordinary = client.get("/health", headers={"Origin": marker})

        for resp in (pre, ordinary):
            assert "reflected-marker" not in resp.text
            assert all("reflected-marker" not in v for v in resp.headers.values())


async def _noop_app(scope, receive, send):
    return None


class TestRealApp:
    """The assembled ``api.server.app``: same behaviour, same order of layers."""

    def test_the_logger_wraps_cors_in_the_real_stack(self):
        import api.server as server_module

        chain = []
        layer = server_module.app.build_middleware_stack()
        while layer is not None:
            chain.append(type(layer).__name__)
            layer = getattr(layer, "app", None)

        assert "RejectedOriginLogMiddleware" in chain and "CORSMiddleware" in chain
        assert chain.index("RejectedOriginLogMiddleware") < chain.index("CORSMiddleware")

    def test_a_refused_preflight_to_the_real_app_is_named_and_stays_a_400(self, caplog):
        import api.server as server_module

        origin = "http://unlisted-real-app-test.example:8077"
        real = TestClient(server_module.app, raise_server_exceptions=False)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            resp = _preflight(real, origin)

        assert resp.status_code == 400
        lines = [m for m in _warnings(caplog) if origin in m]
        assert len(lines) == 1
        assert f",{origin}" in lines[0]
