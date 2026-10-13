# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""When did a request last use each static prompt prefix?

The model server's prefix cache is refreshed by every request that starts
with a cached prefix: serving it counts as a use, and what was used last is
evicted last. So a prefix that a real question used a minute ago does not
need a keep-warm request (:class:`llm.warmup.KeepWarm`) -- that question
already did the keep-warm's job.

:class:`PrefixActivity` is the record of that, kept per prefix and not per
data source: :meth:`llm.router.LLMRouter._call_chain` knows the prompt it
sent, not which source it was routed to, and the keep-warm can build each
source's prefix (the same string, from the same cache) and ask about it.

Only the time of the last use is kept, as a hash of the prefix and never its
text. The record is bounded (the least recently used entry is dropped), so a
deployment with many data sources, or a test that builds many prefixes,
cannot grow it without limit.

Examples
--------
>>> clock = iter([100.0, 130.0, 500.0]).__next__
>>> activity = PrefixActivity(clock=clock)
>>> activity.seconds_since_use("prefix")
>>> activity.note_use("prefix")
>>> activity.seconds_since_use("prefix")
30.0
>>> activity.note_use("")  # no prefix, nothing recorded
>>> len(activity)
1
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

__all__ = ["PrefixActivity", "activity"]


class PrefixActivity:
    """Per-prefix time of the last request that used it. Thread-safe.

    Parameters
    ----------
    clock:
        Returns the current time in seconds on a monotonic scale; tests
        pass a fake one.
    max_entries:
        Prefixes remembered at once, far above the number of data sources a
        deployment has; only a bound on memory.
    """

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, max_entries: int = 256,
    ) -> None:
        self._clock = clock
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._last: OrderedDict[tuple[int, int], float] = OrderedDict()

    @staticmethod
    def _key(prefix: str) -> tuple[int, int]:
        # hash() of a str is computed once per string object and cached on
        # it, and the static prefix is the one cached string object
        # ``build_static_prefix`` returns, so this is O(1) per request after
        # the first. The length makes an accidental collision of two
        # different prefixes vanishingly unlikely.
        return (len(prefix), hash(prefix))

    def note_use(self, prefix: str) -> None:
        """Record that a request using *prefix* has just been served.

        An empty *prefix* (a prompt with no static prefix: the retrieval
        path, interpretation, assumption extraction) is ignored.
        """
        if not prefix:
            return
        key = self._key(prefix)
        now = self._clock()
        with self._lock:
            self._last[key] = now
            self._last.move_to_end(key)
            while len(self._last) > self._max_entries:
                self._last.popitem(last=False)

    def seconds_since_use(self, prefix: str) -> float | None:
        """Seconds since *prefix* was last used, or ``None`` if never (this process)."""
        if not prefix:
            return None
        with self._lock:
            last = self._last.get(self._key(prefix))
        return None if last is None else max(self._clock() - last, 0.0)

    def clear(self) -> None:
        """Forget everything. Test-only."""
        with self._lock:
            self._last.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._last)


#: The process-wide record the router writes to and the keep-warm reads.
activity = PrefixActivity()
