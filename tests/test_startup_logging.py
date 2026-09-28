# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Neither documented way of starting the HTTP API (a plain ``uvicorn
api.server:app --host ... --port ... --no-server-header``, or
``python -m api``) used to make a single ``logger.info(...)`` call reach
anywhere: uvicorn's own default logging config never touches the ROOT
logger, which Python itself starts at ``WARNING`` with no handler at all.
The startup provenance banner (``core.provenance.log_startup_notice``)
and the CORS-allowlist line (``api/server.py``'s ``lifespan``) were both
silently filtered out before a single handler was ever asked to write
them -- confirmed empirically by actually running ``python -m api`` and
observing its stderr contain neither line.

``core.logging_setup.configure_stdlib_logging`` fixes this. This file
tests it two ways:

* directly (``TestConfigureStdlibLogging``), against a saved/restored
  root-logger snapshot, so it never leaks state into any other test in
  the session;
* through the real ``api/server.py`` ``lifespan`` (``TestLifespanReallyEmitsAtInfo``),
  with the root logger reset to a genuinely fresh, unconfigured state
  first (handlers cleared, level reset to the interpreter default of
  ``WARNING``) and ``sys.stderr`` swapped for an in-memory buffer, so the
  assertion is "the real text landed in the real stream a handler writes
  to" -- not "some logger's level was forced up for the duration of this
  test" (which is what ``caplog.at_level(...)`` in
  ``tests/test_server_cors_startup_log.py`` does, and why that file's
  tests alone would still pass even if this bug were reintroduced).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import sys

import pytest

import api.server as server_module
from config import override_settings
from core.logging_setup import configure_stdlib_logging

_VALID_DB_URL = (
    "mssql+pyodbc://svc_account@warehouse.internal:1433/DB"
    "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
)


@contextlib.contextmanager
def _saved_root_logger_state():
    """Snapshot and restore the ROOT logger's handlers/level around a
    test, so directly exercising real logging plumbing here can never
    leak a handler (or a raised level) into any other test in the
    session, whatever order they run in."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


class TestConfigureStdlibLogging:
    def test_attaches_a_handler_and_raises_the_level_when_root_is_unconfigured(self) -> None:
        with _saved_root_logger_state() as root:
            root.handlers = []
            root.setLevel(logging.WARNING)  # the interpreter's own default

            configure_stdlib_logging("INFO")

            assert len(root.handlers) == 1
            assert root.level == logging.INFO

    def test_is_a_noop_when_root_already_has_a_handler(self) -> None:
        """An operator's own logging config (or a prior call to this same
        function) must never be fought -- a second attempt to configure
        must add nothing and change nothing."""
        with _saved_root_logger_state() as root:
            sentinel = logging.NullHandler()
            root.handlers = [sentinel]
            root.setLevel(logging.ERROR)  # an operator's own deliberate choice

            configure_stdlib_logging("INFO")

            assert root.handlers == [sentinel], "must not attach a second handler"
            assert root.level == logging.ERROR, "must not override an operator's own level"

    def test_unrecognised_level_name_falls_back_to_info_rather_than_raising(self) -> None:
        with _saved_root_logger_state() as root:
            root.handlers = []
            root.setLevel(logging.WARNING)

            configure_stdlib_logging("not-a-real-level")  # e.g. a LOG_LEVEL typo

            assert root.level == logging.INFO


class TestLifespanReallyEmitsAtInfo:
    """Drives the REAL ``api/server.py`` ``lifespan`` -- not a mock of
    it -- from a genuinely fresh, unconfigured root-logger state, and
    checks the actual bytes written to the actual stream a
    ``StreamHandler`` writes to. No ``caplog.at_level(...)`` anywhere in
    this class: that call forces a logger's effective level up for the
    test regardless of what the production code path actually
    configures, which is exactly the gap that let this bug ship."""

    async def _run_lifespan_and_swallow_later_failures(self) -> None:
        with contextlib.suppress(Exception):
            async with server_module.lifespan(server_module.app):
                pass  # pragma: no cover - only reached if startup fully succeeds

    def test_cors_line_and_provenance_banner_reach_real_stderr(self) -> None:
        fake_stderr = io.StringIO()
        with _saved_root_logger_state() as root:
            root.handlers = []
            root.setLevel(logging.WARNING)

            with override_settings(
                db_connection_url=_VALID_DB_URL,
                cors_allowed_origins=("http://172.16.101.42:8077",),
                log_level="INFO",
            ):
                real_stderr = sys.stderr
                sys.stderr = fake_stderr
                try:
                    asyncio.run(self._run_lifespan_and_swallow_later_failures())
                finally:
                    sys.stderr = real_stderr

        written = fake_stderr.getvalue()
        assert "CORS allowed origins" in written, (
            f"expected the CORS-allowlist line in real stderr output; got:\n{written}"
        )
        assert "http://172.16.101.42:8077" in written
        assert "Ali Sadeghi Aghili" in written, (
            f"expected the startup provenance banner in real stderr output too; got:\n{written}"
        )

    def test_an_operators_own_handler_is_not_duplicated(self) -> None:
        """With a handler already attached (an operator's own logging
        setup), lifespan's call to configure_stdlib_logging must not add
        a second one -- and must not print the CORS/banner lines twice."""
        fake_stderr = io.StringIO()
        with _saved_root_logger_state() as root:
            operators_handler = logging.StreamHandler(fake_stderr)
            operators_handler.setFormatter(logging.Formatter("OPERATOR %(message)s"))
            root.handlers = [operators_handler]
            root.setLevel(logging.INFO)  # the operator's own choice, e.g. via their own setup

            with override_settings(
                db_connection_url=_VALID_DB_URL,
                cors_allowed_origins=("http://localhost:8080",),
                log_level="INFO",
            ):
                asyncio.run(self._run_lifespan_and_swallow_later_failures())

            assert root.handlers == [operators_handler], "must not attach a second handler"

        written = fake_stderr.getvalue()
        assert written.count("CORS allowed origins") == 1, (
            f"expected the CORS line exactly once (via the operator's own handler), got:\n{written}"
        )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
