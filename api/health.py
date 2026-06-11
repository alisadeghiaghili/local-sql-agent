"""Health-check logic — pings Ollama and the database."""

from __future__ import annotations

import logging

import requests

import config as cfg
from api.models import HealthResponse

logger = logging.getLogger(__name__)


def check_health() -> HealthResponse:
    ollama_ok = _ping_ollama()
    db_ok = _ping_db()

    if ollama_ok and db_ok:
        overall = "ok"
    elif ollama_ok or db_ok:
        overall = "degraded"
    else:
        overall = "down"

    return HealthResponse(
        status=overall,
        ollama=ollama_ok,
        database=db_ok,
        model=cfg.settings.ollama_model,
    )


def _ping_ollama() -> bool:
    try:
        base = cfg.settings.ollama_url.rstrip("/").rsplit("/", 1)[0]
        resp = requests.get(f"{base}/tags", timeout=5)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _ping_db() -> bool:
    try:
        from database.connection import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False
