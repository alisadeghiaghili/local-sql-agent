# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Idle-aware ``pool_pre_ping`` for a SQLAlchemy engine.

Plain ``pool_pre_ping=True`` runs a liveness probe on *every* checkout of
a pooled connection, no matter how recently that same connection was
checked back in. Against a busy warehouse pool, that is a steady stream
of ``SELECT 1`` scaling with checkout rate rather than with how often a
connection actually needs re-verifying -- the DBA-visible symptom this
module exists to fix.

:func:`install_idle_aware_ping` replaces that with SQLAlchemy's own
documented "pessimistic disconnect handling" recipe (pool events, not
the ``pool_pre_ping`` engine argument), with one addition: the probe only
runs when the connection has been sitting idle in the pool for at least
*idle_seconds*. A connection reused a moment after it was last checked in
is handed back unprobed; one that has not been touched in a while is
verified first, exactly as ``pool_pre_ping=True`` always has.

Usage::

    from sqlalchemy import create_engine
    from database.pool_ping import install_idle_aware_ping

    engine = create_engine(url, pool_recycle=3600)
    install_idle_aware_ping(engine, idle_seconds=60)

Callers decide *whether* to call this at all (that is
``config.Settings.db_pool_pre_ping`` -- ``False`` means never call this
function, full stop) and, when they do, what threshold to pass (that is
``config.Settings.db_pool_ping_idle_seconds``, where ``0`` reproduces
plain ``pool_pre_ping=True``'s ping-every-checkout behaviour exactly).
See :func:`database.connection.get_engine` and
:func:`appdb.engine.build_engine` for the two call sites.
"""

from __future__ import annotations

import time

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DisconnectionError

#: Key this module stores its own state under in a pool
#: ``_ConnectionRecord.info`` dict. Namespaced (rather than a short name
#: like ``"last_used"``) because ``.info`` is a plain, shared dict that
#: any other pool-event listener on the same engine could also write to --
#: SQLAlchemy makes no promise that this module is the only user of it.
_LAST_USED_KEY = "local_sql_agent.pool_ping.last_used_monotonic"


def install_idle_aware_ping(engine: Engine, idle_seconds: int) -> None:
    """Register pool events so *engine* only pings a connection on
    checkout once it has been idle for at least *idle_seconds*.

    Three events, matching SQLAlchemy's documented pessimistic
    disconnect-handling recipe with an added idle threshold:

    ``"connect"``
        Fires once per physical DBAPI connection, the moment the pool
        creates it. Stamps a last-used time immediately, so a
        brand-new connection -- known-live, having just been opened --
        is never pinged on the very first checkout that follows, unless
        *idle_seconds* is ``0`` (see ``"checkout"`` below).
    ``"checkin"``
        Fires every time a connection is returned to the pool. Stamps
        the current monotonic time as this connection's last-used
        moment, which is what the next checkout's idle calculation
        measures from.
    ``"checkout"``
        Fires every time a connection is handed to a caller. Computes
        how long the connection has sat since its last stamp; if that
        is at least *idle_seconds* (or *idle_seconds* is ``0``, meaning
        "always"), runs ``SELECT 1`` against it directly on the DBAPI
        cursor. Any failure there is re-raised as
        :class:`sqlalchemy.exc.DisconnectionError`, which is
        SQLAlchemy's documented signal (from this exact recipe) for the
        pool to discard this connection and transparently open a
        replacement -- the caller that triggered the checkout never
        sees the failed connection at all.

    Parameters
    ----------
    engine:
        The engine to instrument. Events are registered on the engine
        itself (which SQLAlchemy resolves to its ``Pool``), not on a
        pool class, so this only ever affects *this* engine's pool.
    idle_seconds:
        Idle threshold in seconds. ``0`` pings on every checkout
        (identical to plain ``pool_pre_ping=True``); a positive value
        skips the probe for a connection reused within that many
        seconds of its last checkin.

    Notes
    -----
    Uses ``time.monotonic()`` throughout, never wall-clock time, so a
    clock step (NTP adjustment, DST) can never make a connection look
    idle for negative time or for far longer than it has actually sat
    unused.

    Does not itself decide whether to call ``install_idle_aware_ping`` at
    all -- that is ``config.Settings.db_pool_pre_ping``, checked by each
    caller before reaching this function. This function has no "off"
    mode of its own; not calling it is the off mode.
    """

    def _stamp_last_used(dbapi_connection, connection_record) -> None:
        connection_record.info[_LAST_USED_KEY] = time.monotonic()

    def _ping_if_idle(dbapi_connection, connection_record, connection_proxy) -> None:
        if idle_seconds != 0:
            last_used = connection_record.info.get(_LAST_USED_KEY)
            if last_used is None:
                # Freshly created by "connect" a moment ago in this same
                # checkout -- known-live, nothing to verify yet.
                return
            idle_for = time.monotonic() - last_used
            if idle_for < idle_seconds:
                # Reused recently enough -- hand it over unprobed.
                return
        # idle_seconds == 0 ("ping on every checkout", matching plain
        # pool_pre_ping=True exactly, including a brand-new connection's
        # very first checkout) or the idle threshold has been reached.

        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001 - any DBAPI error means
            # this connection is unusable; the pool needs to replace it
            # regardless of the specific driver exception type.
            raise DisconnectionError(
                "idle-aware pre-ping failed; the pool will discard this "
                "connection and open a replacement"
            ) from exc
        finally:
            cursor.close()

    event.listen(engine, "connect", _stamp_last_used)
    event.listen(engine, "checkin", _stamp_last_used)
    event.listen(engine, "checkout", _ping_if_idle)
