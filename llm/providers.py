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
from collections.abc import Iterable, Iterator
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


#: Payload keys :func:`load_extra_body` refuses to let ``LLM_EXTRA_BODY``
#: set. Everything here decides *which model is asked what*, or is already
#: a first-class setting with its own validation and its own reporting in
#: the caller-facing ``llm`` status block: letting an environment variable
#: quietly redirect ``model``, rewrite ``messages``, or contradict the
#: ``temperature``/``seed`` that ``observability.llm_status`` reports would
#: make that block describe a request that was never sent.
#:
#: ``max_tokens`` is on the list for the same reason and one more: it is
#: the setting most likely to be reached for here (see
#: ``config.Settings.llm_extra_body_json``'s docstring on reasoning models),
#: and ``LLM_NUM_PREDICT`` is where it belongs -- two places setting one
#: value, with the status block reading only one of them, is exactly the
#: kind of disagreement this project fails loudly on elsewhere.
RESERVED_PAYLOAD_KEYS = frozenset({
    "model", "messages", "temperature", "top_p", "seed", "max_tokens",
    "stop", "stream", "n",
})


class ExtraBodyConfigError(ValueError):
    """``LLM_EXTRA_BODY`` is not a JSON object, or names a reserved key."""


def load_extra_body() -> dict[str, Any]:
    """Parse ``cfg.settings.llm_extra_body_json`` into request fields.

    Read through ``cfg.settings`` on every call rather than cached, so
    ``config.override_settings()`` is visible immediately -- this project's
    configuration contract (see ``config.py``'s module docstring).

    Returns
    -------
    dict[str, Any]
        The fields to merge into a chat-completions body. Empty for an
        unset/blank value, which is the default and sends exactly what
        this project sent before this setting existed.

    Raises
    ------
    ExtraBodyConfigError
        If the value is present but is not valid JSON, is not a JSON
        object, or names one of :data:`RESERVED_PAYLOAD_KEYS`. Every one
        of these is a loud failure rather than a silently-ignored field:
        an operator who set this to turn a model's reasoning off and got
        no error has every reason to believe it worked, and the symptom
        when it did not (an empty response, eventually) points nowhere
        near the typo that caused it.
    """
    raw = (cfg.settings.llm_extra_body_json or "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExtraBodyConfigError(
            f"LLM_EXTRA_BODY is not valid JSON: {exc}. It must be a JSON "
            'object, e.g. {"chat_template_kwargs": {"enable_thinking": false}}'
        ) from exc

    if not isinstance(parsed, dict):
        raise ExtraBodyConfigError(
            "LLM_EXTRA_BODY must be a JSON object of request fields, not "
            f"{type(parsed).__name__} -- these are merged into the request "
            "body, so there is nothing a list or a bare value could mean."
        )

    reserved = sorted(RESERVED_PAYLOAD_KEYS.intersection(parsed))
    if reserved:
        raise ExtraBodyConfigError(
            f"LLM_EXTRA_BODY may not set {', '.join(repr(k) for k in reserved)} "
            "-- those are set from their own settings and reported in the "
            "llm status block, so overriding them here would make that block "
            "describe a request that was never sent. Use LLM_NUM_PREDICT, "
            "LLM_TEMPERATURE, LLM_TOP_P, LLM_SEED and LLM_STOP instead."
        )
    return parsed


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


#: Characters per token for the fallback in :func:`_reasoning_token_stats`
#: when a response carries reasoning text but no token counts at all. The
#: same rough ratio :func:`prompt_engine.static_prefix.estimate_tokens`
#: uses; repeated here because this module must not import
#: ``prompt_engine`` (it pulls in the whole knowledge base).
_CHARS_PER_TOKEN = 4


def _int_or_none(value: Any) -> int | None:
    """*value* as an ``int`` when it is a real, non-boolean number, else ``None``.

    Examples
    --------
    >>> _int_or_none(12), _int_or_none(12.0), _int_or_none("12"), _int_or_none(None)
    (12, 12, None, None)
    >>> _int_or_none(True) is None
    True
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _reasoning_token_stats(
    body: dict[str, Any], reasoning_text: str, content: str,
) -> tuple[int | None, bool]:
    """How many of the completion's tokens were reasoning, and whether that is a guess.

    Three cases, most to least trustworthy:

    1. The server says: ``usage.completion_tokens_details.reasoning_tokens``
       (OpenAI's own field, which some OpenAI-compatible servers fill in).
       Returned as is, ``estimated=False`` -- even when it is ``0``.
    2. The response carries reasoning text but no count. If
       ``usage.completion_tokens`` is known, it is split by the share of
       characters that are reasoning (reasoning text and answer both
       appear in those tokens, and the total is exact, so only the split
       is approximate). Otherwise one token per four characters.
       ``estimated=True``.
    3. No reasoning text and no count: ``(None, False)`` -- "not
       measurable", not "zero". A non-reasoning model and a server that
       hides its reasoning look the same here.

    Only lengths are used. The reasoning text itself is never stored or
    returned: it can quote the prompt, and the audit record that carries
    this number must not hold row values.

    Parameters
    ----------
    body:
        The ``/chat/completions`` response (or its reassembled stream).
    reasoning_text:
        The reasoning text from :func:`_extract_reasoning_text`, or ``""``.
    content:
        The answer text (``message.content``), or ``""``.

    Returns
    -------
    tuple[int | None, bool]
        ``(reasoning_tokens, estimated)``.

    Examples
    --------
    >>> _reasoning_token_stats(
    ...     {"usage": {"completion_tokens": 90, "completion_tokens_details": {"reasoning_tokens": 80}}},
    ...     "", "SELECT 1")
    (80, False)
    >>> _reasoning_token_stats({"usage": {"completion_tokens": 100}}, "a" * 300, "b" * 100)
    (75, True)
    >>> _reasoning_token_stats({}, "a" * 40, "")
    (10, True)
    >>> _reasoning_token_stats({"usage": {"completion_tokens": 20}}, "", "SELECT 1")
    (None, False)
    """
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        reported = _int_or_none(details.get("reasoning_tokens"))
        if reported is not None:
            return max(reported, 0), False
    if not reasoning_text:
        return None, False
    completion_tokens = _int_or_none(usage.get("completion_tokens"))
    if completion_tokens and completion_tokens > 0:
        total_chars = len(reasoning_text) + len(content)
        return round(completion_tokens * len(reasoning_text) / total_chars), True
    return max(1, len(reasoning_text) // _CHARS_PER_TOKEN), True


class StreamError(requests.RequestException):
    """A streamed ``/chat/completions`` response ended, or failed, mid-stream.

    A :class:`requests.RequestException` so that
    :meth:`OpenAIBackend.generate_with_meta` retries it like any other
    transport failure.
    """


class _StreamAssembler:
    """Reassemble ``stream=true`` chunks into the body a non-streaming call returns.

    The streamed protocol splits one response into ``chat.completion.chunk``
    objects: ``choices[0].delta`` carries a piece of ``content`` (and, on
    reasoning models, of a reasoning field), the last choice-bearing chunk
    carries ``finish_reason``, and -- when the request set
    ``stream_options={"include_usage": true}`` -- one more chunk with an
    empty ``choices`` list carries ``usage``. :meth:`body` puts those back
    together as the ``chat.completion`` object the non-streaming request
    would have returned (``choices[0].message.content``, the reasoning
    field under the same name the server streamed it as, ``finish_reason``,
    ``usage``, ``system_fingerprint``), so everything downstream of the
    transport -- reasoning detection, finish-reason mapping, the
    ``llm`` audit block -- reads the same bytes either way.

    Timestamps are passed in by the caller (``perf_counter`` values), so the
    class is a pure function of its input and testable on recorded chunks.

    Examples
    --------
    >>> a = _StreamAssembler()
    >>> a.feed({"id": "c1", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant"}}]}, 1.0)
    >>> a.feed({"choices": [{"index": 0, "delta": {"content": "SEL"}}]}, 1.5)
    >>> a.feed({"choices": [{"index": 0, "delta": {"content": "ECT 1"}, "finish_reason": "stop"}]}, 1.6)
    >>> a.feed({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}, 1.7)
    >>> body = a.body()
    >>> body["choices"][0]["message"]["content"], body["choices"][0]["finish_reason"]
    ('SELECT 1', 'stop')
    >>> body["usage"]
    {'prompt_tokens': 5, 'completion_tokens': 2}
    >>> a.first_token_at
    1.5
    """

    def __init__(self) -> None:
        self._id: Any = None
        self._model: Any = None
        self._created: Any = None
        self._fingerprint: Any = None
        self._role: str = "assistant"
        self._content: list[str] = []
        self._content_seen = False
        self._reasoning: dict[str, list[str]] = {}
        self._finish_reason: Any = None
        self._finished = False
        self._usage: dict[str, Any] | None = None
        self.first_chunk_at: float | None = None
        self.first_token_at: float | None = None

    @property
    def finished(self) -> bool:
        """True once a chunk carried a ``finish_reason``."""
        return self._finished

    def feed(self, chunk: dict[str, Any], t: float) -> None:
        """Fold one decoded chunk into the response.

        Parameters
        ----------
        chunk:
            One decoded ``data:`` payload.
        t:
            When it was received (a ``perf_counter`` value).

        Raises
        ------
        StreamError
            If the chunk is a server-sent error object.
        """
        if not isinstance(chunk, dict):
            raise StreamError("malformed stream chunk (not an object)")
        error = chunk.get("error")
        if error:
            detail = error.get("type") or error.get("code") if isinstance(error, dict) else None
            raise StreamError(f"endpoint reported an error mid-stream ({detail or 'unspecified'})")
        if self.first_chunk_at is None:
            self.first_chunk_at = t
        for key, attr in (
            ("id", "_id"), ("model", "_model"), ("created", "_created"),
            ("system_fingerprint", "_fingerprint"),
        ):
            if chunk.get(key) is not None and getattr(self, attr) is None:
                setattr(self, attr, chunk[key])
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage:
            self._usage = usage
        choices = chunk.get("choices") or []
        choice = next((c for c in choices if isinstance(c, dict) and c.get("index", 0) == 0), None)
        if choice is None:
            return
        delta = choice.get("delta") or {}
        if isinstance(delta.get("role"), str):
            self._role = delta["role"]
        produced = False
        content = delta.get("content")
        if content is not None:
            self._content_seen = True
            if content:
                self._content.append(str(content))
                produced = True
        for key in _REASONING_FIELD_NAMES:
            piece = delta.get(key)
            if isinstance(piece, str):
                self._reasoning.setdefault(key, []).append(piece)
                produced = produced or bool(piece)
        if produced and self.first_token_at is None:
            self.first_token_at = t
        if choice.get("finish_reason") is not None:
            self._finish_reason = choice["finish_reason"]
            self._finished = True

    def body(self) -> dict[str, Any]:
        """The reassembled ``chat.completion`` body.

        ``message.content`` is ``None`` when no chunk ever carried a
        ``content`` field, which is what a non-streaming server returns for
        a response that is all reasoning -- so the two paths treat that
        response identically.
        """
        message: dict[str, Any] = {
            "role": self._role,
            "content": "".join(self._content) if self._content_seen else None,
        }
        for key, parts in self._reasoning.items():
            message[key] = "".join(parts)
        body: dict[str, Any] = {
            "id": self._id,
            "object": "chat.completion",
            "created": self._created,
            "model": self._model,
            "choices": [{"index": 0, "message": message, "finish_reason": self._finish_reason}],
        }
        if self._fingerprint is not None:
            body["system_fingerprint"] = self._fingerprint
        if self._usage is not None:
            body["usage"] = self._usage
        return body


def _iter_sse_events(lines: Iterable[bytes | str]) -> Iterator[dict[str, Any] | None]:
    """Decode server-sent-event lines into chunk objects; ``None`` marks ``[DONE]``.

    Only ``data:`` lines matter; comments (``: keep-alive``), ``event:``
    lines and blanks are skipped. Lines arrive as bytes and are decoded as
    UTF-8 explicitly: ``requests`` would otherwise guess ISO-8859-1 for a
    ``text/event-stream`` response with no charset, which turns every
    Persian character into mojibake.

    Raises
    ------
    StreamError
        On a ``data:`` line that is not valid JSON.

    Examples
    --------
    >>> list(_iter_sse_events([b": ping", b"", b'data: {"a": 1}', b"data: [DONE]"]))
    [{'a': 1}, None]
    """
    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            yield None
            return
        try:
            yield json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StreamError("malformed stream chunk (invalid JSON)") from exc


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
        # Merged last, but unable to reach anything above it:
        # load_extra_body rejects every key set here (RESERVED_PAYLOAD_KEYS)
        # rather than letting the merge win, so this can only ever ADD
        # server-specific fields -- never quietly redirect the request.
        payload.update(load_extra_body())
        return payload

    def _post_streaming(
        self, payload: dict[str, Any], start: float,
    ) -> tuple[dict[str, Any], int, int | None]:
        """POST *payload* (already ``stream=true``) and reassemble the response.

        Parameters
        ----------
        payload:
            The request body, with ``stream`` and ``stream_options`` set.
        start:
            ``time.perf_counter()`` taken just before the request, so the
            time to first token is measured from the same origin as
            ``total_ms``.

        Returns
        -------
        tuple[dict[str, Any], int, int | None]
            ``(body, http_status, ttft_ms)`` -- *body* is the
            ``chat.completion`` object :class:`_StreamAssembler` rebuilt,
            *ttft_ms* the time to the first content or reasoning token (or
            to the first chunk of any kind when the response had no token
            at all).

        Raises
        ------
        requests.Timeout
            If the whole call takes longer than the backend's timeout (the
            per-read timeout alone would let a slow but steady stream run
            for ever, which the non-streaming request cannot).
        StreamError
            If the stream carries an error object, a malformed chunk, or
            ends before a ``finish_reason`` or ``[DONE]``.
        requests.HTTPError
            For a non-2xx status, exactly as the non-streaming call.
        """
        resp = requests.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers,
            json=payload,
            timeout=self._timeout,
            stream=True,
        )
        try:
            resp.raise_for_status()
            assembler = _StreamAssembler()
            done = False
            # chunk_size=64, not requests' default of 512: on a response that
            # is not chunk-encoded (close-delimited), a read of N bytes
            # blocks until N have arrived, which would hold back the first
            # token's timestamp -- the one number this path exists to
            # measure -- by however long the next tokens take. An SSE event
            # is longer than 64 bytes, so the first one is never held back;
            # byte-at-a-time (1) fixes the same thing but costs ~7x the CPU
            # (measured: 343 vs 65 ms per 2000-event response, against 53 at
            # 512). A chunk-encoded response, which is what vLLM and
            # llama.cpp send, yields as each piece arrives at any size.
            for event in _iter_sse_events(resp.iter_lines(chunk_size=64)):
                now = time.perf_counter()
                if now - start > self._timeout:
                    raise requests.Timeout(
                        f"streamed response exceeded the {self._timeout}s timeout"
                    )
                if event is None:
                    done = True
                    break
                assembler.feed(event, now)
            if not (done or assembler.finished):
                raise StreamError("stream ended before the response was complete")
            first = assembler.first_token_at
            if first is None:
                first = assembler.first_chunk_at
            ttft_ms = round((first - start) * 1000) if first is not None else None
            return assembler.body(), resp.status_code, ttft_ms
        finally:
            resp.close()

    def warm_prefix(self, prefix: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Send *prefix* alone as a one-token request, to prime the server's prefix cache.

        The body is the one :meth:`_build_payload` builds for a real request
        (model, sampling fields, ``LLM_EXTRA_BODY``), with ``max_tokens``
        forced to ``1`` and ``temperature`` to ``0``, so the server
        computes -- and a server with prefix caching keeps -- the KV cache
        for exactly the tokens a real request starts with. One attempt, no
        retries, and always non-streaming: a warm-up that fails is simply
        replaced by the first real question.

        Parameters
        ----------
        prefix:
            The static prompt prefix, byte for byte what the real requests
            start with (:func:`llm.router.build_prompt_segments`).
        timeout:
            HTTP timeout in seconds; the backend's own when ``None``.

        Returns
        -------
        dict[str, Any]
            ``{"prompt_tokens", "cached_tokens", "endpoint_status"}``. The
            first two are ``None`` when the server does not report them
            (``cached_tokens`` needs vLLM's ``--enable-prompt-tokens-details``).
            Never the prompt or the response text.

        Raises
        ------
        requests.RequestException
            Any transport failure, a timeout, or a non-2xx status.
        ExtraBodyConfigError
            If ``LLM_EXTRA_BODY`` is invalid.

        Examples
        --------
        >>> from unittest.mock import MagicMock, patch
        >>> reply = MagicMock(status_code=200)
        >>> reply.json.return_value = {"usage": {"prompt_tokens": 4600}}
        >>> with patch("llm.providers.requests.post", return_value=reply) as post:
        ...     info = OpenAIBackend(model="m", api_key="k").warm_prefix("PREFIX")
        >>> post.call_args.kwargs["json"]["max_tokens"], post.call_args.kwargs["json"]["messages"]
        (1, [{'role': 'user', 'content': 'PREFIX'}])
        >>> info
        {'prompt_tokens': 4600, 'cached_tokens': None, 'endpoint_status': 200}
        """
        payload = self._build_payload(prefix)
        payload["max_tokens"] = 1
        payload["temperature"] = 0
        resp = requests.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers,
            json=payload,
            timeout=timeout if timeout is not None else self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        return {
            "prompt_tokens": _int_or_none(usage.get("prompt_tokens")),
            "cached_tokens": _int_or_none(details.get("cached_tokens")),
            "endpoint_status": resp.status_code,
        }

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
        * ``"reasoning_tokens"`` / ``"reasoning_tokens_estimated"`` — how
          many completion tokens were reasoning, and whether that figure is
          the server's own (``usage.completion_tokens_details.reasoning_tokens``)
          or an estimate from the reasoning text's length -- see
          :func:`_reasoning_token_stats`. ``None`` / ``False`` when the
          response shows no reasoning at all.
        * ``"ttft_ms"`` / ``"generation_ms"`` — only with
          ``LLM_STREAM_TIMINGS=true``: time to the first generated token
          (queue plus prefill) and the time from there to the end of the
          stream; ``ttft_ms + generation_ms == total_ms``. ``None`` for a
          non-streaming call, which cannot see inside its own wait.

        With ``LLM_STREAM_TIMINGS=true`` the request is sent with
        ``stream=true`` and ``stream_options={"include_usage": true}`` and
        the chunks are reassembled by :class:`_StreamAssembler` into the
        body a non-streaming call would have returned, so ``raw`` and every
        field above are derived from the same shape either way. The timeout
        then bounds the whole call (checked as chunks arrive) as well as the
        wait for each chunk.

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
        streaming = bool(cfg.settings.llm_stream_timings)
        if streaming:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

        last_exc: Exception | None = None

        for attempt in range(1, self._retries + 1):
            # perf_counter, not monotonic: monotonic() is GetTickCount64-backed
            # on Windows before Python 3.13 (~15.6ms resolution), which would
            # quantise -- or zero out -- total_ms for any fast response.
            start = time.perf_counter()
            try:
                ttft_ms: int | None = None
                if streaming:
                    body, status_code, ttft_ms = self._post_streaming(payload, start)
                else:
                    resp = requests.post(
                        f"{self._base_url}/chat/completions",
                        headers=self._headers,
                        json=payload,
                        timeout=self._timeout,
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    status_code = resp.status_code
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
                # The answer's own text, not `raw`: a response that is all
                # reasoning has `content: null`, which `raw` renders as the
                # four characters "None" -- and those are not answer tokens.
                answer_text = message.get("content")
                reasoning_tokens, reasoning_estimated = _reasoning_token_stats(
                    body, reasoning_text, answer_text if isinstance(answer_text, str) else "",
                )

                total_ms = round((time.perf_counter() - start) * 1000)
                meta: dict[str, Any] = {
                    "raw": body,
                    "endpoint_status": status_code,
                    "attempts": attempt,
                    "total_ms": total_ms,
                    "finish_reason": _normalize_finish_reason(choice.get("finish_reason")),
                    "reasoning_detected": reasoning_detected,
                    "reasoning_tokens": reasoning_tokens,
                    "reasoning_tokens_estimated": reasoning_estimated,
                    "ttft_ms": ttft_ms,
                    "generation_ms": (
                        max(total_ms - ttft_ms, 0) if ttft_ms is not None else None
                    ),
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
        structured_payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": segments.flatten()}],
            "temperature": cfg.settings.llm_temperature,
            "top_p": cfg.settings.llm_top_p,
            "seed": cfg.settings.llm_seed,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": True},
            },
        }
        # Same merge as the unstructured path. A constrained decode is
        # still a decode: a reasoning model asked for JSON reasons first
        # and can be cut off mid-object, so an operator who turned
        # reasoning off for one path has not turned it off at all unless
        # it applies here too.
        structured_payload.update(load_extra_body())
        resp = requests.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers,
            json=structured_payload,
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
