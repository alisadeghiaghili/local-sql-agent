"""
llm/wizard_llm.py — LLM abstraction layer for local-sql-agent.

Supports two providers:

* ``openai`` — OpenAI-compatible API (vLLM / LM Studio / Ollama /v1);
               reads OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL from env.
* ``mock``   — returns empty stubs, no network needed (CI / tests)

All calls enforce a 30-second timeout.  On JSON parse failure the call is
retried ONCE with an explicit repair prompt before raising ValueError.

Typical usage::

    from llm.wizard_llm import WizardLLM

    llm = WizardLLM(provider="openai", model=None)   # OpenAI-compatible endpoint
    result = llm.generate("Return JSON with key x equal to 1", expect_json=True)
    # {"x": 1}

    # Override endpoint for LM Studio / vLLM:
    llm = WizardLLM(provider="openai", model="local-model",
                    base_url="http://localhost:1234/v1")

The SQL pipeline backend (:func:`build_backend`) is built from the same
provider names so the webapp and the agent stay consistent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Union

import requests

from llm.base import LLMBackend

logger = logging.getLogger(__name__)

_TIMEOUT: int = 30  # seconds — enforced on every outbound call

# Matches ```json ... ``` and plain ``` ... ``` fences, including optional
# leading/trailing whitespace inside the fence.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _config_or_none():
    """Return the ``config`` module, or None if it cannot be imported."""
    try:
        import config  # noqa: PLC0415
        return config
    except Exception:  # noqa: BLE001 - standalone use without config
        return None


# ---------------------------------------------------------------------------
# JSON extraction helpers (module-level so they can be unit-tested directly)
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove the outermost markdown code fence from *text*, if present."""
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def _find_json_substring(text: str) -> str:
    """Return the substring starting from the first ``{`` or ``[`` character.

    Raises ValueError if neither character is found.
    """
    for i, ch in enumerate(text):
        if ch in ("{", "["):
            return text[i:]
    raise ValueError(
        "No JSON object or array found in the response. "
        f"First 300 chars: {text[:300]!r}"
    )


def _parse_json(text: str) -> Union[dict, list]:
    """Full extraction pipeline: strip fences → find start → parse.

    Raises
    ------
    ValueError
        If no valid JSON can be extracted from *text*.
    """
    cleaned = _strip_fences(text)
    json_str = _find_json_substring(cleaned)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON parse error after extraction: {exc}. "
            f"Attempted to parse: {json_str[:300]!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Provider back-ends  (internal — not part of the public API)
# ---------------------------------------------------------------------------

class _OpenAIProvider:
    def __init__(self, model: str, base_url: str, api_key: str) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def generate(self, prompt: str) -> str:
        resp = requests.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers,
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def test_connection(self, timeout: float = 5.0) -> bool:
        try:
            r = requests.get(
                f"{self._base_url}/models",
                headers=self._headers,
                timeout=timeout,
            )
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False


class _MockProvider:
    """Deterministic stub — no network, for CI / unit tests."""

    _STUB = json.dumps(
        {"aliases": [], "description": "", "rules": {"rule_text": ""}, "examples": []}
    )

    def generate(self, prompt: str) -> str:  # noqa: ARG002
        return self._STUB

    def test_connection(self, timeout: float = 5.0) -> bool:  # noqa: ARG002
        return True


# ---------------------------------------------------------------------------
# SQL-pipeline backend
# ---------------------------------------------------------------------------

class OpenAIBackend(LLMBackend):
    """OpenAI-compatible chat backend for the SQL pipeline.

    Contract mirrors the legacy Ollama backend: raw text out,
    ``ValueError("OUT_OF_SCOPE")`` sentinel, transport retries with
    exponential back-off, ``RuntimeError`` after all retries exhausted.
    """

    _RETRIES: int = 3
    _BACKOFF_BASE: int = 2

    def __init__(
        self,
        model: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        retries: int = _RETRIES,
        timeout: int = 120,
    ) -> None:
        cfg = _config_or_none()
        self._model = model or (cfg.settings.openai_model if cfg else os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
        base = url or (cfg.settings.openai_base_url if cfg else os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
        self._url = base.rstrip("/")
        self._api_key = api_key if api_key is not None else (cfg.settings.openai_api_key if cfg else os.getenv("OPENAI_API_KEY", ""))
        self._retries = retries
        self._timeout = timeout

    @property
    def name(self) -> str:
        return f"openai:{self._model}"

    @property
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def generate(self, prompt: str) -> str:
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }

        last_exc: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                resp = requests.post(
                    f"{self._url}/chat/completions",
                    json=payload,
                    headers=self._headers,
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                raw: str = (
                    resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                ).strip()
                logger.debug("OpenAI raw (attempt %d): %.300s", attempt, raw)

                if raw.strip().upper() == "OUT_OF_SCOPE":
                    raise ValueError("OUT_OF_SCOPE")
                return raw

            except ValueError:
                raise
            except requests.Timeout:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                wait = self._BACKOFF_BASE ** (attempt - 1)
                logger.warning(
                    "OpenAI attempt %d/%d failed: %s — retrying in %ds",
                    attempt,
                    self._retries,
                    exc,
                    wait,
                )
                if attempt < self._retries:
                    time.sleep(wait)

        raise RuntimeError(
            f"OpenAI endpoint unreachable after {self._retries} retries: {last_exc}"
        )


class MockBackend(LLMBackend):
    """Minimal LLMBackend stub for the SQL pipeline (tests / dry runs)."""

    @property
    def name(self) -> str:
        return "mock:mock"

    def generate(self, prompt: str) -> str:  # noqa: ARG002
        return "SELECT 1"


def build_backend(provider: str | None = None) -> LLMBackend:
    """Build the SQL-pipeline LLM backend for *provider*.

    ``None``/``"openai"`` (and legacy ``"auto"``) map to the OpenAI-compatible
    backend; ``mock`` returns a stub.  Raises ``ValueError`` for unknown
    providers.
    """
    name = (provider or "openai").strip().lower()
    if name in ("auto", "openai"):
        return OpenAIBackend()
    if name == "mock":
        return MockBackend()
    raise ValueError(f"Unsupported LLM provider: {provider!r}")


def generate_sql(question: str, system_prompt: str) -> str:
    """Generate SQL for *question* using the OpenAI-compatible backend.

    This function only calls the LLM — it does **not** execute the
    generated SQL against any database.

    Raises
    ------
    ValueError("OUT_OF_SCOPE")
        Passed through from the model sentinel.
    RuntimeError
        When the endpoint is unreachable after all retries.
    """
    from retrieval.context_retriever import ContextRetriever
    from prompt_engine.builder import PromptBuilder
    from security.sql_guard import clean_sql, validate_sql

    context = ContextRetriever.retrieve(question)
    prompt = PromptBuilder.build(
        question=question,
        system_prompt=system_prompt,
        context=context,
    )
    raw = build_backend().generate(prompt)
    sql = clean_sql(raw)
    validate_sql(sql)
    return sql


# ---------------------------------------------------------------------------
# Public WizardLLM class (setup wizard)
# ---------------------------------------------------------------------------

class WizardLLM:
    """Unified LLM interface for the setup wizard (openai / mock).

    Parameters
    ----------
    provider:
        ``"openai"`` (default; legacy ``"auto"`` is treated as ``"openai"``)
        or ``"mock"``.
    model:
        Model identifier, e.g. ``"gpt-oss-20:F16"``.  When ``None`` the
        model from ``config.settings`` (i.e. .env) is used.
    base_url:
        Optional override for the OpenAI-compatible endpoint.
        Useful for LM Studio, vLLM, or custom deployments.

    Raises
    ------
    ValueError
        If *provider* is not supported, or if OPENAI_API_KEY is missing.
    """

    _SUPPORTED = frozenset({"openai", "mock"})

    def __init__(
        self,
        provider: str,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        provider = provider.strip().lower()
        if provider == "auto":
            provider = "openai"
        if provider not in self._SUPPORTED:
            raise ValueError(
                f"Unsupported provider {provider!r}. "
                f"Choose one of: {', '.join(sorted(self._SUPPORTED))}."
            )

        cfg = _config_or_none()

        self.provider = provider
        self.model = model

        if provider == "openai":
            key = (cfg.settings.openai_api_key if cfg else None) or os.getenv("OPENAI_API_KEY", "")
            if not key:
                raise ValueError(
                    "OPENAI_API_KEY environment variable is required "
                    "for provider='openai'."
                )
            if model is None:
                model = cfg.settings.openai_model if cfg else os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            url = base_url or (cfg.settings.openai_base_url if cfg else os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
            self._backend: Any = _OpenAIProvider(model, url, key)

        else:  # mock
            self._backend = _MockProvider()

        self.model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        """Return True if the configured backend is reachable, False otherwise."""
        return self._backend.test_connection()

    def generate(
        self,
        prompt: str,
        expect_json: bool = True,
    ) -> Union[str, dict, list]:
        """Send *prompt* to the LLM and return the response.

        Parameters
        ----------
        prompt:
            The full prompt string.
        expect_json:
            When True (default), parse the response as JSON and return a
            ``dict`` or ``list``.  On the first parse failure the call is
            retried ONCE with a repair prompt prepended.  Raises
            ``ValueError`` if the retry also fails.
            When False, the raw response string is returned as-is.

        Returns
        -------
        str | dict | list
            Raw string when ``expect_json=False``, otherwise parsed JSON.

        Raises
        ------
        requests.Timeout
            If the backend does not respond within 30 seconds.
        requests.HTTPError
            On non-2xx HTTP responses.
        ValueError
            If ``expect_json=True`` and valid JSON cannot be extracted
            after the retry.
        """
        raw = self._backend.generate(prompt)

        if not expect_json:
            return raw

        # --- first extraction attempt ---
        try:
            return _parse_json(raw)
        except ValueError:
            logger.debug(
                "[WizardLLM] First JSON parse failed for provider=%s; retrying.",
                self.provider,
            )

        # --- single retry with explicit repair instruction ---
        repair_prompt = (
            "The text below must be a valid JSON object or array but failed to parse.\n"
            "Return ONLY the corrected JSON — no explanation, no markdown, no code fences.\n\n"
            + raw
        )
        raw2 = self._backend.generate(repair_prompt)
        try:
            return _parse_json(raw2)
        except ValueError as exc:
            raise ValueError(
                f"[WizardLLM] Could not extract valid JSON after retry "
                f"(provider={self.provider!r}, model={self.model!r}).\n"
                f"Last raw response (first 500 chars): {raw2[:500]!r}"
            ) from exc

    # ------------------------------------------------------------------
    # Convenience factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls) -> "WizardLLM":
        """Construct a WizardLLM from the project's config / environment.

        Reads (in order of precedence):
        * ``WIZARD_LLM_PROVIDER``  — overrides the default (``openai``)
        * ``WIZARD_LLM_MODEL``     — overrides the per-provider model
        * ``WIZARD_LLM_BASE_URL``  — optional endpoint override
        """
        cfg = _config_or_none()
        provider = os.getenv("WIZARD_LLM_PROVIDER") or "openai"
        return cls(
            provider=provider,
            model=os.getenv("WIZARD_LLM_MODEL") or None,
            base_url=os.getenv("WIZARD_LLM_BASE_URL") or None,
        )
