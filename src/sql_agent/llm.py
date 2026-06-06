"""Shared Ollama HTTP client.

Both runners use this instead of duplicating the requests.post block.

Example::

    from sql_agent.llm import call_ollama
    raw = call_ollama("SELECT all users", base_url="http://localhost:11434", model="gemma3:12b")
"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

_DEFAULT_RETRIES = 3
_DEFAULT_TIMEOUT = 60


def call_ollama(
    prompt: str,
    *,
    base_url: str,
    model: str,
    temperature: float = 0.1,
    retries: int = _DEFAULT_RETRIES,
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    """POST *prompt* to Ollama and return the raw response string.

    Retries up to *retries* times with exponential back-off on connection errors.
    Raises ``RuntimeError`` if all attempts fail.
    """
    url = base_url.rstrip("/") + "/api/generate"
    payload = {
        "model":       model,
        "prompt":      prompt,
        "stream":      False,
        "temperature": temperature,
    }
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            text = resp.json().get("response", "").strip()
            logger.debug("Ollama response (%d chars)", len(text))
            return text
        except requests.RequestException as exc:
            last_exc = exc
            wait = 2 ** (attempt - 1)          # 1s, 2s, 4s …
            logger.warning("Ollama attempt %d/%d failed: %s — retrying in %ds", attempt, retries, exc, wait)
            if attempt < retries:
                time.sleep(wait)
    raise RuntimeError(f"Ollama unavailable after {retries} retries: {last_exc}")
