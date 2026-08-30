"""Task-based LLM router — routes each task to a fallback chain of endpoints.

Before this module, provider switching existed only in
:mod:`llm.wizard_llm`, built for the interactive setup wizard, while the
production engine (:class:`~llm.sql_agent.SQLAgent`) was hardwired to a
single backend with no routing at all. This module is the single router
both the engine and the wizard share.

The transport layer has since been reduced to one protocol — every real
endpoint is :class:`~llm.providers.OpenAIBackend`, differing only in
``base_url`` (see that module's docstring). That makes the router's job
*more* important, not less: routing across *endpoints* rather than across
*provider classes* is the only way to still express "a fast local model
for this task, a hosted fallback for when it's down, a different model
entirely for that task" once every one of those is the same class — see
:mod:`llm.endpoints`.

Three design decisions, made deliberately up front because they are
expensive to retrofit later
-----------------------------------------------------------------------------
1. **Route by task, not just by endpoint.** SQL generation needs a
   code-strong model with deterministic decoding; interpretation needs
   fluent Persian and can stream; assumption extraction needs to be cheap
   and fast. :class:`TaskType` is what :meth:`LLMRouter.generate_for_task`
   and :meth:`LLMRouter.generate_structured_for_task` key on, so SQL
   generation can go to one endpoint while interpretation goes to another
   — a router keyed only on "the" endpoint could never express that.
2. **The interface is "give me an object matching this schema", not "here
   is a prompt string".** See :meth:`~llm.base.LLMBackend.generate_structured`.
   If the router only exposed ``generate(prompt) -> str``, every endpoint
   would collapse to string parsing and Phase 2 task 2's determinism work
   (a schema-shaped response is trivially validated; free-text SQL wrapped
   in prose is not) would be lost.
3. **Prompts enter the router segmented, never as one flat string.**
   :class:`PromptSegments` keeps ``static_prefix`` / ``session_context`` /
   ``question`` apart because prefix caching depends on the leading bytes
   staying byte-identical across calls (Phase 2 task 1) — a backend that
   only accepted a flat string could never participate in provider-side
   caching without the router first assembling that string in a stable
   order, which is exactly what :meth:`PromptSegments.flatten` (called by
   the *backend*, never by the router) is for.

Data governance
----------------
Sending schema, business-rule text, or query-result rows to an untrusted
endpoint is not something setting ``OPENAI_BASE_URL`` alone should be able
to do — this product's premise is "runs on your infrastructure", and an
``OpenAIBackend`` pointed at a hosted API is exactly as capable of leaking
that data as the old provider-specific hosted transports were. Routing a
task to an untrusted backend is refused with
:class:`RemoteProviderNotAllowedError` unless ``cfg.settings.llm_allow_remote``
is explicitly ``True``, and every call that does go out while it is true is
written to the audit trail via :func:`~observability.audit.save_audit_record`
— see :meth:`LLMRouter._governance_check` and :func:`is_trusted_backend`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import config as cfg
from llm.base import LLMBackend

logger = logging.getLogger(__name__)

#: The sentinel a backend raises as ``ValueError(_OUT_OF_SCOPE_SENTINEL)``
#: to say "this question is outside the Auction domain". Deliberately a
#: literal here rather than an import from :mod:`llm.structured_schema`:
#: the router must not depend on the structured-output layer, and the
#: string is the wire contract shared by every backend, every caller
#: (``llm/sql_agent.py``, ``api/runner.py``, ``session/engine.py``,
#: ``eval/runner.py``) and this module.
_OUT_OF_SCOPE_SENTINEL = "OUT_OF_SCOPE"


def _is_out_of_scope(exc: BaseException) -> bool:
    """True when *exc* is the terminal out-of-scope signal, not a failure.

    The ``isinstance``-plus-message test (rather than an exact type check)
    matches every caller's own discrimination — ``api/runner.py``,
    ``session/engine.py`` and ``eval/runner.py`` all catch ``ValueError``
    and switch on ``str(exc)`` — so the router cannot classify an
    exception as retryable that the caller would then translate as a
    decline. The message check is what does the real work: a transport
    hiccup that arrives as a ``ValueError`` subclass (notably
    ``requests.exceptions.JSONDecodeError``, which subclasses both
    ``ValueError`` and ``RequestException`` — see ``llm/providers.py``)
    carries a JSON-parse message, never this sentinel, and so stays a
    plain backend failure that the next chain entry should retry.

    Examples
    --------
    >>> _is_out_of_scope(ValueError("OUT_OF_SCOPE"))
    True
    >>> _is_out_of_scope(ValueError("some other problem"))
    False
    >>> _is_out_of_scope(RuntimeError("OUT_OF_SCOPE"))
    False
    >>> import requests
    >>> _is_out_of_scope(requests.exceptions.JSONDecodeError("Expecting value", "", 0))
    False
    """
    return isinstance(exc, ValueError) and str(exc) == _OUT_OF_SCOPE_SENTINEL


class TaskType(str, Enum):
    """What a given LLM call is *for* — the router's routing key.

    Examples
    --------
    >>> TaskType.SQL_GENERATION.value
    'sql_generation'
    """

    SQL_GENERATION = "sql_generation"
    INTERPRETATION = "interpretation"
    ASSUMPTION_EXTRACTION = "assumption_extraction"


class RemoteProviderNotAllowedError(RuntimeError):
    """Raised when a task would route to an untrusted backend without opt-in.

    See the module docstring's "Data governance" section.
    """


@dataclass(frozen=True)
class PromptSegments:
    """A prompt kept in parts, never flattened until a backend demands it.

    Parameters
    ----------
    static_prefix:
        The byte-identical prefix from
        :func:`~prompt_engine.static_prefix.build_static_prefix` (or an
        empty string for a task that has none, e.g. assumption
        extraction over a short snippet).
    session_context:
        Prior-turn context (empty when there is no session).
    question:
        The variable, per-request tail — the question itself, or
        whatever short text the task actually needs summarised/extracted.

    Examples
    --------
    >>> segs = PromptSegments(static_prefix="SCHEMA...", question="how many rows?")
    >>> segs.flatten()
    'SCHEMA...\\n\\nhow many rows?'
    >>> segs2 = PromptSegments(static_prefix="SCHEMA...", session_context="turn 1: ...", question="q2")
    >>> "turn 1" in segs2.flatten()
    True
    """

    static_prefix: str = ""
    session_context: str = ""
    question: str = ""

    def flatten(self) -> str:
        """Concatenate into one string, for backends with no segment awareness.

        This is deliberately the *only* way a backend sees a single flat
        string — see the module docstring's design point 3. Every provider
        backend is free to call this, but the router never collapses the
        segments before that point, keeping the door open for a future
        backend to apply provider-specific caching per segment instead.
        """
        parts = [p for p in (self.static_prefix, self.session_context, self.question) if p]
        return "\n\n".join(parts)


def build_prompt_segments(
    question: str,
    system_prompt: str,
    context: Any,
    *,
    session_context: str = "",
) -> PromptSegments:
    """Build a :class:`PromptSegments` for the SQL-generation task.

    Splits :meth:`~prompt_engine.builder.PromptBuilder.build`'s output back
    into ``static_prefix`` / ``question`` rather than reimplementing prompt
    assembly here: :func:`~prompt_engine.static_prefix.build_static_prefix`
    gives the exact prefix bytes, and the router only needs to know where
    they end in the full built string.

    Parameters
    ----------
    question, system_prompt, context, session_context:
        Same as :meth:`~prompt_engine.builder.PromptBuilder.build`.

    Returns
    -------
    PromptSegments
        On the static-prefix path (see
        :func:`~prompt_engine.static_prefix.should_use_static_prefix`),
        ``static_prefix`` holds the byte-identical cached prefix and
        ``question`` holds everything the template appended after it
        (filters, session context, the question itself). On the retrieval
        fallback path — no stable prefix worth caching, by design — the
        whole built prompt goes into ``question`` and ``static_prefix`` is
        empty.

    Examples
    --------
    >>> from core.models import RetrievalContext
    >>> ctx = RetrievalContext(entities=["Customer"], facts=["Contract"])
    >>> segments = build_prompt_segments("how many?", "You are a T-SQL expert.", ctx)
    >>> "You are a T-SQL expert." in segments.static_prefix
    True
    >>> "how many?" in segments.question
    True
    """
    from prompt_engine.builder import PromptBuilder
    from prompt_engine.static_prefix import build_static_prefix, should_use_static_prefix

    if not should_use_static_prefix(system_prompt):
        full = PromptBuilder.build(
            question, system_prompt, context, session_context=session_context
        )
        return PromptSegments(question=full)

    prefix = build_static_prefix(system_prompt)
    full = PromptBuilder.build_static(
        question, system_prompt, context, session_context=session_context
    )
    if not full.startswith(prefix):
        # Defensive fallback only — should_use_static_prefix() already
        # gates this path, so build_static() is expected to always start
        # with build_static_prefix()'s own output.
        return PromptSegments(question=full)
    return PromptSegments(static_prefix=prefix, question=full[len(prefix):])


@dataclass
class RouteResult:
    """Outcome of one routed call — either text or a structured object, never both.

    Attributes
    ----------
    text:
        The raw text response, for :meth:`LLMRouter.generate_for_task`.
        ``None`` for a structured call.
    structured:
        The parsed object, for :meth:`LLMRouter.generate_structured_for_task`.
        ``None`` for a text call.
    meta:
        Backend call metadata (see :meth:`~llm.base.LLMBackend.generate_with_meta`).
    provider:
        The backend's :attr:`~llm.base.LLMBackend.name` that actually
        produced the result (the winning entry in the fallback chain).
    fallback_used:
        ``True`` if the task's first-choice backend failed and a later
        entry in the fallback chain answered instead.
    """

    text: str | None
    structured: dict[str, Any] | None
    meta: dict[str, Any]
    provider: str
    fallback_used: bool


def is_trusted_backend(backend: LLMBackend) -> bool:
    """True when *backend* may see schema/business-rule/row data.

    This used to be decided by **class**: a blocklist of ``(module, class
    name)`` pairs naming the hosted-provider classes this project shipped
    (``OpenAIBackend``, ``AnthropicBackend``), with every *other* class —
    including every test double, stub, or bespoke local backend — treated
    as local by default. That was the right call under the old design,
    where each transport had its own class: a genuinely new local backend
    (a vLLM/LM Studio wrapper, someone's test suite) just worked, and a new
    hosted transport needed one line added to the blocklist.

    It breaks in both directions the moment :class:`~llm.providers.OpenAIBackend`
    becomes the *only* real transport, because then the class no longer
    says anything about the endpoint: a user's own ``gpt-oss`` server at
    ``http://localhost:8000/v1`` and OpenAI's hosted API at
    ``https://api.openai.com/v1`` are both ``OpenAIBackend`` instances.
    Keeping the blocklist would refuse the local deployment outright (a
    working, on-your-infrastructure setup gated behind an opt-in meant for
    hosted providers); dropping it so the local case works would make the
    hosted endpoint look local too, and row data would flow to it with no
    gate at all. Neither direction is acceptable, and no class-keyed rule
    can express the difference — **trust has to be a property of the
    endpoint**, not of the Python class that happens to speak to it.

    This function reads that property directly off *backend* — see
    :attr:`~llm.base.LLMBackend.trusted`, which every real transport
    (:class:`~llm.providers.OpenAIBackend`) resolves per-endpoint at
    construction time (see :func:`~llm.trust.default_trust_for_url`), and
    which defaults to ``True`` for anything else (a test double, a stub,
    a bespoke subclass with no real endpoint at all) — see that
    property's own docstring for why that default is still correct here:
    a fake with no network endpoint is simply not the thing this gate
    exists to catch. Read via ``getattr(..., True)`` rather than plain
    attribute access: a duck-typed backend that implements only
    ``generate``/``generate_with_meta_segments`` (there are plenty in this
    project's own test suite) is not an :class:`~llm.base.LLMBackend`
    subclass and so has no ``trusted`` attribute at all — it should get
    the same ``True`` default a subclass would inherit, not an
    ``AttributeError``.

    Used by :meth:`LLMRouter._governance_check` (and by
    ``api/runner.py::_interpret``'s own governance gate) to decide whether
    a task needs the ``llm_allow_remote`` opt-in.

    Examples
    --------
    >>> from llm.providers import MockBackend, OpenAIBackend
    >>> is_trusted_backend(MockBackend())
    True
    >>> is_trusted_backend(OpenAIBackend(model="m", api_key="k", base_url="http://localhost:8000/v1"))
    True
    >>> is_trusted_backend(OpenAIBackend(model="m", api_key="k"))
    False
    """
    return getattr(backend, "trusted", True)


class LLMRouter:
    """Routes each :class:`TaskType` to a fallback chain of backends.

    Parameters
    ----------
    chains:
        ``{task: [backend, backend, ...]}``. The first backend in each
        chain is tried first; later entries are only used if an earlier
        one raises. A task with no configured chain falls back to
        ``default_chain``.
    default_chain:
        Used for any :class:`TaskType` not present in *chains*.
    budgets:
        Optional ``{task: seconds}`` per-task latency budget. An attempt
        that exceeds its budget is treated as a failure and the chain
        moves on to the next backend — see :meth:`_call_chain`.

    Examples
    --------
    >>> from llm.providers import MockBackend
    >>> router = LLMRouter(default_chain=[MockBackend(response="SELECT 1")])
    >>> segments = PromptSegments(static_prefix="schema", question="q")
    >>> result = router.generate_for_task(TaskType.SQL_GENERATION, segments)
    >>> result.text, result.provider, result.fallback_used
    ('SELECT 1', 'mock:stub', False)
    """

    def __init__(
        self,
        chains: dict[TaskType, list[LLMBackend]] | None = None,
        *,
        default_chain: list[LLMBackend] | None = None,
        budgets: dict[TaskType, float] | None = None,
    ) -> None:
        self._chains: dict[TaskType, list[LLMBackend]] = chains or {}
        self._default_chain: list[LLMBackend] = default_chain or []
        self._budgets: dict[TaskType, float] = budgets or {}

    @classmethod
    def from_settings(cls) -> "LLMRouter":
        """Build a router from :mod:`config`.

        ``cfg.settings.llm_provider`` is either ``"openai"`` (the default)
        or ``"mock"`` — a convenience for tests and offline runs, mapping
        every :class:`TaskType` to a single :class:`~llm.providers.MockBackend`
        chain with no endpoint configuration involved at all.

        ``"openai"`` builds every task's fallback chain from
        :mod:`llm.endpoints`: :func:`~llm.endpoints.load_endpoints` /
        :func:`~llm.endpoints.load_routes` resolve the named-endpoint
        registry and the per-task routes (both default to the trivial
        single-endpoint case — see that module's docstring — when
        ``LLM_ENDPOINTS``/``LLM_ROUTES`` are unset). Choosing ``"openai"``
        does NOT bypass the data-governance gate: :meth:`_governance_check`
        still refuses any call to an untrusted backend unless
        ``cfg.settings.llm_allow_remote`` is ``True`` — see
        :func:`is_trusted_backend`.

        ``cfg.settings.llm_task_budget_seconds``, if set, is applied as
        the same per-task budget to every :class:`TaskType` (see
        :meth:`_call_chain`).

        Raises
        ------
        ValueError
            If ``cfg.settings.llm_provider`` is not one of ``"openai"``,
            ``"mock"``, or if ``LLM_ENDPOINTS``/``LLM_ROUTES`` is set but
            malformed (see :mod:`llm.endpoints`).
        """
        from llm.endpoints import build_backend, load_endpoints, load_routes
        from llm.providers import MockBackend

        provider = cfg.settings.llm_provider.strip().lower()
        budget = cfg.settings.llm_task_budget_seconds
        budgets = {task: budget for task in TaskType} if budget is not None else None

        if provider == "mock":
            return cls(default_chain=[MockBackend()], budgets=budgets)
        if provider != "openai":
            raise ValueError(
                f"Unsupported LLM_PROVIDER {provider!r}. Choose one of: openai, mock."
            )

        endpoints = load_endpoints()
        routes = load_routes()
        backends_by_name: dict[str, LLMBackend] = {}
        chains: dict[TaskType, list[LLMBackend]] = {}
        for task in TaskType:
            names = routes.get(task.value, ["default"])
            chain: list[LLMBackend] = []
            for name in names:
                if name not in endpoints:
                    raise ValueError(
                        f"LLM_ROUTES references unknown endpoint {name!r} for "
                        f"task {task.value!r}. Known endpoints: {sorted(endpoints)}"
                    )
                if name not in backends_by_name:
                    backends_by_name[name] = build_backend(endpoints[name])
                chain.append(backends_by_name[name])
            chains[task] = chain

        return cls(chains=chains, budgets=budgets)

    def _chain_for(self, task: TaskType) -> list[LLMBackend]:
        chain = self._chains.get(task) or self._default_chain
        if not chain:
            raise RuntimeError(f"No backend configured for task {task!r} and no default_chain set")
        return chain

    def _governance_check(self, backend: LLMBackend, task: TaskType, segments: PromptSegments) -> None:
        """Refuse (or audit) sending *segments* to an untrusted backend.

        Raises
        ------
        RemoteProviderNotAllowedError
            If *backend* is not trusted and ``cfg.settings.llm_allow_remote``
            is ``False``.
        """
        if is_trusted_backend(backend):
            return
        if not cfg.settings.llm_allow_remote:
            raise RemoteProviderNotAllowedError(
                f"Task {task.value!r} would route to remote provider {backend.name!r}, "
                "but Settings.llm_allow_remote is False. Set LLM_ALLOW_REMOTE=true to "
                "explicitly opt this deployment into sending schema/business data to a "
                "hosted LLM provider."
            )
        self._audit_remote_use(backend, task, segments)

    @staticmethod
    def _audit_remote_use(backend: LLMBackend, task: TaskType, segments: PromptSegments) -> None:
        """Write an audit record noting a remote provider was used for *task*.

        Deliberately does not include ``segments.question`` verbatim in
        the audit *message* beyond length — the audit trail should prove
        a remote call happened and for which task, not duplicate a
        potentially sensitive payload a second time. Never raises: an
        audit-trail failure must not fail a real request (mirrors
        ``api/runner.py``'s own audit-writing discipline).
        """
        try:
            from observability.audit import AuditRecord, save_audit_record
            from datetime import datetime

            record = AuditRecord(
                timestamp=datetime.now(),
                request_id=f"remote-llm-{task.value}",
                question=f"[task={task.value}] {len(segments.flatten())} chars sent to remote provider",
                generated_sql="",
                guard={"verdict": "allowed", "rule": None, "injected_top": None, "tables_touched": None},
                tier="T2",
                llm={"provider": backend.name, "remote": True, "task": task.value},
            )
            save_audit_record(record)
        except Exception:  # noqa: BLE001 - audit must never break a real call
            logger.exception("Failed to write remote-provider audit record for task=%s", task.value)

    def _call_chain(
        self, task: TaskType, call: Any, segments: PromptSegments,
    ) -> tuple[Any, dict[str, Any], LLMBackend, bool]:
        """Try each backend in the task's chain in order; return the first success.

        Parameters
        ----------
        call:
            ``lambda backend: (result, meta)`` — either
            ``backend.generate_with_meta(segments.flatten())`` or
            ``backend.generate_structured(segments, schema)``, supplied by
            the two public methods below.

        Returns
        -------
        tuple[Any, dict[str, Any], LLMBackend, bool]
            ``(result, meta, backend, fallback_used)`` — *backend* (not
            just its name) is returned so callers can read
            :attr:`~llm.base.LLMBackend.endpoint` /
            :attr:`~llm.base.LLMBackend.trusted` off the one that actually
            answered, alongside its ``name``.

        Raises
        ------
        ValueError("OUT_OF_SCOPE")
            Re-raised immediately, as-is, the moment any backend in the
            chain raises it — see the inline comment in the loop below.
        RuntimeError
            If every backend in the chain fails.
        """
        chain = self._chain_for(task)
        budget = self._budgets.get(task)
        last_exc: Exception | None = None

        for index, backend in enumerate(chain):
            self._governance_check(backend, task, segments)
            start = time.monotonic()
            try:
                result, meta = call(backend)
            except Exception as exc:  # noqa: BLE001 - any backend failure tries the next
                # OUT_OF_SCOPE is NOT a backend failure -- it is a terminal
                # domain decision by the model ("this question is outside
                # the Auction domain"), which every caller translates as
                # such (api/runner.py -> api.errors.OutOfScopeError;
                # session/engine.py -> the OUT_OF_SCOPE status). Falling
                # through to the next backend would let a second model
                # silently override the first model's correct refusal and
                # hand the user SQL for a question already judged out of
                # domain. Re-raised bare (`raise`, not `raise exc from ...`)
                # so it reaches the callers exactly as it does with a
                # single-entry chain: the SAME exception object -- keeping
                # the `llm_meta` attribute OpenAIBackend attaches to it,
                # which the audit trail reads -- and with `__cause__` left
                # untouched, so the call sites' `exc.__cause__ or exc`
                # unwrap contract resolves to the exception itself.
                if _is_out_of_scope(exc):
                    logger.info(
                        "LLMRouter: backend %r declined task=%s as OUT_OF_SCOPE "
                        "-- terminal, not retried on the rest of the chain",
                        backend.name, task.value,
                    )
                    raise
                last_exc = exc
                logger.warning(
                    "LLMRouter: backend %r failed for task=%s (%s) -- trying next in chain",
                    backend.name, task.value, exc,
                )
                continue

            elapsed = time.monotonic() - start
            if budget is not None and elapsed > budget:
                last_exc = TimeoutError(
                    f"backend {backend.name!r} exceeded latency budget "
                    f"({elapsed:.1f}s > {budget:.1f}s) for task={task.value}"
                )
                logger.warning(str(last_exc))
                continue

            return result, meta, backend, index > 0

        raise RuntimeError(
            f"Every backend in the chain failed for task={task.value}: {last_exc}"
        ) from last_exc

    @staticmethod
    def _finalize_meta(meta: dict[str, Any], backend: LLMBackend, fallback_used: bool) -> dict[str, Any]:
        """Stamp the winning backend's identity onto *meta*.

        Shared by all three public ``generate_*_for_task`` methods so
        ``provider``/``fallback_used`` (already threaded through since
        Phase 2 task 5) and ``endpoint``/``trusted`` (this endpoint-trust
        refactor) land in the ``llm`` status block the same way, from one
        place, rather than three call sites drifting apart.

        ``endpoint``/``trusted`` are read via ``getattr`` with the same
        defaults :func:`is_trusted_backend` uses, for the same reason: a
        duck-typed backend with no ``LLMBackend`` base class has neither
        attribute at all.
        """
        return {
            **meta,
            "provider": backend.name,
            "fallback_used": fallback_used,
            "endpoint": getattr(backend, "endpoint", None),
            "trusted": getattr(backend, "trusted", True),
        }

    def generate_for_task(self, task: TaskType, segments: PromptSegments) -> RouteResult:
        """Route *task* through its fallback chain, returning raw text.

        *segments* is handed to each backend's
        :meth:`~llm.base.LLMBackend.generate_with_meta_segments` — never
        flattened here — so a segment-aware backend still sees
        ``static_prefix`` / ``session_context`` / ``question`` as distinct
        parts by the time its own adapter code runs (Phase 2 task 5's
        third design point; exit criterion 2).

        Examples
        --------
        >>> from llm.providers import MockBackend
        >>> router = LLMRouter(default_chain=[MockBackend(response="hi")])
        >>> router.generate_for_task(TaskType.INTERPRETATION, PromptSegments(question="q")).text
        'hi'
        """
        result, meta, backend, fallback_used = self._call_chain(
            task, lambda backend: backend.generate_with_meta_segments(segments), segments,
        )
        meta = self._finalize_meta(meta, backend, fallback_used)
        return RouteResult(
            text=result, structured=None, meta=meta, provider=backend.name, fallback_used=fallback_used,
        )

    def generate_text_for_task(self, task: TaskType, prompt: str) -> RouteResult:
        """Route *task* through its fallback chain, calling ``generate()`` directly.

        Unlike :meth:`generate_for_task`, this calls
        :meth:`~llm.base.LLMBackend.generate` — the plain, no-metadata
        method every backend implements — rather than
        :meth:`~llm.base.LLMBackend.generate_with_meta_segments`. Intended
        for a task with no static prefix worth caching (e.g.
        :attr:`TaskType.INTERPRETATION`, whose prompt is entirely
        per-request row data and a question — segmenting it would add
        caching machinery with nothing to cache). *prompt* is still
        wrapped in a :class:`PromptSegments` internally so the same
        governance check (:meth:`_governance_check`) and audit trail apply
        as for :meth:`generate_for_task`; only the ``call`` handed to
        :meth:`_call_chain` differs.

        Same fallback-chain and governance behaviour as
        :meth:`generate_for_task`: a :class:`RemoteProviderNotAllowedError`
        from a refused backend still propagates directly (ungoverned
        backends are never called), and a chain-exhausted failure still
        wraps the original exception as ``__cause__``.

        Parameters
        ----------
        task:
            The task this call is for.
        prompt:
            A complete, already-assembled prompt string.

        Returns
        -------
        RouteResult
            ``.text`` is the raw response; ``.structured`` is always
            ``None``.

        Examples
        --------
        >>> from llm.providers import MockBackend
        >>> router = LLMRouter(default_chain=[MockBackend(response="a summary")])
        >>> router.generate_text_for_task(TaskType.INTERPRETATION, "summarise this").text
        'a summary'
        """
        segments = PromptSegments(question=prompt)
        result, meta, backend, fallback_used = self._call_chain(
            task, lambda backend: (backend.generate(prompt), {}), segments,
        )
        meta = self._finalize_meta(meta, backend, fallback_used)
        return RouteResult(
            text=result, structured=None, meta=meta, provider=backend.name, fallback_used=fallback_used,
        )

    def generate_structured_for_task(
        self, task: TaskType, segments: PromptSegments, schema: dict[str, Any],
    ) -> RouteResult:
        """Route *task* through its fallback chain, returning a schema-shaped object.

        Examples
        --------
        >>> from llm.providers import MockBackend
        >>> backend = MockBackend(structured={"sql": "SELECT 1", "out_of_scope": False})
        >>> router = LLMRouter(default_chain=[backend])
        >>> result = router.generate_structured_for_task(
        ...     TaskType.SQL_GENERATION, PromptSegments(question="q"), schema={"type": "object"},
        ... )
        >>> result.structured
        {'sql': 'SELECT 1', 'out_of_scope': False}
        """
        result, meta, backend, fallback_used = self._call_chain(
            task, lambda backend: backend.generate_structured(segments, schema), segments,
        )
        meta = self._finalize_meta(meta, backend, fallback_used)
        return RouteResult(
            text=None, structured=result, meta=meta, provider=backend.name, fallback_used=fallback_used,
        )
