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

import threading
import uuid
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool

import config as cfg
from core.fileperms import restrict_sqlite_family

#: How long a writer waits for SQLite's single write lock to clear before
#: raising ``OperationalError("database is locked")``, in milliseconds --
#: applied via ``PRAGMA busy_timeout`` on every physical connection a
#: file-backed engine opens (see :func:`_set_file_sqlite_pragmas`). Two
#: admin routes writing the application database at the same time (the
#: concurrency bug this module now guards against) is a lock held for a
#: few milliseconds, not seconds -- 30 seconds is not tuned to that case,
#: it is tuned to survive a slow disk or a much larger burst of concurrent
#: admin requests than this deployment's scale ever expects, while still
#: failing loudly well within any HTTP client's own timeout if the lock
#: genuinely never clears (e.g. a stuck transaction).
_SQLITE_BUSY_TIMEOUT_MS = 30_000

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


def _is_memory_sqlite_url(made) -> bool:
    """True for ``sqlite://`` and ``sqlite:///:memory:`` -- no on-disk file
    at all, the same predicate :func:`_canonical_endpoint` already applies
    for the identical reason (that URL names no file a second connection
    could ever open and see the same data through)."""
    return not made.database or made.database == ":memory:"


def _set_file_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """SQLAlchemy ``"connect"`` listener: put every new physical connection
    a file-backed engine's pool opens into WAL journal mode with a busy
    timeout, before this codebase ever runs a statement on it.

    Both are set here, on the raw DBAPI connection, rather than once via
    ``engine.begin()`` right after ``create_engine`` -- a pooled engine
    opens more than one physical connection over its lifetime (this is
    the whole point of moving off ``StaticPool``, see :func:`build_engine`),
    and ``PRAGMA busy_timeout`` is a per-*connection* setting that a
    fresh connection does not inherit from an earlier one. ``journal_mode``
    itself IS persisted in the database file's header and would already
    read back as ``wal`` on a later connection without this -- it is
    re-issued anyway for the ordinary reason every ``PRAGMA journal_mode``
    caller re-issues it (cheap, a no-op once the file is already in WAL
    mode, and correct on the very first connection that creates the file,
    which is the one connection where it is not yet a no-op).
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def _serialize_memory_engine(engine: Engine) -> None:
    """Make *engine* (a ``:memory:``/``sqlite://`` engine on ``StaticPool``)
    safe for concurrent callers by serialising every checkout.

    ``StaticPool`` hands every caller the exact same ``sqlite3.Connection``
    object -- required for an in-memory database (see :func:`build_engine`),
    but that object is not safe for two threads to drive at once even with
    ``check_same_thread=False``: that flag only lifts pysqlite's own
    same-thread guard, it adds no locking of its own, and two threads
    calling ``execute``/``commit``/``rollback`` on the one shared
    connection at literally the same moment is exactly what produced this
    module's original bug report (``sqlite3.InterfaceError``,
    ``OperationalError: cannot commit -- no transaction is active``, and
    rows left inconsistent).

    Not implemented with SQLAlchemy's pool ``checkout``/``checkin``
    events -- that was the first attempt, and it deadlocks. ``StaticPool``
    does not itself limit how many ``_ConnectionFairy`` wrappers can be
    checked out against its one connection at a time (unlike ``QueuePool``,
    which blocks a second checkout with a real semaphore); under genuine
    concurrent overlap, one checkout's matching ``checkin`` event was
    observed to simply never fire (reproduced directly while building this
    fix: instrumented logging showed a ``checkout`` with no following
    ``checkin`` before the next statement ran). A lock acquired on
    ``checkout`` and released on ``checkin`` then never gets released by
    the thread that "lost" that checkin, and every other thread blocks on
    it forever -- the exact hang this module must not reintroduce.

    Instead, this wraps *engine*'s own ``connect`` method (an ordinary
    instance-attribute override -- ``Engine.begin()`` itself calls
    ``self.connect()`` internally, and both an :class:`~sqlalchemy.engine.
    Connection`` and an :class:`~sqlalchemy.engine.Engine` carry a real
    ``__dict__`` despite declaring ``__slots__ = ()``, inherited from a
    non-slotted base, so both directions of this patch are ordinary
    attribute assignment, not something ``__slots__`` blocks): acquire the
    lock, obtain the real connection, then patch *that connection's* own
    ``close`` to release the lock exactly once (guarded against a second,
    redundant ``close()`` call, which :class:`~sqlalchemy.engine.Connection`
    itself allows) before delegating to the original ``close``. Every
    caller in this codebase only ever uses ``with engine.begin() as conn:``
    or ``with engine.connect() as conn:`` (a context manager calls
    ``__exit__`` -> ``close()`` unconditionally, success or exception), so
    this reliably brackets exactly one checkout's full lifetime, however it
    ends. It is also robust to code that does not use ``with`` at all --
    ``sqlalchemy.inspect(engine)`` calls plain ``engine.connect().close()``
    internally, and was exercised directly while building this fix.

    This is a deliberate throughput-for-safety trade unique to the
    in-memory path: every operation against this engine now queues behind
    every other, on every connection, even two reads that would otherwise
    never conflict. ``tests/conftest.py`` is the only place this
    codebase configures ``APP_DB_URL=sqlite://`` (see that file's own
    comment on why: an isolated, disk-free default for the whole test
    suite) -- a real deployment's zero-configuration fallback is the file
    at :attr:`config.Settings.app_db_sqlite_path`, which takes the
    per-connection, per-checkout pool below instead, precisely so
    production traffic is never serialised this way. A deployment that
    explicitly sets ``APP_DB_URL`` to an in-memory SQLite URL inherits the
    same trade-off, which is the correct, safe default for that choice
    rather than the corruption the unserialised version of this function
    had.
    """
    lock = threading.RLock()
    real_connect = engine.connect

    def _locking_connect(*args, **kwargs):
        lock.acquire()
        try:
            conn = real_connect(*args, **kwargs)
        except BaseException:
            lock.release()
            raise

        real_close = conn.close
        released = False

        def _locking_close(*a, **kw):
            nonlocal released
            try:
                return real_close(*a, **kw)
            finally:
                if not released:
                    released = True
                    lock.release()

        conn.close = _locking_close
        return conn

    engine.connect = _locking_connect


def build_engine(url: str) -> Engine:
    """Build a fresh SQLAlchemy engine for *url*, with no table creation and
    no caching -- the shared per-backend pool logic :func:`get_app_engine`
    itself uses, factored out so a caller that needs **two** application
    databases open at once (:mod:`appdb.migrate`'s source and target) can
    get one engine per URL without fighting ``get_app_engine``'s
    ``lru_cache(maxsize=1)`` singleton, which only ever holds one engine
    for whatever ``APP_DB_URL`` currently resolves to.

    Pool choice, by SQLite shape (corrected from an earlier, wrong claim
    that shipped here: this function used to give *every* SQLite URL
    ``poolclass=StaticPool``, on the reasoning that "SQLite's file locking
    is unreliable under SQLAlchemy's default pool across threads". That
    is backwards -- a file-backed database is exactly where the *default*
    pool is the safe choice, and ``StaticPool`` was the actual source of a
    real, reproduced bug: FastAPI runs this codebase's synchronous admin
    and analyst routes in a thread pool, so ``StaticPool``'s one shared
    ``sqlite3.Connection`` was being driven by two threads at once, and
    one thread's commit or rollback ended the other's, mid-transaction --
    ``sqlite3.InterfaceError``, ``OperationalError: cannot commit -- no
    transaction is active``, and rows left inconsistent, all reproduced
    against ``appdb.access_requests``/``appdb.key_store`` racing from real
    threads. ``check_same_thread=False`` never made this safe; it only
    disables pysqlite's guard *against* exactly this):

    * **A named file** gets no explicit ``poolclass`` at all, i.e.
      SQLAlchemy's own default for a file-based pysqlite URL --
      ``QueuePool``. Every checkout that cannot be served by an idle,
      already-open connection opens its own new ``sqlite3.Connection``,
      so two callers on two threads never share one connection and never
      race each other's commits or rollbacks; SQLite's own file locking
      (a writer lock the whole database, not per-row) is what serialises
      their *writes* correctly, exactly as it does for any other
      multi-connection SQLite user. A ``"connect"`` listener
      (:func:`_set_file_sqlite_pragmas`) puts every one of those
      connections into WAL journal mode (so a reader never blocks the
      writer, and vice versa) with a busy timeout (so a writer that finds
      the database locked by another writer waits for it to finish
      instead of failing immediately with ``OperationalError``).
    * **``:memory:``/``sqlite://``** keeps ``poolclass=StaticPool`` plus
      ``check_same_thread=False`` -- not for file-locking reasons at all,
      but because an in-memory database only exists inside one
      ``sqlite3.Connection``'s process memory: SQLAlchemy's default pool
      would hand each checkout an independent, empty database, and every
      write would vanish outside the transaction that made it. Since that
      still means every caller shares the one connection the bug above
      was found on, :func:`_serialize_memory_engine` wraps it in a
      process-wide lock so this path is safe under concurrency too, at
      the cost of serialising every transaction against it (see that
      function's docstring for why that trade-off is confined to this
      path).

    Deliberately does NOT call :func:`appdb.models.create_all` -- unlike
    :func:`get_app_engine`, whose whole point is the zero-configuration
    path, a caller building a second, ad hoc engine (a migration source or
    target that may not be ready for tables yet) decides for itself
    whether and when to create them.
    """
    made = make_url(url)
    if made.get_backend_name() == "sqlite":
        if _is_memory_sqlite_url(made):
            engine = create_engine(
                url, poolclass=StaticPool, connect_args={"check_same_thread": False},
            )
            _serialize_memory_engine(engine)
            return engine
        engine = create_engine(url)
        event.listen(engine, "connect", _set_file_sqlite_pragmas)
        return engine
    return create_engine(url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_app_engine() -> Engine:
    """Return the singleton SQLAlchemy engine for the application database.

    Cached (``lru_cache(maxsize=1)``) the same way
    :func:`database.connection.get_engine` caches the warehouse engine --
    built once, on first call, and reused thereafter. Call
    :func:`dispose_app_engine` to force a fresh engine (test teardown, or a
    changed ``APP_DB_URL`` picked up via ``config.override_settings``).
    Caching the *engine*, not a connection, is what makes this safe under
    concurrent callers regardless of backend: :func:`build_engine` (which
    this delegates to for the actual construction -- see its docstring for
    the pool this singleton gets per SQLite shape) hands out an engine
    whose own pool is what mediates concurrent checkouts, so two threads
    calling this function at once always get the *same* engine object and
    each still gets its own safe connection from it.

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

    Finding 18 (2026 audit): this database holds API key digests, role
    grants, and config-bundle history -- exactly the kind of thing that
    must not be world-readable on a shared host. ``create_all`` above is
    what actually causes SQLite to create the file on disk (the URL
    string alone creates nothing), so :func:`~core.fileperms.
    restrict_sqlite_family` is called immediately after, and only for a
    SQLite backend -- a managed PostgreSQL/SQL Server target is not a
    local file this process could ``chmod`` in the first place, and its
    own access control is the DBA's responsibility, not this codebase's.
    """
    url = resolve_app_db_url()
    engine = build_engine(url)

    from appdb.models import create_all

    create_all(engine)
    made = make_url(url)
    if made.get_backend_name() == "sqlite" and made.database not in (None, "", ":memory:"):
        restrict_sqlite_family(made.database)
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
