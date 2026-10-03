# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Runtime configuration — all values read from environment / .env.

Never hardcode credentials here.
Copy .env.example → .env and fill in real values.

Usage::

    import config as cfg
    print(cfg.settings.openai_model)

Testing::

    Use ``override_settings()`` to safely replace the singleton in tests.
    Because every consumer accesses ``cfg.settings`` at call-time (not at
    import-time), the patch is visible to ALL modules immediately.

        with override_settings(max_rows_returned=5):
            ...  # every cfg.settings access sees the patched value

Three layers, not two
----------------------
This codebase separates three kinds of thing that are easy to lump
together into "configuration":

1. **Engine** — the code itself (``api/``, ``llm/``, ``retrieval/``,
   ``security/``, ...). No domain names, no tuning literals: it must work
   unmodified against any warehouse.
2. **Domain data** — what a *particular* warehouse *is*: its tables,
   columns, business rules, aliases, few-shot examples
   (``project_config/*.yaml``, loaded by :mod:`knowledge.config_loader`
   and :mod:`schema_data.registry`). Swapping warehouses means swapping
   this directory, never editing engine code.
3. **Tuning** — how aggressively the engine retrieves, matches, caches,
   and retries against *this* warehouse and *this* hardware. This module.

A module-level numeric constant elsewhere in the source is not
automatically "tuning" just because layer 3 exists. Sorting a candidate
constant into one of three buckets:

* **Tuning** — a knob an operator might legitimately want different for
  their warehouse or their hardware (a bigger cache, a longer timeout, a
  looser regression tolerance on slower CI hardware, ...). Becomes a
  ``Settings`` field: ``field(default_factory=lambda: os.getenv(...))``,
  documented, read through ``cfg.settings`` at call time so
  ``override_settings()`` reaches it in tests. See
  ``eval_max_accuracy_drop_pct`` / ``eval_max_latency_p95_increase_pct`` /
  ``eval_max_guard_rejection_increase`` below for an example promoted out
  of ``eval/baseline.py`` under exactly this reasoning: how much
  latency/accuracy regression is tolerable is a property of *this*
  deployment's hardware and traffic, not a fixed technical fact.
* **Invariant** — a value that is part of the design's *correctness* and
  must not be tuned, even though it happens to be a number. Stays a
  source constant, with a docstring saying why it is deliberately not
  configurable. The clearest example is
  :data:`security.auth.MIN_KEY_LENGTH` (32): lowering it weakens a
  security property (structural entropy enforced once, at key-issue
  time), so exposing it as an env-overridable knob would let a deployment
  quietly weaken its own auth by setting one variable. Also in this
  bucket: :data:`eval.determinism.MIN_REPEATS` (a statistical floor —
  fewer than 2 repeats cannot measure determinism at all, it is not a
  "less thorough" setting) and :data:`eval.fingerprint.DEFAULT_FLOAT_PRECISION`
  (a golden set's ``expected_fingerprint`` values are hashed at a fixed
  precision; making this env-tunable would silently desynchronise a
  deployment's environment from its own golden set's pinned hashes,
  turning "regression" into "someone's shell profile" with no error at
  either end).
* **Implementation detail** — a value nobody outside the module cares
  about, or one whose only meaningful comparison is against itself /
  against a value already tunable elsewhere. Stays put, no docstring
  justification required beyond the ordinary one.
  :data:`prompt_engine.static_prefix._CHARS_PER_TOKEN` lives here: it is
  a rough characters-per-token heuristic (no real tokenizer dependency),
  used only in comparisons against itself and against
  :attr:`prompt_retrieval_token_budget` below — a deployment whose text
  tokenizes at a different real ratio (e.g. Persian vs English) already
  has the one knob it needs in :attr:`prompt_retrieval_token_budget`;
  giving the ratio its own env var would add a second dial over the same
  effective threshold rather than a genuinely independent one.
  :data:`database.schema_inspector._MAX_SAMPLE_LEN` is the same kind of
  thing one level further out: a display-truncation length inside the
  interactive, untested, coverage-excluded setup wizard
  (``setup_project.py``), never read by the running engine at all.

This rule is enforced, not just documented: ``tests/test_tuning_layer.py``
walks first-party source for a *new* bare module-level numeric constant
outside its allowlist and fails the build, the same way
``tests/test_persian_normalization.py`` already does for a second
translation table built the same way ``core.persian`` builds its own.
See that new test's module docstring for exactly which existing constants
are allowlisted and why — several are legitimately tuning-shaped but
already have a more appropriate, narrower configuration surface than a
process-wide environment variable (e.g. the eval CLI's own
``--determinism-repeats`` flag; see :data:`eval.determinism.DEFAULT_REPEATS`)
and were deliberately left alone rather than duplicated into ``Settings``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Generator

# ---------------------------------------------------------------------------
# .env loading — must happen before Settings() is ever constructed below,
# so that os.getenv() calls in the field default_factories see values from
# .env.  This is the single, earliest point every entry point (app.py,
# api/server.py, tests) goes through, since they all ``import config``.
#
# python-dotenv is a required dependency (see requirements.txt), but we
# degrade gracefully instead of crashing the whole application if it is
# ever missing from an environment (e.g. a minimal container image that
# only sets real environment variables and has no .env file at all).
# ---------------------------------------------------------------------------
#: The ``.env`` file python-dotenv loaded, and what it could not use in it
#: (:func:`core.dotenv_check.scan_dotenv`). Empty path = no ``.env`` file.
#: Set once, below; :meth:`Settings.validate` refuses to start on any problem
#: because python-dotenv itself only prints a one-line warning to stderr.
_dotenv_path: str = ""
_dotenv_problems: list[DotenvProblem] = []

try:
    from dotenv import find_dotenv, load_dotenv

    from core.dotenv_check import DotenvProblem, scan_dotenv

    # find_dotenv() here, not inside load_dotenv(): it searches from the
    # calling file's directory, and the scan must read the same file.
    # PYTHON_DOTENV_DISABLED turns loading off, and with it the check.
    if os.environ.get("PYTHON_DOTENV_DISABLED", "").casefold() not in (
        "1", "true", "t", "yes", "y",
    ):
        _dotenv_path = find_dotenv()
        _dotenv_problems = scan_dotenv(_dotenv_path)
    load_dotenv(_dotenv_path or None)
except ImportError:  # pragma: no cover - exercised only when dependency missing
    pass


#: Origins the bundled ``web/`` UI is documented to be served from, and the
#: default for :attr:`Settings.cors_allowed_origins`.
#:
#: This used to default to empty -- the most restrictive choice, and the
#: wrong one, because the layout this project *documents* is the API on one
#: port and the static UI on another. A browser on 8080 calling 8000 is
#: cross-origin, so the out-of-the-box experience was a preflight answered
#: "400 Disallowed CORS origin", which every browser reports to the page as
#: the uninformative "Failed to fetch". The UI showed API, LLM and DB all
#: down while the CLI, which is not a browser and never sends an Origin,
#: worked perfectly against the same server.
#:
#: Loopback only, and that is what makes it safe to default: a page cannot
#: choose the ``Origin`` the browser sends, so allowing these helps only a
#: page genuinely served from this machine's own 8080 -- and every route it
#: could then reach still requires an API key. Anything else, including any
#: non-loopback host, still has to be named explicitly.
DEFAULT_CORS_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://localhost:8080",
    "http://127.0.0.1:8080",
)


def _parse_port(env_var: str, default: str) -> int:
    """Parse *env_var* (falling back to *default*) as a TCP port number,
    raising a message that names the variable and the bad value instead
    of letting a bare ``int(...)`` ``ValueError`` -- "invalid literal for
    int() with base 10: '...'", which names neither -- propagate out of a
    ``default_factory`` and crash ``import config`` with nothing an
    operator could act on without already knowing this module's
    internals."""
    raw = os.getenv(env_var, default)
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"{env_var} must be an integer (got {raw!r})") from None
    if not (1 <= port <= 65535):
        raise ValueError(f"{env_var} must be between 1 and 65535 (got {port})")
    return port



def _has_placeholder_login_host(url: str) -> bool:
    """Whether *url* still names ``username@server``, the factory default.

    The literal text is matched, and so is the parsed login and host: once
    ``DB_PASSWORD`` is set on the URL it reads ``username:<password>@server``,
    which the literal text no longer matches.

    Examples
    --------
    >>> _has_placeholder_login_host("mssql+pyodbc://username@server:1433/db")
    True
    >>> _has_placeholder_login_host("mssql+pyodbc://username:pw@server/db")
    True
    >>> _has_placeholder_login_host("mssql+pyodbc://nlq:pw@db1/db")
    False
    """
    if "username@server" in url.lower():
        return True
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - an unparsable URL has no login to compare
        return False
    return (parsed.username or "").lower() == "username" and (parsed.host or "").lower() == "server"


def _check_warehouse_url(label: str, url: str, dialect: str, placeholders: set[str]) -> None:
    """Refuse an unset, placeholder or wrong-dialect warehouse connection string.

    Two independent placeholder checks apply: an exact match against
    *placeholders* (single unfilled tokens such as ``"change_me"``) and a
    substring check for ``"username@server"``, the literal host baked into
    :attr:`Settings.db_connection_url`'s factory default. The default is a
    *full connection string*, not a bare token, so it never equals any
    entry in *placeholders* and used to pass validation silently whenever
    a user copied ``.env.example`` and forgot to fill in the variable.

    The dialect check catches the "set SQL_DIALECT but forgot to update the
    connection string" misconfiguration -- a mismatch means every query
    would fail at execution (or be silently misinterpreted). It parses the
    URL string only (``sqlalchemy.engine.make_url`` needs no driver import
    and opens no connection); an exotic backend with no sqlglot-dialect
    mapping is not an error on its own and is skipped.

    Parameters
    ----------
    label:
        Environment variable the URL came from, used in the message.
    url:
        The connection string.
    dialect:
        The sqlglot dialect this connection must speak.
    placeholders:
        Unfilled-token values to refuse.

    Raises
    ------
    ValueError
        Naming *label* and what is wrong with it.
    """
    if not url or url in placeholders:
        raise ValueError(f"{label} is not configured")
    if _has_placeholder_login_host(url):
        raise ValueError(
            f"{label} still has the factory-default placeholder "
            "host (username@server) — set a real connection string in .env"
        )

    from sqlalchemy.engine import make_url

    from security.dialects import sqlglot_dialect_for_backend

    try:
        backend_name = make_url(url).get_backend_name()
        expected_dialect = sqlglot_dialect_for_backend(backend_name)
    except Exception:  # noqa: BLE001 - an unrecognised backend is a
        # different, unrelated concern -- not something this
        # SQL_DIALECT/connection-string consistency check can judge.
        expected_dialect = None
    if expected_dialect is not None and expected_dialect != dialect:
        raise ValueError(
            f"SQL_DIALECT={dialect!r} does not match "
            f"{label}'s backend ({backend_name!r}, which "
            f"this deployment targets as {expected_dialect!r}) -- "
            "every query would fail at execution against the wrong "
            f"dialect. Set SQL_DIALECT to match {label}, or "
            f"fix {label} to point at the intended database."
        )


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings resolved from environment variables."""

    # ── LLM provider (OpenAI-compatible endpoint only) ──────────────────
    openai_base_url: str = field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    """Base URL of the single, trivial-case OpenAI-compatible endpoint
    (:class:`~llm.endpoints.EndpointConfig` named ``"default"``) — a local
    ``gpt-oss``/vLLM/llama.cpp server, or OpenAI's own hosted API. See
    ``LLM_ENDPOINTS``/``LLM_ROUTES`` (:attr:`llm_endpoints_json` /
    :attr:`llm_routes_json`) for routing across more than one endpoint."""

    openai_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    )
    """Model tag sent to :attr:`openai_base_url`."""

    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    """Bearer token for :attr:`openai_base_url`. Empty is valid — many
    self-hosted OpenAI-compatible servers don't check it."""

    # ── HTTP server binding (``python -m api`` launcher) ────────────────
    # The API port has always been settable on the `uvicorn` command line
    # (`--port`) but never in `.env` -- an operator who only ever edits
    # `.env` (the documented workflow for every other setting on this
    # page) had no way to change it there, and started the server on the
    # default port while the rest of their `.env` assumed a different one.
    # These two exist so `.env` is a complete description of where the
    # server binds, and `python -m api` (this module's own `__main__.py`)
    # is a launcher that actually reads them -- the plain `uvicorn ...`
    # command in the runbook/README keeps working unmodified either way,
    # since uvicorn itself never reads these variables.
    api_host: str = field(default_factory=lambda: os.getenv("API_HOST", "127.0.0.1"))
    """Interface ``python -m api`` binds to. Defaults to the loopback-only
    ``127.0.0.1`` -- a freshly-cloned checkout should not be reachable
    from the network until an operator deliberately widens it. The
    documented deployment command binds ``0.0.0.0`` (every interface, see
    ``docs/deployment-runbook.md`` step 4) explicitly, on the command
    line, precisely because that choice should be visible at the call
    site rather than silently inherited from a default an operator never
    looked at."""

    api_port: int = field(default_factory=lambda: _parse_port("API_PORT", "8000"))
    """Port ``python -m api`` binds to. ``8000`` matches every other
    default in this codebase that assumes the API is reachable at
    ``http://localhost:8000`` (``web/js/config.js``'s ``DEFAULT_API_PORT``,
    ``DEFAULT_CORS_ALLOWED_ORIGINS`` above's counterpart on the UI side,
    the runbook, the README). Changing it here changes what ``python -m
    api`` binds to; a plain ``uvicorn ...`` invocation is unaffected --
    its port comes only from its own ``--port`` flag.

    A non-integer or out-of-range (outside 1-65535) ``API_PORT`` fails
    ``import config`` immediately with a message naming ``API_PORT`` and
    the offending value (see :func:`_parse_port`) rather than a bare
    ``int()`` traceback that names neither."""

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    """Level (``"DEBUG"``/``"INFO"``/``"WARNING"``/...) the application's
    own loggers emit at, applied to the ROOT logger by
    :func:`core.logging_setup.configure_stdlib_logging` -- called once,
    early, by both ``api/server.py``'s ``lifespan`` and ``python -m api``
    (``api/__main__.py``) -- but only when nothing has configured logging
    yet (root has no handler). Exists because neither documented way of
    starting the HTTP API otherwise makes an ``INFO`` line -- the startup
    provenance banner, the CORS-allowlist line -- reach anywhere at all:
    uvicorn's own default logging config never touches the root logger,
    which Python itself starts at ``WARNING`` with no handler. See
    ``core.logging_setup``'s module docstring for the full mechanism."""

    db_connection_url: str = field(
        default_factory=lambda: os.getenv(
            "DB_CONNECTION_URL",
            "mssql+pyodbc://username@server:1433/Auction_DM"
            "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes",
        )
    )
    """The single warehouse connection, used only when there is no
    ``project_config/datasources.yaml``. Any password written inside it must
    be percent-encoded (``@`` as ``%40``); to avoid that, leave the password
    out and give it raw in :attr:`db_password`."""

    db_password: str = field(
        default_factory=lambda: os.getenv("DB_PASSWORD", ""),
        repr=False,
    )
    """The raw password for :attr:`db_connection_url` (``DB_PASSWORD``), with
    no URL encoding. Empty (the default) leaves the URL exactly as written.
    When set, the URL must carry a login but no password: it is set on the
    parsed URL by :func:`database.datasources.apply_db_password`, and a
    password present in both places is refused at start-up. Ignored when
    ``datasources.yaml`` exists (each source there names its own
    ``password_env``). Excluded from ``repr`` so it never reaches a log."""
    db_pool_pre_ping: bool = field(
        default_factory=lambda: os.getenv("DB_POOL_PRE_PING", "true").lower()
        in ("1", "true", "yes")
    )
    """Whether :func:`database.connection.get_engine` (and, for a
    non-SQLite backend, :func:`appdb.engine.build_engine`) installs the
    idle-aware pre-ping described under :attr:`db_pool_ping_idle_seconds`
    below (Finding 1, 2026 warehouse-load audit, revised by the
    idle-aware-ping follow-up). Defaults to ``True``. ``False`` means
    exactly what it always has: never ping a pooled connection on
    checkout, full stop — :attr:`db_pool_ping_idle_seconds` has no effect
    at all when this is off.

    What it trades off
    -------------------
    **On (default).** A connection that has sat idle in the pool for at
    least :attr:`db_pool_ping_idle_seconds` is probed with ``SELECT 1``
    before being handed to the caller; one that has been used more
    recently than that is handed over unprobed. A connection that has
    gone stale (the SQL Server side closed it, a firewall/load-balancer
    idle-timed it out) is discovered and replaced before the caller's
    real query ever sees it, instead of surfacing as a query failure —
    real safety, which is why the default stays on. Unlike the
    unconditional ``pool_pre_ping=True`` this setting used to map to
    one-for-one, the probe cost now scales with how often a connection
    goes idle for that long, not with every single checkout — see
    :attr:`db_pool_ping_idle_seconds` for the threshold and the
    ``DB_POOL_PING_IDLE_SECONDS=0`` escape hatch back to today's
    ping-every-checkout behaviour.

    **Off.** No probe on checkout, ever, regardless of idle time. A
    connection that went stale while idle in the pool is only discovered
    when a real query is sent through it, which then fails once and is
    transparently retried on a fresh connection (SQLAlchemy's own pool
    invalidate-and-retry behaviour on a disconnect-class error) — one
    query pays a one-time retry cost instead of any checkout paying a
    probe cost.

    **Why turning it off is a reasonable choice here, not just a
    trade-off**: with :attr:`db_pool_recycle_seconds` (below) set well
    under whatever idle timeout the network path (SQL Server itself, a
    firewall, a load balancer) actually enforces, a connection is
    proactively recycled before it would ever go stale from sitting idle
    — the failure this setting exists to catch becomes rare rather than
    routine. Turn this off only after confirming (with the DBA) what that
    idle timeout actually is and setting ``DB_POOL_RECYCLE_SECONDS``
    comfortably below it; turning it off with no matching recycle margin
    trades a quiet steady cost for occasional, noisier
    first-query-after-idle failures.

    Read once by :func:`database.connection.get_engine` /
    :func:`appdb.engine.build_engine` when the engine is constructed
    (like every other pool-shape setting there); changing it at runtime
    has no effect until the next :func:`~database.connection.dispose_engine`
    / :func:`~appdb.engine.dispose_app_engine`. Also consulted by
    :func:`api.health._ping_db` — see that function's docstring for why it
    changes how many round trips one health probe costs, not just whether
    checkout is probed."""
    db_pool_ping_idle_seconds: int = field(
        default_factory=lambda: int(os.getenv("DB_POOL_PING_IDLE_SECONDS", "60"))
    )
    """How long (in seconds) a pooled connection must have sat unused
    before :attr:`db_pool_pre_ping` (when on) actually probes it on
    checkout, rather than handing it straight to the caller. Default
    ``60``.

    This is the fix for the specific complaint that started the
    idle-aware-ping follow-up to Finding 1: with plain
    ``pool_pre_ping=True``, *every* checkout of a pooled connection sends
    ``SELECT 1`` first, even one microsecond after the previous caller
    checked the very same connection back in — a DBA sees this as a
    constant, checkout-rate-scaled stream of ``SELECT 1`` regardless of
    how few real questions are actually being asked. Tracking how long a
    connection has actually sat idle (via SQLAlchemy's documented
    connect/checkin/checkout pool events, see
    :func:`database.pool_ping.install_idle_aware_ping`) and skipping the
    probe when it has not been idle that long turns the cost back into
    "roughly one probe per burst of activity" instead of "one probe per
    query", while keeping exactly the same safety net for a connection
    that really has gone stale from sitting unused.

    ``0`` means "ping literally every checkout" — the old, simpler
    guarantee, for an operator who wants that back rather than the
    idle-aware trade-off, and its failure handling (invalidate the whole
    pool, not just one connection) now matches plain ``pool_pre_ping=True``
    exactly. It is not a byte-for-byte reproduction of old
    ``pool_pre_ping=True`` in one narrow respect: SQLAlchemy's own
    pre-ping never pings the very first checkout of a brand-new
    connection (a per-connection "fresh" flag, independent of idle time);
    ``0`` here pings even that one, since it honours "every checkout"
    literally. See :func:`database.pool_ping.install_idle_aware_ping`'s
    docstring for the full detail — the difference is observable only
    once per physical connection, on its first use, never again after.
    A negative value is not a supported input and is treated as ``0``
    would only coincidentally be handled the same way the idle comparison
    happens to read it (every checkout looks "idle enough"); use ``0``
    explicitly for that behaviour rather than relying on a negative
    number.

    Has no effect at all when :attr:`db_pool_pre_ping` is ``False`` — that
    setting means "never ping", and this one only ever narrows *when*
    that ping fires, never widens it back on. Read once when the engine is
    built, exactly like :attr:`db_pool_pre_ping` itself."""
    db_pool_recycle_seconds: int = field(
        default_factory=lambda: int(os.getenv("DB_POOL_RECYCLE_SECONDS", "3600"))
    )
    """Seconds after which :func:`database.connection.get_engine` recycles
    a pooled connection (SQLAlchemy's ``pool_recycle``), previously a bare
    ``3600`` literal in that function. See :attr:`db_pool_pre_ping` above
    for why lowering this (below whatever idle timeout the network path
    to the warehouse actually enforces) is the documented alternative to
    leaving ``pool_pre_ping`` on."""
    sql_dialect: str = field(
        default_factory=lambda: os.getenv("SQL_DIALECT", "tsql")
    )
    """The single SQL dialect this deployment targets, as a
    `sqlglot <https://sqlglot.com/>`_ dialect key -- one of ``"tsql"``
    (default), ``"postgres"``, ``"mysql"``, ``"sqlite"`` (see
    :data:`security.dialects.DIALECT_PROFILES`). Resolved once from config
    at start-up, the same way :class:`~llm.router.LLMRouter` resolves an
    endpoint -- this is portability (one dialect per deployment), not
    runtime routing across several live databases.

    SQL generation itself is unaffected by this setting: the model always
    generates ``tsql`` regardless (see ``<PROJECT_CONFIG_DIR>/system_prompt.md``
    and the multi-dialect phase report for why the static prompt prefix must
    stay byte-identical across every deployment). When this resolves to
    anything other than ``"tsql"``, ``llm.sql_agent.SQLAgent`` transpiles
    the guard-approved ``tsql`` SQL to this dialect with sqlglot and
    re-validates the **transpiled** text with
    :func:`~security.sql_guard.validate_sql` pinned to this dialect before
    executing it -- never executing anything the guard has not approved in
    the dialect it will actually run in. When this is ``"tsql"`` (the
    default), that transpile-and-revalidate step is skipped entirely and
    the guard-approved SQL is executed exactly as produced, unchanged from
    this deployment's original, single-dialect behaviour.

    Validated at start-up by :meth:`validate` via
    :func:`security.dialects.require_dialect_supported`, which fails
    closed for an unknown dialect or one with no system-catalogue
    blocklist configured -- see that function's docstring."""
    db_application_name: str = field(
        default_factory=lambda: os.getenv("DB_APPLICATION_NAME", "local-sql-agent")
    )
    """Client application name identified to the warehouse (Finding 5,
    2026 warehouse-load audit) — what a DBA sees as ``program_name`` in
    ``sys.dm_exec_sessions``/``sys.dm_exec_requests`` when attributing a
    trace or a blocking session to this application instead of to "some
    unlabelled connection". Only applied when :attr:`db_connection_url`
    is an ``mssql+pyodbc`` URL that does not already set one (see
    :func:`database.connection_identity.with_application_name`) — never
    overrides a value an operator already configured, and is a no-op for
    every other dialect (SQLite in tests, or a non-``pyodbc`` driver)."""
    query_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("QUERY_TIMEOUT_SECONDS", "60"))
    )
    max_rows_returned: int = field(
        default_factory=lambda: int(os.getenv("MAX_ROWS_RETURNED", "1000"))
    )
    default_top_n: int = field(
        default_factory=lambda: int(
            os.getenv("DEFAULT_TOP_N", os.getenv("MAX_ROWS_RETURNED", "1000"))
        )
    )
    """Row cap injected by :func:`security.sql_guard.ensure_top` into
    generated SQL that has no ``TOP``/row-limit clause of its own.
    Defaults to whatever ``MAX_ROWS_RETURNED`` resolves to (the same
    number the client-side ``fetchmany`` cap already uses), so the two
    caps agree unless ``DEFAULT_TOP_N`` is set independently."""
    log_dir: str = field(
        default_factory=lambda: os.getenv("LOG_DIR", "logs")
    )
    export_dir: str = field(
        default_factory=lambda: os.getenv("EXPORT_DIR", "exports")
    )
    project_config_dir: str = field(
        default_factory=lambda: os.getenv("PROJECT_CONFIG_DIR", "project_config")
    )
    """Directory :mod:`knowledge.config_loader` and :mod:`schema_data.registry`
    read their YAML files from (``aliases.yaml``, ``entities.yaml``,
    ``business_rules.yaml``, ``examples.yaml``, ``metrics.yaml``,
    ``schema.yaml``). Defaults to ``project_config`` — the git-ignored
    directory holding this deployment's real domain data and warehouse
    schema. Point this at ``project_config.example`` to run against the
    committed, sample-data template instead (a fresh clone with no
    ``project_config/`` at all, or CI).

    Deliberately **not** an automatic fallback: when this resolves to
    ``project_config`` (the default) and that directory or one of its
    files is missing, loading still raises ``ConfigNotFoundError`` exactly
    as before — silently running on sample aliases/business rules against
    a real warehouse would produce confidently wrong SQL, which is worse
    than refusing to start. The example directory is only ever reached by
    explicitly setting this variable."""
    # ── query result cache ────────────────────────────────────────────────
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("CACHE_TTL_SECONDS", "300"))
    )
    """How long (seconds) a cached query result stays valid.  0 = disabled."""

    cache_max_size: int = field(
        default_factory=lambda: int(os.getenv("CACHE_MAX_SIZE", "256"))
    )
    """Maximum number of distinct (question, mode) pairs to keep in memory."""

    # ── /health probe cache (Finding 3, 2026 audit) ─────────────────────────
    health_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("HEALTH_CACHE_TTL_SECONDS", "15"))
    )
    """How long (seconds) ``api/health.py::check_health()`` reuses a probe
    result before running the (real, ~5s-timeout, connection-pool-using)
    LLM-endpoint and database checks again.

    ``/health`` needs no credentials and ``api/middleware.py`` deliberately
    keeps it exempt from rate limiting -- a liveness probe must never be
    throttled away, or an orchestrator kills a healthy container -- which
    before this field existed meant an unauthenticated caller could hold
    open a connection from the same pool ``/query`` uses, with no limit on
    how often. Caching (not rate-limiting) is the fix: see
    ``api/health.py``'s own "Result caching" section for the incident that
    motivated it and the reasoning for why caching is the right lever here.

    Long enough to collapse a burst or a tightly-polling load balancer
    into a single real probe; short enough that a genuine outage (or
    recovery) still surfaces well within a liveness-check interval.
    ``api/health.py`` re-asserts ``0 < value <= 30`` as a documented
    invariant of the deployment default, not enforced here -- see that
    module's own test coverage. Exposed as ``api.health.HEALTH_CACHE_TTL_SECONDS``
    via that module's ``__getattr__`` (a live read of this field on every
    access, never a value captured once at import time) so existing callers
    reading it as a module-level constant keep working unchanged."""

    # ── Admin panel: expensive-card result cache (Finding 2, 2026 audit) ────
    admin_expensive_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("ADMIN_EXPENSIVE_CACHE_TTL_SECONDS", "300"))
    )
    """How long (seconds) the admin panel's warehouse-touching cards --
    currently ``GET /admin/health/checks`` (every
    :mod:`scripts.verify_deployment` check, run live) and
    ``GET /admin/schema-drift`` (a full catalogue reflection of every
    table/column in every schema) -- reuse their last result before
    running the underlying checks again. See :mod:`api.admin_result_cache`'s
    module docstring for the incident this fixes: ``web/admin/main.js``'s
    30-second auto-refresh used to re-run both of these on every tick with
    an admin tab merely left open, and the deployment-checks card also ran
    a rolled-back ``CREATE TABLE`` DDL attempt and a ``WAITFOR DELAY``
    probe on every one of those ticks (see ``api.admin_routes.admin_health_checks``'s
    ``deep`` parameter — gating those two checks behind an explicit
    opt-in is a separate fix, not this TTL).

    ``0`` disables caching entirely (every request runs the check live),
    matching :attr:`cache_ttl_seconds`'s own "0 = disabled" convention.
    Each card's own ``?refresh=1`` query parameter bypasses a still-valid
    cached value for that one request without changing this setting or
    affecting any other card's cache. A failed check (e.g. the warehouse
    is genuinely unreachable) is never itself cached — see
    :mod:`api.admin_result_cache`'s "a failed computation is never
    cached" note — so a real outage is never masked behind a stale
    "everything passed" result for the length of this TTL.

    300s (5 minutes) is long enough that leaving the panel open all day
    costs a handful of runs rather than thousands, and short enough that
    an operator who deployed a fix and reopens the panel a few minutes
    later sees it reflected without needing to know to press refresh."""

    # ── JSONL log rotation ──────────────────────────────────────────────────
    log_max_bytes: int = field(
        default_factory=lambda: int(os.getenv("LOG_MAX_BYTES", str(50 * 1024 * 1024)))
    )
    """Size cap (bytes) for a JSONL log file (``query_log.jsonl``,
    ``audit_log.jsonl``) before it is rotated. ``<= 0`` disables rotation
    (the file grows without bound). Read at call time by
    ``logs.logger._rotation_settings()``, per this project's
    read-through-``cfg.settings``-at-call-time convention -- this field was
    previously read directly via ``os.getenv`` in ``logs/logger.py``
    because ``config.py`` was locked by concurrent work when that module
    was written; it is free now, so the field lives here like every other
    setting.

    Raised from ``10 MiB`` alongside :attr:`log_backup_count` below —
    together they bound how much audit history a rotation can ever discard
    (see that field's docstring for the arithmetic and the incident that
    prompted it). Raise this further on a deployment with materially higher
    traffic than the ~1 KB/record this project has actually observed;
    lower it only if disk space is the tighter constraint than audit
    history, which for a compliance/analysis log is rarely the right
    trade."""

    log_backup_count: int = field(
        default_factory=lambda: int(os.getenv("LOG_BACKUP_COUNT", "20"))
    )
    """Number of rotated log backups to retain. ``<= 0`` keeps no history
    (the file is cleared in place instead of shifted to ``.1`` on
    rotation). See :attr:`log_max_bytes` for why this lives here rather
    than behind a direct ``os.getenv`` call.

    Raised from ``5`` together with :attr:`log_max_bytes`'s five-fold
    increase (``10 MiB -> 50 MiB``): a first production deployment's
    ``audit_log.jsonl`` is the only source this project has ever had for
    real accuracy/latency numbers, and losing its earliest records to a
    rotation nobody was watching would be silent and unrecoverable —
    there is no second chance at "the first week". Observed audit records
    average roughly 1 KB each (per-stage timings, the full ``llm`` status
    block, guard verdict); the old ``10 MiB x 5`` = 50 MiB ceiling (about
    50,000 records total) could plausibly be exhausted within the first
    production week by traffic well short of anything unusual, especially
    stacked on top of whatever pre-deployment dev/test traffic already
    shares the same file. The new ``50 MiB x 20`` = 1 GiB ceiling (on the
    order of a million records) is not "unbounded" — a genuinely runaway
    write loop still eventually rotates its oldest history away rather
    than filling the disk forever — but comfortably outlasts any real
    single-organisation deployment's first weeks. Reassess this number
    once real production volume is known; do not simply raise it further
    "to be safe" without knowing the actual record rate, since that trades
    away the runaway-growth backstop for no measured benefit."""

    # ── Phase 2: static prompt prefix / prefix-cache latency win ────────────
    prompt_retrieval_token_budget: int = field(
        default_factory=lambda: int(os.getenv("PROMPT_RETRIEVAL_TOKEN_BUDGET", "6000"))
    )
    """Token-estimate threshold (see ``prompt_engine.static_prefix.estimate_tokens``)
    above which :class:`~prompt_engine.builder.PromptBuilder` falls back to
    per-question retrieval instead of the static, byte-identical prefix. The
    knowledge base measured at Phase 2 kickoff (system prompt + full schema +
    relationships + business rules + examples) is ~4.6k real tokens, comfortably
    under this default — today's 12-table schema always takes the static path.
    A larger schema added in a later phase can exceed the budget and
    transparently falls back to the six-retriever pipeline (still exercised,
    never dead code) instead of blowing up the prompt or losing accuracy."""

    # ── Phase 2: deterministic decoding (docs/api-contract-v2.md §6) ───────
    llm_temperature: float = field(
        default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.0"))
    )
    """Sampling temperature sent to the backend. ``0.0`` (the default) makes
    decoding deterministic given a fixed seed — required so that the same
    question produces byte-identical SQL on every run."""

    llm_top_p: float = field(
        default_factory=lambda: float(os.getenv("LLM_TOP_P", "1.0"))
    )
    """Nucleus-sampling cutoff. ``1.0`` disables nucleus sampling, which
    matters for determinism only when combined with ``llm_temperature=0.0``
    and a fixed ``llm_seed``."""

    llm_seed: int = field(
        default_factory=lambda: int(os.getenv("LLM_SEED", "7"))
    )
    """Fixed sampling seed, sent on every request. Honoured by vLLM and
    llama.cpp; accepted but not guaranteed-deterministic by OpenAI's own
    hosted API (see its ``system_fingerprint`` field) — this project sends
    it regardless (never harmful) and reports whether the endpoint *claims*
    to honour it in the ``llm`` status block's ``seed_honored`` rather than
    asserting determinism this process cannot verify on its own. Combined
    with ``temperature=0`` and a genuinely deterministic endpoint, this is
    what makes ``the same question, run twice, produce byte-identical SQL``
    (a Phase 2 exit criterion) rather than merely "usually similar"."""

    llm_num_predict: int = field(
        default_factory=lambda: int(os.getenv("LLM_NUM_PREDICT", "512"))
    )
    """Bounded max completion length (sent as ``max_tokens``). A generated
    SQL statement is a few dozen tokens at most; capping this bounds worst-case
    decode latency and protects against a runaway generation looping forever."""

    llm_extra_body_json: str = field(
        default_factory=lambda: os.getenv("LLM_EXTRA_BODY", "")
    )
    """Extra JSON fields merged into every chat-completions request body, as
    a JSON object. Empty (the default) sends exactly what this project has
    always sent.

    This exists because "turn the model's reasoning off" has no place in
    the OpenAI chat-completions schema, and every server spells it
    differently: vLLM and SGLang take
    ``{"chat_template_kwargs": {"enable_thinking": false}}`` for Qwen3,
    Ollama takes ``{"think": false}``, and others use ``reasoning_effort``.
    Encoding those dialects here would mean this project claiming to know
    every inference server's private vocabulary, and silently sending the
    wrong key whenever it guessed wrong. A passthrough says the honest
    thing instead: these are your server's fields, sent verbatim.

    It matters more than a tuning knob suggests. A reasoning model spends
    its completion budget thinking before it answers, so with
    :attr:`llm_num_predict` at its default a Qwen3-class model can consume
    every token on reasoning and be cut off (``finish_reason: "length"``)
    before emitting a single character of SQL — which arrives as
    ``EMPTY_SQL_RESPONSE``, a message about the response rather than about
    the cause.

    Keys that would let this field change *which model is asked what* are
    rejected at parse time rather than merged — see
    :data:`llm.providers.RESERVED_PAYLOAD_KEYS`. Everything else is the
    operator's business, not this module's."""

    llm_stop: tuple[str, ...] = field(default_factory=tuple)
    """Optional stop sequences appended to every request's ``stop`` field.
    Empty by default — most models terminate cleanly at the SQL statement's
    natural end; set this only if a specific model needs an explicit fence."""

    # ── Phase 2: query-result cache normalisation ───────────────────────────
    cache_normalize_questions: bool = field(
        default_factory=lambda: os.getenv("CACHE_NORMALIZE_QUESTIONS", "true").lower()
        not in ("0", "false", "no")
    )
    """When true (default), cache keys are built from a normalised form of
    the question (collapsed whitespace, Persian/Arabic digits folded to
    ASCII, ZWNJ stripped, ي/ك folded to ی/ک) so trivially different
    phrasings of the same question share a cache entry. See
    ``api.query_cache._normalize_question``."""

    # ── Phase 2: LLM router / endpoint-trust governance ─────────────────────
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "openai")
    )
    """One of ``"openai"`` (the default — route through :mod:`llm.endpoints`)
    or ``"mock"`` (every task answered by :class:`~llm.providers.MockBackend`,
    no endpoint configuration needed — for tests and offline runs). See
    ``llm.router.LLMRouter.from_settings``."""

    llm_allow_remote: bool = field(
        default_factory=lambda: os.getenv("LLM_ALLOW_REMOTE", "false").lower()
        in ("1", "true", "yes")
    )
    """Explicit per-deployment opt-in gate for sending schema, business
    rules, or query-result rows to an untrusted (not on this deployment's
    own infrastructure) LLM endpoint. This product's premise is "runs on
    your infrastructure" — pointing ``OPENAI_BASE_URL``/``LLM_ENDPOINTS``
    at a hosted API alone must never be sufficient to start exfiltrating
    data; this flag is the separate, deliberate switch, and every call to
    an untrusted endpoint made while it is true is written to the audit
    trail (see ``llm.router.LLMRouter``). Defaults to ``False``."""

    llm_trusted: bool | None = field(
        default_factory=lambda: (
            {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
            .get(os.getenv("LLM_TRUSTED", "").strip().lower())
        )
    )
    """Explicit trust override for the single ``"default"`` endpoint (see
    :attr:`openai_base_url`). ``None`` (unset — the default) defers to
    :func:`~llm.trust.default_trust_for_url`, which trusts loopback/private/
    ``.local`` addresses and nothing else. Set to ``true``/``false`` to
    override that heuristic explicitly — e.g. a local endpoint reachable
    under a public-looking hostname that should still count as trusted, or
    a loopback endpoint that should not. Endpoints declared in
    :attr:`llm_endpoints_json` each carry their own independent ``trusted``
    override instead of sharing this one."""

    llm_endpoints_json: str = field(
        default_factory=lambda: os.getenv("LLM_ENDPOINTS", "")
    )
    """Additional named endpoints beyond the trivial ``"default"`` one, as
    a JSON array of ``{"name", "base_url", "model", "api_key", "trusted"}``
    objects (``api_key``/``trusted`` optional). Empty (the default) means
    only ``"default"`` exists. See :mod:`llm.endpoints`."""

    llm_routes_json: str = field(
        default_factory=lambda: os.getenv("LLM_ROUTES", "")
    )
    """Per-task endpoint fallback chains, as a JSON object mapping a
    :class:`~llm.router.TaskType` value (``"sql_generation"``,
    ``"interpretation"``, ``"assumption_extraction"``) to a list of
    endpoint names tried in order. Empty (the default) routes every task to
    ``["default"]`` alone. See :mod:`llm.endpoints`."""

    llm_task_budget_seconds: float | None = field(
        default_factory=lambda: (
            float(os.getenv("LLM_TASK_BUDGET_SECONDS", ""))
            if os.getenv("LLM_TASK_BUDGET_SECONDS", "").strip()
            else None
        )
    )
    """Per-task latency budget (seconds) applied uniformly to every
    :class:`~llm.router.TaskType` by :meth:`~llm.router.LLMRouter.from_settings`.
    ``None`` (default, unset) disables the budget check entirely — see
    ``llm.router.LLMRouter._call_chain``."""

    # ── Phase 3: conversational sessions (docs/api-contract-v2.md §9) ──────
    session_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("SESSION_TTL_SECONDS", "1800"))
    )
    """Idle-expiry window (seconds) for an in-memory conversational session
    (``session.store.SessionStore``). A session not touched (no new turn,
    no transcript read) for this long is evicted, freeing its cached
    turns/memory. ``0`` disables sessions entirely (every lookup misses)."""

    session_max_count: int = field(
        default_factory=lambda: int(os.getenv("SESSION_MAX_COUNT", "500"))
    )
    """Maximum number of concurrent sessions kept in memory. Beyond this,
    the least-recently-active session is evicted to make room for a new
    one — the same LRU discipline ``api.query_cache.QueryCache`` already
    applies to cached responses."""

    session_max_turns: int = field(
        default_factory=lambda: int(os.getenv("SESSION_MAX_TURNS", "50"))
    )
    """Transcript cap per session. Beyond this many turns, the oldest turn
    (and its session-memory sidecar) is dropped from the session so a
    long-lived conversation cannot grow without bound."""

    session_prompt_turns: int = field(
        default_factory=lambda: int(os.getenv("SESSION_PROMPT_TURNS", "3"))
    )
    """How many of the most recent turns are rendered into the prompt's
    session-context block (question, SQL, result *column names* — never
    row data; see ``docs/api-contract-v2.md`` §8). Older turns fall out of
    the prompt window but remain readable via ``GET /v2/sessions/{sid}``."""

    refinement_scan_cap: int = field(
        default_factory=lambda: int(
            os.getenv(
                "REFINEMENT_SCAN_CAP",
                str(int(os.getenv("MAX_ROWS_RETURNED", "1000")) * 100),
            )
        )
    )
    """§2's inner-scan bound: when a refining turn drops the previous
    turn's display ``TOP`` to compute a result over *all* matching rows
    (not just the ones previously shown), the resulting unbounded scan is
    re-capped at this many rows instead of being left unbounded. Defaults
    to ``max_rows_returned * 100``, mirroring ``default_top_n``'s own
    fallback-to-``MAX_ROWS_RETURNED`` pattern above."""

    # ── Phase 9: session persistence + cross-session memory ─────────────────
    session_store_path: str = field(
        default_factory=lambda: os.getenv("SESSION_STORE_PATH", "logs/sessions.db")
    )
    """SQLite file backing :class:`session.persistence.SessionPersistence`
    (question, generated SQL, result column names/row_count/truncated, the
    ``TurnMemory`` sidecar, and memory entries — never result rows; see that
    module's docstring). Defaults to ``logs/sessions.db`` — ``*.db`` is
    already gitignored, and ``logs/`` is where the audit log already lives,
    so this introduces no new category of stored data. Empty string
    disables persistence entirely: ``session.store.SessionStore`` then
    behaves exactly as it did before this phase (TTL expiry deletes, no
    rehydration, ``GET /v2/sessions`` reflects only the in-memory hot set).
    Read once, at ``api.v2_routes.get_session_store``'s lazy-construction
    time, like every other singleton-constructor setting in this codebase
    (``session_ttl_seconds``, ``session_max_count``, ...)."""

    session_retention_days: int = field(
        default_factory=lambda: int(os.getenv("SESSION_RETENTION_DAYS", "30"))
    )
    """How long (days) a persisted conversation stays listable and
    reopenable — the third, previously-conflated lifetime alongside
    :attr:`session_prompt_turns` (the prompt window) and
    :attr:`session_ttl_seconds` (how long the in-memory record stays hot).
    ``session.store.SessionStore.purge_expired`` permanently deletes a
    persisted session past this many days since its ``last_active_at``;
    it does not affect the (much shorter) in-memory TTL, which only
    demotes a session out of memory, never deletes it, when persistence is
    attached."""

    session_title_max_length: int = field(
        default_factory=lambda: int(os.getenv("SESSION_TITLE_MAX_LENGTH", "80"))
    )
    """Cap on a session title's length — both the auto-derived title (the
    first question, truncated at a word boundary) and a title supplied via
    ``PATCH /v2/sessions/{sid}``. A title is presentation only: it is
    validated under the same length/control-character rules as a memory
    value (see :attr:`memory_value_max_length`) precisely because it is
    user text of the same shape, but it never enters a prompt."""

    memory_enabled: bool = field(
        default_factory=lambda: os.getenv("MEMORY_ENABLED", "true").lower()
        not in ("0", "false", "no")
    )
    """Master switch for cross-session memory (``docs/api-contract-v2.md``
    §5). When ``False``, a stored entry is never applied to a turn's
    assumptions/filters (``session.engine.TurnEngine.ask`` skips
    :func:`session.memory.apply_memory_to_assumptions` entirely) — the
    ``GET``/``PUT``/``DELETE /v2/memory*`` endpoints themselves are
    unaffected, so an operator can disable the feature's *effect* without
    losing an analyst's already-pinned entries."""

    memory_max_entries_per_principal: int = field(
        default_factory=lambda: int(os.getenv("MEMORY_MAX_ENTRIES_PER_PRINCIPAL", "20"))
    )
    """Cap on how many distinct memory keys one principal may have pinned
    at once. Exceeding it on ``PUT /v2/memory/{key}`` (a *new* key; updating
    an already-pinned key never counts against this cap) is an explicit
    422, never a silent eviction of an older entry — see
    ``docs/api-contract-v2.md`` §5."""

    memory_value_max_length: int = field(
        default_factory=lambda: int(os.getenv("MEMORY_VALUE_MAX_LENGTH", "120"))
    )
    """Default per-key cap on a memory value's length, in
    ``project_config/memory_policy.yaml``'s absence of a narrower
    ``max_length`` for that specific key. A memory value is untrusted text
    that reaches the prompt's variable suffix (never the static prefix),
    so it is also rejected outright for a newline or control character —
    see ``session.memory.validate_memory_value``."""

    cors_allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
        )
        or DEFAULT_CORS_ALLOWED_ORIGINS
    )
    """Origins allowed to call this API cross-origin (``CORSMiddleware`` in
    ``api/server.py``), comma-separated.

    Defaults to :data:`DEFAULT_CORS_ALLOWED_ORIGINS` — the loopback
    origins the bundled ``web/`` UI is documented to be served from.
    Setting the variable replaces that list entirely rather than adding
    to it, so a real deployment naming its own origin does not silently
    keep localhost allowed as well.

    Same-origin requests never need CORS at all, so this only matters for
    the split-origin layout ``web/README.md`` and
    ``docs/fa/getting-started.md`` describe: the API on one port, the
    static UI on another."""

    llm_structured_output: bool = field(
        default_factory=lambda: os.getenv("LLM_STRUCTURED_OUTPUT", "false").lower()
        in ("1", "true", "yes")
    )
    """Phase 2 task 3 feature flag: when ``True``, SQL generation asks for
    a single constrained JSON object (``{"sql", "out_of_scope",
    "confidence", "assumptions"}``, see ``llm.structured_schema``) instead
    of free text plus ``clean_sql`` regex surgery. Defaults to ``False``
    until the golden-set evaluation (``python -m eval.cli run --live
    --structured``) demonstrates it does not regress accuracy — see the
    Phase 2 report for whether that evaluation has been run yet."""

    # ── Deployment readiness: per-(principal, ip) rate limiting ─────────────
    # Previously ``api/middleware.py``'s own module-level ``os.getenv()``
    # reads, evaluated once at that module's import time -- which is why
    # ``tests/conftest.py`` had to set these env vars *before* anything
    # imported ``api.middleware`` at all (see that file's own comment).
    # Moved here so the values are read through ``cfg.settings`` at call
    # time like every other tuning knob (see this module's "Three layers,
    # not two" section) -- ``RateLimitMiddleware.__init__`` now resolves
    # them itself when it is actually constructed, so
    # ``config.override_settings(rate_limit_requests=...)`` reaches a
    # middleware instance built with no explicit constructor kwargs, not
    # just one a test hand-configures. See ``api/middleware.py``'s module
    # docstring for why the *shared* ``api.server.app`` instance still
    # needs its generous test-suite value in place before the first
    # request of a whole pytest session, not merely "at some point before
    # a given test" -- Starlette builds and caches that app's middleware
    # stack exactly once, on the first request it ever serves.
    rate_limit_requests: int = field(
        default_factory=lambda: int(os.getenv("RATE_LIMIT_REQUESTS", "600"))
    )
    """Sustained request allowance per :attr:`rate_limit_window_seconds`,
    per rate-limit bucket (see :attr:`rate_limit_burst` for capacity above
    this, and ``api.middleware.RateLimitMiddleware._bucket_key`` for what a
    "bucket" is).

    Raise this if legitimate traffic is getting 429s under normal use
    (more concurrent analysts, or a UI that legitimately fires several
    requests per interaction); lower it if a single caller should be
    throttled harder than this — e.g. a deployment that expects only a
    handful of analysts and wants a tighter ceiling on a misbehaving
    client or a scripting mistake.

    Old default was ``60`` — chosen back when the bucket was keyed on raw
    IP alone. In the shape this product actually ships in (one web UI,
    one shared service key, ten-plus analysts sitting behind it), the
    bucket key becomes ``principal:<id>|ip:<ip>`` (see
    ``RateLimitMiddleware._bucket_key``'s docstring) but a single web UI
    still means every analyst using it shares **one IP** too — so ``60``
    req/min was, in practice, 60 requests per minute for the *entire
    organisation*, not per analyst. Ten people each asking one question
    every few seconds blew through it. ``600`` (10 req/sec sustained)
    assumes up to roughly 30 analysts each firing an interactive query —
    a human clicking "ask" every few seconds, not a batch client — at
    once: 30 analysts x one request every 3s is exactly 600/min. A
    deployment with a different expected head-count should raise or
    lower this proportionally rather than trust the default blindly."""

    rate_limit_window_seconds: float = field(
        default_factory=lambda: float(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
    )
    """Length (seconds) of the sliding window :attr:`rate_limit_requests`
    refills over. Unchanged from the original default (``60``) — the
    *rate* (:attr:`rate_limit_requests` per this many seconds) is what
    needed raising for real deployment traffic, not the window length
    itself; a shorter window makes the same requests/window ratio bursts
    tighter over any sub-window, a longer one loosens it, but ``60`` is a
    reasonable "per minute" unit for a human operator reading a 429 body
    to reason about either way."""

    rate_limit_burst: int = field(
        default_factory=lambda: int(os.getenv("RATE_LIMIT_BURST", "40"))
    )
    """Extra tokens above :attr:`rate_limit_requests` a bucket can spend
    instantly (bucket capacity = ``rate_limit_requests + rate_limit_burst``
    — see ``api.middleware.RateLimitMiddleware``). Old default was ``10``.
    Raised to ``40`` alongside :attr:`rate_limit_requests`'s five-fold
    increase so the burst allowance scales with it — enough to absorb a
    web UI firing a handful of parallel calls on one page load (e.g. an
    initial dashboard fetch) without immediately eating into the
    sustained rate, while staying far short of "unlimited": a genuinely
    runaway or scripted caller still exhausts 640 tokens (600 + 40) in
    well under a minute at any real request rate and starts getting 429s,
    it just is not punished for one legitimate burst."""

    # ── Admin panel, phase 2: the application database ──────────────────────
    app_db_url: str = field(
        default_factory=lambda: os.getenv("APP_DB_URL", "")
    )
    """A SQLAlchemy URL for the application database — the key store, role
    grants, and (see :mod:`session.persistence`) conversational sessions.
    Empty (the default) falls back to a SQLite file at
    :attr:`app_db_sqlite_path`, created automatically. A configured value
    is expected to name a database that **already exists** — this
    application creates tables inside it, never the database itself (see
    ``docs/admin-panel-architecture.md`` §5.3): ``CREATE DATABASE`` needs
    rights a DBA will not grant an application.

    Must never resolve to the same server+database as
    :attr:`db_connection_url` — or, with :mod:`database.datasources`
    configured (``project_config/datasources.yaml``), any configured
    warehouse data source — :func:`appdb.engine.raise_if_same_database` is
    checked at start-up (``api/server.py``'s ``lifespan``, once per
    source) and refuses to start otherwise, because every warehouse
    connection is deliberately read-only (``docs/db-hardening.md``) and
    the application database needs writes."""

    app_db_sqlite_path: str = field(
        default_factory=lambda: os.getenv("APP_DB_SQLITE_PATH", "logs/app.db")
    )
    """SQLite file used when :attr:`app_db_url` is unset. Defaults to
    ``logs/app.db`` — alongside ``logs/sessions.db`` and the audit log,
    introducing no new category of stored-data location. ``*.db`` is
    already gitignored (see ``tests/test_no_runtime_artifacts_tracked.py``)."""

    key_cache_ttl_seconds: float = field(
        default_factory=lambda: float(os.getenv("KEY_CACHE_TTL_SECONDS", "5.0"))
    )
    """How long (seconds) the in-memory API-key cache
    (:mod:`appdb.key_store`) serves a database-backed key set before
    re-querying. Keys move into the application database precisely so a
    revoked/disabled key can be shut off without a restart
    (``docs/admin-panel-architecture.md`` §5.5/§5.6) — reading the key
    table on every single request would mean a network round trip to the
    application database per request, so this cache exists to avoid that.
    Every mutation (issue/disable/enable/revoke/ACL change/role grant)
    invalidates the cache explicitly rather than waiting out this TTL, so
    it bounds only the *unforced* staleness window — a revocation always
    takes effect on the very next request regardless of this value. Kept
    short by default because it is a safety-relevant staleness bound, not
    a raw performance knob; raise it only if the application database is
    under measured load from key lookups alone."""

    # ── Phase 8: API-key authentication ─────────────────────────────────────
    api_keys_json: str = field(
        default_factory=lambda: os.getenv("API_KEYS_JSON", "")
    )
    """JSON array of ``{"id", "name", "key_sha256", "denied_columns"?}``
    objects — the deployment's configured API keys. Only the SHA-256 hex
    digest of a key is ever stored here, never the raw key itself; issue
    new keys with ``scripts/issue_api_key.py``. Parsed at call time by
    :func:`security.auth.load_api_keys`, per this module's
    read-through-``cfg.settings``-at-call-time convention. Empty (the
    default) means no caller can authenticate — see :attr:`auth_required`
    for what that implies at startup. A multi-line value only survives
    ``.env`` wrapped in single quotes with no apostrophe inside, so for more
    than one key prefer :attr:`api_keys_file`. Setting both is refused."""

    api_keys_file: str = field(
        default_factory=lambda: os.getenv("API_KEYS_FILE", "")
    )
    """Path to a file holding exactly what :attr:`api_keys_json` holds: the
    JSON array of key objects, in any formatting, with no ``.env`` quoting
    rules to trip over. Recommended location: ``project_config/api_keys.json``
    (``project_config/`` is git-ignored). A relative path resolves against the
    repository root, like ``PROJECT_CONFIG_DIR``, not the working directory.
    Empty (the default) means unused. Read once per process, like an
    environment variable, so edits need a restart. A missing or unreadable
    file, invalid JSON, a repeated key inside one object, or both this and
    :attr:`api_keys_json` being set is refused at startup. Read by
    :func:`security.auth.load_api_keys`."""

    auth_required: bool = field(
        default_factory=lambda: os.getenv("AUTH_REQUIRED", "true").lower()
        not in ("0", "false", "no")
    )
    """Whether every non-``/health`` route requires a valid API key.
    Defaults to ``True`` — this is a fail-closed system, not a fail-open
    one. Setting this to ``False`` is a deliberate escape hatch (local
    development, an isolated network with its own perimeter security)
    that ``api/server.py``'s ``lifespan`` logs a ``WARNING`` for on
    *every* startup, not just the first, so a silently-disabled front
    door is never quiet in the logs. When ``True`` and :attr:`api_keys_json`
    / :attr:`api_keys_file` resolve to zero configured keys, ``lifespan``
    raises ``RuntimeError`` instead of starting a server nobody could ever
    authenticate to."""

    app_docs_public: bool = field(
        default_factory=lambda: os.getenv("APP_DOCS_PUBLIC", "false").lower()
        in ("1", "true", "yes")
    )
    """When ``False`` (the default), ``/docs``, ``/redoc``, and
    ``/openapi.json`` require the same authentication as every other
    non-``/health`` route — the generated API documentation describes
    exactly what a caller can do to production data, which is not
    something to publish to an unauthenticated network. Set ``True`` to
    serve them without credentials (e.g. a deployment that already sits
    behind its own perimeter auth)."""

    # ── Phase 5b: deterministic value resolution ────────────────────────────
    resolve_value_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("RESOLVE_VALUE_TIMEOUT_SECONDS", "2.0"))
    )
    """Per-(table, column) deadline for ``retrieval.value_resolver.resolve_value``'s
    database round trip. Deliberately much tighter than
    :attr:`query_timeout_seconds` (which bounds a full analytic query) —
    this is a small lookup on the critical path *before* SQL generation
    even starts, so a breach falls back to the no-match path (see
    ``resolve_value``'s docstring) rather than blocking the request for as
    long as a real query is allowed to run."""

    resolve_value_max_concurrency: int = field(
        default_factory=lambda: int(os.getenv("RESOLVE_VALUE_MAX_CONCURRENCY", "8"))
    )
    """How many value-resolution queries may be in flight at once,
    process-wide. This is the bound a ``ThreadPoolExecutor``'s
    ``max_workers`` used to provide, before
    ``retrieval.value_resolver._run_under_deadline`` replaced that pool
    with daemon threads (see its docstring for why it had to). Read at
    call time, so it responds to ``override_settings``. Waiting for a
    free slot spends the caller's own
    :attr:`resolve_value_timeout_seconds`: a saturated resolver must
    report a miss on time rather than queue past its deadline."""

    resolve_value_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("RESOLVE_VALUE_CACHE_TTL_SECONDS", "300"))
    )
    """How long (seconds) a cached entity-value resolution stays valid.
    ``<= 0`` disables the cache. Dimension tables (customers, brokers,
    symbols, ...) change slowly, so this can reasonably sit much longer
    than an individual request without serving stale data in practice."""

    resolve_value_cache_max_size: int = field(
        default_factory=lambda: int(os.getenv("RESOLVE_VALUE_CACHE_MAX_SIZE", "512"))
    )
    """Maximum number of distinct ``(mention, table, column, scope_key)``
    resolutions kept in memory before LRU eviction — mirrors
    :attr:`cache_max_size`'s discipline for the unrelated query-result
    cache."""

    dimension_vocabulary_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("DIMENSION_VOCABULARY_TTL_SECONDS", "3600"))
    )
    """How long (seconds) a small dimension's prefetched value set
    (``retrieval.dimension_vocabulary``) is served without triggering a
    refresh. Unlike :attr:`resolve_value_cache_ttl_seconds`, this is
    **not** a per-request latency knob and, since the stale-while-revalidate
    redesign, it is **not an expiry either**: a cached value past this TTL
    is still served (a stale trading-hall or commodity name beats no name
    at all) while a background refresh for it is triggered — see that
    module's docstring's "Stale-while-revalidate, warmed lazily" section.
    ``<= 0`` makes every read stale immediately (a background refresh
    fires on every match that consults it, rate-limited on failure by
    :data:`retrieval.dimension_vocabulary._BACKGROUND_REFRESH_BACKOFF_SECONDS`),
    which is the closest this module comes to "disabled" — it still stores
    and serves whatever was last fetched rather than discarding it."""

    dimension_vocabulary_warm_on_startup: bool = field(
        default_factory=lambda: os.getenv(
            "DIMENSION_VOCABULARY_WARM_ON_STARTUP", "true",
        ).lower() in ("1", "true", "yes")
    )
    """When ``True``, ``api/server.py``'s ``lifespan`` calls
    ``retrieval.dimension_vocabulary.warm_all`` once during startup, before
    the server begins accepting requests.

    Its meaning changed with the stale-while-revalidate redesign: the
    vocabulary cache is no longer inert without this flag — a cold or
    stale entry now self-heals via a background refresh triggered from the
    request path itself (never awaited, so no request blocks on it; see
    ``retrieval.dimension_vocabulary``'s module docstring). This flag is
    now a pure **optimisation**: it pre-pays the very first cold-cache miss
    for each dimension at startup instead of leaving it to whichever
    request happens to ask about that dimension first. A deployment that
    turns this off still gets working, self-healing dimension resolution
    — just with one extra miss per dimension after each restart, and (per
    the TTL note above) after each TTL expiry regardless.

    Defaults to ``True``: every restart without
    it reproduces "the first question that mentions any prefetched
    dimension after a cold start silently drops that filter" (self-healing
    only for the *next* question, never the one that triggered the
    refresh) — a real, reported production symptom, not a theoretical one.
    This codebase's test suite is unaffected either way: it builds a
    ``TestClient(app)`` against no live database, and ``warm_all`` catches
    and logs a failure per column rather than raising (see below), so a
    startup warm-up attempt against the tests' unmocked/refused connection
    degrades to the same "no match" cache state a disabled flag would
    leave it in, not a test failure. Set this back to ``false`` only to
    restore the old opt-in behaviour (e.g. a deployment whose startup path
    must not touch the database at all). A warm-up failure (e.g. the
    database is unreachable at startup) is logged as a warning, never
    raised — unlike the auth/config checks above this one, an empty
    vocabulary cache degrades every dimension it would have covered to "no
    match" (this phase's universal safe-miss behaviour), not a server that
    cannot start at all."""

    dimension_vocabulary_token_fallback_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "DIMENSION_VOCABULARY_TOKEN_FALLBACK_ENABLED", "true",
        ).lower() in ("1", "true", "yes")
    )
    """When ``True``, ``retrieval.dimension_vocabulary.match_question_against_vocabulary``
    falls back to a distinctive-token score (see that module's docstring,
    "Matching rules") for a table whose cached value never appears as one
    exact contiguous substring of the question — a hall/currency/etc.
    named by only part of its stored value (an inserted word, a different
    modifier order) rather than the exact string.

    Defaults to ``True`` (confirmed root cause:
    "dimension_vocabulary matching requires the ENTIRE cached value to
    appear as one contiguous substring... no token/partial matching" — the
    concrete mechanism behind the reported "the hall named in the question
    is ignored" complaint). The fix is additive and cannot regress an
    already-working exact match (it only ever runs after that pass finds
    nothing), and its scoring only ever counts a value's *distinctive*
    tokens (excluding whatever token is common to most of that column's
    values), with a tie between two distinct values going to the exact
    same clarification path an ambiguous exact match already uses rather
    than a silent guess. Set to ``false`` only if a real deployment's
    dimension values are short/generic enough that this trades away too
    many clean matches for clarifications, or — worst case — a match
    against a value whose distinctive words are scattered through an
    unrelated question."""

    # ── Evaluation harness: golden-set regression gate (see eval/baseline.py) ─
    eval_max_accuracy_drop_pct: float = field(
        default_factory=lambda: float(os.getenv("EVAL_MAX_ACCURACY_DROP_PCT", "5.0"))
    )
    """Percentage-point drop in ``accuracy_pct`` (current vs baseline) above
    which ``eval.baseline.compare_to_baseline`` treats a run as regressed.
    E.g. ``5.0`` means baseline 90% -> current 84% (a 6-point drop) fails,
    but baseline 90% -> current 86% (a 4-point drop) does not. A per-deployment
    tuning knob rather than a fixed technical fact: how much accuracy
    variance is tolerable is a product decision that can reasonably differ
    across warehouses. Still overridable per invocation via
    ``python -m eval.cli run --max-accuracy-drop-pct``, which takes
    precedence when passed explicitly."""

    eval_max_latency_p95_increase_pct: float = field(
        default_factory=lambda: float(os.getenv("EVAL_MAX_LATENCY_P95_INCREASE_PCT", "20.0"))
    )
    """Relative percentage increase in ``latency_p95`` (current vs baseline)
    above which a run is considered regressed. E.g. ``20.0`` means a
    baseline p95 of 2.0s tolerates up to 2.4s before failing. Slower or
    more variable hardware legitimately wants a looser tolerance here than
    a deployment on fast, dedicated hardware -- this is the textbook
    "how aggressively to retry/tolerate for *this* hardware" tuning knob.
    Overridable per invocation via
    ``python -m eval.cli run --max-latency-p95-increase-pct``."""

    eval_max_guard_rejection_increase: int = field(
        default_factory=lambda: int(os.getenv("EVAL_MAX_GUARD_REJECTION_INCREASE", "0"))
    )
    """Absolute increase in ``guard_rejections`` (current vs baseline) above
    which a run is considered regressed. Defaults to ``0`` -- any new guard
    rejection versus baseline is treated as safety-relevant and flagged,
    since it means SQL that used to pass the security guard no longer does
    (or the generator started producing worse SQL). Overridable per
    invocation via ``python -m eval.cli run --max-guard-rejection-increase``."""

    eval_golden_path: str = field(
        default_factory=lambda: os.getenv("EVAL_GOLDEN_PATH", "eval_data/golden.jsonl")
    )
    """Path to the golden-set ``.jsonl`` file the admin panel's config-version
    dry-run (``docs/admin-panel-architecture.md`` §6.2, phase 3 spec §5)
    runs a candidate ``project_config/`` bundle against before it can be
    applied. Read at call time by :mod:`appdb.config_versions`, the same
    ``eval_data/golden.jsonl`` a deployment already maintains for
    ``python -m eval.cli run`` -- this setting exists so the panel can find
    it without a request body needing to name a filesystem path. Point this
    at ``eval_data.example/golden.jsonl`` (alongside ``PROJECT_CONFIG_DIR``)
    to run the dry-run against the committed example data instead."""

    eval_baseline_path: str = field(
        default_factory=lambda: os.getenv("EVAL_BASELINE_PATH", "eval_data/baseline.json")
    )
    """Path to the baseline JSON file ``python -m eval.cli run --save-baseline``
    writes (:mod:`eval.baseline`). Read at call time by the admin panel's
    feedback-loop stats endpoint (``docs/admin-panel-architecture.md`` §3's
    "closing the loop visibly" -- phase 4 spec §5) to show the golden set's
    most recently recorded accuracy alongside its size and flag volume,
    without re-running the harness against a live endpoint on every panel
    load. A missing file (no baseline has been recorded yet) degrades to
    "no baseline recorded" rather than an error -- this setting exists so
    the panel can find the file without a request needing to name a
    filesystem path, the same reasoning as ``eval_golden_path`` above."""

    config_version_cache_ttl_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("CONFIG_VERSION_CACHE_TTL_SECONDS", "5")
        )
    )
    """How long :mod:`appdb.config_versions` may reuse a cached active
    version id before re-reading it, mirroring ``key_cache_ttl_seconds``
    and existing for the same reason: the id is on the per-request path
    (``api/runner.py`` folds it into every query-cache key, so an applied
    configuration version invalidates stale answers), and reading it from
    the application database on every question would be a network round
    trip per request on an external backend.

    Applying a version invalidates this cache explicitly, so a change is
    visible immediately in the process that made it; the TTL is what bounds
    staleness in *other* processes, where the only cost of being late is a
    few cache hits that should have been misses. Set to ``0`` to disable
    the cache and read the id every time."""

    config_export_dir: str = field(
        default_factory=lambda: os.getenv("CONFIG_EXPORT_DIR", "")
    )
    """Directory :mod:`appdb.config_versions` writes the ``project_config/``
    YAML bundle to on every applied version (spec §7) -- offline inspection
    with familiar tools, and an off-box backup, without this system's
    correctness depending on git being installed. Empty (the default)
    disables the export write entirely; a deployment that wants it points
    this at a directory of its own -- optionally one under version control
    with its own remote (``docs/admin-panel-architecture.md`` §6.3: "git as
    an output, not as the engine" -- never this project's own repository)."""

    # ── Admin panel, phase 5: migration between backends ────────────────────
    migration_quiet_window_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("MIGRATION_QUIET_WINDOW_SECONDS", "60")
        )
    )
    """How recent a write to the application database may be before
    :mod:`appdb.migrate` refuses to run (``docs/admin-panel-architecture.md``
    §5.4/§5.4.1, §3 tier 3's not-yet-built maintenance mode). The tool scans
    every migrated table's own timestamp columns (``created_at``,
    ``updated_at``, ...) for the most recent one and compares its age
    against this window; anything younger means the application was still
    writing a moment ago and the copy this tool is about to take may not
    include everything, with no error at the time it is lost (§7).

    ``60`` is a deliberately generous default: this check exists to catch
    "the operator forgot to stop the application", not to shave seconds off
    a maintenance window, and a false refusal here costs an operator one
    re-run a minute later while a false pass risks silently dropped writes.
    A deployment confident in its own shutdown discipline may lower it; one
    whose application takes longer than a minute to fully quiesce should
    raise it instead of routinely overriding the refusal by other means."""

    # ── Admin panel, phase 6: the operational tier ───────────────────────────
    maintenance_drain_deadline_seconds: int = field(
        default_factory=lambda: int(os.getenv("MAINTENANCE_DRAIN_DEADLINE_SECONDS", "120"))
    )
    """How long (seconds) an operator should expect in-flight requests to
    take to drain after maintenance mode is switched on (``api.maintenance``).

    Purely informational: this codebase has no mechanism that forcibly
    cancels a request already past its admission check (see
    ``api.maintenance.require_not_in_maintenance``'s docstring for why
    "drain" falls out of *where* the check runs, not from any cancellation
    logic) — killing a query mid-execution buys nothing and loses the
    analyst their answer, per ``docs/admin-panel-architecture.md``'s
    maintenance-mode definition. This value is returned alongside the
    toggle-on response so an operator has a concrete number to watch
    rather than guessing: if every in-flight request has not finished by
    the time this many seconds have passed, treat that as a hang worth
    investigating (a stuck LLM call, a wedged database connection), not as
    evidence the switch failed. ``120`` is deliberately generous — comfortably
    above :attr:`query_timeout_seconds` plus the self-correction rounds a
    single query may spend."""

    admin_action_log_retention_days: int = field(
        default_factory=lambda: int(os.getenv("ADMIN_ACTION_LOG_RETENTION_DAYS", "365"))
    )
    """How many days of ``admin_action_log.jsonl`` history
    (:mod:`appdb.admin_audit`) are kept before :func:`appdb.admin_audit.purge_expired_admin_actions`
    discards a record permanently. ``<= 0`` disables the purge entirely —
    keep everything forever, the safest choice for a compliance-sensitive
    deployment that would rather manage disk space by hand than lose
    evidence automatically.

    This log is exempt from the size-based rotation every other JSONL log
    in this project uses (:attr:`log_max_bytes`/:attr:`log_backup_count`)
    precisely so this setting is the ONLY thing that can make an admin
    action disappear. Size-based rotation discards the *oldest* evidence
    first, exactly when there is the *most* activity — which means anyone
    wanting to bury a specific admin action could do so on purpose by
    generating enough unrelated admin noise to roll it off the end of the
    file before anyone reads it. A trail whose whole purpose is "each
    admin role can read that the other one acted"
    (``docs/admin-panel-architecture.md`` §2.4) cannot depend on a
    retention mechanism an interested party can defeat by volume. Retention
    by time closes that path: a record is discarded only once it is
    genuinely older than this many days, never because something noisier
    was appended after it. An on-prem deployment with an externally
    imposed retention requirement sets this explicitly rather than
    accepting the default; see ``docs/deployment-runbook.md``."""

    # ── Admin panel, phase 6: a small, separate bucket for auth FAILURES ────
    # See docs/admin-panel-architecture.md §9's resolved "is IP alone
    # enough for failed admin auth?" question, and
    # api.middleware.RateLimitMiddleware's own docstring for how this
    # bucket is kept separate from the shared, much larger
    # rate_limit_requests bucket every other unauthenticated/authenticated
    # request draws from.
    auth_failure_rate_limit_requests: int = field(
        default_factory=lambda: int(os.getenv("AUTH_FAILURE_RATE_LIMIT_REQUESTS", "20"))
    )
    """Sustained allowance, per window, for requests that presented an
    ``Authorization`` header that failed to resolve to a principal at all
    (a wrong/expired/typo'd key — never a *missing* header, which is
    ordinary unauthenticated traffic and keeps using
    :attr:`rate_limit_requests`'s much larger shared bucket).

    Deliberately small and deliberately separate. Guessing a real key is
    arithmetically impossible (``security.auth``'s keys are
    ``secrets.token_urlsafe(32)`` — 256 bits of entropy), so this is not
    a brute-force defence; tightening it further would be security
    theatre against a threat that does not exist. The actual problem this
    solves: ``api.auth.AuthMiddleware`` runs before the rate limiter, so a
    single misbehaving client looping on a stale/rotated key would
    otherwise consume tokens from the *same* IP-keyed bucket that
    genuinely unauthenticated traffic from that address (a monitoring
    probe behind the same reverse proxy, most concretely) also draws
    from — starving the probe and making the resulting 429 look exactly
    like an outage. A small, separate ceiling for auth failures protects
    that shared budget without touching the limit that actually matters
    for real traffic."""

    auth_failure_rate_limit_window_seconds: float = field(
        default_factory=lambda: float(os.getenv("AUTH_FAILURE_RATE_LIMIT_WINDOW_SEC", "60"))
    )
    """Window (seconds) :attr:`auth_failure_rate_limit_requests` refills
    over — same unit as :attr:`rate_limit_window_seconds`, kept as a
    separate setting so the two buckets' windows can be tuned
    independently."""

    auth_failure_rate_limit_burst: int = field(
        default_factory=lambda: int(os.getenv("AUTH_FAILURE_RATE_LIMIT_BURST", "5"))
    )
    """Extra tokens above :attr:`auth_failure_rate_limit_requests` a single
    auth-failure bucket may spend instantly — mirrors
    :attr:`rate_limit_burst`'s role for the shared bucket, sized much
    smaller to match."""

    def validate(self) -> None:
        """Raise ValueError if any required setting is missing or still a placeholder.

        A ``.env`` line python-dotenv could not use (unparsable, a leftover
        line of a broken multi-line value, or a variable assigned twice with
        different values) is refused first, listing every such line by number
        and variable name -- never by value -- because the settings read below
        may be wrong for exactly that reason. See :mod:`core.dotenv_check`.

        Every warehouse connection string is checked by
        :func:`_check_warehouse_url`: ``db_connection_url`` (with
        ``db_password`` applied) when there is no ``datasources.yaml``,
        otherwise each data source's connection (see
        :mod:`database.datasources`). A structured source whose
        ``username_env`` / ``password_env`` variable is unset or empty is
        refused, naming the source and the variable.
        """
        if _dotenv_problems:
            from core.dotenv_check import format_problems

            raise ValueError(format_problems(_dotenv_path, _dotenv_problems))
        placeholders = {
            "your_password_here", "your_server_here",
            "your_db_here", "change_me", "",
        }
        if not self.openai_model or self.openai_model in placeholders:
            raise ValueError("OPENAI_MODEL is not configured")
        # Fail closed for an unconfigured/unsupported SQL_DIALECT -- an
        # unknown dialect, or one whose profile has no system-catalogue
        # blocklist, must never reach request-serving code (see
        # security.dialects.require_dialect_supported's docstring for why
        # an empty blocklist is refused rather than treated as "nothing to
        # block"). Deferred import: security.dialects has no dependency on
        # config, but importing it at module scope here would still make
        # every `import config` pay for importing security.dialects even
        # when nothing ever calls validate() -- matches this module's own
        # "read at call time" convention for cross-module dependencies.
        from security.dialects import require_dialect_supported

        require_dialect_supported(self.sql_dialect)

        # Every warehouse connection string gets the same checks: the
        # single DB_CONNECTION_URL when there is no datasources.yaml, or
        # each source's connection when there is one (in which case
        # DB_CONNECTION_URL itself is not used and not required). See
        # database.datasources.
        from database.datasources import validate_datasource_urls

        validate_datasource_urls(
            lambda label, url, dialect: _check_warehouse_url(label, url, dialect, placeholders),
            self,
            placeholders,
        )

        # LLM_EXTRA_BODY is parsed on every request, so a malformed value
        # would otherwise surface as an error on the first question rather
        # than at start-up -- and the operator most likely to set it is one
        # who just watched a reasoning model return nothing and is trying to
        # turn that off. Failing here means a typo is caught by the same
        # pre-flight run (scripts/verify_deployment.py) that already proves
        # the rest of the configuration, instead of by an analyst.
        # Deferred import for the same reason as the two above: llm.providers
        # imports this module.
        from llm.providers import load_extra_body

        load_extra_body()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (cached after first call)."""
    return Settings()


# Module-level singleton — ALL modules must access settings as ``cfg.settings``
# (i.e. ``import config as cfg``) so that override_settings() patches are
# visible at call-time rather than being captured at import-time.
settings: Settings = get_settings()


@contextmanager
def override_settings(**kwargs: Any) -> Generator[Settings, None, None]:
    """Context manager for tests: temporarily replace ``cfg.settings``.

    Because all consumers read ``cfg.settings`` lazily (not via a local
    ``from config import settings`` binding), every module sees the new
    value for the lifetime of the ``with`` block.

    The original singleton is restored on exit, even on exception.

    Usage::

        import config as cfg
        from config import override_settings

        with override_settings(max_rows_returned=5) as s:
            assert s.max_rows_returned == 5
            assert cfg.settings.max_rows_returned == 5
    """
    import config as _cfg  # always the real module object
    original = _cfg.settings
    patched  = Settings(**{
        **{f: getattr(original, f) for f in original.__slots__},
        **kwargs,
    })
    _cfg.settings = patched
    try:
        yield patched
    finally:
        _cfg.settings = original
