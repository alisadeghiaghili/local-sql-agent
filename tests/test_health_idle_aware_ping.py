# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``api.health._ping_db`` against a REAL engine -- the fix for the gap an
independent review of the idle-aware-ping change found: with checkout no
longer pinging a recently-used connection (see ``database.pool_ping``),
``/health`` could report the database healthy on nothing more than a
successful checkout, with zero round trips actually reaching the database.

``_ping_db`` now always issues its own explicit ``SELECT 1`` regardless of
how recently the checked-out connection was used -- these two tests prove
that against a real (SQLite-backed) engine built by
``database.connection.get_engine`` itself, the same way
``tests/test_warehouse_traffic_shape.py`` does, rather than trusting a
fully mocked ``get_engine`` to represent the real code path faithfully.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import create_engine as real_create_engine
from sqlalchemy import event

import config as cfg
import database.connection as dbconn
from api.health import _ping_db, reset_health_cache


class _RealSqliteEngine:
    """See ``tests/test_warehouse_traffic_shape.py`` for the identical
    pattern and its rationale -- duplicated here (rather than imported
    across test modules) to keep this file independently readable."""

    def __init__(self, db_path, **setting_overrides):
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


class TestPingDbRecentlyUsedConnection:
    def test_recently_used_connection_is_still_actually_queried(self, tmp_path) -> None:
        """The MAJOR review finding, proven against real code: a connection
        used moments ago is NOT re-pinged by checkout's own idle-aware
        probe (idle time is far under the 60s default threshold) -- so if
        ``_ping_db`` relied on checkout alone, it would send nothing at
        all. It must send its own ``SELECT 1`` regardless."""
        with _RealSqliteEngine(tmp_path / "warehouse.sqlite3") as ctx:
            # Use the connection once, ordinarily -- this is what leaves it
            # "recently used" rather than idle.
            with ctx.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            ctx.captured.clear()

            reset_health_cache()
            ok, detail = _ping_db()

            assert ok is True
            assert "SELECT 1" in detail
            # Checkout itself did not re-probe (the connection was just
            # used) -- the statement(s) captured here can only be
            # _ping_db's own explicit query.
            assert ctx.captured.count("SELECT 1") == 1

    def test_dead_database_makes_health_report_failure(self, tmp_path) -> None:
        """Once the engine's pool is fully disposed and the underlying
        file removed, a real checkout/query fails for real -- proving
        ``_ping_db``'s exception handling against an actual SQLAlchemy
        failure, not just a mocked ``get_engine`` short-circuit."""
        db_path = tmp_path / "warehouse.sqlite3"
        with _RealSqliteEngine(db_path) as ctx:
            with ctx.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")

            # Simulate the database going away: dispose every pooled
            # connection and delete the file itself, so the next checkout
            # opens a connection to a file that must be recreated empty,
            # then point sqlite at a path whose parent no longer exists to
            # force a real, unrecoverable connection failure.
            ctx.engine.dispose()
            db_path.unlink()
            bad_dir = tmp_path / "does-not-exist"
            with cfg.override_settings(db_connection_url=f"sqlite:///{bad_dir}/warehouse.sqlite3"):
                dbconn.get_engine.cache_clear()
                reset_health_cache()
                ok, detail = _ping_db()

        assert ok is False
        assert detail  # a non-empty, exception-derived explanation
