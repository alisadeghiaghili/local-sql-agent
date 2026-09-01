# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Opt-in integration tests proving Phase 1's ``database/executor.py`` /
``database/connection.py`` claims against a REAL SQL Server.

Phase 1 changed ``text()`` -> ``exec_driver_sql`` (fixing ``:name`` being
misparsed as a bind parameter), added a driver-level query timeout, an
always-rolled-back transaction, ``stream_results``, and removed
``fast_executemany`` — see ``database/executor.py``'s module and function
docstrings. None of it was verified against a real database while it was
written, because no SQL Server was available. These tests are how the
team can actually verify those claims, on demand, against a real server.

Running these tests
--------------------
Skipped by default (both by pytest collection reporting them as
``SKIPPED``, and by design — see ``live_engine`` below). To run them for
real::

    RUN_LIVE_DB_TESTS=1 DB_CONNECTION_URL=<a real connection string> \\
        pytest tests/integration/test_executor_live.py -v -m integration

They skip cleanly (not fail) when:

* ``RUN_LIVE_DB_TESTS`` is not set to a truthy value — the default state
  in CI and on every developer machine without a database handy.
* ``cfg.settings.validate()`` rejects the configured
  ``DB_CONNECTION_URL`` (still a placeholder).
* A connection cannot actually be established (wrong host, credentials,
  firewall, VPN not connected, ...) — reported via ``pytest.skip`` with
  the underlying error, not a hard failure, since "no database reachable
  from here" is an environment fact, not a test failure.

``tests/integration/conftest.py`` is the explicit, documented opt-out
from ``tests/conftest.py``'s autouse ``_no_real_database`` guard — see
its docstring for why overriding it there (rather than weakening the
guard for the whole suite) is safe.
"""

from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import config as cfg
from config import override_settings
from database.connection import dispose_engine, get_engine
from database.executor import execute_sql

pytestmark = pytest.mark.integration

_SCRATCH_TABLE = "_nlq_agent_write_probe"


def _opted_in() -> bool:
    return os.getenv("RUN_LIVE_DB_TESTS", "").strip().lower() in ("1", "true", "yes")


@pytest.fixture(scope="module")
def live_engine():
    """A real, connected engine — or a clean ``pytest.skip``.

    Module-scoped so the whole file pays the connection cost once, not
    once per test.
    """
    if not _opted_in():
        pytest.skip(
            "Opt-in integration test: set RUN_LIVE_DB_TESTS=1 (and a real "
            "DB_CONNECTION_URL) to run this against a live database."
        )
    try:
        cfg.settings.validate()
    except ValueError as exc:
        pytest.skip(f"DB_CONNECTION_URL is not configured for a real database: {exc}")

    dispose_engine()  # start from a known-clean pool, ignoring any prior test's cache
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"Configured database is not reachable: {exc}")

    yield engine
    dispose_engine()


class TestBindParameterLiteralBug:
    """The bug ``exec_driver_sql`` (vs. ``conn.execute(text(sql))``) fixes:
    SQLAlchemy's ``text()`` construct parses a literal ``:name`` inside the
    SQL string as a bind-parameter placeholder. A query containing a
    literal like ``N'label: value'`` would raise on the unbound
    parameter — or worse, be silently reinterpreted — under the old
    implementation.
    """

    def test_colon_literal_executes_as_written(self, live_engine):
        df = execute_sql("SELECT N'label: value' AS Greeting")
        assert list(df["Greeting"]) == ["label: value"]


class TestRowCap:
    """``execute_sql`` fetches with ``fetchmany(max_rows_returned)``, so
    the cap applies even when the query itself could return far more
    rows than that."""

    def test_row_cap_bounds_what_comes_back(self, live_engine):
        with override_settings(max_rows_returned=3):
            # sys.all_objects reliably has far more than 3 rows on any
            # real SQL Server instance, including a bare/empty database.
            df = execute_sql("SELECT TOP 1000 name FROM sys.all_objects")
        assert len(df) <= 3


class TestWriteIsRefusedOrRolledBack:
    """Defense in depth: even if a write somehow reached ``execute_sql``
    (application-layer ``security.sql_guard.validate_sql`` should already
    have refused it upstream — this test bypasses that on purpose to
    exercise the database-layer backstop on its own), the query runs
    inside a transaction that is always rolled back and never committed,
    so nothing persists regardless of what the login is permitted to do.
    """

    def test_create_table_does_not_persist(self, live_engine):
        with live_engine.connect() as conn:
            conn.execute(text(
                f"IF OBJECT_ID('{_SCRATCH_TABLE}') IS NOT NULL "
                f"DROP TABLE {_SCRATCH_TABLE}"
            ))
            conn.commit()

        try:
            # Either this raises (most likely: CREATE TABLE returns no
            # row set, so the subsequent fetchmany() fails and
            # execute_sql wraps that as RuntimeError) or it silently runs
            # inside the always-rolled-back transaction — either way,
            # the assertion below is what actually matters.
            try:
                execute_sql(f"CREATE TABLE {_SCRATCH_TABLE} (x INT)")
            except RuntimeError:
                pass

            with live_engine.connect() as conn:
                oid = conn.execute(
                    text(f"SELECT OBJECT_ID('{_SCRATCH_TABLE}') AS oid")
                ).scalar()
            assert oid is None, (
                f"{_SCRATCH_TABLE} persisted -- execute_sql's "
                "always-rolled-back transaction did not hold"
            )
        finally:
            with live_engine.connect() as conn:
                conn.execute(text(
                    f"IF OBJECT_ID('{_SCRATCH_TABLE}') IS NOT NULL "
                    f"DROP TABLE {_SCRATCH_TABLE}"
                ))
                conn.commit()


class TestDriverTimeout:
    """The driver-level ``.timeout`` (bounding the whole query, unlike
    ``SET LOCK_TIMEOUT`` which only bounds time spent waiting to *acquire*
    a lock) must make a deliberately slow query fail fast rather than
    hang for the test-runner's default timeout (or forever)."""

    def test_slow_query_times_out_rather_than_hanging(self, live_engine):
        with override_settings(query_timeout_seconds=2):
            start = time.perf_counter()
            with pytest.raises(RuntimeError):
                execute_sql("WAITFOR DELAY '00:00:15'; SELECT 1 AS x")
            elapsed = time.perf_counter() - start
        # Generous upper bound: proves it aborted near the configured
        # timeout, not after riding out the full 15s delay.
        assert elapsed < 10
