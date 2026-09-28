# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The API's effective CORS allowlist must be logged once, at startup.

Real deployment incident this covers: the API and the static UI ended up
on different ports/hosts, and ``CORS_ALLOWED_ORIGINS`` was left at its
loopback-only default. Every browser call the UI made was silently
blocked by the browser's own cross-origin check, which reports the exact
same "Failed to fetch" a genuinely dead backend would -- so the operator
had nothing in the startup log to tell "CORS rejected this" apart from
"nothing is listening." ``api/server.py``'s ``lifespan`` now logs the
effective, post-``.env`` allowlist unconditionally (INFO level), right
after ``cfg.settings.validate()`` -- see ``docs/deployment-runbook.md``
step 5 and ``docs/fa/getting-started.md`` §2.3.1, both of which now point
an operator at this exact line.

Driven the same way ``TestLifespanValidatesConfig`` in
``tests/test_api_endpoints.py`` drives ``lifespan`` directly (not through
``TestClient``): this file's tests only care that the log line appears,
with the right content, before anything else about startup succeeds or
fails, so any exception ``lifespan`` raises further along (unrelated to
this test -- e.g. no configured API key) is swallowed rather than
asserted on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

import api.server as server_module
from config import override_settings

# A connection string that clears Settings.validate()'s placeholder /
# "username@server" checks without needing a real reachable database --
# lifespan is allowed to fail LATER than the CORS log line (e.g. no
# configured API key), which every test below suppresses.
_VALID_DB_URL = (
    "mssql+pyodbc://svc_account@warehouse.internal:1433/DB"
    "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
)


async def _run_lifespan_and_swallow_later_failures() -> None:
    with contextlib.suppress(Exception):
        async with server_module.lifespan(server_module.app):
            pass  # pragma: no cover - only reached if startup fully succeeds


def _cors_log_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == server_module.logger.name and "CORS allowed origins" in r.getMessage()
    ]


def test_logs_configured_origins_at_startup(caplog: pytest.LogCaptureFixture) -> None:
    with override_settings(
        db_connection_url=_VALID_DB_URL,
        cors_allowed_origins=("http://172.16.101.42:8077", "http://localhost:8080"),
    ):
        with caplog.at_level(logging.INFO, logger=server_module.logger.name):
            asyncio.run(_run_lifespan_and_swallow_later_failures())

    messages = _cors_log_messages(caplog)
    assert messages, (
        f"expected an INFO 'CORS allowed origins: ...' log line at startup; "
        f"got log records: {[r.message for r in caplog.records]}"
    )
    assert "http://172.16.101.42:8077" in messages[0]
    assert "http://localhost:8080" in messages[0]


def test_logs_empty_allowlist_explicitly_rather_than_a_blank_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty CORS_ALLOWED_ORIGINS (every cross-origin call blocked) must
    still produce a readable line, not silently log nothing -- an operator
    staring at "why is CORS empty" needs to see that it IS empty, not
    wonder whether the log line itself is missing."""
    with override_settings(db_connection_url=_VALID_DB_URL, cors_allowed_origins=()):
        with caplog.at_level(logging.INFO, logger=server_module.logger.name):
            asyncio.run(_run_lifespan_and_swallow_later_failures())

    messages = _cors_log_messages(caplog)
    assert messages, "expected the CORS log line even when the allowlist is empty"
    assert "none" in messages[0].lower()


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
