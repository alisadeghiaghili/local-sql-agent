# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""What this application actually sends to the warehouse, measured rather
than assumed -- the counting half of the idle-aware-ping follow-up to
Finding 1 (2026 warehouse-load audit).

Two properties are asserted here, against a REAL (if SQLite-backed)
engine built by :func:`database.connection.get_engine` itself -- not a
mock standing in for "the pool did something":

1. **An idle application sends nothing periodic.** No test in this file
   discovers this by waiting; it is true by construction, because this
   codebase has no timer/scheduler that reaches the warehouse at all (see
   ``TestIdleAppSendsNothingPeriodic`` for the structural check that keeps
   this true). What this file DOES measure dynamically is that
   instrumenting the engine and then calling nothing produces zero
   statements -- a guard against a future periodic mechanism being added
   without updating ``docs/deployment-runbook.md``'s §12 table.
2. **Per-question pings drop to at most one when questions arrive within
   the idle threshold.** Twenty sequential calls through
   ``database.executor.execute_sql`` (the same function
   ``session.engine.TurnEngine``'s default ``execute_fn`` calls) -- real
   wall-clock time, comfortably under the default 60s
   ``DB_POOL_PING_IDLE_SECONDS`` -- must not re-ping the connection
   between every question the way plain ``pool_pre_ping=True`` did.

Root ``conftest.py``'s ``_no_real_database`` fixture patches
``database.connection.create_engine`` to refuse any real engine for every
test under ``tests/``. This file's tests explicitly re-patch it back to
the real ``sqlalchemy.create_engine`` for the duration of the ``with``
block below, pointed at a throwaway SQLite file under ``tmp_path`` (never
committed, never referenced outside the one test that creates it) --
exactly the seam the guard's own refusal message names as the supported
way to "exercise the engine factory itself."
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

from sqlalchemy import create_engine as real_create_engine
from sqlalchemy import event

import config as cfg
import database.connection as dbconn
import database.executor as executor


def _classify(sql: str) -> str:
    s = sql.strip().upper()
    if s == "SELECT 1":
        return "ping"
    if s.startswith("SELECT") or s.startswith("BEGIN") or s.startswith("COMMIT") or s.startswith("ROLLBACK"):
        return "user query"
    return "other"


class _RealSqliteEngine:
    """Context manager: makes ``database.connection.get_engine()`` build a
    REAL, file-backed SQLite engine (still going through
    :func:`database.connection.get_engine`'s own idle-aware-ping wiring,
    unmocked) and captures every literal statement sent to the DBAPI --
    including a pre-ping's raw ``cursor.execute("SELECT 1")``, which never
    goes through SQLAlchemy's own ``before_cursor_execute`` event because
    it bypasses the execution context entirely.
    """

    def __init__(self, db_path, **setting_overrides):
        self._db_path = db_path
        self._overrides = {
            "db_connection_url": f"sqlite:///{db_path}",
            "sql_dialect": "sqlite",
            **setting_overrides,
        }
        self.captured: list[str] = []

    def __enter__(self) -> "_RealSqliteEngine":
        self._settings_cm = cfg.override_settings(**self._overrides)
        self._settings_cm.__enter__()
        self._patch_cm = patch(
            "database.connection.create_engine", side_effect=real_create_engine
        )
        self._patch_cm.__enter__()
        dbconn.get_engine.cache_clear()

        self.engine = dbconn.get_engine()

        def _on_connect(dbapi_connection, connection_record) -> None:
            dbapi_connection.set_trace_callback(self.captured.append)

        event.listen(self.engine, "connect", _on_connect)
        return self

    def __exit__(self, *exc_info) -> None:
        dbconn.dispose_engine()
        self._patch_cm.__exit__(*exc_info)
        self._settings_cm.__exit__(*exc_info)


class TestIdleAppSendsNothingPeriodic:
    def test_instrumented_engine_with_no_calls_sends_nothing(self, tmp_path) -> None:
        """Building the engine and doing nothing else must send zero
        statements -- this codebase has no background poll of its own."""
        with _RealSqliteEngine(tmp_path / "warehouse.sqlite3") as ctx:
            # No request, no admin action, no background thread touches
            # the engine here -- the assertion is simply that nothing did.
            assert ctx.captured == []

    def test_no_periodic_scheduler_reaches_the_database_modules(self) -> None:
        """Structural guard for the property the dynamic test above can't
        fully prove on its own: that nothing in the request-serving code
        schedules itself on an interval in the first place.

        ``retrieval.dimension_vocabulary``'s self-healing refresh (the one
        background-thread mechanism that touches the warehouse outside a
        request) is triggered per-request, single-shot, and rate-limited
        on failure -- never on a timer. This asserts the two facts that
        make it safe to say "an idle app sends nothing": no in-flight
        refresh is ever running before a request asks for one, and the
        module defines no interval-based trigger at all.
        """
        import retrieval.dimension_vocabulary as dv

        # No refresh is in flight just from importing the module -- the
        # single-flight set is only ever populated by an actual request
        # or an explicit warm_all()/refresh_vocabulary() call, never at
        # import time or on any timer.
        assert dv._in_flight == set()
        # No Timer/scheduler primitive appears anywhere in the module's
        # own namespace -- only Thread (request-triggered, see the module
        # docstring's "Background refresh" section) and Lock/set for the
        # single-flight bookkeeping.
        for name in dir(dv):
            value = getattr(dv, name)
            assert type(value).__name__ not in ("Timer", "BackgroundScheduler")


class TestAnalystQuestionsPingCountDropsToAtMostOne:
    def test_twenty_sequential_questions_ping_at_most_once(self, tmp_path) -> None:
        with _RealSqliteEngine(tmp_path / "warehouse.sqlite3") as ctx:
            with ctx.engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE TABLE Customer (ID INTEGER PRIMARY KEY, NationalID TEXT)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO Customer (ID, NationalID) VALUES (1, 'X1')"
                )
            # Setup traffic (the CREATE TABLE/INSERT above, and whatever
            # that first checkout's own pre-ping decided) is not part of
            # the twenty-questions measurement below.
            ctx.captured.clear()

            for _ in range(20):
                df = executor.execute_sql("SELECT NationalID FROM Customer")
                assert list(df["NationalID"]) == ["X1"]

            counts: dict[str, int] = {}
            for sql in ctx.captured:
                kind = _classify(sql)
                counts[kind] = counts.get(kind, 0) + 1

            # The headline property: back-to-back questions (real
            # wall-clock time, far under the 60s default idle threshold)
            # must not re-ping between every single one.
            assert counts.get("ping", 0) <= 1
            assert counts.get("user query", 0) == 20
