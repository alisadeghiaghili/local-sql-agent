# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``database.pool_ping.install_idle_aware_ping`` -- the idle-aware
follow-up to Finding 1 (2026 warehouse-load audit): only ping a pooled
connection on checkout once it has actually sat idle for a configurable
threshold, instead of on every single checkout.

These tests exercise :func:`install_idle_aware_ping` directly against a
bare :class:`sqlalchemy.pool.QueuePool` wired to a fake, in-process DBAPI
stand-in (never a real database, and never SQLite either -- a bare
``Pool`` with a ``creator`` callable is a documented, fully supported way
to drive SQLAlchemy's pool/event machinery without any real driver).
That stand-in lets each test control, precisely, whether a given
connection's ``SELECT 1`` succeeds or fails, and a fake monotonic clock
(patched onto ``database.pool_ping.time``) lets each test control exactly
how "idle" a connection appears to be, without a single real
``time.sleep``.

Wiring tests -- whether ``database.connection.get_engine`` /
``appdb.engine.build_engine`` call this function at all, and with what
threshold -- live in ``tests/test_connection.py`` and
``tests/test_appdb_engine_idle_ping.py``. This file is only about
:func:`install_idle_aware_ping`'s own behaviour.
"""

from __future__ import annotations

import types

import pytest
from sqlalchemy.pool import QueuePool

import database.pool_ping as pool_ping_mod
from database.pool_ping import install_idle_aware_ping


class FakeClock:
    """A controllable stand-in for ``time.monotonic()``."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def monotonic(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeCursor:
    def __init__(self, conn: "FakeDBAPIConnection", log: list[tuple[int, str]]) -> None:
        self._conn = conn
        self._log = log

    def execute(self, sql: str, *args, **kwargs) -> None:
        self._log.append((self._conn.id, sql))
        if sql.strip().upper() == "SELECT 1" and self._conn.fail_ping:
            raise RuntimeError("simulated ping failure")

    def close(self) -> None:
        pass


class FakeDBAPIConnection:
    """A minimal stand-in for a raw DBAPI connection -- just enough surface
    (``cursor``, ``rollback``, ``close``) for SQLAlchemy's ``Pool`` to treat
    it as a real connection, plus a ``fail_ping`` switch this test suite
    flips to simulate a stale/dead connection."""

    def __init__(self, id_: int, log: list[tuple[int, str]], fail_ping: bool = False) -> None:
        self.id = id_
        self._log = log
        self.fail_ping = fail_ping
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self, self._log)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakePool:
    """Bundles a :class:`QueuePool` over :class:`FakeDBAPIConnection`
    instances with the shared execution log and connection registry each
    test asserts against."""

    def __init__(self, fail_first_n: int = 0, **pool_kwargs) -> None:
        self.executed: list[tuple[int, str]] = []
        self.created: list[FakeDBAPIConnection] = []
        self._fail_first_n = fail_first_n

        def creator() -> FakeDBAPIConnection:
            conn = FakeDBAPIConnection(
                id_=len(self.created),
                log=self.executed,
                fail_ping=len(self.created) < fail_first_n,
            )
            self.created.append(conn)
            return conn

        kwargs = {"pool_size": 1, "max_overflow": 0}
        kwargs.update(pool_kwargs)
        self.pool = QueuePool(creator=creator, **kwargs)

    def ping_calls(self) -> int:
        return sum(1 for _cid, sql in self.executed if sql.strip().upper() == "SELECT 1")


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(
        pool_ping_mod, "time", types.SimpleNamespace(monotonic=clock.monotonic)
    )
    return clock


class TestIdleAwarePing:
    def test_reused_within_threshold_is_not_pinged(self, fake_clock: FakeClock) -> None:
        fp = FakePool()
        install_idle_aware_ping(fp.pool, idle_seconds=60)

        conn = fp.pool.connect()
        conn.close()  # checkin -- stamps last_used at t=0

        fake_clock.advance(30)  # well under the 60s threshold
        conn = fp.pool.connect()
        conn.close()

        assert fp.ping_calls() == 0
        assert len(fp.created) == 1  # same connection reused throughout

    def test_idle_past_threshold_is_pinged(self, fake_clock: FakeClock) -> None:
        fp = FakePool()
        install_idle_aware_ping(fp.pool, idle_seconds=60)

        conn = fp.pool.connect()
        conn.close()  # checkin -- stamps last_used at t=0
        # The checkout above was the connection's very first -- confirm it
        # was not itself pinged, so the assertion below can only be about
        # the *second* checkout, the one that is actually idle-past-threshold.
        assert fp.ping_calls() == 0

        fake_clock.advance(61)  # past the 60s threshold
        conn = fp.pool.connect()
        conn.close()

        assert fp.ping_calls() == 1
        assert len(fp.created) == 1  # the ping succeeded -- no replacement needed

    def test_failed_ping_replaces_connection_and_callers_statement_still_succeeds(
        self, fake_clock: FakeClock
    ) -> None:
        # The first connection this pool ever creates has a dying ping.
        fp = FakePool(fail_first_n=1)
        install_idle_aware_ping(fp.pool, idle_seconds=60)

        conn = fp.pool.connect()
        conn.close()  # checkin -- stamps last_used at t=0, connection #0

        fake_clock.advance(120)  # idle well past the threshold
        conn = fp.pool.connect()  # triggers a ping on #0, which fails

        # The pool transparently discarded #0 and handed back a fresh
        # connection (#1) instead of propagating the failure to the caller.
        assert len(fp.created) == 2
        assert fp.created[0].closed is True

        # The caller's own statement runs normally on the replacement.
        cursor = conn.dbapi_connection.cursor()
        cursor.execute("SELECT 42")
        assert fp.executed[-1] == (1, "SELECT 42")
        conn.close()

    def test_threshold_zero_pings_every_checkout(self, fake_clock: FakeClock) -> None:
        fp = FakePool()
        install_idle_aware_ping(fp.pool, idle_seconds=0)

        conn = fp.pool.connect()  # very first checkout of a brand-new connection
        conn.close()

        assert fp.ping_calls() == 1  # pinged even though nothing was ever idle

        # No time passes at all between checkin and the next checkout.
        conn = fp.pool.connect()
        conn.close()
        assert fp.ping_calls() == 2

    def test_never_used_connection_is_not_pinged_on_its_first_checkout(
        self, fake_clock: FakeClock
    ) -> None:
        """With a positive threshold, a connection the pool just created
        (never checked in before) is not pinged on the checkout that
        immediately follows -- it is known-live, having just been opened."""
        fp = FakePool()
        install_idle_aware_ping(fp.pool, idle_seconds=60)

        conn = fp.pool.connect()
        assert fp.ping_calls() == 0
        conn.close()
