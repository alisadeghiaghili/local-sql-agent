# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The application database's SQLAlchemy engine, and the same-database refusal.

This is the highest-risk file in this phase. ``docs/db-hardening.md``
specifies a read-only login for the warehouse connection
(:data:`config.Settings.db_connection_url`), and ``database/executor.py``
additionally rolls back every transaction it runs as a second,
application-layer backstop. Neither of those protections exists on the
application database this module builds an engine for — it needs writes,
by design (the key store, role grants). If the two ever resolved to the
same server and database, this module's own writes would be the mechanism
that undoes the warehouse's read-only posture, silently, from inside the
one place this codebase's whole defense-in-depth story assumes writes
cannot reach production data.

:func:`raise_if_same_database` is therefore checked at start-up
(``api/server.py``'s ``lifespan``) before anything else touches either
engine, and it compares the two URLs **after parsing**, not by string
equality — ``localhost`` and ``127.0.0.1`` naming the same database is the
same mistake spelled differently, and a string comparison would miss it.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool

import config as cfg

#: Host spellings that all name "this machine" for the purpose of deciding
#: whether two connection URLs point at the same server. Not an exhaustive
#: list of every loopback spelling a resolver might produce (e.g. a literal
#: "0.0.0.0" is not "the same host" in the sense this check cares about --
#: it is "any interface", a different question) -- just the two spellings
#: an operator actually types interchangeably in a connection string.
_LOOPBACK_HOST_ALIASES = frozenset({"localhost", "127.0.0.1", "::1"})


def resolve_app_db_url() -> str:
    """The effective application-database URL, resolved from settings.

    ``cfg.settings.app_db_url`` when set; otherwise a SQLite URL built from
    ``cfg.settings.app_db_sqlite_path``, with the file's parent directory
    created if missing (mirroring ``session.persistence.SessionPersistence``'s
    own "parent directories are created if missing" behaviour).
    """
    configured = cfg.settings.app_db_url.strip()
    if configured:
        return configured

    db_path = cfg.settings.app_db_sqlite_path
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    # sqlite:/// + a relative or absolute filesystem path is exactly the
    # SQLAlchemy sqlite URL shape (three slashes, then the path as given).
    return f"sqlite:///{db_path}"


def _canonical_endpoint(url_str: str) -> tuple:
    """A comparable, parsed identity for *url_str*: (backend, ...).

    Two URLs naming the same physical database produce equal tuples here,
    regardless of superficial spelling differences (``localhost`` vs.
    ``127.0.0.1``, a relative vs. resolved-absolute SQLite path). Two URLs
    for different backends (e.g. ``sqlite`` vs. ``mssql``) are never equal,
    which is the correct answer -- a SQLite application database can never
    collide with a SQL Server warehouse connection.

    For SQLite, "the database" is the file itself: the path is resolved to
    an absolute, real path so ``./logs/app.db`` and
    ``logs/../logs/app.db`` compare equal, and an in-memory database
    (``sqlite://`` with no path, or ``sqlite:///:memory:``) is its own
    distinct token -- two *different* in-memory databases are never "the
    same" one just because both are unnamed, so this deliberately does
    NOT treat two in-memory URLs as colliding with each other, only a
    named file with itself.

    For every other backend, identity is (host, port, database name) --
    the exact fields the spec names -- with the host normalised across
    :data:`_LOOPBACK_HOST_ALIASES` so ``localhost`` and ``127.0.0.1``
    naming the same server are recognised as one mistake, not two
    different servers.
    """
    url = make_url(url_str)
    backend = url.get_backend_name()

    if backend == "sqlite":
        database = url.database
        if not database or database == ":memory:":
            # Two distinct in-memory databases are never "the same
            # database" merely for both being unnamed -- a fresh random
            # token on every call guarantees no two calls (even with the
            # literal same URL string) ever compare equal here. This is
            # deliberately NOT id(url_str): CPython interns short string
            # literals, so two separately-written "sqlite://" literals in
            # calling code can share one object id, which made this
            # function's very first version wrongly treat them as one
            # in-memory database -- an implementation-detail-dependent bug
            # caught by this function's own test before it shipped.
            return ("sqlite", "memory", uuid.uuid4())
        resolved = str(Path(database).expanduser().resolve())
        return ("sqlite", resolved)

    host = (url.host or "").strip().lower()
    if host in _LOOPBACK_HOST_ALIASES:
        host = "__loopback__"
    # Case-folded, deliberately. Database-name case sensitivity varies by
    # backend and even by platform -- SQL Server is case-insensitive under
    # most collations, PostgreSQL folds unquoted names, MySQL depends on
    # the host filesystem -- so there is no single correct answer to
    # inherit.
    #
    # The failure directions are not symmetric, and that is what decides
    # it. A false positive here costs an operator a rename and a clear
    # error at start-up. A false negative lets this module's writes land
    # on the warehouse and silently undo the read-only posture
    # docs/db-hardening.md exists to establish. So the check over-matches
    # on purpose.
    database = (url.database or "").strip().lower()
    return (backend, host, url.port, database)


def raise_if_same_database(app_db_url: str, warehouse_url: str) -> None:
    """Refuse if *app_db_url* and *warehouse_url* name the same database.

    Parameters
    ----------
    app_db_url:
        The resolved application-database URL (see
        :func:`resolve_app_db_url`).
    warehouse_url:
        ``cfg.settings.db_connection_url`` -- the read-only warehouse
        connection.

    Raises
    ------
    RuntimeError
        If the two URLs resolve to the same (backend, host, port,
        database) identity after parsing -- see :func:`_canonical_endpoint`
        for exactly what "same" means, including the ``localhost`` /
        ``127.0.0.1`` equivalence. Same server, *different* database is
        never refused: that is a normal, deliberate deployment shape.
    """
    if _canonical_endpoint(app_db_url) == _canonical_endpoint(warehouse_url):
        raise RuntimeError(
            "APP_DB_URL resolves to the same server and database as "
            "DB_CONNECTION_URL (the read-only warehouse connection) -- "
            f"refusing to start. Application database: {app_db_url!r}; "
            f"warehouse: {warehouse_url!r}. The application database needs "
            "write access (docs/db-hardening.md specifies the warehouse "
            "login as read-only, and database/executor.py always rolls "
            "back its transactions); pointing both at the same database "
            "would undo that posture. Point APP_DB_URL at a different "
            "database (same server is fine), or leave it unset to use the "
            "SQLite fallback."
        )


def build_engine(url: str) -> Engine:
    """Build a fresh SQLAlchemy engine for *url*, with no table creation and
    no caching -- the shared per-backend pool logic :func:`get_app_engine`
    itself uses, factored out so a caller that needs **two** application
    databases open at once (:mod:`appdb.migrate`'s source and target) can
    get one engine per URL without fighting ``get_app_engine``'s
    ``lru_cache(maxsize=1)`` singleton, which only ever holds one engine
    for whatever ``APP_DB_URL`` currently resolves to.

    A SQLite URL is given ``poolclass=StaticPool`` plus
    ``check_same_thread=False`` for the same reason :func:`get_app_engine`
    always has: SQLite's file locking is unreliable under SQLAlchemy's
    default pool across threads, and an in-memory URL (``sqlite://``)
    specifically requires a single shared connection or every checkout
    would see an empty, independent database.

    Deliberately does NOT call :func:`appdb.models.create_all` -- unlike
    :func:`get_app_engine`, whose whole point is the zero-configuration
    path, a caller building a second, ad hoc engine (a migration source or
    target that may not be ready for tables yet) decides for itself
    whether and when to create them.
    """
    made = make_url(url)
    if made.get_backend_name() == "sqlite":
        return create_engine(
            url, poolclass=StaticPool, connect_args={"check_same_thread": False},
        )
    return create_engine(url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_app_engine() -> Engine:
    """Return the singleton SQLAlchemy engine for the application database.

    Cached (``lru_cache(maxsize=1)``) the same way
    :func:`database.connection.get_engine` caches the warehouse engine --
    built once, on first call, and reused thereafter. Call
    :func:`dispose_app_engine` to force a fresh engine (test teardown, or a
    changed ``APP_DB_URL`` picked up via ``config.override_settings``).

    Every table in :data:`appdb.models.metadata` is created
    (``checkfirst=True``, a no-op if they already exist) the moment the
    engine is built -- unconditionally, the same way
    ``session/persistence.py`` always ran ``CREATE TABLE IF NOT EXISTS``
    at construction time, before any migration tool existed. This is what
    makes the zero-configuration SQLite path actually zero-configuration,
    and it is exactly as safe against a managed backend a DBA already
    provisioned: it creates *tables* inside the database, never the
    database itself (``docs/admin-panel-architecture.md`` §5.3). An
    organisation that instead wants schema changes to go through Alembic
    can still do so (``appdb/migrations/``); running both is harmless
    since table creation here is idempotent.
    """
    engine = build_engine(resolve_app_db_url())

    from appdb.models import create_all

    create_all(engine)
    return engine


def dispose_app_engine() -> None:
    """Dispose the cached application-database engine and clear the cache.

    Mirrors :func:`database.connection.dispose_engine` exactly -- see that
    function's docstring for the three use cases (test teardown, a
    changed ``APP_DB_URL`` picked up via ``config.override_settings``, and
    graceful shutdown).
    """
    if get_app_engine.cache_info().currsize > 0:
        get_app_engine().dispose()
    get_app_engine.cache_clear()
