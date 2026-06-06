"""Ollama HTTP client — generates SQL from a natural language question."""

from __future__ import annotations

import logging
import time

import requests

from config import OLLAMA_MODEL, OLLAMA_URL
from schema.retriever import retrieve_tables
from schema.schema_registry import build_schema_context

logger = logging.getLogger(__name__)

_RETRIES = 3
_TIMEOUT = 120


def generate_sql(question: str, system_prompt: str) -> str:
    """Send *question* to Ollama and return the raw SQL string.

    Raises
    ------
    ValueError("OUT_OF_SCOPE")
        When the model replies with the sentinel ``OUT_OF_SCOPE``.
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

    payload = {
        "model":  OLLAMA_MODEL,
        "prompt": full_prompt,
        "stream": False,
    }

    last_exc: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=_TIMEOUT)
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()
            logger.debug("Ollama raw response: %s", repr(raw[:300]))

            if raw.strip().upper() == "OUT_OF_SCOPE":
                raise ValueError("OUT_OF_SCOPE")

            return raw

        except ValueError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            wait = 2 ** (attempt - 1)
            logger.warning("Ollama attempt %d/%d failed: %s — retrying in %ds", attempt, _RETRIES, exc, wait)
            if attempt < _RETRIES:
                time.sleep(wait)

    raise RuntimeError(f"Ollama unavailable after {_RETRIES} retries: {last_exc}")
