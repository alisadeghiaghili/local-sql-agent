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

#: ``finish_reason`` values an OpenAI-compatible ``/chat/completions``
#: response can carry that this project's contract already names 1:1 (see
#: ``observability.llm_status._VALID_FINISH_REASONS``). Anything else is
#: preserved, not discarded -- see :func:`_normalize_finish_reason`.
_KNOWN_FINISH_REASONS = frozenset({"stop", "length", "content_filter", "tool_calls"})

#: Field names, across OpenAI-compatible servers this project has seen
#: documented, that carry a model's reasoning/chain-of-thought text
#: separately from its final answer in ``content``. Never verified against
#: a live gpt-oss endpoint -- see the module docstring and
#: :func:`_extract_reasoning_text`.
_REASONING_FIELD_NAMES = ("reasoning_content", "reasoning", "thinking", "thought")

#: Literal markers that show up in ``content`` itself when a server's
#: OpenAI-compatible shim fails to separate a model's reasoning channel out
#: of its response -- gpt-oss's own "harmony" response format uses
#: ``<|channel|>``/``<|start|>``/``<|message|>`` control tokens, and several
#: other reasoning models (and the servers that front them) use a
#: ``<think>...</think>`` block. Kept intentionally narrow (literal,
#: well-known markers only) so this never misfires on an ordinary SQL
#: response -- see :func:`_content_carries_reasoning_markers`.
_REASONING_MARKER_RE = re.compile(
    r"<\|channel\|>\s*analysis|<\|start\|>assistant<\|channel\|>|<think>", re.IGNORECASE,
)


def _normalize_finish_reason(raw_reason: Any) -> str:
    """Map a raw OpenAI-compatible ``finish_reason`` onto this project's contract.

    ``"stop"``/``"length"`` -- the two values the original four-value
    contract (``stop | length | schema_violation | error``) already named --
    pass through unchanged. ``"content_filter"`` (a moderation block) and
    ``"tool_calls"`` (the model tried to call a function instead of
    answering) are real values several OpenAI-compatible servers emit that
    the original contract didn't anticipate; this project's contract now
    recognises them too (see ``observability/llm_status.py``'s
    ``_VALID_FINISH_REASONS`` and ``docs/api-contract-v2.md`` §6) rather
    than collapsing them into the generic ``"error"`` bucket, which would
    make a moderation block indistinguishable, in a week of audit logs,
    from a dead endpoint.

    Anything else -- a server-specific string this project has never seen,
    or a missing/non-string ``finish_reason`` field entirely -- is
    preserved behind an ``"other:"`` prefix instead of being discarded or
    forced into ``"error"``: the whole point of deriving this value at all
    is that an operator reading the log should see what the endpoint
    actually said, not a value this module made up because it didn't
    recognise the real one. A missing/absent value becomes ``"other:none"``
    for the same reason: silently defaulting it to ``"stop"`` would claim a
    complete, successful generation that was never confirmed.

    Examples
    --------
    >>> _normalize_finish_reason("stop")
    'stop'
    >>> _normalize_finish_reason("length")
    'length'
    >>> _normalize_finish_reason("content_filter")
    'content_filter'
    >>> _normalize_finish_reason("tool_calls")
    'tool_calls'
    >>> _normalize_finish_reason("eos_token")
    'other:eos_token'
    >>> _normalize_finish_reason(None)
    'other:none'
    """
    if isinstance(raw_reason, str) and raw_reason in _KNOWN_FINISH_REASONS:
        return raw_reason
    label = raw_reason if isinstance(raw_reason, str) and raw_reason.strip() else "none"
    return f"other:{label}"


def _extract_reasoning_text(message: Any) -> str:
    """Best-effort extraction of a model's reasoning text from *message*.

    Several OpenAI-compatible servers expose a model's chain-of-thought
    under a field separate from ``content`` -- most commonly
    ``reasoning_content`` (vLLM's DeepSeek-R1-style convention, also used by
    some gpt-oss deployments), sometimes ``reasoning`` or
    ``thinking``/``thought`` on other community servers. This project has
    never been tested against a live gpt-oss endpoint (see the module
    docstring), so this check only reads well-known field names and never
    guesses at new ones.

    Used purely for *detection* -- see :meth:`OpenAIBackend.generate_with_meta`:
    the real ``content`` field is always what is returned as the model's
    answer. This function's result is never substituted for it; silently
    stripping reasoning prose and hoping what remains is SQL would turn a
    diagnosable protocol mismatch into a mysterious accuracy problem.
    """
    if not isinstance(message, dict):
        return ""
    for key in _REASONING_FIELD_NAMES:
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _content_carries_reasoning_markers(content: str) -> bool:
    """True when *content* itself contains raw reasoning-channel markup.

    Some servers -- notably a gpt-oss deployment whose OpenAI-compatible
    shim doesn't fully separate the model's own "harmony" response format
    into distinct fields -- can leak internal channel markers
    (``<|channel|>analysis``, ...) or a ``<think>...</think>`` block
    straight into ``content`` instead of a separate reasoning field. Never
    verified against a live endpoint (see the module docstring); kept
    intentionally narrow (literal, well-known markers only) so it never
    misfires on an ordinary SQL response.
    """
    return bool(content) and bool(_REASONING_MARKER_RE.search(content))


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
        * ``"finish_reason"`` — the response's ``choices[0].finish_reason``,
          mapped onto this project's contract by
          :func:`_normalize_finish_reason`: ``"stop"``/``"length"`` pass
          through, ``"content_filter"``/``"tool_calls"`` are recognised
          too, and anything else is preserved as ``"other:<raw>"`` rather
          than being discarded. Never hardcoded — this is what lets a
          caller tell a truncated response (``"length"``) from a genuinely
          complete one (``"stop"``) instead of both reading as success.
        * ``"reasoning_detected"`` — ``True`` when the response appears to
          carry a model's reasoning/chain-of-thought text (see
          :func:`_extract_reasoning_text` and
          :func:`_content_carries_reasoning_markers`) rather than, or in
          addition to, a final answer. Detection only; ``raw`` is always
          ``content`` itself, never the reasoning text.

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
                choice: dict[str, Any] = (body.get("choices") or [{}])[0]
                message: dict[str, Any] = choice.get("message") or {}
                raw: str = str(message.get("content", "")).strip()
                logger.debug("OpenAI raw (attempt %d): %.300s", attempt, raw)

                # Reasoning-channel detection (see module docstring's note
                # on the gpt-oss deployment target and the helpers above):
                # never substituted for `raw` -- only ever used to flag the
                # response as suspect, in `meta`, so a downstream "No
                # SELECT / CTE found" rejection reads as a protocol
                # mismatch instead of the model looking incompetent at SQL.
                reasoning_text = _extract_reasoning_text(message)
                reasoning_detected = bool(reasoning_text) or _content_carries_reasoning_markers(raw)
                if reasoning_detected:
                    logger.warning(
                        "OpenAI response (attempt %d) appears to carry reasoning-channel "
                        "text rather than a final answer; excerpt: %.200s",
                        attempt, reasoning_text or raw,
                    )

                meta: dict[str, Any] = {
                    "raw": body,
                    "endpoint_status": resp.status_code,
                    "attempts": attempt,
                    "total_ms": round((time.monotonic() - start) * 1000),
                    "finish_reason": _normalize_finish_reason(choice.get("finish_reason")),
                    "reasoning_detected": reasoning_detected,
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
        choice: dict[str, Any] = (body.get("choices") or [{}])[0]
        text = choice["message"]["content"]
        # A schema-constrained decode can still be cut off by max_tokens
        # (a half-emitted JSON object) -- derive finish_reason the same way
        # generate_with_meta does rather than assuming "stop" here too, for
        # the same reason: json.loads(text) below would already raise on a
        # truncated body, but if a future caller catches that and falls
        # back to *meta*, it must not read "structured_output: True" next
        # to a finish_reason that silently claims a clean completion.
        meta = {
            "raw": body,
            "endpoint_status": resp.status_code,
            "attempts": 1,
            "structured_output": True,
            "finish_reason": _normalize_finish_reason(choice.get("finish_reason")),
        }
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
