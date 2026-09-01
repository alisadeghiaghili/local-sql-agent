# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The OpenAI-compatible LLM transport shared by :mod:`llm.router` and :mod:`llm.wizard_llm`.

Before this module existed, provider switching lived only in
``llm/wizard_llm.py`` (built for the interactive setup wizard) while the
production engine was hardwired to Ollama with no way to swap providers at
all. Later, Phase 2 added :class:`OpenAIBackend` and ``AnthropicBackend``
here as two of several interchangeable hosted transports behind
:mod:`llm.router`.

That premise changed. The Ollama-specific transport and the Anthropic
Messages API transport are both gone: **OpenAI-compatible is the only
protocol this project speaks**, and ``base_url`` is what selects the
endpoint — a user's local ``gpt-oss`` server behind an OpenAI-compatible
API, a self-hosted vLLM/llama.cpp instance, or OpenAI's own hosted API are
all just an :class:`OpenAIBackend` with a different ``base_url``. Routing
across *multiple* such endpoints, with fallback chains and per-task
selection, is what :mod:`llm.router` and :mod:`llm.endpoints` are for —
this module only supplies the one transport class they route through, plus
:class:`MockBackend` for tests.

Because every real endpoint is now the same class, trust (whether an
endpoint may see schema/business-rule/row data) can no longer be decided
by class name — see :mod:`llm.trust` and :func:`llm.router.is_trusted_backend`
for why, and how it is decided instead.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

import config as cfg
from llm.base import LLMBackend
from llm.trust import default_trust_for_url

logger = logging.getLogger(__name__)

_TIMEOUT: int = 120
_RETRIES: int = 3
_BACKOFF_BASE: int = 2

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

_OUT_OF_SCOPE_SENTINEL = "OUT_OF_SCOPE"


def _strip_fences(text: str) -> str:
    """Remove the outermost markdown code fence from *text*, if present."""
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def _find_json_substring(text: str) -> str:
    """Return the substring starting from the first ``{`` or ``[`` character."""
    for i, ch in enumerate(text):
        if ch in ("{", "["):
            return text[i:]
    raise ValueError(f"No JSON object found in response. First 200 chars: {text[:200]!r}")


def parse_json_response(text: str) -> dict | list:
    """Best-effort JSON extraction: strip fences, find the start, parse.

    Shared fallback for :meth:`LLMBackend.generate_structured`'s default
    implementation (string-parse a plain-text response) when a provider
    has no native constrained-decoding path.

    Uses :class:`json.JSONDecoder.raw_decode` rather than a plain
    ``json.loads`` on the whole tail: a model that answers "here is the
    JSON: {...} let me know if you need anything else" produces valid
    JSON followed by trailing prose, which ``json.loads`` rejects outright
    (``Extra data``) even though the object itself parsed fine.
    ``raw_decode`` parses just the first JSON value and ignores whatever
    follows it.

    Raises
    ------
    ValueError
        If no valid JSON can be extracted from *text*.

    Examples
    --------
    >>> parse_json_response('```json\\n{"a": 1}\\n```')
    {'a': 1}
    >>> parse_json_response('here is the result: {"a": 1} done')
    {'a': 1}
    """
    cleaned = _strip_fences(text)
    json_str = _find_json_substring(cleaned)
    try:
        obj, _end = json.JSONDecoder().raw_decode(json_str)
        return obj
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse error: {exc}. Text: {json_str[:200]!r}") from exc


class OpenAIBackend(LLMBackend):
    """The one real transport: an OpenAI-compatible ``/chat/completions`` endpoint.

    Works unchanged against OpenAI's own hosted API, a self-hosted
    ``gpt-oss``/vLLM/llama.cpp server, or LM Studio — anything that speaks
    the OpenAI chat-completions wire format, selected entirely by
    *base_url*. Mirrors the production-grade contract the retired
    Ollama-specific transport used to provide on its own: transport
    retries with exponential back-off, the ``ValueError("OUT_OF_SCOPE")``
    sentinel, and deterministic-decoding request fields — none of that is
    specific to Ollama, so none of it should have been lost when Ollama
    was.

    Parameters
    ----------
    model:
        Model identifier, e.g. ``"gpt-4o-mini"`` or a local server's own
        tag (``"gpt-oss-20b"``).
    api_key:
        Bearer token. Empty is valid — many self-hosted OpenAI-compatible
        servers don't check it.
    base_url:
        API base URL. Defaults to ``https://api.openai.com/v1``.
    trusted:
        Whether this endpoint may see schema/business-rule/row data. When
        ``None`` (the default), resolved from *base_url* via
        :func:`~llm.trust.default_trust_for_url` — loopback/private/``.local``
        addresses are trusted by default, everything else (including the
        factory-default ``https://api.openai.com/v1``) is not. An explicit
        ``True``/``False`` always wins over that default. See
        :func:`~llm.router.is_trusted_backend`.
    retries:
        Number of transport-level retries with exponential back-off.
    timeout:
        Per-request timeout in seconds.

    Examples
    --------
    >>> OpenAIBackend(model="m", api_key="k", base_url="http://localhost:8000/v1").trusted
    True
    >>> OpenAIBackend(model="m", api_key="k").trusted
    False
    >>> OpenAIBackend(model="m", api_key="k", base_url="http://localhost:8000/v1", trusted=False).trusted
    False
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        *,
        trusted: bool | None = None,
        retries: int = _RETRIES,
        timeout: int = _TIMEOUT,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._trusted = trusted if trusted is not None else default_trust_for_url(self._base_url)
        self._retries = retries
        self._timeout = timeout

    @classmethod
    def from_settings(cls) -> "OpenAIBackend":
        """Build the trivial single-endpoint backend from :mod:`config`.

        Reads ``cfg.settings.openai_base_url`` / ``openai_model`` /
        ``openai_api_key`` — the plain ``OPENAI_*`` variables, with no
        ``LLM_ENDPOINTS``/``LLM_ROUTES`` multi-endpoint configuration
        involved. Used wherever a single, config-driven backend is enough
        on its own: ``llm/wizard_llm.py``, ``app.py``'s REPL,
        ``eval/cli.py --live``. :meth:`~llm.router.LLMRouter.from_settings`
        does NOT use this — it goes through :mod:`llm.endpoints` instead,
        so a multi-endpoint deployment is honoured for production traffic
        even though these simpler call sites only ever need the one
        endpoint.

        Examples
        --------
        >>> import config as cfg
        >>> with cfg.override_settings(openai_model="m", openai_api_key="k"):
        ...     backend = OpenAIBackend.from_settings()
        >>> backend.name
        'openai:m'
        """
        return cls(
            model=cfg.settings.openai_model,
            api_key=cfg.settings.openai_api_key,
            base_url=cfg.settings.openai_base_url,
        )

    @property
    def name(self) -> str:
        return f"openai:{self._model}"

    @property
    def trusted(self) -> bool:
        return self._trusted

    @property
    def endpoint(self) -> str | None:
        return self._base_url

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_payload(self, prompt: str) -> dict[str, Any]:
        """Build the ``/chat/completions`` request body for *prompt*.

        Every sampling knob is read from :mod:`config` at call time, same
        as the retired Ollama transport, and for the same reason: the same
        question, run twice, must produce byte-identical SQL, which needs
        ``temperature=0``, ``top_p=1``, and a fixed ``seed`` sent on every
        request. Unlike Ollama's own request shape (a nested ``options``
        object), the OpenAI-compatible wire format sends these as
        top-level sibling fields of ``model``/``messages``.

        ``seed`` is honoured by vLLM and llama.cpp; OpenAI's own hosted API
        accepts the field but does not guarantee determinism from it (see
        its docs on ``system_fingerprint``) — sent regardless, since it is
        never harmful, and the caller-facing status block reports whether
        the endpoint *claimed* to honour it rather than asserting
        determinism this module cannot verify (see
        ``observability.llm_status.build_llm_status``'s ``seed_honored``).

        Examples
        --------
        >>> backend = OpenAIBackend(model="m", api_key="k")
        >>> payload = backend._build_payload("hello")
        >>> payload["model"], payload["messages"]
        ('m', [{'role': 'user', 'content': 'hello'}])
        >>> sorted(k for k in payload if k not in ("model", "messages"))
        ['max_tokens', 'seed', 'temperature', 'top_p']
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": cfg.settings.llm_temperature,
            "top_p": cfg.settings.llm_top_p,
            "seed": cfg.settings.llm_seed,
            "max_tokens": cfg.settings.llm_num_predict,
        }
        if cfg.settings.llm_stop:
            payload["stop"] = list(cfg.settings.llm_stop)
        return payload

    def generate(self, prompt: str) -> str:
        """POST *prompt* and return the raw response string.

        Thin wrapper over :meth:`generate_with_meta` that discards the
        metadata half of its return value — see that method for the full
        retry/error contract, which is identical here.
        """
        raw, _meta = self.generate_with_meta(prompt)
        return raw

    def generate_with_meta(self, prompt: str) -> tuple[str, dict[str, Any]]:
        """POST *prompt*; return ``(raw_text, meta)``.

        *meta* carries the raw response body (undiscarded) plus call-level
        facts the body itself cannot express:

        * ``"raw"`` — the full ``resp.json()`` dict, including ``usage``
          (``prompt_tokens`` / ``completion_tokens``) and
          ``system_fingerprint`` where the endpoint returns one — exactly
          what ``observability/llm_status.py::build_llm_status`` expects
          as its ``raw`` argument.
        * ``"endpoint_status"`` — the HTTP status code of the attempt that
          produced this result.
        * ``"attempts"`` — which transport attempt (1-based) succeeded.
        * ``"total_ms"`` — wall-clock time of the successful attempt, in
          milliseconds, measured here (never inferred or guessed) since an
          OpenAI-compatible response carries no server-side timing of its
          own for :func:`~observability.llm_status.build_llm_status` to
          read.

        On the ``OUT_OF_SCOPE`` sentinel, *meta* is attached to the raised
        ``ValueError`` as an ``llm_meta`` attribute (rather than lost),
        since that is still a genuine, successful model response — just
        one the caller must not treat as a corrected-SQL attempt.

        Raises
        ------
        ValueError("OUT_OF_SCOPE")
            Passed through from the model sentinel; carries ``.llm_meta``.
        requests.Timeout
            Propagated immediately — not retried. Caller maps to ModelTimeoutError.
        RuntimeError
            When the endpoint is unreachable, or every retry's response
            body was unparsable, after all retries.
        """
        payload = self._build_payload(prompt)

        last_exc: Exception | None = None

        for attempt in range(1, self._retries + 1):
            start = time.monotonic()
            try:
                resp = requests.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers,
                    json=payload,
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                body: dict[str, Any] = resp.json()
                raw: str = str(
                    body.get("choices", [{}])[0].get("message", {}).get("content", "")
                ).strip()
                logger.debug("OpenAI raw (attempt %d): %.300s", attempt, raw)

                meta: dict[str, Any] = {
                    "raw": body,
                    "endpoint_status": resp.status_code,
                    "attempts": attempt,
                    "total_ms": round((time.monotonic() - start) * 1000),
                }

                if raw.strip().upper() == _OUT_OF_SCOPE_SENTINEL:
                    exc = ValueError(_OUT_OF_SCOPE_SENTINEL)
                    exc.llm_meta = meta  # type: ignore[attr-defined]
                    raise exc

                return raw, meta

            except requests.Timeout:
                # Timeout is a hard failure -- propagate immediately, do not retry.
                raise
            except requests.RequestException as exc:
                # Mirrors the retired Ollama transport's discrimination:
                # requests.exceptions.JSONDecodeError subclasses BOTH
                # ValueError and RequestException, so a truncated/non-JSON
                # body from a flaky endpoint is caught here, not by a
                # `except ValueError` clause (there is none) that would
                # otherwise also catch -- and mask -- the OUT_OF_SCOPE
                # sentinel raised two lines above (a plain ValueError, not
                # a RequestException, so it never reaches this branch).
                last_exc = exc
                wait = _BACKOFF_BASE ** (attempt - 1)
                logger.warning(
                    "OpenAI attempt %d/%d failed: %s -- retrying in %ds",
                    attempt, self._retries, exc, wait,
                )
                if attempt < self._retries:
                    time.sleep(wait)

        raise RuntimeError(
            f"OpenAI-compatible endpoint {self._base_url!r} unreachable "
            f"after {self._retries} retries: {last_exc}"
        )

    def generate_structured(self, segments: "PromptSegments", schema: dict) -> tuple[dict, dict[str, Any]]:
        """Constrained decoding via ``response_format: json_schema`` (strict mode)."""
        resp = requests.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers,
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": segments.flatten()}],
                "temperature": cfg.settings.llm_temperature,
                "top_p": cfg.settings.llm_top_p,
                "seed": cfg.settings.llm_seed,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "result", "schema": schema, "strict": True},
                },
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        text = body["choices"][0]["message"]["content"]
        meta = {"raw": body, "endpoint_status": resp.status_code, "attempts": 1, "structured_output": True}
        return json.loads(text), meta

    def test_connection(self) -> bool:
        try:
            r = requests.get(f"{self._base_url}/models", headers=self._headers, timeout=5)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False


class MockBackend(LLMBackend):
    """Deterministic stub backend — no network, for CI / the router's own tests.

    Trusted by default (inherits :attr:`~llm.base.LLMBackend.trusted`'s
    ``True`` default): a stub with no real endpoint is not what the
    remote-provider governance gate exists to catch — see that property's
    docstring.
    """

    def __init__(self, response: str = "SELECT 1", structured: dict | None = None) -> None:
        self._response = response
        self._structured = structured if structured is not None else {}

    @property
    def name(self) -> str:
        return "mock:stub"

    def generate(self, prompt: str) -> str:  # noqa: ARG002
        return self._response

    def generate_with_meta(self, prompt: str) -> tuple[str, dict[str, Any]]:  # noqa: ARG002
        return self._response, {"raw": {}, "endpoint_status": 200, "attempts": 1}

    def generate_structured(self, segments: "PromptSegments", schema: dict) -> tuple[dict, dict[str, Any]]:  # noqa: ARG002
        return dict(self._structured), {"raw": {}, "endpoint_status": 200, "attempts": 1, "structured_output": True}

    def test_connection(self) -> bool:
        return True
