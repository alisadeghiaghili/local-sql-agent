# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Prime the model server's prefix cache so the first question is not the slow one.

Every SQL-generation request starts with the same bytes: the static prompt
prefix (:func:`prompt_engine.static_prefix.build_static_prefix`) is
byte-identical per data source, so a model server that caches prompt
prefixes (vLLM with ``--enable-prefix-caching``, llama.cpp's prompt cache)
only pays prefill for the short per-question tail. The cache starts empty,
though. After a restart of this server, or of the model server, the first
question pays the full prefill of the whole prefix -- about a minute on a
large local model -- and nothing before this module made that cost land on
anything but a user's question.

:func:`warm_prefix_cache` sends the model server one minimal request per
data source: the messages are exactly the cacheable prefix the real requests
start with, ``max_tokens`` is ``1`` and the temperature ``0``
(:meth:`llm.providers.OpenAIBackend.warm_prefix`). The server computes the
prefix's KV cache to answer it and keeps it.
:func:`start_background_warmup` runs that on a daemon thread from
``api/server.py``'s ``lifespan``, so the server accepts requests at once; a
question that arrives while the warm-up is still running is served as it
would have been without one (and on a server that shares in-flight prefix
blocks, may even benefit from it).

Why the prefix is not re-implemented here
-----------------------------------------
The warm-up prompt is taken from :func:`llm.router.build_prompt_segments` --
the one function both engines build real prompts with -- called with an
empty question. Its ``static_prefix`` is therefore the real requests'
prefix by construction: it cannot drift from it when the template, the
per-source scoping or the join-only rules change. ``tests/test_llm_warmup.py``
asserts that a real request's flattened prompt starts with exactly the text
the warm-up sent.

Failure policy
--------------
A warm-up is an optimisation. Every failure -- an unreachable endpoint, a
timeout, a server that rejects ``max_tokens=1``, a bad ``LLM_EXTRA_BODY``,
an untrusted endpoint, a configuration error -- is logged as one line and
swallowed; nothing here can stop the server starting or a question being
answered. The lines never contain prompt text, response text or keys: the
exception's class and, for an HTTP error, its status code are all that is
said about a failure.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import config as cfg
from llm.router import (
    LLMRouter,
    PromptSegments,
    RemoteProviderNotAllowedError,
    build_prompt_segments,
)

logger = logging.getLogger(__name__)

#: Held for the whole of one warm-up pass, so a startup warm-up and a
#: manual ``POST /admin/llm/warmup`` never send the same prefix twice at
#: once.
_warmup_lock = threading.Lock()


@dataclass(frozen=True)
class SourceWarmup:
    """What happened when one data source's prefix was warmed.

    Parameters
    ----------
    source:
        The data source name, or ``None`` for the whole schema (a
        deployment with one data source).
    status:
        ``"warmed"``, ``"skipped"`` (nothing to warm, or not allowed to)
        or ``"failed"``.
    detail:
        Why it was skipped or failed, in words that carry no prompt text;
        ``None`` for ``"warmed"``.
    duration_ms:
        Wall-clock time spent on the request, or ``None`` when none was
        sent.
    prompt_tokens:
        ``usage.prompt_tokens`` the server reported for the warm-up
        request -- the size of the prefix in the model's own tokens.
    cached_tokens:
        How many of those the server already had cached, when it reports
        that (vLLM's ``--enable-prompt-tokens-details``); a value close to
        ``prompt_tokens`` means the cache was already warm.

    Examples
    --------
    >>> SourceWarmup("sales", "warmed", None, 41000, 4612, None).as_dict()["prompt_tokens"]
    4612
    """

    source: str | None
    status: str
    detail: str | None = None
    duration_ms: int | None = None
    prompt_tokens: int | None = None
    cached_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """A JSON-ready copy, for ``POST /admin/llm/warmup``."""
        return asdict(self)


def _source_label(source: str | None) -> str:
    """The name a log line uses for *source*."""
    return source if source is not None else "(whole schema)"


def _describe_failure(exc: BaseException) -> str:
    """A log-safe description of *exc*: its class, plus the HTTP status if it has one.

    Exception *messages* are left out on purpose. Those of ``requests``
    carry URLs, and those of an endpoint's error body can echo what was
    sent.

    Examples
    --------
    >>> _describe_failure(TimeoutError("anything at all"))
    'TimeoutError'
    >>> import requests
    >>> r = requests.Response(); r.status_code = 400
    >>> _describe_failure(requests.HTTPError(response=r))
    'HTTPError (HTTP 400)'
    """
    text = type(exc).__name__
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        text += f" (HTTP {status})"
    return text


def warm_sources() -> tuple[str | None, ...]:
    """The data sources whose prefix a real request can start with.

    ``(None,)`` for a deployment with one data source, whose prompts are
    built for the whole schema; the source names, default first, when
    several are configured -- the same split
    :func:`prompt_engine.source_scope.scoped_source` makes.

    Examples
    --------
    >>> warm_sources()
    (None,)
    """
    import database.datasources as datasources

    names = datasources.datasource_names()
    return (None,) if len(names) < 2 else tuple(names)


def _prefix_segments(system_prompt: str, source: str | None) -> PromptSegments:
    """The cacheable part of a real prompt for *source*, and nothing else.

    Built by :func:`~llm.router.build_prompt_segments` with an empty
    question and context, so ``static_prefix`` is what real requests
    carry; it is empty when *source* is on the retrieval path (no stable
    prefix to warm).

    Examples
    --------
    >>> seg = _prefix_segments("You are a T-SQL expert.", None)
    >>> seg.static_prefix.startswith("\\nYou are a T-SQL expert.")
    True
    >>> seg.question, seg.session_context
    ('', '')
    """
    from core.models import RetrievalContext

    built = build_prompt_segments("", system_prompt, RetrievalContext(), source=source)
    return PromptSegments(static_prefix=built.static_prefix)


def _warm_one(
    router: LLMRouter, system_prompt: str, source: str | None, timeout: float,
) -> SourceWarmup:
    """Warm one source; never raises."""
    label = _source_label(source)
    try:
        segments = _prefix_segments(system_prompt, source)
    except Exception as exc:  # noqa: BLE001 - a warm-up must not fail anything else
        logger.warning(
            "LLM prefix warm-up: source=%s skipped, could not build the prefix (%s)",
            label, _describe_failure(exc),
        )
        return SourceWarmup(source, "skipped", f"could not build the prefix ({_describe_failure(exc)})")
    if not segments.static_prefix:
        logger.info(
            "LLM prefix warm-up: source=%s skipped, its prompt uses retrieval, not a static prefix",
            label,
        )
        return SourceWarmup(source, "skipped", "prompt uses retrieval, not a static prefix")

    start = time.perf_counter()
    try:
        report = router.warm_prefix(segments, timeout=timeout)
    except NotImplementedError:
        logger.info("LLM prefix warm-up: source=%s skipped, the backend has no prefix cache", label)
        return SourceWarmup(source, "skipped", "backend has no prefix cache to warm")
    except RemoteProviderNotAllowedError:
        logger.info(
            "LLM prefix warm-up: source=%s skipped, the endpoint is not trusted "
            "and LLM_ALLOW_REMOTE is off", label,
        )
        return SourceWarmup(source, "skipped", "endpoint not allowed (LLM_ALLOW_REMOTE is off)")
    except Exception as exc:  # noqa: BLE001 - every failure is logged and swallowed
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        what = _describe_failure(exc)
        logger.warning(
            "LLM prefix warm-up: source=%s failed after %d ms (%s)", label, elapsed_ms, what,
        )
        return SourceWarmup(source, "failed", what, duration_ms=elapsed_ms)

    elapsed_ms = round((time.perf_counter() - start) * 1000)
    prompt_tokens = report.get("prompt_tokens")
    cached_tokens = report.get("cached_tokens")
    logger.info(
        "LLM prefix warm-up: source=%s warmed in %d ms, prompt_tokens=%s%s",
        label, elapsed_ms,
        prompt_tokens if prompt_tokens is not None else "unknown",
        f", cached_tokens={cached_tokens}" if cached_tokens is not None else "",
    )
    return SourceWarmup(
        source, "warmed", None, duration_ms=elapsed_ms,
        prompt_tokens=prompt_tokens, cached_tokens=cached_tokens,
    )


def warm_prefix_cache(
    system_prompt: str,
    router_factory: Callable[[], LLMRouter],
    *,
    timeout_seconds: float | None = None,
) -> list[SourceWarmup] | None:
    """Warm the prefix cache for every data source, within one time budget.

    Sources are warmed one after another (a model server prefills them one
    after another anyway); each request is given what is left of the
    budget as its HTTP timeout, and a source reached after the budget is
    spent is skipped. Never raises.

    Parameters
    ----------
    system_prompt:
        The loaded system prompt, as ``api/server.py`` holds it.
    router_factory:
        Returns the :class:`~llm.router.LLMRouter` real requests use
        (``api.runner``'s agent's router). Called inside this function so
        that a failure to build it is logged rather than raised.
    timeout_seconds:
        Total budget in seconds; ``LLM_PREFIX_WARMUP_TIMEOUT_SECONDS`` when
        ``None``. The budget bounds each request's timeout, so a request
        that connects slowly and then reads slowly can overrun it by up to
        one more timeout; it is a bound, not a deadline to the millisecond.

    Returns
    -------
    list[SourceWarmup] | None
        One entry per source, in order. ``None`` when another warm-up is
        already running (nothing was sent).

    Examples
    --------
    >>> from llm.providers import MockBackend
    >>> router = LLMRouter(default_chain=[MockBackend()])
    >>> [r.status for r in warm_prefix_cache("You are a T-SQL expert.", lambda: router)]
    ['skipped']
    """
    if not _warmup_lock.acquire(blocking=False):
        logger.info("LLM prefix warm-up: another warm-up is already running, not starting a second")
        return None
    try:
        budget = float(
            timeout_seconds if timeout_seconds is not None
            else cfg.settings.llm_prefix_warmup_timeout_seconds
        )
        deadline = time.perf_counter() + budget
        try:
            router = router_factory()
            sources = warm_sources()
        except Exception as exc:  # noqa: BLE001 - nothing to warm, nothing to break
            logger.warning(
                "LLM prefix warm-up: not started, could not set up (%s)", _describe_failure(exc),
            )
            return [SourceWarmup(None, "skipped", f"could not set up ({_describe_failure(exc)})")]

        results: list[SourceWarmup] = []
        for source in sources:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                logger.warning(
                    "LLM prefix warm-up: source=%s skipped, the %.0f s budget is spent",
                    _source_label(source), budget,
                )
                results.append(SourceWarmup(source, "skipped", "time budget exhausted"))
                continue
            results.append(_warm_one(router, system_prompt, source, remaining))
        return results
    finally:
        _warmup_lock.release()


def start_background_warmup(
    system_prompt: str,
    router_factory: Callable[[], LLMRouter],
) -> threading.Thread:
    """Run :func:`warm_prefix_cache` on a daemon thread and return at once.

    The thread is a daemon so that it can never keep the process alive at
    shutdown, and it catches everything, so a bug in the warm-up is a log
    line and not an unhandled-thread-exception traceback.

    Parameters
    ----------
    system_prompt, router_factory:
        As for :func:`warm_prefix_cache`.

    Returns
    -------
    threading.Thread
        The started thread (tests ``join`` it; the server ignores it).

    Examples
    --------
    >>> from llm.providers import MockBackend
    >>> router = LLMRouter(default_chain=[MockBackend()])
    >>> t = start_background_warmup("You are a T-SQL expert.", lambda: router)
    >>> t.join(5); t.is_alive()
    False
    """

    def _run() -> None:
        try:
            warm_prefix_cache(system_prompt, router_factory)
        except Exception as exc:  # noqa: BLE001 - belt and braces: warm_prefix_cache already catches
            logger.warning("LLM prefix warm-up: stopped unexpectedly (%s)", _describe_failure(exc))

    thread = threading.Thread(target=_run, name="llm-prefix-warmup", daemon=True)
    thread.start()
    return thread
