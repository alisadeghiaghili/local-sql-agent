# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Shared worker-thread concurrency bound for pipeline routes.

All blocking pipeline operations (LLM calls, database queries, turn engine
execution) run in a worker thread (``asyncio.to_thread``) to avoid blocking
the event loop. This module enforces one global semaphore across all such
operations, bounding how many worker threads may be doing pipeline work at
once, regardless of which route dispatched them.

One shared semaphore, not one per event loop
----------------------------------------------
An earlier version of this module kept a *dict* of semaphores keyed by
``asyncio.get_running_loop()``, out of a concern that a single
``asyncio.Semaphore`` instance shared across event loops would raise
"attached to a different event loop" errors. That concern does not apply
here: ``asyncio.Semaphore`` (unlike the ``asyncio.Lock`` of very old Python
versions) does not bind to a loop at construction time, only lazily, on
its first ``await`` — and even then, plain counting/waking logic has no
loop-affinity problem as long as it is only ever awaited from coroutines,
never shared with, say, a raw ``concurrent.futures`` primitive. A full test
session run against one module-level ``asyncio.Semaphore`` (this module's
current shape) produced zero such errors. The per-loop dict bought nothing
and cost a permanent, unbounded, strong reference to every event loop that
ever called :func:`run_bounded` (each loop's semaphore stayed in the dict
forever, since nothing ever evicted it).
"""

from __future__ import annotations

import asyncio
import os
from typing import Callable, TypeVar

_QUERY_THREAD_LIMIT: int = int(os.getenv("QUERY_THREAD_LIMIT", "16"))

# The ONE semaphore shared by every capped pipeline route: POST /query's
# _run_query_bounded, POST /query/stream's worker, the v2 turn helpers
# (_ask_turn_bounded, the streaming pipeline worker in
# _ask_turn_streaming_stages), and PATCH .../assumptions. A module-level
# object rather than one constructed inside run_bounded, precisely so a
# test can replace it wholesale (see _get_semaphore's docstring below) to
# exercise a tight bound (e.g. asyncio.Semaphore(1)) without touching
# QUERY_THREAD_LIMIT or restarting the process.
_semaphore: asyncio.Semaphore = asyncio.Semaphore(_QUERY_THREAD_LIMIT)


def _get_semaphore() -> asyncio.Semaphore:
    """Return the semaphore currently in effect, looked up at call time.

    ``run_bounded`` calls this on every invocation instead of closing over
    a local alias captured once (e.g. ``from api.concurrency import
    _semaphore`` at another module's import time). That matters for
    exactly one reason: it lets a test reassign the module attribute --
    ``api.concurrency._semaphore = asyncio.Semaphore(1)`` -- and have
    every subsequent ``run_bounded`` call, from every caller that already
    imported this module (``api/server.py``, ``api/v2_routes.py``, ...),
    immediately start waiting on the replacement. A caller that captured
    the old object directly would keep waiting on a semaphore nothing
    else contends for any more, and the swap would silently do nothing.
    """
    return _semaphore


T = TypeVar("T")


async def run_bounded(fn: Callable[..., T], /, *args, **kwargs) -> T:
    """Run a blocking function in a worker thread under the concurrency bound.

    Parameters
    ----------
    fn:
        The blocking function to run. Its return value is returned verbatim,
        and any exception it raises is propagated unchanged.
    *args:
        Positional arguments forwarded to ``fn``.
    **kwargs:
        Keyword arguments forwarded to ``fn``.

    Returns
    -------
    T
        Whatever ``fn`` returned.

    Raises
    ------
    Any exception ``fn`` raises is propagated unchanged.

    Notes
    -----
    The semaphore slot is held only for as long as ``fn`` itself is
    actually running: acquired immediately before dispatching to the
    worker thread (``asyncio.to_thread``) and released the instant ``fn``
    returns or raises. It is **not** held for the lifetime of a whole HTTP
    request or streaming response -- that is a separate concern, owned by
    ``api.middleware.ConcurrencyMiddleware``'s own in-flight counter, which
    holds its slot for as long as the full response (including a streamed
    body) takes to send. This semaphore only ever bounds "how many worker
    threads are doing pipeline work right now"; the middleware bounds "how
    many requests are admitted at all".

    Because a caller wraps this coroutine in ``asyncio.create_task`` in
    more than one place in this codebase (see
    ``api.v2_routes._ask_turn_streaming_stages``), it is worth being
    explicit about cancellation: if the *task* awaiting ``run_bounded`` is
    cancelled while it is still waiting on ``async with semaphore:`` (i.e.
    before the worker thread ever starts), the wait is abandoned and the
    slot is never acquired at all. If cancellation instead arrives while
    ``await asyncio.to_thread(fn, ...)`` is in flight, the ``async with``
    block still releases the semaphore slot as soon as the
    ``CancelledError`` propagates out of this coroutine -- but the
    underlying OS worker thread is **not** interrupted by that and keeps
    running ``fn`` to completion (or failure) on its own; only this
    coroutine's *wait* for that thread is cancelled, not the thread
    itself. A cancelled caller must not assume ``fn`` stopped running.

    Deliberately NOT covered here: queue reads that merely wait for a
    producer that is itself running under ``run_bounded`` (see
    ``api.v2_routes._ask_turn_streaming_stages``'s ``events.get()`` loop)
    must stay on a plain ``asyncio.to_thread(...)`` call, outside this
    function. Routing such a read through ``run_bounded`` too would make
    it compete for the very semaphore its own producer is holding, and
    with a small enough limit (``QUERY_THREAD_LIMIT=1`` is the case the
    test suite exercises) that reader would sit blocked on the semaphore
    instead of draining the queue as events actually arrive -- turning a
    live progress stream into one delivered in a single burst only after
    the producer finishes, at best, and a straightforward source of
    worker-thread-pool exhaustion under real concurrent load at worst.
    """
    semaphore = _get_semaphore()
    async with semaphore:
        return await asyncio.to_thread(fn, *args, **kwargs)
