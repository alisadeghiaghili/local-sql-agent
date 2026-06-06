"""Ollama HTTP client — generates SQL from a natural language question.

Retries on transient network errors with exponential back-off.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from config import settings
from schema.retriever import retrieve_tables
from schema.schema_registry import build_schema_context
from security.sql_guard import clean_sql

logger = logging.getLogger(__name__)

_RETRIES: int = 3
_TIMEOUT: int = 120
_BACKOFF_BASE: int = 2


def generate_sql(question: str, system_prompt: str) -> str:
    """Send *question* to Ollama and return a cleaned SQL string.

    Parameters
    ----------
    question:
        Natural-language question from the user.
    system_prompt:
        Full system prompt (loaded once at startup).

    Returns
    -------
    str
        A clean, validated SQL string ready for execution.

    Raises
    ------
    ValueError("OUT_OF_SCOPE")
        When the model responds with the sentinel ``OUT_OF_SCOPE``.
    RuntimeError
        When Ollama is unreachable after all retries.
    """
    selected_tables = retrieve_tables(question)
    schema_context  = build_schema_context(selected_tables)

    full_prompt = (
        f"{system_prompt}\n\n"
        f"{schema_context}\n\n"
        f"Question:\n{question}\n\nSQL:\n"
    )

    payload: dict[str, Any] = {
        "model":  settings.ollama_model,
        "prompt": full_prompt,
        "stream": False,
    }

    last_exc: Exception | None = None

    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.post(
                settings.ollama_url,
                json=payload,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            raw: str = resp.json().get("response", "").strip()
            logger.debug("Ollama raw (attempt %d): %.300s", attempt, raw)

            if raw.strip().upper() == "OUT_OF_SCOPE":
                raise ValueError("OUT_OF_SCOPE")

            return clean_sql(raw)

        except ValueError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            wait = _BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                "Ollama attempt %d/%d failed: %s — retrying in %ds",
                attempt, _RETRIES, exc, wait,
            )
            if attempt < _RETRIES:
                time.sleep(wait)

    raise RuntimeError(
        f"Ollama unreachable after {_RETRIES} retries: {last_exc}"
    )
