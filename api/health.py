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
"""

from __future__ import annotations

import logging

import requests

import config as cfg
from api.models import HealthResponse

logger = logging.getLogger(__name__)


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
    openai_ok, openai_detail = _ping_openai()
    db_ok, db_detail = _ping_db()

    if openai_ok and db_ok:
        overall = "ok"
    elif openai_ok or db_ok:
        overall = "degraded"
    else:
        overall = "down"

    return HealthResponse(
        status=overall,
        openai=openai_ok,
        database=db_ok,
        model=cfg.settings.openai_model,
        openai_detail=openai_detail,
        database_detail=db_detail,
    )


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
