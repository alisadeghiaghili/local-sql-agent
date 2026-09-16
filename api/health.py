# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Health-check logic — probes the OpenAI-compatible LLM endpoint and the SQL Server database.

Exposed via ``GET /health``.  Returns a :class:`~api.models.HealthResponse`
with an overall status of ``"ok"``, ``"degraded"``, or ``"down"`` plus
per-component boolean flags.

Typical usage::

    from api.health import check_health

    resp = check_health()
    # HealthResponse(status='ok', openai=True, database=True, model='gpt-oss-20b')

Result caching (Finding 3, 2026 audit)
---------------------------------------
Every call used to run both probes for real: two ~5 s network round trips,
the database one checking a connection out of the same pool ``/query``
serves from. ``/health`` needs no credentials and ``api/middleware.py``
deliberately keeps it exempt from rate limiting (liveness checks must
never be throttled away, or an orchestrator kills a healthy container) --
which meant an unauthenticated caller could hold the pool open for
seconds at a time with no limit on how often. Measured live during the
audit: fifteen concurrent unauthenticated ``/health`` calls finished in
3.65 s rather than 30 s, i.e. all fifteen ran their probes in parallel,
each holding a pool connection for the duration.

Rate-limiting was the wrong lever (see the comment above ``_EXEMPT_PATHS``
in ``api/middleware.py``); caching is the right one. ``check_health()`` now
probes at most once per :data:`HEALTH_CACHE_TTL_SECONDS` and every caller
inside that window — sequential or concurrent — gets the same cached
:class:`~api.models.HealthResponse`. ``_cache_lock`` is held for the full
probe, not just the cache read/write, so a burst that arrives while the
cache is cold collapses onto a single in-flight probe instead of each
caller racing to start its own; that is deliberate, not an oversight —
the whole point is that the pool sees at most one probe per TTL no matter
how many callers show up at once.

``reset_health_cache()`` exists purely for tests: without a way to force a
cold cache, one test's warm result would leak into the next and hide a
real regression.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

import config as cfg
from api.models import HealthResponse

logger = logging.getLogger(__name__)

#: HEALTH_CACHE_TTL_SECONDS lives on config.Settings
#: (:attr:`config.Settings.health_cache_ttl_seconds`), env-overridable via
#: ``HEALTH_CACHE_TTL_SECONDS``, per this project's "tuning knobs live in
#: Settings, not as bare module constants" rule (config.py's "Three
#: layers, not two") -- the same rule ``RATE_LIMIT_REQUESTS`` and
#: ``LOG_MAX_BYTES`` already follow. It is re-exposed as a plain module
#: attribute below via ``__getattr__`` (PEP 562, the same lazy-loader
#: pattern ``knowledge/aliases.py`` and friends already use in this
#: codebase) purely so existing call sites and tests reading
#: ``health.HEALTH_CACHE_TTL_SECONDS`` as a constant keep working -- every
#: access re-reads ``cfg.settings`` at call time, it is never captured
#: once at import time.
def __getattr__(name: str):
    if name == "HEALTH_CACHE_TTL_SECONDS":
        return cfg.settings.health_cache_ttl_seconds
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: Guards both the cached fields below AND the probe calls themselves --
#: see "Result caching" above for why holding it across the probe (rather
#: than just the read/write of the cache) is the point, not a bug.
_cache_lock = threading.Lock()
_cached_response: HealthResponse | None = None
#: A `time.monotonic()` timestamp, never wall-clock time -- monotonic is
#: immune to NTP adjustments and DST jumps stepping the clock backwards,
#: either of which could otherwise freeze this cache indefinitely. ``None``
#: (rather than ``0.0``) means "no cached value yet" -- the sentinel
#: :func:`check_health` actually branches on is ``_cached_response is not
#: None``, so this field's own starting value is never read before both
#: are set together; ``None`` is used anyway, matching
#: ``_cached_response``'s own sentinel, so this stays a plain state flag
#: rather than a numeric literal a reader (or this repo's own
#: tuning-layer guard, ``tests/test_tuning_layer.py``) might mistake for
#: a configuration default.
_cache_expires_at: float | None = None


def reset_health_cache() -> None:
    """Force the next :func:`check_health` call to probe for real.

    Test-only seam. Without it, whichever test runs first would warm the
    module-level cache and every later test would silently observe its
    stale result instead of exercising its own monkeypatched probes --
    a passing suite that proves nothing.
    """
    global _cached_response, _cache_expires_at
    with _cache_lock:
        _cached_response = None
        _cache_expires_at = None


def check_health() -> HealthResponse:
    """Probe all external dependencies and return an aggregated health status.

    Runs two synchronous probes sequentially:

    1. :func:`_ping_openai` — HTTP GET to the configured OpenAI-compatible
       endpoint's ``/models`` (5 s timeout).
    2. :func:`_ping_db` — ``SELECT 1`` via the shared SQLAlchemy engine.

    Combined worst-case latency is ~10 seconds (two back-to-back 5 s
    timeouts in the fully-down case).

    Returns
    -------
    HealthResponse
        A dataclass with the following fields:

        * ``status`` (``str``) —

          * ``"ok"``       — both the LLM endpoint and the database are reachable.
          * ``"degraded"`` — exactly one dependency is reachable.
          * ``"down"``     — neither dependency is reachable.

        * ``openai``   (``bool``) — ``True`` if the endpoint responded with HTTP 200.
        * ``database`` (``bool``) — ``True`` if ``SELECT 1`` executed without error.
        * ``model``    (``str``)  — ``cfg.settings.openai_model`` at call time.

    Examples
    --------
    Probing real dependencies needs a live endpoint and database, so the
    call is skipped here — and so are the assertions about its result,
    which would otherwise raise ``NameError`` on an unbound ``resp``.

    >>> resp = check_health()                          # doctest: +SKIP
    >>> resp.status in ("ok", "degraded", "down")      # doctest: +SKIP
    True
    >>> isinstance(resp.openai, bool)                  # doctest: +SKIP
    True
    >>> isinstance(resp.database, bool)                # doctest: +SKIP
    True
    """
    global _cached_response, _cache_expires_at
    with _cache_lock:
        now = time.monotonic()
        if (
            _cached_response is not None
            and _cache_expires_at is not None
            and now < _cache_expires_at
        ):
            return _cached_response

        # Still holding the lock: a concurrent caller that arrives while
        # this probe is in flight blocks here and receives the SAME
        # fresh result below, rather than starting a probe of its own.
        # See "Result caching" in the module docstring.
        openai_ok, openai_detail = _ping_openai()
        db_ok, db_detail = _ping_db()

        if openai_ok and db_ok:
            overall = "ok"
        elif openai_ok or db_ok:
            overall = "degraded"
        else:
            overall = "down"

        result = HealthResponse(
            status=overall,
            openai=openai_ok,
            database=db_ok,
            model=cfg.settings.openai_model,
            openai_detail=openai_detail,
            database_detail=db_detail,
        )
        _cached_response = result
        # cfg.settings read directly here, not via the module-level
        # HEALTH_CACHE_TTL_SECONDS name -- that name only exists via this
        # module's own __getattr__ (PEP 562), which is invoked for
        # `health.HEALTH_CACHE_TTL_SECONDS`-style external access, never
        # for a bare name reference inside this module's own code.
        _cache_expires_at = time.monotonic() + cfg.settings.health_cache_ttl_seconds
        return result


def _ping_openai() -> tuple[bool, str]:
    """Probe the configured endpoint. Returns ``(ok, detail)``.

    This probe used to disagree with the engine in two ways, and either
    was enough to show a red LLM light on a deployment where the CLI was
    answering questions perfectly through the same endpoint.

    **It sent an Authorization header even with no key.** Many
    self-hosted OpenAI-compatible servers check no credentials at all, so
    ``openai_api_key`` is legitimately empty -- and this built
    ``Authorization: Bearer `` with nothing after it. A malformed header
    is not the same as no header: plenty of servers reject the first with
    401 while accepting the second. :class:`~llm.providers.OpenAIBackend`
    omits the header entirely when the key is empty, so the engine
    succeeded where the probe failed. Both now use the same rule.

    **It judges an endpoint the engine never calls.** Generation goes to
    ``/chat/completions``; this asks ``/models``, because a probe must be
    cheap and must not consume tokens on every liveness check. That is a
    reasonable trade, but it means a 404 here says only that this server
    does not implement an endpoint we do not use -- which is not a fault
    and must not be reported as one. It is now treated as healthy, with
    the reason stated.

    A 401 or 403 is the opposite: the endpoint is reachable and has
    actively rejected our credentials, so generation will fail too. That
    stays a failure, and now says so instead of leaving an operator to
    guess between a wrong key and a wrong host.
    """
    base = cfg.settings.openai_base_url.rstrip("/")
    # Same rule as llm.providers.OpenAIBackend._headers: an empty key
    # means send no header, never an empty Bearer.
    headers = {}
    if cfg.settings.openai_api_key:
        headers["Authorization"] = f"Bearer {cfg.settings.openai_api_key}"

    try:
        resp = requests.get(f"{base}/models", headers=headers, timeout=5)
    except Exception as exc:  # noqa: BLE001
        return False, f"{base} is unreachable: {type(exc).__name__}"

    if resp.status_code == 200:
        return True, f"{base}/models responded 200"
    if resp.status_code in (401, 403):
        return False, (
            f"{base} is reachable but rejected our credentials "
            f"(HTTP {resp.status_code}) -- check OPENAI_API_KEY"
        )
    if resp.status_code in (404, 405):
        # Not a fault. The engine does not use this endpoint.
        return True, (
            f"{base} is reachable; it does not implement /models "
            f"(HTTP {resp.status_code}), which many local servers do not. "
            "Generation uses /chat/completions and is unaffected"
        )
    return False, f"{base}/models returned HTTP {resp.status_code}"


def _ping_db() -> tuple[bool, str]:
    """Return ``(ok, detail)`` for ``SELECT 1`` on the configured database.

    Uses the shared :func:`~database.connection.get_engine` singleton so no
    extra connection pool is created. The connection is checked out from the
    pool, used for a single no-op query, and immediately returned.

    Timeout behaviour is governed by SQLAlchemy ``pool_pre_ping`` (built-in
    liveness check) plus the driver-level socket timeout.

    The detail carries the exception *type* and message rather than a bare
    ``False``, for the same reason as :func:`_ping_openai`: "wrong host",
    "wrong credentials" and "driver not installed" are three different
    problems with three different fixes and one indistinguishable symptom.
    """
    try:
        from database.connection import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "SELECT 1 succeeded"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
