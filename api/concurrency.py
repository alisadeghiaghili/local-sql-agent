# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Shared worker-thread concurrency bound for pipeline routes.

All blocking pipeline operations (LLM calls, database queries, turn engine
execution) run in a worker thread to avoid blocking the event loop. This
module enforces a global semaphore across all such operations to prevent
resource exhaustion when many concurrent requests arrive.

The semaphore is keyed on the event loop it belongs to, so each test
isolation or multi-loop scenario gets its own independent counter without
deadlock or interference.
"""

from __future__ import annotations

import asyncio
import os
from typing import Callable, TypeVar

_QUERY_THREAD_LIMIT: int = int(os.getenv("QUERY_THREAD_LIMIT", "16"))

# Semaphore per event loop. The asyncio.to_thread worker pool is global,
# but each event loop's semaphore is independent. This avoids the
# "attached to a different event loop" errors that occur if we try to
# reuse one semaphore instance across loop boundaries.
_semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _get_semaphore() -> asyncio.Semaphore:
    """Get or create the semaphore for the current event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        raise RuntimeError(
            "run_bounded must be called from an async context with a running event loop"
        ) from None

    if loop not in _semaphores:
        _semaphores[loop] = asyncio.Semaphore(_QUERY_THREAD_LIMIT)

    return _semaphores[loop]


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
    Any exception ``fn`` raises is propagated unchanged. The semaphore
    slot is always released, even on exception or client disconnect.

    Notes
    -----
    This function acquires one slot from the shared semaphore BEFORE
    dispatching the worker thread, so the slot is held for the entire
    duration of ``fn``'s execution. This is what prevents resource
    exhaustion when many concurrent requests are pending.

    Calls to ``asyncio.to_thread`` within an ASGI streaming context hold
    the slot until the streamed body has completely sent (or the client
    disconnects). The streaming response middleware ensures cleanup
    via a finally block.
    """
    semaphore = _get_semaphore()
    async with semaphore:
        return await asyncio.to_thread(fn, *args, **kwargs)
