# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Build the ``docs/api-contract-v2.md`` §6 ``llm`` status block.

An OpenAI-compatible chat-completions response carries token counts under
``usage.prompt_tokens`` / ``usage.completion_tokens`` — this module reads
those, not Ollama's ``prompt_eval_count`` / ``eval_count`` (the shape this
module used to read, back when Ollama was the only transport).

What changed, and what didn't, moving off Ollama
--------------------------------------------------
* **Token counts** now come from ``raw["usage"]`` instead of top-level
  ``prompt_eval_count``/``eval_count`` fields.
* **``prefill_ms``/``decode_ms`` are always ``None``.** Ollama's response
  separated prompt-evaluation time from generation time
  (``prompt_eval_duration``/``eval_duration``); an OpenAI-compatible
  response does not distinguish prefill from decode at all. Reporting a
  number here would mean inventing one, which this module does not do —
  see ``total_ms`` below for the one timing figure that IS still genuine.
* **``total_ms`` is caller-supplied, not derived.** There is no
  server-side total-duration field in the OpenAI response shape either;
  :class:`~llm.providers.OpenAIBackend` measures the real wall-clock time
  of its own request and passes it through as *total_ms*, so this is
  still a measured number, never a guess.
* **``tokens_per_second`` needs ``decode_ms``, which no longer exists**, so
  it is ``None`` rather than a value computed against a number this
  module doesn't have.
* **``seed_honored`` is new.** ``temperature``/``top_p``/``seed`` are still
  sent on every request (see :meth:`~llm.providers.OpenAIBackend._build_payload`),
  but ``seed`` is honoured by vLLM/llama.cpp and merely accepted-without-
  guarantee by OpenAI's own hosted API. Rather than asserting determinism
  this process cannot verify, this field reports whether the endpoint's
  response carried a ``system_fingerprint`` — the mechanism OpenAI's own
  docs describe for detecting when a backend change might affect
  reproducibility — as ``True``, or ``None`` when the response carries no
  such signal at all (never ``False``: the absence of the field means
  "unknown", not "confirmed not honoured"). It can also only be ``True``
  when *seed* itself is not ``None`` — a ``system_fingerprint`` on a call
  that never asked for a seed does not mean the (nonexistent) seed was
  honoured; see :func:`build_llm_status`'s ``seed`` parameter.
* **``endpoint``/``trusted`` are new**, threaded from
  :attr:`~llm.base.LLMBackend.endpoint` / :attr:`~llm.base.LLMBackend.trusted`
  of whichever backend in the router's fallback chain actually answered
  (see ``llm.router.LLMRouter._finalize_meta``) — the same "which backend
  answered" story ``provider``/``fallback_used`` already told, extended to
  "and was it trusted, and what endpoint was that".
* **``finish_reason`` is now derived, not caller-hardcoded.**
  :meth:`~llm.providers.OpenAIBackend.generate_with_meta` reads
  ``choices[0].finish_reason`` off the response itself (see
  :func:`~llm.providers._normalize_finish_reason`) instead of every call
  site passing a literal ``"stop"`` — the gap that made a truncated
  response (``finish_reason: "length"``) indistinguishable from a clean
  completion in the audit log. The contract's recognised set grew from
  four values to six (``content_filter``, ``tool_calls`` added — real
  values an OpenAI-compatible endpoint can return), and an ``"other:<raw>"``
  passthrough covers anything else, so a genuinely novel value is still
  visible in the log instead of being coerced into ``"error"``. See
  ``docs/api-contract-v2.md`` §6.
* **``reasoning_detected`` is new.** The deployment target (gpt-oss) emits
  a reasoning/chain-of-thought channel this project has never previously
  read at all; when the model's answer looks like it landed there instead
  of (or alongside) ``content``, this flag says so, turning what would
  otherwise present as a generic "no SELECT found" parse failure into a
  diagnosable protocol mismatch. See
  :func:`~llm.providers._extract_reasoning_text` and
  :func:`~llm.providers._content_carries_reasoning_markers` — both
  deliberately conservative and unverified against a live gpt-oss
  endpoint. Deliberately a boolean, not a text excerpt: see this field's
  parameter docstring below for why.

``prefix_cache_hit``
---------------------
Unchanged in spirit, sourced from the new token fields: ``prefix_cache_hit
= prompt_tokens < (static_prefix_tokens * 0.5)``. Taken literally that
formula is true whenever ``prompt_tokens == 0`` — which is exactly what a
*total transport failure* looks like (no request ever reached the model,
so no prompt tokens were ever evaluated). Reporting a cache "hit" for a
request that never ran is worse than unhelpful, it is actively misleading.
The web UI already found and fixed the equivalent bug at render time
(``web/js/render/llm-status.js``, commit ``d78c70f``, which suppresses the
cache badge entirely when ``prompt_tokens <= 0``). The server has no
render step to hide behind, so the guard has to live in the derivation
itself: :func:`build_llm_status` never reports ``prefix_cache_hit = True``
when ``prompt_tokens`` is ``0``, regardless of the raw formula.
"""

from __future__ import annotations

from typing import Any, Mapping, TypedDict

#: ``finish_reason`` values recognised by the contract as first-class,
#: literal values. Originally just ``stop | length | schema_violation |
#: error``; ``content_filter`` (a moderation block) and ``tool_calls`` (the
#: model tried to call a function instead of answering) were added once
#: ``llm/providers.py`` started actually reading ``choices[0].finish_reason``
#: from the response instead of hardcoding "stop" everywhere -- real
#: OpenAI-compatible endpoints return both, and collapsing them into the
#: generic "error" bucket would make a moderation block indistinguishable,
#: in a week of audit logs, from a dead endpoint. See ``docs/api-contract-v2.md``
#: §6, updated in the same change.
_VALID_FINISH_REASONS = frozenset(
    {"stop", "length", "content_filter", "tool_calls", "schema_violation", "error"}
)

#: Prefix :func:`~llm.providers._normalize_finish_reason` uses for a raw
#: endpoint value outside :data:`_VALID_FINISH_REASONS` -- accepted here too
#: (see :func:`_finish_reason_is_recognised`) so a genuinely novel value
#: from some other server is preserved in the audit log rather than
#: rejected outright or silently coerced into "error".
_OTHER_FINISH_REASON_PREFIX = "other:"


def _finish_reason_is_recognised(value: str) -> bool:
    """True for a contract literal, or any ``"other:<raw>"`` passthrough value."""
    return value in _VALID_FINISH_REASONS or (
        isinstance(value, str) and value.startswith(_OTHER_FINISH_REASON_PREFIX)
    )


class LlmStatus(TypedDict):
    """Shape of ``docs/api-contract-v2.md`` §6's ``Turn.llm`` block."""

    backend: str
    model: str
    endpoint: str | None
    trusted: bool
    endpoint_status: int
    attempts: int
    finish_reason: str
    structured_output: bool
    prompt_tokens: int
    completion_tokens: int
    prefill_ms: int | None
    decode_ms: int | None
    total_ms: int | None
    tokens_per_second: float | None
    prefix_cache_hit: bool
    temperature: float
    seed: int | None
    seed_honored: bool | None
    corrections: int
    provider: str
    fallback_used: bool
    reasoning_detected: bool


def _int(value: Any) -> int:
    """Best-effort int coercion; missing/invalid input becomes ``0``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_llm_status(
    raw: Mapping[str, Any] | None,
    *,
    model: str,
    endpoint: str | None = None,
    trusted: bool = False,
    endpoint_status: int = 0,
    backend: str = "openai",
    attempts: int = 1,
    finish_reason: str = "error",
    structured_output: bool = False,
    static_prefix_tokens: int = 0,
    temperature: float = 0.0,
    seed: int | None = None,
    corrections: int = 0,
    provider: str | None = None,
    fallback_used: bool = False,
    total_ms: int | None = None,
    reasoning_detected: bool = False,
) -> LlmStatus:
    """Build the §6 ``llm`` block from a raw OpenAI-compatible response and call metadata.

    Parameters
    ----------
    raw:
        The raw, undiscarded JSON body of the ``/chat/completions``
        response (``resp.json()``), or ``None``/``{}`` when the call never
        reached the model at all (e.g. a connection error before any HTTP
        response was received). Fields this function reads —
        ``usage.prompt_tokens``, ``usage.completion_tokens``,
        ``system_fingerprint`` — are all read defensively: a missing or
        malformed field degrades to ``0``/``None`` rather than raising, so
        the block is always constructible from whatever is known, per the
        contract's "``llm`` is still populated as far as it got" rule (§6).
    model:
        The model tag that was requested (not necessarily present in a
        failed *raw* response, so it is supplied by the caller, which
        always knows what it asked for).
    endpoint:
        The backend's :attr:`~llm.base.LLMBackend.endpoint` (typically its
        ``base_url``) — which physical endpoint actually served this call.
        ``None`` for a backend with no real endpoint (e.g. a test double).
    trusted:
        The backend's :attr:`~llm.base.LLMBackend.trusted` — whether that
        endpoint was permitted to see schema/business-rule/row data for
        this call. Defaults to ``False``: an unspecified endpoint is not
        assumed safe.
    endpoint_status:
        HTTP status code of the final attempt. ``0`` conventionally means
        no HTTP response was ever received (pure transport failure).
    backend:
        Backend identifier. Defaults to ``"openai"`` — the only transport
        this project speaks; see ``llm/providers.py``.
    attempts:
        Number of transport-level attempts made (``>1`` means retries
        happened before either succeeding or exhausting retries).
    finish_reason:
        One of ``"stop"``, ``"length"``, ``"content_filter"``,
        ``"tool_calls"``, ``"schema_violation"``, ``"error"``, or an
        ``"other:<raw>"`` passthrough for a real endpoint value none of
        those anticipated (see ``llm.providers._normalize_finish_reason``,
        which derives this from the response's own ``choices[0].finish_reason``
        rather than a caller-supplied literal, so a truncated response
        reports ``"length"`` instead of silently reading as ``"stop"``).
        The caller still supplies it explicitly because some context
        ``raw`` alone cannot express (e.g. constrained-decoding schema
        validation happens outside the endpoint's own response, and a
        total transport failure has no ``raw`` to read at all).
    structured_output:
        Whether constrained decoding was used for this call.
    static_prefix_tokens:
        The measured (heuristic) token count of the static prompt prefix
        for the current skill version (see contract §6: "Record
        ``static_prefix_tokens`` per skill version so the ratio stays
        meaningful"). ``0`` or negative disables the cache-hit computation
        (there is nothing meaningful to compare against), and
        :attr:`LlmStatus.prefix_cache_hit` is reported as ``False``.
    temperature:
        Sampling temperature used for the request.
    seed:
        Sampling seed used for the request, or ``None`` if none was
        requested. Feeds :attr:`LlmStatus.seed_honored`: that field can
        only be ``True`` when a seed was actually asked for (see its
        derivation below) -- reporting a seed as "honoured" when none was
        ever requested would claim a determinism guarantee nobody asked
        for.
    corrections:
        Number of self-correction rounds spent before this result.
    provider:
        Phase 2 task 5: which backend in the router's fallback chain
        actually produced this result (:attr:`~llm.router.RouteResult.provider`,
        e.g. ``"openai:gpt-oss-20b"``). Defaults to *backend* when not
        given, so a caller that doesn't go through
        :class:`~llm.router.LLMRouter` (there is currently only one entry
        in the chain, so "provider" and "backend" mean the same thing)
        doesn't need to pass it explicitly.
    fallback_used:
        ``True`` if the task's first-choice backend failed and a later
        entry in the router's fallback chain answered instead
        (:attr:`~llm.router.RouteResult.fallback_used`). ``False`` when
        there was no router involved at all.
    total_ms:
        Wall-clock duration of the call, in milliseconds, as measured by
        the backend itself (see ``llm.providers.OpenAIBackend.generate_with_meta``'s
        ``"total_ms"`` meta field) — never derived or invented here.
        ``None`` (the default) when the caller has no measured figure to
        supply (e.g. a total transport failure before any attempt
        completed).
    reasoning_detected:
        ``True`` when the backend flagged this response as carrying a
        model's reasoning/chain-of-thought text rather than, or alongside,
        a final answer (see ``llm.providers.OpenAIBackend.generate_with_meta``'s
        ``"reasoning_detected"`` meta field). Deliberately a boolean, not
        an excerpt of the text itself: the reasoning channel can quote
        prompt content (including, at the interpretation task, real row
        data), and this block is embedded in the audit trail, which must
        never carry row values -- see ``observability/audit.py``. ``False``
        by default, same as an endpoint that never said anything about it.

    Returns
    -------
    LlmStatus
        A plain dict (typed as :class:`LlmStatus`) matching the contract
        exactly, safe to JSON-serialise and embed as a ``Turn``'s ``llm``
        field or an :class:`~observability.audit.AuditRecord`'s ``llm``
        field.

    Raises
    ------
    ValueError
        If *finish_reason* is not a contract-recognised literal (see
        :data:`_VALID_FINISH_REASONS`) and does not start with the
        ``"other:"`` passthrough prefix either.

    Examples
    --------
    A successful, cache-hit response:

    >>> raw = {
    ...     "usage": {"prompt_tokens": 120, "completion_tokens": 148},
    ...     "system_fingerprint": "fp_44709d6fcb",
    ... }
    >>> status = build_llm_status(
    ...     raw, model="gpt-oss-20b", endpoint="http://localhost:8000/v1",
    ...     trusted=True, endpoint_status=200, finish_reason="stop",
    ...     structured_output=True, static_prefix_tokens=4600,
    ...     temperature=0.0, seed=7, total_ms=2310,
    ... )
    >>> status["prompt_tokens"], status["completion_tokens"]
    (120, 148)
    >>> status["prefill_ms"], status["decode_ms"]
    (None, None)
    >>> status["total_ms"]
    2310
    >>> status["tokens_per_second"] is None
    True
    >>> status["prefix_cache_hit"]
    True
    >>> status["seed_honored"]
    True

    A total transport failure — no response body at all. Zero prompt
    tokens must *not* read as a cache hit, even though ``0 < 4600 * 0.5``
    is arithmetically true:

    >>> status = build_llm_status(
    ...     None, model="gpt-oss-20b", endpoint_status=0, attempts=3,
    ...     finish_reason="error", static_prefix_tokens=4600,
    ... )
    >>> status["prompt_tokens"]
    0
    >>> status["prefix_cache_hit"]
    False
    >>> status["seed_honored"] is None
    True
    >>> status["endpoint_status"], status["attempts"]
    (0, 3)
    """
    if not _finish_reason_is_recognised(finish_reason):
        raise ValueError(
            f"finish_reason must be one of {sorted(_VALID_FINISH_REASONS)} "
            f"or start with {_OTHER_FINISH_REASON_PREFIX!r}, got {finish_reason!r}"
        )

    body: Mapping[str, Any] = raw or {}
    usage: Mapping[str, Any] = body.get("usage") or {}

    prompt_tokens = _int(usage.get("prompt_tokens"))
    completion_tokens = _int(usage.get("completion_tokens"))

    # OpenAI-compatible responses don't separate prefill from decode --
    # reporting a number here would mean inventing one (see module
    # docstring). tokens_per_second needs decode_ms, so it is None too.
    prefill_ms: int | None = None
    decode_ms: int | None = None
    tokens_per_second: float | None = None

    # system_fingerprint's presence is the endpoint's own signal that it
    # tracks (and so plausibly honours) determinism-affecting config --
    # its absence means "unknown", not "confirmed not honoured". But that
    # signal only means anything if a seed was actually requested in the
    # first place: with seed=None, "honoured" would claim a determinism
    # guarantee nobody asked for, so it stays None regardless of what the
    # endpoint reports.
    seed_honored: bool | None = (
        True if (seed is not None and body.get("system_fingerprint")) else None
    )

    # See module docstring: never a cache hit at zero prompt tokens, and
    # never a cache hit when there is no meaningful prefix to compare to.
    prefix_cache_hit = (
        prompt_tokens > 0
        and static_prefix_tokens > 0
        and prompt_tokens < static_prefix_tokens * 0.5
    )

    return LlmStatus(
        backend=backend,
        model=model,
        endpoint=endpoint,
        trusted=trusted,
        endpoint_status=endpoint_status,
        attempts=attempts,
        finish_reason=finish_reason,
        structured_output=structured_output,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prefill_ms=prefill_ms,
        decode_ms=decode_ms,
        total_ms=total_ms,
        tokens_per_second=tokens_per_second,
        prefix_cache_hit=prefix_cache_hit,
        temperature=temperature,
        seed=seed,
        seed_honored=seed_honored,
        corrections=corrections,
        provider=provider if provider is not None else backend,
        fallback_used=fallback_used,
        reasoning_detected=reasoning_detected,
    )


def finish_reason_from_meta(meta: Mapping[str, Any] | None) -> str:
    """The real ``finish_reason`` a completed LLM call produced, or ``"stop"``.

    Shared by every call site that builds this block from a backend's
    ``generate_with_meta`` output (``api/runner.py``, ``session/engine.py``)
    so "derive the real value instead of hardcoding a literal" is one
    function, not N independently-drifting copies of the same fallback.

    *meta* is whatever :meth:`~llm.base.LLMBackend.generate_with_meta` (via
    :class:`~llm.router.LLMRouter`, or an exception's ``llm_meta``
    attribute after translation) attached -- see
    :meth:`~llm.providers.OpenAIBackend.generate_with_meta`'s
    ``"finish_reason"`` meta field, itself derived from the response's own
    ``choices[0].finish_reason`` by
    :func:`~llm.providers._normalize_finish_reason`.

    Falling back to ``"stop"`` only covers a backend that never populated
    the field at all (:class:`~llm.providers.MockBackend`, used throughout
    the test suite, and any future backend that doesn't override
    ``generate_with_meta``) -- every call that reaches a real endpoint gets
    that endpoint's own, possibly non-``"stop"``, value instead of this
    fallback silently overriding it. This is the one change that lets a
    truncated response (``finish_reason: "length"``) read as truncated in
    the audit log instead of as a clean completion.

    A caller handling a *total transport failure* (no response ever
    arrived) must NOT call this -- there is no real finish_reason to read,
    and "error" belongs there unconditionally; see each call site's
    surrounding comment for why that branch is not routed through here.

    Examples
    --------
    >>> finish_reason_from_meta({"finish_reason": "length"})
    'length'
    >>> finish_reason_from_meta({"finish_reason": "other:eos_token"})
    'other:eos_token'
    >>> finish_reason_from_meta({})
    'stop'
    >>> finish_reason_from_meta(None)
    'stop'
    """
    return (meta or {}).get("finish_reason") or "stop"
