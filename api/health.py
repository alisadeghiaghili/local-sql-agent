"""Health-check logic — probes the OpenAI-compatible LLM and the SQL Server database.

Exposed via ``GET /health``.  Returns a :class:`~api.models.HealthResponse`
with an overall status of ``"ok"``, ``"degraded"``, or ``"down"`` plus
per-component boolean flags.

Typical usage::

    from api.health import check_health

    resp = check_health()
    # HealthResponse(status='ok', openai=True, database=True, model='gpt-oss-20:F16')
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

    1. :func:`_ping_openai` — HTTP GET to ``<base_url>/models`` (5 s timeout).
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
    >>> resp = check_health()          # doctest: +SKIP
    >>> resp.status in ("ok", "degraded", "down")
    True
    >>> isinstance(resp.openai, bool)
    True
    >>> isinstance(resp.database, bool)
    True
    """
    openai_ok = _ping_openai()
    db_ok     = _ping_db()

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
    )


def _ping_openai() -> bool:
    """Return ``True`` if the OpenAI-compatible ``/models`` endpoint responds with HTTP 200.

    The URL is ``cfg.settings.openai_base_url`` with ``/models`` appended,
    authenticated with the configured Bearer token.

    Timeout: 5 seconds.  Any exception (connection refused, DNS failure,
    non-200 status) is caught and causes the function to return ``False``
    without propagating the error.

    Returns
    -------
    bool
        ``True``  — the endpoint is reachable and ``/models`` returned HTTP 200.

        ``False`` — unreachable, wrong status code, or any exception.
    """
    try:
        base = cfg.settings.openai_base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {cfg.settings.openai_api_key}"}
        resp = requests.get(f"{base}/models", headers=headers, timeout=5)
        return resp.status_code == 200
    except Exception:   # noqa: BLE001
        return False


def _ping_db() -> bool:
    """Return ``True`` if ``SELECT 1`` executes without error on the configured database.

    Uses the shared :func:`~database.connection.get_engine` singleton so no
    extra connection pool is created.  The connection is checked out from the
    pool, used for a single no-op query, and immediately returned.

    Timeout behaviour is governed by SQLAlchemy ``pool_pre_ping`` (built-in
    liveness check) plus the pyodbc socket timeout configured at the ODBC
    driver level.

    Returns
    -------
    bool
        ``True``  — ``SELECT 1`` executed successfully.

        ``False`` — connection refused, authentication error, or any other
        exception from the driver or SQLAlchemy.
    """
    try:
        from database.connection import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:   # noqa: BLE001
        return False
