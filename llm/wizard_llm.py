"""LLM abstraction layer for the setup wizard.

Supports four providers:

* ``ollama``    — local Ollama instance (POST /api/generate)
* ``openai``    — OpenAI-compatible API  (reads OPENAI_API_KEY)
* ``anthropic`` — Anthropic Claude API   (reads ANTHROPIC_API_KEY)
* ``mock``      — returns empty stubs, no network needed (CI / tests)

All calls enforce a 30-second timeout and one automatic retry on JSON parse
failure.  Markdown code fences are stripped before JSON parsing.

Typical usage::

    from llm.wizard_llm import WizardLLM

    llm = WizardLLM(provider="ollama", model="llama3")
    result = llm.generate("Return JSON with key 'x': 1", expect_json=True)
    # {'x': 1}
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 30  # seconds per LLM call
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove markdown code fences from *text*."""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _extract_json(text: str) -> dict | list:
    """Try to extract a JSON object or array from *text*.

    Tries in order:
    1. Direct parse after fence stripping
    2. Find first '{' or '[' and slice from there
    """
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Try to find JSON substring
    for start_ch, end_ch in (("{{", "}}"), ("[", "]")):
        s = cleaned.find(start_ch[0])
        if s != -1:
            # Find matching close
            depth = 0
            for i, ch in enumerate(cleaned[s:], start=s):
                if ch == start_ch[0]:
                    depth += 1
                elif ch == end_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cleaned[s : i + 1])
                        except json.JSONDecodeError:
                            break
    raise ValueError(f"No valid JSON found in LLM response: {text[:300]}")


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

class _OllamaProvider:
    def __init__(self, model: str, base_url: str) -> None:
        self._model    = model
        self._base_url = base_url.rstrip("/")
        # Normalize: accept both base URL and full /api/generate URL
        if not self._base_url.endswith("/api/generate"):
            self._url = f"{self._base_url}/api/generate"
        else:
            self._url = self._base_url

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
            base = self._base_url.replace("/api/generate", "")
            r = requests.get(f"{base}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False


class _OpenAIProvider:
    def __init__(self, model: str, base_url: str, api_key: str) -> None:
        self._model    = model
        self._base_url = base_url.rstrip("/")
        self._api_key  = api_key

    def generate(self, prompt: str) -> str:
        resp = requests.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
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
            resp = requests.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False


class _AnthropicProvider:
    def __init__(self, model: str, api_key: str) -> None:
        self._model   = model
        self._api_key = api_key
        self._url     = "https://api.anthropic.com/v1/messages"

    def generate(self, prompt: str) -> str:
        resp = requests.post(
            self._url,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()

    def test_connection(self) -> bool:
        try:
            resp = requests.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": self._api_key, "anthropic-version": "2023-06-01"},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False


class _MockProvider:
    """Returns deterministic empty stubs — no network required."""

    def generate(self, prompt: str) -> str:  # noqa: ARG002
        return '{"aliases": [], "description": "", "rules": {"rule_text": ""}, "examples": []}'

    def test_connection(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Public WizardLLM class
# ---------------------------------------------------------------------------

class WizardLLM:
    """Unified LLM interface for the setup wizard.

    Parameters
    ----------
    provider:
        One of ``"ollama"``, ``"openai"``, ``"anthropic"``, ``"mock"``.
    model:
        Model tag / name (e.g. ``"llama3"``, ``"gpt-4o-mini"``,
        ``"claude-3-haiku-20240307"``).
    base_url:
        Optional override for API base URL.  Useful for OpenAI-compatible
        local endpoints (e.g. LM Studio, vLLM).
    """

    def __init__(
        self,
        provider: str,
        model: str,
        base_url: str | None = None,
    ) -> None:
        import os
        self.provider = provider
        self.model    = model

        if provider == "ollama":
            url = base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")
            self._backend: Any = _OllamaProvider(model, url)

        elif provider == "openai":
            key = os.getenv("OPENAI_API_KEY", "")
            if not key:
                raise ValueError(
                    "OPENAI_API_KEY environment variable is required for provider='openai'."
                )
            url = base_url or os.getenv("WIZARD_LLM_BASE_URL", "https://api.openai.com/v1")
            self._backend = _OpenAIProvider(model, url, key)

        elif provider == "anthropic":
            key = os.getenv("ANTHROPIC_API_KEY", "")
            if not key:
                raise ValueError(
                    "ANTHROPIC_API_KEY environment variable is required for provider='anthropic'."
                )
            self._backend = _AnthropicProvider(model, key)

        elif provider == "mock":
            self._backend = _MockProvider()

        else:
            raise ValueError(
                f"Unknown LLM provider: {provider!r}. "
                "Choose from: ollama, openai, anthropic, mock."
            )

    def test_connection(self) -> bool:
        """Return True if the backend is reachable."""
        return self._backend.test_connection()

    def generate(
        self,
        prompt: str,
        expect_json: bool = True,
    ) -> str | dict | list:
        """Send *prompt* to the LLM and return the response.

        Parameters
        ----------
        prompt:
            The full prompt string.
        expect_json:
            If True, attempt to parse the response as JSON and return a
            ``dict`` or ``list``.  On the first parse failure the prompt is
            retried once with an explicit JSON reminder appended.  If the
            retry also fails a ``ValueError`` is raised.

        Returns
        -------
        str | dict | list
            Raw string if ``expect_json=False``, otherwise parsed JSON.

        Raises
        ------
        requests.Timeout
            If the backend does not respond within 30 seconds.
        requests.HTTPError
            On non-2xx HTTP responses.
        ValueError
            If ``expect_json=True`` and JSON cannot be extracted after retry.
        """
        raw = self._backend.generate(prompt)

        if not expect_json:
            return raw

        try:
            return _extract_json(raw)
        except ValueError:
            logger.debug("First JSON parse failed, retrying with JSON reminder")

        # Retry once with explicit JSON instruction
        retry_prompt = (
            prompt
            + "\n\nIMPORTANT: Respond with ONLY a valid JSON object or array. "
            "No explanation, no markdown, no code fences."
        )
        raw2 = self._backend.generate(retry_prompt)
        try:
            return _extract_json(raw2)
        except ValueError as exc:
            raise ValueError(
                f"LLM did not return valid JSON after retry.\nLast response: {raw2[:500]}"
            ) from exc

    @classmethod
    def from_env(cls) -> "WizardLLM":
        """Construct a WizardLLM from environment variables.

        Reads:
        * ``WIZARD_LLM_PROVIDER`` (default: ``ollama``)
        * ``WIZARD_LLM_MODEL``    (default: ``llama3``)
        * ``WIZARD_LLM_BASE_URL`` (optional)
        """
        import os
        return cls(
            provider=os.getenv("WIZARD_LLM_PROVIDER", "ollama"),
            model=os.getenv("WIZARD_LLM_MODEL", "llama3"),
            base_url=os.getenv("WIZARD_LLM_BASE_URL") or None,
        )
