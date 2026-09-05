# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Repository-root pytest configuration.

Lives at the repo root (an ancestor of both ``tests/`` and ``eval/tests``,
the two directories ``setup.cfg``'s ``testpaths`` collects) so its hooks
apply across a single combined run of both, unlike ``tests/conftest.py``
(scoped to ``tests/`` only).

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

import time

import pytest

_REPO_ROOT = Path(__file__).resolve().parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"

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
