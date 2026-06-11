"""Ollama HTTP client — generates SQL from a natural language question.

Pipeline
--------
    question
        → ContextRetriever   (entities, facts, relationships, rules, examples, filters)
        → PromptBuilder      (assembles structured prompt)
        → Ollama HTTP API    (LLM inference)
        → clean_sql          (security sanitisation)

Retries on transient network errors with exponential back-off.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

import config as cfg
from retrieval.context_retriever import ContextRetriever
from prompt_engine.builder import PromptBuilder
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
        Natural-language question in Persian or English.
    system_prompt:
        Raw content of prompts/system_prompt.md, loaded once at startup.

    Raises
    ------
    ValueError("OUT_OF_SCOPE")
        When the model responds with the sentinel ``OUT_OF_SCOPE``.
    RuntimeError
        When Ollama is unreachable after all retries.
    """
    context = ContextRetriever.retrieve(question)

    full_prompt = PromptBuilder.build(
        question=question,
        system_prompt=system_prompt,
        context=context,
    )

    payload: dict[str, Any] = {
        "model":  cfg.settings.ollama_model,
        "prompt": full_prompt,
        "stream": False,
    }

    last_exc: Exception | None = None

    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.post(
                cfg.settings.ollama_url,
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
