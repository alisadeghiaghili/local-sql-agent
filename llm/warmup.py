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

Keeping it warm
---------------
The start-up pass primes the cache once. On a model server shared with other
users the cache is evicted again whenever their traffic needs the memory, and
the next question pays the whole prefill. :class:`KeepWarm` (started from
``api/server.py``'s ``lifespan`` when ``LLM_PREFIX_KEEPWARM_SECONDS`` is above
zero) repeats the same pass every that many seconds. It is not a second
implementation of the warm-up: every pass is :func:`warm_prefix_cache`, with
three differences that only apply to it --

* a source whose prefix a real request used within the last interval is
  skipped (:mod:`llm.prefix_activity`; the request refreshed the cache, so
  under traffic the keep-warm sends nothing);
* a repeated outcome is logged once, then at ``DEBUG`` until it changes (an
  endpoint that stays down is one warning, not one per pass);
* it does nothing while ``paused()`` says so (maintenance mode) and stops at
  shutdown.

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
from datetime import datetime, timezone
from typing import Any

import config as cfg
from llm.prefix_activity import PrefixActivity, activity as _default_activity
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


class _OutcomeLog:
    """Says each source's outcome once per streak, then only at ``DEBUG``.

    A keep-warm pass repeats every few minutes for as long as the server
    runs; an endpoint that is down, or a source on the retrieval path, must
    be one log line and not one per pass. The first time a source has an
    outcome *kind* it is logged at the level the call site chose; the same
    kind again is logged at ``DEBUG``; a different kind is a new streak and
    is logged at its level again. A success that ends a streak of failures
    says how many passes failed.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str | None, tuple[str, int]] = {}

    def failures_before(self, source: str | None) -> int:
        """How many consecutive failures *source* had up to now (0 if none)."""
        with self._lock:
            kind, count = self._state.get(source, ("", 0))
        return count if kind == "failed" else 0

    def emit(
        self, source: str | None, kind: str, level: int, message: str, *args: object,
    ) -> None:
        """Log *message* at *level* if *kind* starts a streak for *source*, else at DEBUG."""
        with self._lock:
            previous, count = self._state.get(source, ("", 0))
            new = previous != kind
            self._state[source] = (kind, 1 if new else count + 1)
        logger.log(level if new else logging.DEBUG, message, *args)


def _warm_one(
    router: LLMRouter,
    system_prompt: str,
    source: str | None,
    timeout: float,
    *,
    skip_used_within: float | None = None,
    outcomes: _OutcomeLog | None = None,
    activity: PrefixActivity | None = None,
) -> SourceWarmup:
    """Warm one source; never raises.

    Parameters
    ----------
    skip_used_within:
        When given (the keep-warm), a source whose prefix a real request
        used within this many seconds is skipped: that request already
        refreshed the server's cache. ``None`` (the start-up pass and
        ``POST /admin/llm/warmup``) always sends.
    outcomes:
        When given (the keep-warm), repeated outcomes are logged once per
        streak instead of every time.
    activity:
        The record of real requests; the process-wide one by default. A
        successful warm-up is recorded in it too, so a keep-warm pass right
        after a start-up or manual warm-up does not repeat it.
    """
    label = _source_label(source)
    activity = activity if activity is not None else _default_activity

    def say(kind: str, level: int, message: str, *args: object) -> None:
        if outcomes is not None:
            outcomes.emit(source, kind, level, message, *args)
        else:
            logger.log(level, message, *args)

    try:
        segments = _prefix_segments(system_prompt, source)
    except Exception as exc:  # noqa: BLE001 - a warm-up must not fail anything else
        say(
            "skipped:unbuildable", logging.WARNING,
            "LLM prefix warm-up: source=%s skipped, could not build the prefix (%s)",
            label, _describe_failure(exc),
        )
        return SourceWarmup(source, "skipped", f"could not build the prefix ({_describe_failure(exc)})")
    if not segments.static_prefix:
        say(
            "skipped:retrieval", logging.INFO,
            "LLM prefix warm-up: source=%s skipped, its prompt uses retrieval, not a static prefix",
            label,
        )
        return SourceWarmup(source, "skipped", "prompt uses retrieval, not a static prefix")
    if skip_used_within is not None:
        idle = activity.seconds_since_use(segments.static_prefix)
        if idle is not None and idle < skip_used_within:
            say(
                "skipped:recent", logging.DEBUG,
                "LLM prefix warm-up: source=%s skipped, a request used its prefix %.0f s ago",
                label, idle,
            )
            return SourceWarmup(
                source, "skipped", f"a request used this prefix {idle:.0f} s ago; it is warm",
            )

    start = time.perf_counter()
    try:
        report = router.warm_prefix(segments, timeout=timeout)
    except NotImplementedError:
        say(
            "skipped:no-cache", logging.INFO,
            "LLM prefix warm-up: source=%s skipped, the backend has no prefix cache", label,
        )
        return SourceWarmup(source, "skipped", "backend has no prefix cache to warm")
    except RemoteProviderNotAllowedError:
        say(
            "skipped:remote", logging.INFO,
            "LLM prefix warm-up: source=%s skipped, the endpoint is not trusted "
            "and LLM_ALLOW_REMOTE is off", label,
        )
        return SourceWarmup(source, "skipped", "endpoint not allowed (LLM_ALLOW_REMOTE is off)")
    except Exception as exc:  # noqa: BLE001 - every failure is logged and swallowed
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        what = _describe_failure(exc)
        say(
            "failed", logging.WARNING,
            "LLM prefix warm-up: source=%s failed after %d ms (%s)", label, elapsed_ms, what,
        )
        return SourceWarmup(source, "failed", what, duration_ms=elapsed_ms)

    elapsed_ms = round((time.perf_counter() - start) * 1000)
    prompt_tokens = report.get("prompt_tokens")
    cached_tokens = report.get("cached_tokens")
    activity.note_use(segments.static_prefix)
    recovered = outcomes.failures_before(source) if outcomes is not None else 0
    say(
        "warmed", logging.INFO,
        "LLM prefix warm-up: source=%s warmed in %d ms, prompt_tokens=%s%s%s",
        label, elapsed_ms,
        prompt_tokens if prompt_tokens is not None else "unknown",
        f", cached_tokens={cached_tokens}" if cached_tokens is not None else "",
        f" (recovered after {recovered} failed pass(es))" if recovered else "",
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
    skip_used_within: float | None = None,
    outcomes: _OutcomeLog | None = None,
    activity: PrefixActivity | None = None,
    cancel: Callable[[], bool] | None = None,
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
    skip_used_within, outcomes, activity:
        What distinguishes a keep-warm pass (:class:`KeepWarm`) from the
        start-up one; see :func:`_warm_one`. The defaults are the start-up
        pass.
    cancel:
        Polled before each source; when it returns true the remaining
        sources are skipped (the keep-warm's shutdown).

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
        logger.log(
            logging.DEBUG if outcomes is not None else logging.INFO,
            "LLM prefix warm-up: another warm-up is already running, not starting a second",
        )
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
            message = "LLM prefix warm-up: not started, could not set up (%s)"
            if outcomes is not None:
                outcomes.emit(None, "setup", logging.WARNING, message, _describe_failure(exc))
            else:
                logger.warning(message, _describe_failure(exc))
            return [SourceWarmup(None, "skipped", f"could not set up ({_describe_failure(exc)})")]

        results: list[SourceWarmup] = []
        for source in sources:
            if cancel is not None and cancel():
                results.append(SourceWarmup(source, "skipped", "shutting down"))
                continue
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                logger.warning(
                    "LLM prefix warm-up: source=%s skipped, the %.0f s budget is spent",
                    _source_label(source), budget,
                )
                results.append(SourceWarmup(source, "skipped", "time budget exhausted"))
                continue
            results.append(_warm_one(
                router, system_prompt, source, remaining,
                skip_used_within=skip_used_within, outcomes=outcomes, activity=activity,
            ))
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


# ---------------------------------------------------------------------------
# Keeping the cache warm
# ---------------------------------------------------------------------------

class KeepWarm:
    """Repeats the warm-up every *interval_seconds*, skipping what real traffic keeps warm.

    Each pass is :func:`warm_prefix_cache` (so the request, the
    data-governance gate, the time budget and the "retrieval path means
    nothing to warm" rule are the warm-up's own), with ``skip_used_within``
    set to the interval. See the module docstring's "Keeping it warm".

    The first pass runs one interval after :meth:`start`, not at once: the
    start-up warm-up (or the first question) is what primes a cold server.

    Parameters
    ----------
    system_prompt, router_factory:
        As for :func:`warm_prefix_cache`.
    interval_seconds:
        Seconds between passes, and the age under which a real request
        counts as having kept a prefix warm.
    paused:
        Returns true while passes must not send anything (maintenance mode).
        An exception from it counts as "not paused".
    activity:
        The record of real requests (:mod:`llm.prefix_activity`); the
        process-wide one by default, which is the one the router writes to.
    wait:
        ``wait(seconds) -> bool`` blocks for that long and returns true when
        the keep-warm was stopped meanwhile. The default waits on the stop
        event; tests pass a fake that advances a fake clock.
    now:
        Returns the current wall-clock time, for ``last_run_at``.

    Examples
    --------
    >>> from llm.providers import MockBackend
    >>> router = LLMRouter(default_chain=[MockBackend()])
    >>> keepwarm = KeepWarm("You are a T-SQL expert.", lambda: router, 60)
    >>> [r.status for r in keepwarm.run_once()]
    ['skipped']
    >>> keepwarm.status()["last_run_outcome"]
    'skipped'
    """

    def __init__(
        self,
        system_prompt: str,
        router_factory: Callable[[], LLMRouter],
        interval_seconds: float,
        *,
        paused: Callable[[], bool] | None = None,
        activity: PrefixActivity | None = None,
        wait: Callable[[float], bool] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._router_factory = router_factory
        self.interval_seconds = float(interval_seconds)
        self._paused = paused
        self._activity = activity if activity is not None else _default_activity
        self._stop = threading.Event()
        self._wait = wait if wait is not None else self._stop.wait
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))
        self._outcomes = _OutcomeLog()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._runs = 0
        self._last_run_at: str | None = None
        self._last_outcome: str | None = None
        self._last_results: list[SourceWarmup] = []

    # -- control ---------------------------------------------------------

    def start(self) -> threading.Thread:
        """Start the daemon thread (once) and return it."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="llm-prefix-keepwarm", daemon=True,
            )
            self._thread.start()
            return self._thread

    def stop(self, timeout: float = 5.0) -> bool:
        """Ask the thread to stop and wait up to *timeout* seconds for it.

        Returns true when it has ended. A pass in the middle of a request
        to the model server ends with that request (bounded by
        ``LLM_PREFIX_WARMUP_TIMEOUT_SECONDS``); the thread is a daemon, so a
        process that is exiting does not wait for it.
        """
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            return not thread.is_alive()
        return True

    @property
    def running(self) -> bool:
        """True while the thread is alive and has not been asked to stop."""
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and not self._stop.is_set())

    # -- one pass --------------------------------------------------------

    def _is_paused(self) -> bool:
        if self._paused is None:
            return False
        try:
            return bool(self._paused())
        except Exception:  # noqa: BLE001 - a broken pause check must not stop the keep-warm
            return False

    def run_once(self) -> list[SourceWarmup] | None:
        """Run one pass now; never raises.

        Returns
        -------
        list[SourceWarmup] | None
            One entry per source, as for :func:`warm_prefix_cache`; ``None``
            when nothing was attempted (paused, stopped, or another warm-up
            was running).
        """
        if self._stop.is_set():
            return None
        if self._is_paused():
            self._record("paused", [])
            return None
        try:
            results = warm_prefix_cache(
                self._system_prompt, self._router_factory,
                skip_used_within=self.interval_seconds,
                outcomes=self._outcomes, activity=self._activity,
                cancel=self._stop.is_set,
            )
        except Exception as exc:  # noqa: BLE001 - belt and braces: warm_prefix_cache already catches
            self._outcomes.emit(
                None, "crashed", logging.WARNING,
                "LLM prefix keep-warm: a pass stopped unexpectedly (%s)", _describe_failure(exc),
            )
            self._record("failed", [SourceWarmup(None, "failed", _describe_failure(exc))])
            return None
        if results is None:
            self._record("busy", [])
            return None
        if any(r.status == "failed" for r in results):
            outcome = "failed"
        elif any(r.status == "warmed" for r in results):
            outcome = "warmed"
        else:
            outcome = "skipped"
        self._record(outcome, results)
        return results

    def _record(self, outcome: str, results: list[SourceWarmup]) -> None:
        with self._lock:
            self._runs += 1
            self._last_run_at = self._now().isoformat()
            self._last_outcome = outcome
            self._last_results = list(results)

    def _run(self) -> None:
        logger.info(
            "LLM prefix keep-warm: every %.0f s; a source a real request used within that "
            "time is skipped", self.interval_seconds,
        )
        while not self._stop.is_set():
            if self._wait(self.interval_seconds):
                break
            self.run_once()
        logger.info("LLM prefix keep-warm: stopped")

    # -- what the admin routes show ---------------------------------------

    def status(self) -> dict[str, Any]:
        """A JSON-ready account of the keep-warm and its last pass.

        ``last_run_outcome`` is ``"warmed"`` (at least one prefix was sent
        and answered), ``"skipped"`` (every source was skipped: kept warm by
        traffic, on the retrieval path, or not allowed), ``"failed"`` (at
        least one request failed), ``"paused"`` (maintenance mode) or
        ``"busy"`` (another warm-up was running); ``None`` before the first
        pass. ``last_run`` has one entry per source, as
        ``POST /admin/llm/warmup`` does. No prompt text anywhere.
        """
        paused = self._is_paused()
        with self._lock:
            return {
                "enabled": True,
                "interval_seconds": self.interval_seconds,
                "running": self.running,
                "paused": paused,
                "runs": self._runs,
                "last_run_at": self._last_run_at,
                "last_run_outcome": self._last_outcome,
                "last_run": [r.as_dict() for r in self._last_results],
            }


#: The keep-warm :func:`start_keepwarm` started, if any.
_keepwarm: KeepWarm | None = None
_keepwarm_lock = threading.Lock()


def start_keepwarm(
    system_prompt: str,
    router_factory: Callable[[], LLMRouter],
    *,
    paused: Callable[[], bool] | None = None,
) -> KeepWarm | None:
    """Start the process's keep-warm if ``LLM_PREFIX_KEEPWARM_SECONDS`` asks for one.

    Returns ``None`` (and starts nothing) when the setting is ``0``, when
    ``LLM_PROVIDER=mock`` (there is no model server to keep warm), or when a
    keep-warm is already running. Never raises.

    Examples
    --------
    >>> start_keepwarm("You are a T-SQL expert.", lambda: None) is None
    True
    """
    global _keepwarm
    try:
        seconds = float(cfg.settings.llm_prefix_keepwarm_seconds)
        if seconds <= 0:
            return None
        if str(cfg.settings.llm_provider).lower() == "mock":
            logger.info("LLM prefix keep-warm: not started, LLM_PROVIDER=mock has no model server")
            return None
        with _keepwarm_lock:
            if _keepwarm is not None and _keepwarm.running:
                return None
            keepwarm = KeepWarm(system_prompt, router_factory, seconds, paused=paused)
            keepwarm.start()
            _keepwarm = keepwarm
            return keepwarm
    except Exception as exc:  # noqa: BLE001 - an optimisation, startup must not fail on it
        logger.warning("LLM prefix keep-warm could not be started: %s", _describe_failure(exc))
        return None


def stop_keepwarm(timeout: float = 5.0) -> None:
    """Stop the process's keep-warm, if one is running (``lifespan`` shutdown). Never raises."""
    global _keepwarm
    with _keepwarm_lock:
        keepwarm, _keepwarm = _keepwarm, None
    if keepwarm is None:
        return
    try:
        if not keepwarm.stop(timeout):
            logger.warning(
                "LLM prefix keep-warm: still finishing a request to the model server at "
                "shutdown; it is a daemon thread and ends with the process",
            )
    except Exception as exc:  # noqa: BLE001 - shutdown must not fail on it
        logger.warning("LLM prefix keep-warm could not be stopped: %s", _describe_failure(exc))


def keepwarm_status() -> dict[str, Any]:
    """The keep-warm's status, or the reason there is none.

    With no keep-warm running: ``{"enabled": False, "reason": ...}`` and the
    same keys with empty values. Safe to call from a request.

    Examples
    --------
    >>> keepwarm_status()["enabled"]
    False
    """
    with _keepwarm_lock:
        keepwarm = _keepwarm
    if keepwarm is not None:
        return keepwarm.status()
    seconds = float(cfg.settings.llm_prefix_keepwarm_seconds)
    if seconds <= 0:
        reason = "LLM_PREFIX_KEEPWARM_SECONDS is 0"
    elif str(cfg.settings.llm_provider).lower() == "mock":
        reason = "LLM_PROVIDER=mock has no model server"
    else:
        reason = "not started"
    return {
        "enabled": False,
        "reason": reason,
        "interval_seconds": seconds,
        "running": False,
        "paused": False,
        "runs": 0,
        "last_run_at": None,
        "last_run_outcome": None,
        "last_run": [],
    }
