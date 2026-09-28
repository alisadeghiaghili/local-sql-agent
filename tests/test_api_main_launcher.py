# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``python -m api`` (``api/__main__.py``).

The API port has always been settable on the ``uvicorn`` command line
(``--port``) but never in ``.env``, where every other setting lives. This
launcher exists so ``API_HOST``/``API_PORT`` in ``.env`` are a complete
description of where the server binds, without requiring the operator to
remember or script a separate command line every time. It must:

* bind to exactly ``cfg.settings.api_host`` / ``cfg.settings.api_port``,
  read at call time (so ``override_settings()`` reaches it, same as every
  other consumer of ``config.settings`` in this codebase);
* run the same ASGI app string (``"api.server:app"``) the documented
  ``uvicorn api.server:app ...`` command runs;
* pass ``server_header=False`` -- the programmatic equivalent of that
  command's ``--no-server-header`` flag (``docs/deployment-runbook.md``
  step 4: uvicorn appends its own ``Server: uvicorn`` header at the
  protocol layer, after the ASGI app returns, where no middleware can
  strip it -- this flag is the only place that banner is actually
  suppressed on the wire).

``main()`` is exercised directly (never the ``if __name__ == "__main__"``
guard), with ``uvicorn.run`` monkeypatched so no real server ever binds a
socket during the test.
"""

from __future__ import annotations

import config as cfg
from config import override_settings

import api.__main__ as api_main


def test_main_binds_host_and_port_from_settings(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(api_main.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))

    with override_settings(api_host="192.0.2.1", api_port=9123):
        api_main.main()

    assert len(calls) == 1, "expected exactly one uvicorn.run() call"
    args, kwargs = calls[0]
    assert args == ("api.server:app",), (
        f"expected the same ASGI app string the documented `uvicorn api.server:app ...` "
        f"command runs, got {args!r}"
    )
    assert kwargs["host"] == "192.0.2.1"
    assert kwargs["port"] == 9123
    assert kwargs["server_header"] is False, (
        "expected server_header=False -- the programmatic equivalent of the documented "
        "command's --no-server-header flag"
    )


def test_main_reads_settings_at_call_time_not_import_time(monkeypatch) -> None:
    """``cfg.settings`` is read inside ``main()``, not captured at module
    import -- so a test (or a real deployment reading a freshly-loaded
    ``.env``) that changes ``config.settings`` before calling ``main()``
    is honoured, mirroring every other ``cfg.settings.<field>`` consumer
    in this codebase (see config.py's own module docstring)."""
    calls = []
    monkeypatch.setattr(api_main.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))

    with override_settings(api_host="10.0.0.5", api_port=8123):
        api_main.main()
    with override_settings(api_host="10.0.0.9", api_port=8199):
        api_main.main()

    assert len(calls) == 2
    assert (calls[0][1]["host"], calls[0][1]["port"]) == ("10.0.0.5", 8123)
    assert (calls[1][1]["host"], calls[1][1]["port"]) == ("10.0.0.9", 8199)


def test_default_api_host_and_port(monkeypatch) -> None:
    """Defaults (no API_HOST/API_PORT set): loopback-only host, port 8000
    -- matching every other default in this codebase that assumes the API
    is reachable at http://localhost:8000 (web/js/config.js's
    DEFAULT_API_PORT, DEFAULT_CORS_ALLOWED_ORIGINS, the runbook, the
    README)."""
    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.delenv("API_PORT", raising=False)
    default_settings = cfg.Settings()
    assert default_settings.api_host == "127.0.0.1"
    assert default_settings.api_port == 8000
