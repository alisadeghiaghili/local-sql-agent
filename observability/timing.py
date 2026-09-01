# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Per-stage timing collection matching ``docs/api-contract-v2.md`` §4.

The contract's ``Turn.timings`` field has an exact, fixed shape::

    {
      "total_ms": 2840, "plan_ms": 4, "prompt_ms": 11, "llm_ms": 2310,
      "guard_ms": 6, "execute_ms": 480, "interpret_ms": 0
    }

:class:`StageTimer` accumulates named stage durations and renders that
exact shape via :meth:`StageTimer.snapshot`. It is the seam the API layer
will call once wiring lands (see the package docstring).

Concurrency
-----------
The API is multi-threaded, so there must be no module-level mutable
accumulator shared across requests. Each request creates its **own**
:class:`StageTimer` instance — instances are cheap and carry no shared
state with each other. Within a single instance, an internal
``threading.Lock`` still guards the duration dict, so a stage measured
concurrently from more than one thread (e.g. two sub-tasks both timed as
``"execute"``) accumulates safely rather than racing.

Only :func:`time.perf_counter` is used — a monotonic clock that is
immune to system clock adjustments (NTP steps, DST, manual changes),
which matters because durations are subtracted, never compared to an
absolute point in time.

Usage::

    from observability.timing import StageTimer

    timer = StageTimer()
    with timer.stage("plan"):
        ...
    with timer.stage("prompt"):
        ...
    timings = timer.snapshot()   # -> contract-shaped dict, ready to embed in a Turn
"""

from __future__ import annotations

import functools
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from typing import Callable, ClassVar, Iterator, TypeVar

#: Stage names recognised by :meth:`StageTimer.stage`, in the order the
#: contract's ``timings`` object lists them (excluding ``total_ms``, which
#: is derived rather than a stage a caller times explicitly).
STAGE_NAMES: tuple[str, ...] = (
    "plan", "prompt", "llm", "guard", "execute", "interpret",
)

_F = TypeVar("_F", bound=Callable[..., object])


class UnknownStageError(ValueError):
    """Raised when :meth:`StageTimer.stage` is called with an unrecognised name.

    Keeping the stage vocabulary closed (rather than accepting any string)
    guarantees :meth:`StageTimer.snapshot` always renders the exact key set
    the API contract expects — a typo'd stage name fails loudly at the call
    site instead of silently vanishing from the timings a client sees.
    """


class StageTimer:
    """Accumulate wall-clock durations for the named pipeline stages of one request.

    Create exactly one instance per request/turn; never share an instance
    across requests or reuse it after the request completes. Do not store
    it at module scope.

    Parameters
    ----------
    None — the timer starts its ``total_ms`` clock at construction time.

    Examples
    --------
    >>> import time
    >>> timer = StageTimer()
    >>> with timer.stage("plan"):
    ...     time.sleep(0.01)
    >>> with timer.stage("llm"):
    ...     time.sleep(0.01)
    >>> snap = timer.snapshot()
    >>> sorted(snap.keys())
    ['execute_ms', 'guard_ms', 'interpret_ms', 'llm_ms', 'plan_ms', 'prompt_ms', 'total_ms']
    >>> snap["plan_ms"] >= 10
    True
    >>> snap["guard_ms"]
    0
    >>> try:
    ...     timer.stage("not_a_real_stage")
    ... except UnknownStageError as exc:
    ...     print(exc)
    unknown stage 'not_a_real_stage'; expected one of ('plan', 'prompt', 'llm', 'guard', 'execute', 'interpret')

    Note the error is raised by the ``stage(...)`` call itself, without
    needing to enter a ``with`` block — a typo'd name never gets to
    silently time nothing.
    """

    STAGES: ClassVar[frozenset[str]] = frozenset(STAGE_NAMES)

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self._durations: dict[str, float] = {}
        self._lock = threading.Lock()

    def stage(self, name: str) -> AbstractContextManager[None]:
        """Context manager timing one block of code as stage *name*.

        Parameters
        ----------
        name:
            One of :data:`STAGE_NAMES` (``"plan"``, ``"prompt"``, ``"llm"``,
            ``"guard"``, ``"execute"``, ``"interpret"``).

        Raises
        ------
        UnknownStageError
            If *name* is not a recognised stage. Raised immediately, at
            call time — not deferred until the ``with`` block is entered
            — so a typo'd stage name fails at the call site rather than
            silently timing nothing.

        Notes
        -----
        Timing the same stage more than once **adds** to its running total
        rather than overwriting it — useful when a stage naturally runs in
        more than one pass (e.g. an LLM call retried after a transport
        error should show its total time across attempts under ``llm_ms``).
        The elapsed time is recorded even if the wrapped block raises, so a
        failed stage still contributes accurate timing to the record.
        """
        if name not in self.STAGES:
            raise UnknownStageError(
                f"unknown stage {name!r}; expected one of {STAGE_NAMES}"
            )
        return self._timed(name)

    @contextmanager
    def _timed(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - t0
            with self._lock:
                self._durations[name] = self._durations.get(name, 0.0) + elapsed

    def record(self, name: str, seconds: float) -> None:
        """Manually add *seconds* to stage *name*'s running total.

        For code paths that cannot naturally use :meth:`stage` as a
        context manager (e.g. a duration measured by other means and
        reported after the fact).

        Parameters
        ----------
        name:
            One of :data:`STAGE_NAMES`.
        seconds:
            Duration to add, in seconds. Negative values are rejected.

        Raises
        ------
        UnknownStageError
            If *name* is not a recognised stage.
        ValueError
            If *seconds* is negative.

        Examples
        --------
        >>> timer = StageTimer()
        >>> timer.record("guard", 0.006)
        >>> timer.snapshot()["guard_ms"]
        6
        """
        if name not in self.STAGES:
            raise UnknownStageError(
                f"unknown stage {name!r}; expected one of {STAGE_NAMES}"
            )
        if seconds < 0:
            raise ValueError(f"seconds must be >= 0, got {seconds!r}")
        with self._lock:
            self._durations[name] = self._durations.get(name, 0.0) + seconds

    def decorate(self, name: str) -> Callable[[_F], _F]:
        """Return a decorator that times a whole function call as stage *name*.

        Equivalent to wrapping the function body in ``with self.stage(name):``,
        for call sites that prefer a decorator to a context manager.

        Parameters
        ----------
        name:
            One of :data:`STAGE_NAMES`.

        Examples
        --------
        >>> timer = StageTimer()
        >>> @timer.decorate("execute")
        ... def run() -> int:
        ...     return 42
        >>> run()
        42
        >>> timer.snapshot()["execute_ms"] >= 0
        True
        """
        def wrapper(fn: _F) -> _F:
            @functools.wraps(fn)
            def inner(*args: object, **kwargs: object) -> object:
                with self.stage(name):
                    return fn(*args, **kwargs)
            return inner  # type: ignore[return-value]
        return wrapper

    def snapshot(self) -> dict[str, int]:
        """Return the current timings as a contract-§4-shaped dict.

        ``total_ms`` is wall-clock elapsed time since this timer was
        constructed (not the sum of the individual stages), so it also
        captures time spent outside any named stage. All values are
        rounded to the nearest millisecond. Stages never timed default to
        ``0``, so the returned dict always has all seven keys regardless
        of how many stages were actually used — safe to embed directly
        into a ``Turn`` even for a request that errored out early.

        Returns
        -------
        dict[str, int]
            Keys: ``total_ms``, ``plan_ms``, ``prompt_ms``, ``llm_ms``,
            ``guard_ms``, ``execute_ms``, ``interpret_ms``.

        Examples
        --------
        >>> timer = StageTimer()
        >>> snap = timer.snapshot()
        >>> snap["total_ms"] >= 0
        True
        >>> [snap[f"{n}_ms"] for n in ("plan", "prompt", "llm", "guard", "execute", "interpret")]
        [0, 0, 0, 0, 0, 0]
        """
        with self._lock:
            durations = dict(self._durations)
        total_ms = round((time.perf_counter() - self._start) * 1000)
        result: dict[str, int] = {"total_ms": total_ms}
        for name in STAGE_NAMES:
            result[f"{name}_ms"] = round(durations.get(name, 0.0) * 1000)
        return result
