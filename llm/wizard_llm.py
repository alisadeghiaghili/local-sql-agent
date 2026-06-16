"""
llm/wizard_llm.py — LLM abstraction layer for local-sql-agent.

Supports four providers:

* ``ollama``    — local Ollama instance  (POST /api/generate)
* ``openai``    — OpenAI-compatible API  (reads OPENAI_API_KEY from env)
* ``anthropic`` — Anthropic Claude API   (reads ANTHROPIC_API_KEY from env)
* ``mock``      — returns empty stubs, no network needed (CI / tests)

All calls enforce a 30-second timeout.  On JSON parse failure the call is
retried ONCE with an explicit repair prompt before raising ValueError.

Typical usage::

    from llm.wizard_llm import WizardLLM

    llm = WizardLLM(provider="ollama", model="llama3")
    result = llm.generate("Return JSON with key x equal to 1", expect_json=True)
    # {"x": 1}

    # Override endpoint for LM Studio / vLLM:
    llm = WizardLLM(provider="openai", model="local-model",
                    base_url="http://localhost:1234/v1")
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Union

import requests

logger = logging.getLogger(__name__)

_TIMEOUT: int = 30  # seconds — enforced on every outbound call

# Matches ```json ... ``` and plain ``` ... ``` fences, including optional
# leading/trailing whitespace inside the fence.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


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

class _OllamaProvider:
    def __init__(self, model: str, base_url: str) -> None:
        self._model = model
        # Accept either a bare host ("http://localhost:11434") or a full URL
        # ending with "/api/generate".
        base = base_url.rstrip("/")
        self._url = base if base.endswith("/api/generate") else f"{base}/api/generate"
        # Health-check URL (list local models)
        self._tags_url = self._url.replace("/api/generate", "/api/tags")

    def generate(self, prompt: str) -> str:
        resp = requests.post(
            self._url,
            json={"model": self._model, "prompt": prompt, "stream": False},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    def test_connection(self) -> bool:
        try:
            r = requests.get(self._tags_url, timeout=5)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False


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

    def test_connection(self) -> bool:
        try:
            r = requests.get(
                f"{self._base_url}/models",
                headers=self._headers,
                timeout=5,
            )
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False


class _AnthropicProvider:
    _BASE_URL = "https://api.anthropic.com/v1"

    def __init__(self, model: str, api_key: str, base_url: str | None = None) -> None:
        self._model = model
        self._api_key = api_key
        base = (base_url or self._BASE_URL).rstrip("/")
        self._messages_url = f"{base}/messages"
        self._models_url = f"{base}/models"

    @property
    def _headers(self) -> dict:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def generate(self, prompt: str) -> str:
        resp = requests.post(
            self._messages_url,
            headers=self._headers,
            json={
                "model": self._model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()

    def test_connection(self) -> bool:
        try:
            r = requests.get(self._models_url, headers=self._headers, timeout=5)
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

    def test_connection(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Public WizardLLM class
# ---------------------------------------------------------------------------

class WizardLLM:
    """Unified LLM interface supporting ollama, openai, anthropic, and mock.

    Parameters
    ----------
    provider:
        One of ``"ollama"``, ``"openai"``, ``"anthropic"``, ``"mock"``.
    model:
        Model identifier, e.g. ``"llama3"``, ``"gpt-4o-mini"``,
        ``"claude-3-5-sonnet-20241022"``.
    base_url:
        Optional override for the provider's default endpoint.
        Useful for LM Studio, vLLM, or custom Ollama deployments.

    Raises
    ------
    ValueError
        If *provider* is not one of the four supported values, or if a
        required API key environment variable is missing.
    """

    _SUPPORTED = frozenset({"ollama", "openai", "anthropic", "mock"})

    def __init__(
        self,
        provider: str,
        model: str,
        base_url: str | None = None,
    ) -> None:
        provider = provider.strip().lower()
        if provider not in self._SUPPORTED:
            raise ValueError(
                f"Unsupported provider {provider!r}. "
                f"Choose one of: {', '.join(sorted(self._SUPPORTED))}."
            )

        self.provider = provider
        self.model = model

        if provider == "ollama":
            url = base_url or os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
            self._backend: Any = _OllamaProvider(model, url)

        elif provider == "openai":
            key = os.getenv("OPENAI_API_KEY", "")
            if not key:
                raise ValueError(
                    "OPENAI_API_KEY environment variable is required "
                    "for provider='openai'."
                )
            url = base_url or os.getenv("WIZARD_LLM_BASE_URL", "https://api.openai.com/v1")
            self._backend = _OpenAIProvider(model, url, key)

        elif provider == "anthropic":
            key = os.getenv("ANTHROPIC_API_KEY", "")
            if not key:
                raise ValueError(
                    "ANTHROPIC_API_KEY environment variable is required "
                    "for provider='anthropic'."
                )
            self._backend = _AnthropicProvider(model, key, base_url)

        else:  # mock
            self._backend = _MockProvider()

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
        * ``WIZARD_LLM_PROVIDER``  — default ``ollama``
        * ``WIZARD_LLM_MODEL``     — default ``llama3`` (or ``OLLAMA_MODEL``)
        * ``WIZARD_LLM_BASE_URL``  — optional endpoint override
        """
        try:
            import config as cfg  # noqa: PLC0415
            default_model = cfg.settings.ollama_model
        except Exception:  # noqa: BLE001
            default_model = "llama3"

        return cls(
            provider=os.getenv("WIZARD_LLM_PROVIDER", "ollama"),
            model=os.getenv("WIZARD_LLM_MODEL", default_model),
            base_url=os.getenv("WIZARD_LLM_BASE_URL") or None,
        )
