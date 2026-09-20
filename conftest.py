# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Repository-root pytest configuration.

Lives at the repo root (an ancestor of both ``tests/`` and ``eval/tests``,
the two directories ``setup.cfg``'s ``testpaths`` collects) so its hooks
and autouse fixtures apply across a single combined run of both, unlike
``tests/conftest.py`` (scoped to ``tests/`` only). That distinction is why
``_no_real_database`` and ``_no_background_dimension_refresh`` below live
here rather than in ``tests/conftest.py``, where they originally were: a
combined ``pytest tests/ eval/tests`` run collected ``eval/tests`` with
neither guard active, and a cold dimension-vocabulary lookup during an
eval test could spin up a background thread that reached the real
database. See each fixture's own docstring for the detail.

Phase 4 made ``project_config/`` configurable (``PROJECT_CONFIG_DIR``, see
``config.Settings.project_config_dir``) so CI and a fresh clone — which
never have the real, git-ignored ``project_config/`` — can run against the
committed ``project_config.example/`` template instead. That template
intentionally ships anonymised placeholder data (different table names, no
real ring names, no real synonyms, no real business rules), the same way
``project_config.example/aliases.yaml`` already did before this phase.

A test that asserts a *specific real domain value* — a real table/column
name, a real ring name's exact display text, a real Persian synonym, the
`eval_data.example/golden.jsonl` golden set's real-schema SQL and expected
rows, the exact 12-table/87-column schema snapshot — has nothing to check
that value against once ``PROJECT_CONFIG_DIR`` points at the example
directory: it is not a bug in the code, it is the example data genuinely
not containing what the test looks for. Marking such a test
``@pytest.mark.domain_data`` (or ``pytestmark = pytest.mark.domain_data``
for a whole module) makes that explicit; this hook turns the marker into a
clean, clearly-reasoned skip instead of a confusing failure whenever the
example config is in effect. Against the real ``project_config/`` (the
default, and every developer's normal local run), the marker does nothing
at all — these tests run exactly as before.

A test that only happened to reach for a real name as a stand-in for "any
known table" (most of what Phase 4's CI-greening pass found) is NOT marked
this way — it was rewritten instead to derive the name it needs from
whatever schema is actually loaded, so it keeps running, and keeps proving
something, under both configurations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import time

import pytest

_REPO_ROOT = Path(__file__).resolve().parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"

# ---------------------------------------------------------------------------
# The two trees _no_real_database / _no_background_dimension_refresh guard
# ---------------------------------------------------------------------------
# Resolved once, at import time, rather than re-resolved per test: both are
# fixed, well-known locations relative to this file.
_GUARDED_TEST_DIRS = (
    (_REPO_ROOT / "tests").resolve(),
    (_REPO_ROOT / "eval" / "tests").resolve(),
)


def _item_needs_database_guard(request: pytest.FixtureRequest) -> bool:
    """True when the collected item lives under ``tests/`` or ``eval/tests/``.

    ``_no_real_database`` and ``_no_background_dimension_refresh`` are
    autouse at the repository root so they cover a combined
    ``pytest tests/ eval/tests`` run (see both fixtures' docstrings for why
    that combined run is the whole point) -- but the root ``conftest.py``
    also applies to *every other* collection rooted here, including CI's
    separate ``pytest --doctest-modules ... database ... config.py`` step,
    which collects doctests straight out of the source tree. Those doctests
    are not test-suite code: ``database.connection.dispose_engine``'s own
    doctest legitimately builds and disposes a real (if immediately-closed)
    engine to demonstrate the function, and unconditionally patching
    ``create_engine`` out from under it turned a passing doctest into
    ``AssertionError: This test tried to build a real SQLAlchemy engine``
    across every supported Python version in CI.

    So both fixtures below check this first and no-op (patch nothing,
    change no flag) for anything collected outside the two directories the
    assertion message itself already claims to be about. ``request.node.path``
    (a ``pathlib.Path`` on the pytest versions this repo supports) is
    resolved and compared with :meth:`~pathlib.Path.is_relative_to` against
    both resolved guarded directories -- never by matching the substring
    ``"tests"`` in the path, which would also match an unrelated module
    named e.g. ``schema_data/tests_helper.py``, or the repository itself
    living under a directory literally called ``tests`` on some checkout.
    """
    path = request.node.path.resolve()
    return any(path.is_relative_to(guarded) for guarded in _GUARDED_TEST_DIRS)

_SKIP_REASON = (
    "requires real project_config/ domain data -- PROJECT_CONFIG_DIR is "
    "pointing at project_config.example/, which ships anonymised "
    "placeholder data only (see conftest.py and the domain_data marker "
    "in setup.cfg)"
)


def _running_against_example_config() -> bool:
    """True when ``cfg.settings.project_config_dir`` resolves to
    ``project_config.example/`` exactly (this repo's one committed
    template directory) -- not merely "not named project_config", so an
    operator pointing at some other real, custom directory is never
    mistaken for "no real data available" and does not skip anything.

    Deliberately does NOT fire just because the real ``project_config/``
    happens to be absent while the setting is left at its default: that
    case must keep failing loudly at collection (``ConfigNotFoundError``,
    see ``knowledge/config_loader.py``), not be silently masked by a
    skip -- see Phase 4's "no automatic fallback" decision in
    ``config.Settings.project_config_dir``.
    """
    import config as cfg  # deferred: this module is imported before config.py's

    configured = Path(cfg.settings.project_config_dir)
    resolved = configured if configured.is_absolute() else (_REPO_ROOT / configured)
    try:
        return resolved.resolve() == _EXAMPLE_CONFIG_DIR.resolve()
    except OSError:
        return False


def _refuse_real_engine(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(
        "This test tried to build a real SQLAlchemy engine.\n"
        "\n"
        "Nothing under tests/ or eval/tests should open a real database "
        "connection: the default DB_CONNECTION_URL points at the literal "
        "host 'server', so the attempt does not fail fast — it blocks on "
        "DNS and the ODBC login timeout for ~21s per call.\n"
        "\n"
        "Patch the seam your test actually needs:\n"
        "  - patch('api.health.check_health')            for /health routes\n"
        "  - patch('database.executor.execute_query')    for query execution\n"
        "  - patch('database.connection.create_engine')  to exercise the "
        "engine factory itself\n"
    )


@pytest.fixture(autouse=True)
def _no_real_database(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail fast, and loudly, if a test reaches real engine construction.

    Lives here, at the repository root, rather than in ``tests/conftest.py``
    where it originally did, because a plain ``pytest`` conftest only
    applies to the directory tree rooted where it lives: ``tests/conftest.py``
    covers ``tests/`` but has no effect on ``eval/tests``, which collects
    into the very same session whenever CI runs ``pytest tests/ eval/tests``.
    With the fixture scoped to ``tests/`` alone, that combined run executed
    ``eval/tests`` with no engine guard at all — the one place this was
    caught was a background dimension-vocabulary refresh thread (see
    ``_no_background_dimension_refresh`` below) spawned from an eval test,
    which went on to reach the real ``pyodbc`` driver and dial the
    configured warehouse host. Moving both fixtures to this file, an
    ancestor of both ``tests/`` and ``eval/tests``, closes that gap for the
    whole combined run without changing anything about how either fixture
    behaves for ``tests/`` on its own.

    Deliberately scoped to ``tests/`` and ``eval/tests`` only (see
    :func:`_item_needs_database_guard`), and a plain no-op -- no patch, no
    cache clear -- everywhere else: being autouse at the repository root
    also makes this fixture apply to CI's separate
    ``pytest --doctest-modules ... database ... config.py`` step, which
    collects doctests directly out of the source tree, not out of either
    test suite. Patching ``database.connection.create_engine`` out from
    under that step broke ``database.connection.dispose_engine``'s own
    doctest, which legitimately builds (and immediately disposes) a real
    engine to demonstrate the function -- the doctest is not a test-suite
    test and was never meant to be caught by this guard.

    ``get_engine`` is ``lru_cache``-backed, so a real engine built by an
    earlier test would be reused by later ones without ever calling
    ``create_engine`` again. The cache is therefore cleared on both sides of
    every guarded test, which also removes a source of order-dependent
    behaviour. Left untouched (not cleared) when this fixture is a no-op,
    so an ungated doctest run never pays for, or is affected by, a cache
    it never asked for.
    """
    if not _item_needs_database_guard(request):
        yield
        return

    from database.connection import get_engine

    get_engine.cache_clear()
    try:
        with patch(
            "database.connection.create_engine",
            side_effect=_refuse_real_engine,
        ):
            yield
    finally:
        get_engine.cache_clear()


@pytest.fixture(autouse=True)
def _no_background_dimension_refresh(request: pytest.FixtureRequest) -> Iterator[None]:
    """Disable ``retrieval.dimension_vocabulary``'s background self-healing
    refresh for every test under ``tests/`` and ``eval/tests``, and restore
    it afterwards.

    Scoped the same way, and for the same reason, as ``_no_real_database``
    above (see :func:`_item_needs_database_guard`): being autouse at the
    repository root also makes this fixture apply to CI's
    ``pytest --doctest-modules`` step over the source tree, where flipping
    a module-global flag off and back on around every doctest is at best
    pointless and at worst racy against whatever else that process is
    doing. Outside ``tests/`` and ``eval/tests`` this is a plain ``yield``
    -- the flag is left exactly as the module default (or whatever a prior
    caller set it to) leaves it.

    Phase 5b's stale-while-revalidate redesign makes a cold or stale
    dimension-vocabulary lookup trigger a background refresh against the
    real database by default (see that module's docstring). Left enabled
    here, an ordinary route test that mentions any of ``Ring``/``Currency``/
    ``Broker``/``DeliveryPlace``/``Symbol`` -- ordinary questions do -- would
    spin up a background thread reaching ``database.connection.create_engine``
    on every single such test, since the vocabulary cache starts cold and
    nothing in either suite warms it. ``_no_real_database`` above turns that
    into a caught, logged ``AssertionError`` rather than a ~21s hang, but a
    background thread quietly doing that on every cold lookup during
    ordinary tests is still exactly the class of hidden-async-work problem
    that produced this phase's one real test flake elsewhere (a leaked
    ``time.sleep`` in a different module's shared thread pool -- see
    ``tests/test_value_resolver.py``). Disabling the trigger here keeps
    every dimension-vocabulary read synchronous and silent, in both
    ``tests/`` and ``eval/tests``: a cold/stale lookup still returns
    immediately (no candidates, or stale candidates) but launches nothing.

    This fixture originally lived in ``tests/conftest.py``, scoped to
    ``tests/`` only -- see ``_no_real_database`` above for why that left
    ``eval/tests`` completely uncovered and why both fixtures now live here
    instead, at the repository root.

    ``tests/test_dimension_vocabulary.py``'s background-refresh tests
    re-enable this locally, always with an injected ``execute_fn`` and
    always inside a ``try/finally`` that restores the disabled state
    before the test ends.

    Restores whatever value was in effect *before* this fixture ran
    (``is_background_refresh_enabled()``), not a hardcoded ``True``. A
    hardcoded restore was the actual, observed cause of a real-database
    connection attempt during a full-suite run: this fixture and the
    per-class fixture in ``TestBackgroundRefresh`` both run with function
    scope, so for a test in that class this one's setup runs first
    (leaving the flag ``False``), the class fixture's setup then flips it
    to ``True`` for the test body, and teardown unwinds in the opposite
    order -- the class fixture's teardown restores ``False`` first, and
    only THEN does this fixture's own teardown run. A hardcoded
    ``set_background_refresh_enabled(True)`` here would stomp that back to
    ``True`` regardless, leaving it wrong for every subsequent test until
    another ``TestBackgroundRefresh`` test happened to reset it -- a
    window in which an ordinary test's cold vocabulary lookup would
    launch a real background thread. Save/restore make each fixture
    responsible only for the value it actually changed.
    """
    if not _item_needs_database_guard(request):
        yield
        return

    from retrieval.dimension_vocabulary import (
        is_background_refresh_enabled, set_background_refresh_enabled,
    )

    previous = is_background_refresh_enabled()
    set_background_refresh_enabled(False)
    try:
        yield
    finally:
        set_background_refresh_enabled(previous)


@pytest.fixture(autouse=True)
def _reset_health_cache_between_tests():
    """Start every test with a cold ``api.health`` probe cache.

    Finding 3's fix (``api/health.py``) makes ``check_health()`` reuse a
    probe result for :data:`api.health.HEALTH_CACHE_TTL_SECONDS`. That is
    the point in production -- it is what stops an unauthenticated caller
    from draining the connection pool ``/query`` shares -- but it is
    exactly wrong across a test suite: ``tests/test_health.py`` calls
    ``check_health()`` repeatedly in the same process, each time with a
    *different* monkeypatched probe result, and expects each call to
    reflect its own patch. Without this reset, whichever test happens to
    run first inside the TTL window "wins" and every later one silently
    observes its stale, cached answer instead of exercising its own
    mocks -- a suite that still prints green while testing nothing.
    ``tests/security_audit/test_health_probe_cache.py`` already resets
    the cache itself (it is testing the cache), so this fixture is
    redundant there and harmless everywhere else.
    """
    import api.health as health

    health.reset_health_cache()
    yield
    health.reset_health_cache()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not _running_against_example_config():
        return
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if item.get_closest_marker("domain_data") is not None:
            item.add_marker(skip)


#: How long :func:`pytest_sessionfinish` will wait, in total, for app-owned
#: background threads to finish before giving up and naming them. Test-harness
#: shutdown hygiene, not a tuning knob: too small and a healthy thread gets
#: reported as stuck, too large and a genuinely wedged one hangs CI. Seconds.
_THREAD_DRAIN_BUDGET_SECONDS = 10.0


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drain app-owned background threads before the interpreter tears down.

    A daemon thread that is still *running* when finalisation begins is
    killed at its next GIL acquisition. If that happens inside a C
    extension call -- a pandas frame being built, a mock being invoked --
    the process dies with a bare exit code 139 *after* pytest has printed
    a green summary: no failing test, no traceback, nothing to act on.
    Re-running passes, so it reads as a flake rather than a defect.

    Making those threads daemons (which
    ``retrieval.value_resolver._run_under_deadline`` and
    ``retrieval.dimension_vocabulary``'s background refresh both are)
    fixes the *other* half of this -- a non-daemon thread is
    unconditionally joined during finalisation, which is strictly worse.
    It does not fix this half. Being a daemon means the interpreter is
    *allowed* to cut the thread off; it does not make being cut off safe.

    So join them here instead, while the interpreter is still healthy and
    a join is an ordinary operation. A thread that finishes normally
    before finalisation cannot be killed mid-call during it.

    Two details matter:

    * The background-refresh trigger is switched off first, so a thread
      cannot start while others are being drained -- otherwise the drain
      races the thing it is draining.
    * The budget is bounded. A thread that will not finish is reported by
      name rather than hanging the run: the point is to make the next
      exit-139 start from a name instead of a guess, not to trade a crash
      for a hang.
    """
    import threading

    try:
        from retrieval.dimension_vocabulary import set_background_refresh_enabled

        set_background_refresh_enabled(False)
    except Exception:  # noqa: BLE001 - draining must never fail a finished run
        pass

    main = threading.main_thread()

    def _live() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t is not main and t.is_alive()]

    lingering = _live()
    if not lingering:
        return

    deadline = time.monotonic() + _THREAD_DRAIN_BUDGET_SECONDS
    for t in lingering:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            t.join(timeout=remaining)
        except RuntimeError:  # pragma: no cover - join of a not-yet-started thread
            pass

    survivors = _live()
    if not survivors:
        return

    writer = session.config.get_terminal_writer()
    writer.line("")
    writer.line(
        f"threads still alive after a {_THREAD_DRAIN_BUDGET_SECONDS:g}s drain "
        f"({len(survivors)}):",
        yellow=True,
    )
    for t in survivors:
        kind = "daemon" if t.daemon else "NON-DAEMON -- will be joined at exit"
        writer.line(f"  {t.name}: {kind}", yellow=True)
