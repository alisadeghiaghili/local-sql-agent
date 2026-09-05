# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The bundled web UI must be able to reach the API out of the box.

``docs/fa/getting-started.md`` and ``web/README.md`` both describe one
layout: the API on port 8000, the static UI served from ``web/`` on port
8080. A browser on 8080 calling 8000 is cross-origin, so that layout only
works if the API's CORS allowlist contains the UI's origin.

It defaulted to empty. The preflight came back ``400 Disallowed CORS
origin``, and a browser reports that to the page as the same opaque
"Failed to fetch" it uses for a refused connection -- so the UI showed
API, LLM and DB all down while the CLI, which is not a browser and sends
no ``Origin``, answered questions perfectly against the same server.

These tests exercise the real ``CORSMiddleware`` through the real app
with a real preflight request, because the bug was in the interaction
between the middleware and the setting, not in either alone.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config import DEFAULT_CORS_ALLOWED_ORIGINS, Settings, override_settings

_UI_ORIGIN = "http://localhost:8080"


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/v2/sessions",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )


@pytest.fixture()
def client() -> TestClient:
    import api.server as server_module

    # No lifespan: this is about middleware, and startup would demand a
    # real database and a real LLM endpoint to say anything about CORS.
    return TestClient(server_module.app, raise_server_exceptions=False)


class TestBundledUiOriginIsAllowedByDefault:
    def test_default_settings_allow_the_documented_ui_origin(self):
        assert _UI_ORIGIN in Settings().cors_allowed_origins, (
            "the layout this project documents is API on 8000, UI on 8080; a "
            "default that blocks it makes the documented happy path fail"
        )

    def test_preflight_from_the_ui_origin_succeeds(self, client):
        with override_settings(cors_allowed_origins=DEFAULT_CORS_ALLOWED_ORIGINS):
            resp = _preflight(client, _UI_ORIGIN)
        assert resp.status_code == 200, (
            f"preflight from {_UI_ORIGIN} was refused ({resp.status_code} "
            f"{resp.text!r}) -- the browser reports this to the page as the "
            "uninformative 'Failed to fetch'"
        )
        assert resp.headers.get("access-control-allow-origin") == _UI_ORIGIN

    def test_an_unlisted_origin_is_still_refused(self):
        """The default is a convenience for loopback, not an open door."""
        import api.server as server_module

        client = TestClient(server_module.app, raise_server_exceptions=False)
        with override_settings(cors_allowed_origins=DEFAULT_CORS_ALLOWED_ORIGINS):
            resp = _preflight(client, "https://not-your-site.example.com")
        assert resp.status_code != 200 or (
            resp.headers.get("access-control-allow-origin") != "https://not-your-site.example.com"
        ), "an origin nobody configured must not be granted CORS access"

    def test_setting_the_variable_replaces_the_default(self):
        """A deployment naming its own origin must not silently keep localhost."""
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"CORS_ALLOWED_ORIGINS": "https://bi.example.com"}):
            configured = Settings().cors_allowed_origins
        assert configured == ("https://bi.example.com",)
        assert _UI_ORIGIN not in configured
