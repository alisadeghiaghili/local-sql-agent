"""
llm/wizard_llm.py — LLM abstraction layer for local-sql-agent.

Supports five providers:

* ``auto``     — probe the configured providers and pick the most accessible
                 one.  By default ``ollama`` is preferred; falls back to the
                 next provider that answers (see :func:`select_provider`).
* ``ollama``   — local Ollama instance  (POST /api/generate)
* ``openai``   — OpenAI-compatible API  (reads OPENAI_API_KEY from env)
* ``anthropic`` — Anthropic Claude API  (reads ANTHROPIC_API_KEY from env)
* ``mock``     — returns empty stubs, no network needed (CI / tests)

All calls enforce a 30-second timeout.  On JSON parse failure the call is
retried ONCE with an explicit repair prompt before raising ValueError.

Typical usage::

    from llm.wizard_llm import WizardLLM

    llm = WizardLLM(provider="auto", model=None)   # auto-pick provider
    result = llm.generate("Return JSON with key x equal to 1", expect_json=True)
    # {"x": 1}

    # Override endpoint for LM Studio / vLLM:
    llm = WizardLLM(provider="openai", model="local-model",
                    base_url="http://localhost:1234/v1")

The SQL pipeline backend (:func:`build_backend`) is built from the same
provider names so the webapp dropdown and the agent stay consistent.
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

# Providers considered by "auto" selection, in preference order.
_PROBE_CANDIDATES: tuple[str, ...] = ("ollama", "openai", "anthropic")
_PROBE_TIMEOUT: float = 3.0  # seconds per provider health probe


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

    def test_connection(self, timeout: float = 5.0) -> bool:
        try:
            r = requests.get(self._tags_url, timeout=timeout)
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

    def test_connection(self, timeout: float = 5.0) -> bool:
        try:
            r = requests.get(self._models_url, headers=self._headers, timeout=timeout)
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
# Provider auto-selection
# ---------------------------------------------------------------------------

def select_provider(
    prefer: str = "ollama",
    timeout: float = _PROBE_TIMEOUT,
    _candidates: tuple[str, ...] = _PROBE_CANDIDATES,
) -> str:
    """Return the most accessible provider name.

    Probes every candidate provider with a short HTTP health request and
    keeps the ones that answer.  *prefer* (``ollama`` by default) wins when
    reachable; otherwise the fastest-responding reachable provider is
    returned.  Raises ``RuntimeError`` if none of the candidates respond.

    Parameters
    ----------
    prefer:
        Provider given priority when multiple candidates are reachable.
    timeout:
        Per-provider probe timeout in seconds.
    """
    cfg = _config_or_none()

    def _probe(name: str) -> float | None:
        """Return probe latency in seconds, or None when unreachable."""
        try:
            if name == "ollama":
                model = cfg.settings.ollama_model if cfg else os.getenv("OLLAMA_MODEL", "llama3")
                url = cfg.settings.ollama_url if cfg else os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
                probe = _OllamaProvider(model, url)
            elif name == "openai":
                key = cfg.settings.openai_api_key if cfg else os.getenv("OPENAI_API_KEY", "")
                if not key:
                    return None  # unconfigured → not a candidate
                model = cfg.settings.openai_model if cfg else os.getenv("OPENAI_MODEL", "gpt-4o-mini")
                url = cfg.settings.openai_base_url if cfg else os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
                probe = _OpenAIProvider(model, url, key)
            elif name == "anthropic":
                key = cfg.settings.anthropic_api_key if cfg else os.getenv("ANTHROPIC_API_KEY", "")
                if not key:
                    return None
                model = cfg.settings.anthropic_model if cfg else "claude-3-5-sonnet-20241022"
                probe = _AnthropicProvider(model, key)
            else:
                return None
            start = time.perf_counter()
            ok = probe.test_connection(timeout)
            return (time.perf_counter() - start) if ok else None
        except Exception:  # noqa: BLE001 - any failure means "not accessible"
            return None

    candidates = [prefer] + [p for p in _candidates if p != prefer]
    latencies: dict[str, float] = {}
    for name in candidates:
        latency = _probe(name)
        if latency is not None:
            latencies[name] = latency
            logger.info("[wizard_llm] provider %r reachable (%.0f ms)", name, latency * 1000)

    if prefer in latencies:
        return prefer
    if latencies:
        return min(latencies, key=latencies.get)
    raise RuntimeError(
        "No LLM provider is reachable. Check OLLAMA_URL / OPENAI_BASE_URL / "
        "ANTHROPIC_API_KEY in .env"
    )


# ---------------------------------------------------------------------------
# SQL-pipeline backend (same provider vocabulary as WizardLLM)
# ---------------------------------------------------------------------------

class OpenAIBackend(LLMBackend):
    """OpenAI-compatible chat backend for the SQL pipeline.

    Contract mirrors :class:`~llm.ollama_backend.OllamaBackend`: raw text
    out, ``ValueError("OUT_OF_SCOPE")`` sentinel, transport retries with
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

    ``None``/``"auto"`` resolves via :func:`select_provider`; ``ollama`` and
    ``openai`` map to their LLMBackend implementations; ``mock`` returns a
    stub.  Raises ``ValueError`` for unknown providers.
    """
    cfg = _config_or_none()
    name = (provider or (cfg.settings.llm_provider if cfg else "auto") or "auto").strip().lower()
    if name == "auto":
        name = select_provider()

    if name == "ollama":
        from llm.ollama_backend import OllamaBackend  # noqa: PLC0415
        return OllamaBackend()
    if name == "openai":
        return OpenAIBackend()
    if name == "mock":
        return MockBackend()
    raise ValueError(f"Unsupported LLM provider: {provider!r}")


# ---------------------------------------------------------------------------
# Public WizardLLM class
# ---------------------------------------------------------------------------

class WizardLLM:
    """Unified LLM interface supporting auto, ollama, openai, anthropic, mock.

    Parameters
    ----------
    provider:
        One of ``"auto"``, ``"ollama"``, ``"openai"``, ``"anthropic"``,
        ``"mock"``.  ``"auto"`` resolves to the most accessible provider via
        :func:`select_provider`.
    model:
        Model identifier, e.g. ``"llama3"``, ``"gpt-4o-mini"``,
        ``"claude-3-5-sonnet-20241022"``.  When ``None`` the per-provider
        model from ``config.settings`` (i.e. .env) is used.
    base_url:
        Optional override for the provider's default endpoint.
        Useful for LM Studio, vLLM, or custom Ollama deployments.

    Raises
    ------
    ValueError
        If *provider* is not supported, or if a required API key
        environment variable is missing.
    RuntimeError
        If ``provider="auto"`` and no provider is reachable.
    """

    _SUPPORTED = frozenset({"auto", "ollama", "openai", "anthropic", "mock"})

    def __init__(
        self,
        provider: str,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        provider = provider.strip().lower()
        if provider not in self._SUPPORTED:
            raise ValueError(
                f"Unsupported provider {provider!r}. "
                f"Choose one of: {', '.join(sorted(self._SUPPORTED))}."
            )

        cfg = _config_or_none()

        if provider == "auto":
            provider = select_provider()

        self.provider = provider
        self.model = model

        if provider == "ollama":
            if model is None:
                model = cfg.settings.ollama_model if cfg else os.getenv("OLLAMA_MODEL", "llama3")
            url = base_url or (cfg.settings.ollama_url if cfg else os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate"))
            self._backend: Any = _OllamaProvider(model, url)

        elif provider == "openai":
            key = (cfg.settings.openai_api_key if cfg else None) or os.getenv("OPENAI_API_KEY", "")
            if not key:
                raise ValueError(
                    "OPENAI_API_KEY environment variable is required "
                    "for provider='openai'."
                )
            if model is None:
                model = cfg.settings.openai_model if cfg else os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            url = base_url or (cfg.settings.openai_base_url if cfg else os.getenv("WIZARD_LLM_BASE_URL", "https://api.openai.com/v1"))
            self._backend = _OpenAIProvider(model, url, key)

        elif provider == "anthropic":
            key = (cfg.settings.anthropic_api_key if cfg else None) or os.getenv("ANTHROPIC_API_KEY", "")
            if not key:
                raise ValueError(
                    "ANTHROPIC_API_KEY environment variable is required "
                    "for provider='anthropic'."
                )
            if model is None:
                model = cfg.settings.anthropic_model if cfg else "claude-3-5-sonnet-20241022"
            self._backend = _AnthropicProvider(model, key, base_url)

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
        * ``WIZARD_LLM_PROVIDER``  — overrides ``LLM_PROVIDER`` (.env)
        * ``WIZARD_LLM_MODEL``     — overrides the per-provider model
        * ``WIZARD_LLM_BASE_URL``  — optional endpoint override
        """
        cfg = _config_or_none()
        provider = os.getenv("WIZARD_LLM_PROVIDER") or (
            cfg.settings.llm_provider if cfg else "auto"
        )
        return cls(
            provider=provider,
            model=os.getenv("WIZARD_LLM_MODEL") or None,
            base_url=os.getenv("WIZARD_LLM_BASE_URL") or None,
        )
