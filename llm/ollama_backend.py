"""Ollama HTTP backend — thin wrapper around the Ollama /api/generate endpoint.

This module replaces the transport logic that was previously inlined in
``llm/ollama_client.py``.  That module is kept for backward compatibility.
"""

from __future__ import annotations

import logging
import time

import requests

import config as cfg
from llm.base import LLMBackend

logger = logging.getLogger(__name__)

_RETRIES: int = 3
_TIMEOUT: int = 120
_BACKOFF_BASE: int = 2


class OllamaBackend(LLMBackend):
    """Send prompts to a locally-running Ollama instance.

    Parameters
    ----------
    model:
        Ollama model tag, e.g. ``"llama3"`` or ``"codellama:13b"``.
        Defaults to ``cfg.settings.ollama_model``.
    url:
        Ollama API base URL.  Defaults to ``cfg.settings.ollama_url``.
    retries:
        Number of transport-level retries with exponential back-off.
    timeout:
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        model: str | None = None,
        url: str | None = None,
        retries: int = _RETRIES,
        timeout: int = _TIMEOUT,
    ) -> None:
        self._model = model or cfg.settings.ollama_model
        self._url = url or cfg.settings.ollama_url
        self._retries = retries
        self._timeout = timeout

    @property
    def name(self) -> str:
        return f"ollama:{self._model}"

    def generate(self, prompt: str) -> str:
        """POST *prompt* to Ollama and return the raw response string.

        Raises
        ------
        ValueError("OUT_OF_SCOPE")
            Passed through from the model sentinel.
        RuntimeError
            When the endpoint is unreachable after all retries.
        """
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
        }

        last_exc: Exception | None = None

        for attempt in range(1, self._retries + 1):
            try:
                resp = requests.post(self._url, json=payload, timeout=self._timeout)
                resp.raise_for_status()
                raw: str = resp.json().get("response", "").strip()
                logger.debug("Ollama raw (attempt %d): %.300s", attempt, raw)

                if raw.strip().upper() == "OUT_OF_SCOPE":
                    raise ValueError("OUT_OF_SCOPE")

                return raw

            except ValueError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                wait = _BACKOFF_BASE ** (attempt - 1)
                logger.warning(
                    "Ollama attempt %d/%d failed: %s — retrying in %ds",
                    attempt,
                    self._retries,
                    exc,
                    wait,
                )
                if attempt < self._retries:
                    time.sleep(wait)

        raise RuntimeError(
            f"Ollama unreachable after {self._retries} retries: {last_exc}"
        )
