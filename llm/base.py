"""Abstract base for all LLM backends.

To add a new backend (OpenAI, Anthropic, vLLM, …) implement ``LLMBackend``
and register it in ``llm/sql_agent.py``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SQLGenerationResult:
    """Outcome of one LLM call."""

    sql: str
    """Cleaned, sanitised SQL string."""

    raw_response: str
    """Verbatim text the model returned (useful for debugging)."""

    attempt: int = 1
    """Which attempt produced this result (1 = first try, 2+ = correction)."""

    correction_prompts: list[str] = field(default_factory=list)
    """The correction prompts sent in previous failed attempts."""

    llm_meta: dict[str, Any] = field(default_factory=dict)
    """Best-effort transport metadata for the call that produced this
    result — whatever :meth:`LLMBackend.generate_with_meta` returned
    alongside the text (e.g. the OpenAI-compatible response's raw
    ``usage.prompt_tokens`` / ``usage.completion_tokens`` fields). Empty
    for backends that don't
    override :meth:`LLMBackend.generate_with_meta`. Consumed by
    ``api/runner.py`` to build the ``docs/api-contract-v2.md`` §6 ``llm``
    audit block without the backend needing any per-instance mutable
    state — see :meth:`LLMBackend.generate_with_meta`'s docstring."""

    injected_top: int | None = None
    """The row cap :func:`~security.sql_guard.ensure_top` injected into
    ``sql``, or ``None`` if the model's own SQL already carried a
    row-limit clause (nothing was injected). Set by
    ``SQLAgent._clean_validate_cap`` by comparing ``ensure_top``'s output
    to its input — ``ensure_top`` itself only returns the capped string,
    so this is the caller noticing whether that string actually changed.
    Consumed by ``api/runner.py`` to populate
    ``docs/api-contract-v2.md`` §4's ``guard.injected_top``: it tells a
    reader whether the number they are looking at was truncated."""


class LLMBackend(ABC):
    """Contract every LLM backend must satisfy.

    Backends are stateless — all state lives in the prompt.
    ``generate`` is the only required method; it must:

    * Accept a complete prompt string.
    * Return the model's raw text output (no cleaning).
    * Raise ``RuntimeError`` on unrecoverable transport/API failures.
    * Raise ``ValueError("OUT_OF_SCOPE")`` when the model signals it cannot
      answer (pass the sentinel through unchanged).
    """

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Send *prompt* to the model and return raw text."""
        ...

    def generate_with_meta(self, prompt: str) -> tuple[str, dict[str, Any]]:
        """Send *prompt* to the model; return ``(raw_text, meta)``.

        *meta* is an opt-in, backend-specific dict of transport metadata
        useful for observability (e.g. token counts, timings, HTTP status)
        — see ``observability/llm_status.py::build_llm_status`` for the
        shape a caller typically wants to build from it. This is a
        **stateless** call: metadata for one call is returned to that
        call's caller, never stashed on ``self``. ``SQLAgent`` is a
        long-lived singleton shared across concurrent requests (see
        ``api/runner.py``'s module docstring), so any backend that cached
        "the last response" as an instance attribute would race across
        requests; threading the metadata through the return value instead
        of instance state is what keeps this safe.

        The default implementation here makes the metadata path fully
        opt-in: a backend that only implements :meth:`generate` (the sole
        abstract method) keeps working unchanged, just with an empty
        *meta* dict — exactly what :class:`~llm.providers.MockBackend`
        does, and what :class:`~llm.providers.OpenAIBackend` overrides.

        Parameters
        ----------
        prompt:
            Complete prompt string, as passed to :meth:`generate`.

        Returns
        -------
        tuple[str, dict[str, Any]]
            ``(raw_text, meta)`` — *raw_text* is exactly what
            :meth:`generate` would have returned; *meta* is ``{}`` unless
            overridden.

        Examples
        --------
        >>> class EchoBackend(LLMBackend):
        ...     def generate(self, prompt: str) -> str:
        ...         return prompt.upper()
        >>> text, meta = EchoBackend().generate_with_meta("hi")
        >>> text, meta
        ('HI', {})
        """
        return self.generate(prompt), {}

    async def agenerate_with_meta(self, prompt: str) -> tuple[str, dict[str, Any]]:
        """Async counterpart of :meth:`generate_with_meta`.

        Phase 2 (latency) task 4 moves the API layer to ``async def``
        handlers so a slow LLM call no longer ties up one of Starlette's
        threadpool workers for the duration of the request. A backend
        that implements a genuine async transport (built on
        ``httpx.AsyncClient`` with connection keep-alive, say) should
        override this method directly.

        The default implementation here — like :meth:`generate_with_meta`'s
        default — makes async support fully opt-in: a backend that only
        implements the synchronous :meth:`generate` keeps working
        unchanged, just via :func:`asyncio.to_thread`, which still frees
        the event loop (the call runs in a worker thread) even though the
        transport itself remains blocking underneath.

        Returns
        -------
        tuple[str, dict[str, Any]]
            Same contract as :meth:`generate_with_meta`.

        Examples
        --------
        >>> import asyncio
        >>> class EchoBackend(LLMBackend):
        ...     def generate(self, prompt: str) -> str:
        ...         return prompt.upper()
        >>> text, meta = asyncio.run(EchoBackend().agenerate_with_meta("hi"))
        >>> text, meta
        ('HI', {})
        """
        return await asyncio.to_thread(self.generate_with_meta, prompt)

    def generate_with_meta_segments(
        self, segments: "PromptSegments"
    ) -> tuple[str, dict[str, Any]]:
        """Segment-aware counterpart of :meth:`generate_with_meta`.

        This is what :meth:`~llm.router.LLMRouter.generate_for_task` calls
        — never ``generate_with_meta`` directly — so that the router
        itself never flattens :class:`~llm.router.PromptSegments` before
        it reaches a backend (Phase 2 task 5's third design point: "prompts
        enter the router segmented, never as one flat string"). Whether a
        given backend then chooses to flatten internally is that backend's
        own decision, not the router's.

        The default implementation here does flatten (via
        :meth:`~llm.router.PromptSegments.flatten`) and delegate to
        :meth:`generate_with_meta` — the correct behaviour for a backend
        with no segment-aware transport, and in particular for
        :class:`~llm.providers.OpenAIBackend`: OpenAI-compatible prefix
        caching (automatic on OpenAI's own API, implicit KV-cache reuse on
        llama.cpp/vLLM) is byte-identical-prefix-based, exactly like
        Ollama's before it — flattening the segments in a stable order
        *is* the correct way to get that benefit, not a compromise. A
        backend with an explicit, segment-level caching mechanism (e.g. an
        Anthropic-style ``cache_control`` breakpoint on the static prefix
        block) would override this method instead of relying on the
        default.

        Parameters
        ----------
        segments:
            A :class:`~llm.router.PromptSegments`, accepted as a
            duck-typed object exposing ``.flatten() -> str`` for the same
            circular-import reason documented on :meth:`generate_structured`.

        Returns
        -------
        tuple[str, dict[str, Any]]
            Same contract as :meth:`generate_with_meta`.

        Examples
        --------
        >>> class EchoBackend(LLMBackend):
        ...     def generate(self, prompt: str) -> str:
        ...         return prompt.upper()
        >>> class FakeSegments:
        ...     def flatten(self) -> str:
        ...         return "hi"
        >>> text, meta = EchoBackend().generate_with_meta_segments(FakeSegments())
        >>> text, meta
        ('HI', {})
        """
        return self.generate_with_meta(segments.flatten())

    def generate_structured(
        self, segments: "PromptSegments", schema: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return an object matching *schema*, not a raw string.

        This is the interface :mod:`llm.router` calls — "give me an object
        matching this schema", not "here is a prompt string" (Phase 2 task
        5's second design point). Constrained decoding is provider-specific
        (OpenAI-compatible ``response_format: json_schema``, Ollama's
        ``format``, Anthropic's forced tool use), so a backend that
        supports it natively — e.g. :class:`~llm.providers.OpenAIBackend`
        — overrides this method. This default makes it fully opt-in for
        everything else: flatten *segments* into one prompt, append a
        plain-text JSON instruction, call :meth:`generate`, and best-effort
        parse the result with :func:`llm.providers.parse_json_response` —
        exactly the "every provider collapses to string parsing" fallback
        the design note warns is the wrong DEFAULT behaviour to build the
        whole system around, but which remains a legitimate degrade path
        for a provider that genuinely has no structured-output mode.

        Parameters
        ----------
        segments:
            A :class:`~llm.router.PromptSegments` (``static_prefix``,
            ``session_context``, ``question``) — accepted as a duck-typed
            object exposing ``.flatten() -> str`` here to avoid a circular
            import with :mod:`llm.router`, which imports this module.
        schema:
            A JSON Schema describing the expected object shape.

        Returns
        -------
        tuple[dict[str, Any], dict[str, Any]]
            ``(parsed_object, meta)``.

        Raises
        ------
        ValueError
            If the response cannot be parsed as JSON matching *schema*
            (schema conformance itself is NOT validated here — only that
            valid JSON was extracted; callers needing strict validation
            should check the result themselves).
        """
        from llm.providers import parse_json_response  # noqa: PLC0415 - avoid import cycle

        instruction = (
            "\n\nRespond with ONLY a single JSON object matching this schema "
            f"(no markdown fences, no explanation): {schema}"
        )
        raw, meta = self.generate_with_meta(segments.flatten() + instruction)
        parsed = parse_json_response(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected a JSON object, got {type(parsed).__name__}: {raw[:200]!r}")
        meta = {**meta, "structured_output": False}
        return parsed, meta

    @property
    def name(self) -> str:
        """Human-readable identifier shown in logs and REPL header."""
        return type(self).__name__

    @property
    def trusted(self) -> bool:
        """Whether this backend's endpoint may see schema/business-rule/row data.

        Defaults to ``True``. This is deliberately *not* the same default
        the endpoint-trust computation itself uses for a real network
        transport (see :func:`~llm.trust.default_trust_for_url`, which
        defaults an *unrecognised base_url* to untrusted) — this default
        is for backends that have no endpoint at all: a test double, a
        stub, or a bespoke :class:`LLMBackend` subclass someone writes for
        their own local model. None of those are the thing
        :meth:`~llm.router.LLMRouter._governance_check` exists to catch
        (a real hosted API that would otherwise receive data silently),
        so they should not need any ceremony (constructing an
        :class:`~llm.endpoints.EndpointConfig`, computing a trust flag) to
        stay usable. A backend that talks to a real, configurable
        endpoint — :class:`~llm.providers.OpenAIBackend` — overrides this
        to report its own resolved per-endpoint trust instead of
        inheriting this default.
        """
        return True

    @property
    def endpoint(self) -> str | None:
        """The endpoint identifier shown in the ``llm`` status block, or ``None``.

        ``None`` for a backend with no real network endpoint (a test
        double, or a bespoke subclass that doesn't override this). A
        backend with a genuine transport — :class:`~llm.providers.OpenAIBackend`
        — overrides this to return its ``base_url``.
        """
        return None
