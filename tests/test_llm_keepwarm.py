# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The periodic prefix-cache keep-warm (``llm.warmup.KeepWarm``).

The start-up warm-up primes a model server's prefix cache once; on a shared
server it is evicted again. The keep-warm repeats the warm-up every
``LLM_PREFIX_KEEPWARM_SECONDS``. What is pinned here is what makes that safe
to leave on:

* it **skips a source a real request used within the interval** (so it costs
  nothing under traffic) and sends the same request the warm-up sends
  otherwise;
* it runs **every interval**, and **stops** at shutdown;
* it **obeys the warm-up's rules**: nothing to an untrusted endpoint without
  ``LLM_ALLOW_REMOTE``, nothing under ``LLM_PROVIDER=mock``, nothing for a
  source on the retrieval path;
* it **pauses** in maintenance mode;
* a failure is **logged once per streak**, never fatal, and no line holds
  prompt text, response text or a key.

Time is a fake clock and the model server a fake backend throughout: no test
sleeps for an interval or opens a network connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config as cfg
from api import runner
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache
from config import Settings, override_settings
from llm import prefix_activity, warmup
from llm.base import LLMBackend
from llm.prefix_activity import PrefixActivity
from llm.providers import MockBackend
from llm.router import LLMRouter, PromptSegments, TaskType
from llm.sql_agent import SQLAgent

SYSTEM_PROMPT = "You are a T-SQL expert."
SECRET = "sk-test-SECRET-MARKER-0123456789"
INTERVAL = 60.0


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingBackend(LLMBackend):
    """A model server's stand-in: records what ``warm_prefix`` is sent."""

    def __init__(self, *, fail: BaseException | None = None, trusted: bool = True,
                 block: threading.Event | None = None) -> None:
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.fail = fail
        self._trusted = trusted
        self._block = block

    @property
    def trusted(self) -> bool:
        return self._trusted

    @property
    def name(self) -> str:
        return "recording:test"

    def generate(self, prompt: str) -> str:
        return "SELECT 1"

    def warm_prefix(self, prefix: str, *, timeout: float | None = None) -> dict:
        self.calls.append(prefix)
        self.entered.set()
        if self._block is not None:
            self._block.wait(10)
        if self.fail is not None:
            raise self.fail
        return {"prompt_tokens": 4321, "cached_tokens": None}


def _router(backend: LLMBackend) -> LLMRouter:
    return LLMRouter(default_chain=[backend])


def _keepwarm(backend: LLMBackend, clock: FakeClock, **kwargs) -> warmup.KeepWarm:
    kwargs.setdefault("interval_seconds", INTERVAL)
    interval = kwargs.pop("interval_seconds")
    return warmup.KeepWarm(
        SYSTEM_PROMPT, lambda: _router(backend), interval,
        activity=kwargs.pop("activity", PrefixActivity(clock=clock)),
        now=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
        **kwargs,
    )


def _prefix(source: str | None = None) -> str:
    from prompt_engine.static_prefix import build_static_prefix

    return build_static_prefix(SYSTEM_PROMPT, source)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_VALID_URL = dict(
    db_connection_url="mssql+pyodbc://prod-db-host:1433/RealDB?driver=ODBC+Driver+17+for+SQL+Server",
)


class TestSettings:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("LLM_PREFIX_KEEPWARM_SECONDS", raising=False)
        assert Settings().llm_prefix_keepwarm_seconds == 0

    def test_blank_env_is_off(self, monkeypatch):
        monkeypatch.setenv("LLM_PREFIX_KEEPWARM_SECONDS", "  ")
        assert Settings().llm_prefix_keepwarm_seconds == 0

    def test_env_sets_it(self, monkeypatch):
        monkeypatch.setenv("LLM_PREFIX_KEEPWARM_SECONDS", "240")
        assert Settings().llm_prefix_keepwarm_seconds == 240

    @pytest.mark.parametrize("ok", [0, 30, 30.5, 300, 3600])
    def test_validate_accepts_zero_or_at_least_thirty(self, ok):
        with override_settings(llm_prefix_keepwarm_seconds=ok, **_VALID_URL):
            cfg.settings.validate()

    @pytest.mark.parametrize("bad", [1, 29.9, -30, float("nan"), float("inf")])
    def test_validate_refuses_anything_else(self, bad):
        with override_settings(llm_prefix_keepwarm_seconds=bad, **_VALID_URL):
            with pytest.raises(ValueError, match="LLM_PREFIX_KEEPWARM_SECONDS"):
                cfg.settings.validate()


# ---------------------------------------------------------------------------
# The skip rule: a prefix a real request used within the interval is warm
# ---------------------------------------------------------------------------

class TestSkipWhenRecentlyUsed:
    def test_sends_the_warmup_request_when_nothing_used_the_prefix(self):
        clock = FakeClock()
        backend = RecordingBackend()
        results = _keepwarm(backend, clock).run_once()
        assert [r.status for r in results] == ["warmed"]
        assert backend.calls == [_prefix()]

    def test_skips_a_prefix_a_real_request_used_within_the_interval(self):
        clock = FakeClock()
        activity = PrefixActivity(clock=clock)
        backend = RecordingBackend()
        keepwarm = _keepwarm(backend, clock, activity=activity)

        activity.note_use(_prefix())
        clock.advance(INTERVAL - 1)
        (result,) = keepwarm.run_once()

        assert result.status == "skipped"
        assert "warm" in result.detail
        assert backend.calls == []

    def test_sends_once_the_last_use_is_a_full_interval_old(self):
        clock = FakeClock()
        activity = PrefixActivity(clock=clock)
        backend = RecordingBackend()
        keepwarm = _keepwarm(backend, clock, activity=activity)

        activity.note_use(_prefix())
        clock.advance(INTERVAL)
        (result,) = keepwarm.run_once()

        assert result.status == "warmed"
        assert backend.calls == [_prefix()]

    def test_a_keepwarm_request_counts_as_a_use_so_the_next_pass_skips(self):
        clock = FakeClock()
        backend = RecordingBackend()
        keepwarm = _keepwarm(backend, clock)

        assert keepwarm.run_once()[0].status == "warmed"
        clock.advance(INTERVAL / 2)
        assert keepwarm.run_once()[0].status == "skipped"
        assert len(backend.calls) == 1

    def test_a_failed_request_is_not_a_use(self):
        clock = FakeClock()
        backend = RecordingBackend(fail=TimeoutError())
        keepwarm = _keepwarm(backend, clock)
        assert keepwarm.run_once()[0].status == "failed"
        assert keepwarm.run_once()[0].status == "failed"
        assert len(backend.calls) == 2

    def test_only_the_source_without_recent_traffic_is_sent(self):
        from tests._source_fixtures import SOURCES, configured_sources

        clock = FakeClock()
        activity = PrefixActivity(clock=clock)
        backend = RecordingBackend()
        with configured_sources():
            keepwarm = _keepwarm(backend, clock, activity=activity)
            busy, quiet = SOURCES[0], SOURCES[1]
            activity.note_use(_prefix(busy))
            clock.advance(10)
            results = keepwarm.run_once()
            by_source = {r.source: r.status for r in results}
            assert by_source[busy] == "skipped"
            assert by_source[quiet] == "warmed"
            assert backend.calls and _prefix(busy) not in backend.calls
            assert _prefix(quiet) in backend.calls

    def test_the_router_records_the_use_the_keepwarm_reads(self, monkeypatch):
        """End to end through the real router: a served question keeps its prefix warm."""
        clock = FakeClock()
        activity = PrefixActivity(clock=clock)
        monkeypatch.setattr(prefix_activity, "activity", activity)
        monkeypatch.setattr("llm.router.prefix_activity", activity)
        backend = RecordingBackend()
        router = _router(backend)
        keepwarm = warmup.KeepWarm(
            SYSTEM_PROMPT, lambda: router, INTERVAL, activity=activity,
        )

        router.generate_for_task(
            TaskType.SQL_GENERATION, PromptSegments(static_prefix=_prefix(), question="q"),
        )
        clock.advance(INTERVAL - 1)
        assert keepwarm.run_once()[0].status == "skipped"
        assert backend.calls == []

        clock.advance(2)
        assert keepwarm.run_once()[0].status == "warmed"


class TestRouterRecordsUse:
    @pytest.fixture()
    def activity(self, monkeypatch):
        recorded = PrefixActivity(clock=FakeClock())
        monkeypatch.setattr("llm.router.prefix_activity", recorded)
        return recorded

    def test_a_served_prefix_is_recorded(self, activity):
        _router(RecordingBackend()).generate_for_task(
            TaskType.SQL_GENERATION, PromptSegments(static_prefix="P" * 50, question="q"),
        )
        assert activity.seconds_since_use("P" * 50) == 0

    def test_a_decline_is_a_use_too(self, activity):
        class Declines(RecordingBackend):
            def generate(self, prompt: str) -> str:
                raise ValueError("OUT_OF_SCOPE")

        with pytest.raises(ValueError):
            _router(Declines()).generate_for_task(
                TaskType.SQL_GENERATION, PromptSegments(static_prefix="P" * 50, question="q"),
            )
        assert activity.seconds_since_use("P" * 50) == 0

    def test_a_fallback_backend_answering_is_not_a_use_of_the_first(self, activity):
        class Down(RecordingBackend):
            def generate(self, prompt: str) -> str:
                raise RuntimeError("down")

        router = LLMRouter(default_chain=[Down(), RecordingBackend()])
        router.generate_for_task(
            TaskType.SQL_GENERATION, PromptSegments(static_prefix="P" * 50, question="q"),
        )
        assert activity.seconds_since_use("P" * 50) is None

    def test_a_failed_request_is_not_a_use(self, activity):
        class Down(RecordingBackend):
            def generate(self, prompt: str) -> str:
                raise RuntimeError("down")

        with pytest.raises(RuntimeError):
            _router(Down()).generate_for_task(
                TaskType.SQL_GENERATION, PromptSegments(static_prefix="P" * 50, question="q"),
            )
        assert activity.seconds_since_use("P" * 50) is None

    def test_a_prompt_without_a_prefix_records_nothing(self, activity):
        _router(RecordingBackend()).generate_for_task(
            TaskType.SQL_GENERATION, PromptSegments(question="q"),
        )
        assert len(activity) == 0


class TestPrefixActivity:
    def test_unknown_prefix_has_no_age(self):
        assert PrefixActivity(clock=FakeClock()).seconds_since_use("p") is None

    def test_age_grows_with_the_clock_and_resets_on_use(self):
        clock = FakeClock()
        activity = PrefixActivity(clock=clock)
        activity.note_use("p")
        clock.advance(25)
        assert activity.seconds_since_use("p") == 25
        activity.note_use("p")
        assert activity.seconds_since_use("p") == 0

    def test_distinct_prefixes_are_independent(self):
        clock = FakeClock()
        activity = PrefixActivity(clock=clock)
        activity.note_use("a")
        clock.advance(5)
        assert activity.seconds_since_use("a") == 5
        assert activity.seconds_since_use("b") is None

    def test_the_record_is_bounded_and_drops_the_least_recent(self):
        activity = PrefixActivity(clock=FakeClock(), max_entries=2)
        for prefix in ("a", "b", "c"):
            activity.note_use(prefix)
        assert len(activity) == 2
        assert activity.seconds_since_use("a") is None
        assert activity.seconds_since_use("c") is not None

    def test_it_keeps_a_hash_not_the_text(self):
        activity = PrefixActivity(clock=FakeClock())
        activity.note_use("PRIVATE-TEXT")
        assert "PRIVATE-TEXT" not in repr(activity._last)


# ---------------------------------------------------------------------------
# The interval, and stopping
# ---------------------------------------------------------------------------

class FakeWait:
    """``wait(seconds)``: advances the fake clock, and reports a stop after *passes* waits."""

    def __init__(self, clock: FakeClock, passes: int) -> None:
        self.clock = clock
        self.passes = passes
        self.requested: list[float] = []

    def __call__(self, seconds: float) -> bool:
        self.requested.append(seconds)
        if len(self.requested) > self.passes:
            return True
        self.clock.advance(seconds)
        return False


class TestInterval:
    def test_one_pass_per_interval_and_no_pass_before_the_first_wait(self):
        clock = FakeClock()
        backend = RecordingBackend()
        wait = FakeWait(clock, passes=3)
        keepwarm = _keepwarm(backend, clock, wait=wait)

        keepwarm.start().join(5)

        assert wait.requested == [INTERVAL] * 4
        assert keepwarm.status()["runs"] == 3
        # Idle server: nothing used the prefix between passes, so each pass sends.
        assert len(backend.calls) == 3

    def test_traffic_between_passes_means_no_requests(self):
        clock = FakeClock()
        activity = PrefixActivity(clock=clock)
        backend = RecordingBackend()

        class Traffic(FakeWait):
            def __call__(self, seconds: float) -> bool:
                stop = super().__call__(seconds)
                activity.note_use(_prefix())  # a question lands just before each pass
                return stop

        keepwarm = _keepwarm(backend, clock, activity=activity, wait=Traffic(clock, passes=4))
        keepwarm.start().join(5)

        assert keepwarm.status()["runs"] == 4
        assert backend.calls == []
        assert keepwarm.status()["last_run_outcome"] == "skipped"

    def test_the_wait_is_the_configured_interval(self):
        clock = FakeClock()
        wait = FakeWait(clock, passes=1)
        keepwarm = _keepwarm(RecordingBackend(), clock, wait=wait, interval_seconds=240)
        keepwarm.start().join(5)
        assert wait.requested[0] == 240


class TestShutdown:
    def test_stop_ends_a_thread_waiting_for_its_next_pass(self):
        backend = RecordingBackend()
        keepwarm = warmup.KeepWarm(SYSTEM_PROMPT, lambda: _router(backend), 3600)
        thread = keepwarm.start()
        assert keepwarm.running

        assert keepwarm.stop(5) is True

        assert not thread.is_alive()
        assert not keepwarm.running
        assert backend.calls == []

    def test_a_stopped_keepwarm_sends_nothing_more(self):
        backend = RecordingBackend()
        keepwarm = _keepwarm(backend, FakeClock())
        keepwarm.stop(1)
        assert keepwarm.run_once() is None
        assert backend.calls == []

    def test_stop_between_sources_skips_the_rest(self):
        from tests._source_fixtures import SOURCES, configured_sources

        backend = RecordingBackend()
        stopped = threading.Event()
        original = backend.warm_prefix

        def stopping(prefix, *, timeout=None):
            stopped.set()  # shutdown arrives while the first request is in flight
            return original(prefix, timeout=timeout)

        backend.warm_prefix = stopping
        with configured_sources():
            results = warmup.warm_prefix_cache(
                SYSTEM_PROMPT, lambda: _router(backend), cancel=stopped.is_set,
            )
        assert results[0].status == "warmed"
        assert [r.detail for r in results[1:]] == ["shutting down"] * (len(SOURCES) - 1)
        assert len(backend.calls) == 1

    def test_the_thread_is_a_daemon(self):
        keepwarm = warmup.KeepWarm(SYSTEM_PROMPT, lambda: _router(RecordingBackend()), 3600)
        try:
            assert keepwarm.start().daemon
        finally:
            keepwarm.stop(5)

    def test_start_twice_is_one_thread(self):
        keepwarm = warmup.KeepWarm(SYSTEM_PROMPT, lambda: _router(RecordingBackend()), 3600)
        try:
            assert keepwarm.start() is keepwarm.start()
        finally:
            keepwarm.stop(5)


class TestProcessWideControl:
    @pytest.fixture(autouse=True)
    def _clean(self):
        warmup.stop_keepwarm()
        yield
        warmup.stop_keepwarm()

    def test_setting_zero_starts_nothing(self):
        with override_settings(llm_prefix_keepwarm_seconds=0):
            assert warmup.start_keepwarm(SYSTEM_PROMPT, lambda: _router(RecordingBackend())) is None
            status = warmup.keepwarm_status()
        assert status["enabled"] is False
        assert "LLM_PREFIX_KEEPWARM_SECONDS" in status["reason"]

    def test_a_positive_setting_starts_one_that_stop_ends(self):
        with override_settings(llm_prefix_keepwarm_seconds=3600, llm_provider="openai"):
            keepwarm = warmup.start_keepwarm(SYSTEM_PROMPT, lambda: _router(RecordingBackend()))
            assert keepwarm is not None and keepwarm.running
            assert warmup.keepwarm_status()["enabled"] is True
            assert warmup.keepwarm_status()["interval_seconds"] == 3600
            # A second start while one runs is not a second thread.
            assert warmup.start_keepwarm(SYSTEM_PROMPT, lambda: _router(RecordingBackend())) is None
            warmup.stop_keepwarm()
            assert not keepwarm.running
            assert warmup.keepwarm_status()["enabled"] is False

    def test_a_start_that_raises_is_swallowed(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError(SECRET)

        monkeypatch.setattr(warmup, "KeepWarm", boom)
        with override_settings(llm_prefix_keepwarm_seconds=60, llm_provider="openai"):
            assert warmup.start_keepwarm(SYSTEM_PROMPT, lambda: None) is None

    def test_stop_without_a_keepwarm_is_a_no_op(self):
        warmup.stop_keepwarm()


# ---------------------------------------------------------------------------
# The rules it shares with the start-up warm-up
# ---------------------------------------------------------------------------

class TestWarmupRulesApply:
    def test_an_untrusted_endpoint_is_never_sent_the_schema(self):
        backend = RecordingBackend(trusted=False)
        with override_settings(llm_allow_remote=False):
            results = _keepwarm(backend, FakeClock()).run_once()
        assert results[0].status == "skipped"
        assert "LLM_ALLOW_REMOTE" in results[0].detail
        assert backend.calls == []

    def test_an_untrusted_endpoint_is_warmed_when_the_operator_allowed_it(self):
        backend = RecordingBackend(trusted=False)
        with override_settings(llm_allow_remote=True):
            results = _keepwarm(backend, FakeClock()).run_once()
        assert results[0].status == "warmed"

    def test_a_mock_backend_is_never_sent_anything(self):
        results = _keepwarm(MockBackend(), FakeClock()).run_once()
        assert results[0].status == "skipped"
        assert "no prefix cache" in results[0].detail

    def test_provider_mock_starts_no_keepwarm_at_all(self):
        warmup.stop_keepwarm()
        with override_settings(llm_prefix_keepwarm_seconds=60, llm_provider="mock"):
            assert warmup.start_keepwarm(SYSTEM_PROMPT, lambda: _router(MockBackend())) is None
            status = warmup.keepwarm_status()
        assert status["enabled"] is False
        assert "mock" in status["reason"]

    def test_a_source_on_the_retrieval_path_is_not_warmed(self):
        backend = RecordingBackend()
        with override_settings(prompt_retrieval_token_budget=1):
            results = _keepwarm(backend, FakeClock()).run_once()
        assert results[0].status == "skipped"
        assert "retrieval" in results[0].detail
        assert backend.calls == []

    def test_the_request_is_the_one_the_warmup_sends(self):
        keepwarm_backend, startup_backend = RecordingBackend(), RecordingBackend()
        _keepwarm(keepwarm_backend, FakeClock()).run_once()
        warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(startup_backend))
        assert keepwarm_backend.calls == startup_backend.calls

    def test_a_warmup_already_running_makes_the_pass_busy_and_sends_nothing(self):
        release = threading.Event()
        slow = RecordingBackend(block=release)
        other = RecordingBackend()
        thread = warmup.start_background_warmup(SYSTEM_PROMPT, lambda: _router(slow))
        try:
            assert slow.entered.wait(5)
            keepwarm = _keepwarm(other, FakeClock())
            assert keepwarm.run_once() is None
            assert keepwarm.status()["last_run_outcome"] == "busy"
            assert other.calls == []
        finally:
            release.set()
            thread.join(5)


class TestMaintenanceMode:
    def test_nothing_is_sent_while_paused(self):
        backend = RecordingBackend()
        paused = {"on": True}
        keepwarm = _keepwarm(backend, FakeClock(), paused=lambda: paused["on"])

        assert keepwarm.run_once() is None
        assert backend.calls == []
        status = keepwarm.status()
        assert status["paused"] is True and status["last_run_outcome"] == "paused"

        paused["on"] = False
        assert keepwarm.run_once()[0].status == "warmed"
        assert keepwarm.status()["paused"] is False

    def test_a_broken_pause_check_does_not_stop_the_keepwarm(self):
        def broken() -> bool:
            raise RuntimeError("no maintenance state")

        backend = RecordingBackend()
        assert _keepwarm(backend, FakeClock(), paused=broken).run_once()[0].status == "warmed"

    def test_the_servers_pause_follows_the_maintenance_switch(self):
        import api.maintenance as maintenance

        maintenance.reset_for_testing()
        try:
            backend = RecordingBackend()
            keepwarm = _keepwarm(
                backend, FakeClock(), paused=lambda: bool(maintenance.get_state()["active"]),
            )
            maintenance.enable(note=None, actor_principal_id="ops")
            assert keepwarm.run_once() is None
            maintenance.disable(actor_principal_id="ops")
            assert keepwarm.run_once()[0].status == "warmed"
        finally:
            maintenance.reset_for_testing()


# ---------------------------------------------------------------------------
# Failures: one line per streak
# ---------------------------------------------------------------------------

def _warmup_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "llm.warmup" and "keep-warm" not in r.getMessage()]


class TestFailureStreaks:
    def test_a_streak_of_failures_is_one_warning(self, caplog):
        backend = RecordingBackend(fail=requests_error())
        keepwarm = _keepwarm(backend, FakeClock())
        with caplog.at_level(logging.DEBUG, logger="llm.warmup"):
            for _ in range(5):
                assert keepwarm.run_once()[0].status == "failed"

        warnings = [r for r in _warmup_records(caplog) if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "failed after" in warnings[0].getMessage()
        # The other four are still there, one level down, for whoever turns DEBUG on.
        debug = [r for r in _warmup_records(caplog) if r.levelno == logging.DEBUG]
        assert len(debug) == 4
        assert len(backend.calls) == 5, "a failed pass is retried on the next interval"

    def test_recovery_is_logged_and_a_new_failure_warns_again(self, caplog):
        clock = FakeClock()
        backend = RecordingBackend(fail=TimeoutError())
        keepwarm = _keepwarm(backend, clock)
        with caplog.at_level(logging.DEBUG, logger="llm.warmup"):
            keepwarm.run_once()
            keepwarm.run_once()
            keepwarm.run_once()
            backend.fail = None
            assert keepwarm.run_once()[0].status == "warmed"
            clock.advance(INTERVAL)
            backend.fail = TimeoutError()
            assert keepwarm.run_once()[0].status == "failed"

        records = _warmup_records(caplog)
        levels = [(r.levelno, r.getMessage()) for r in records if r.levelno >= logging.INFO]
        assert [lvl for lvl, _ in levels] == [logging.WARNING, logging.INFO, logging.WARNING]
        assert "recovered after 3 failed pass(es)" in levels[1][1]

    def test_a_failure_is_never_fatal_and_is_reported_in_the_status(self):
        keepwarm = _keepwarm(RecordingBackend(fail=RuntimeError(SECRET)), FakeClock())
        results = keepwarm.run_once()
        assert results[0].detail == "RuntimeError"
        status = keepwarm.status()
        assert status["last_run_outcome"] == "failed"
        assert SECRET not in json.dumps(status)

    def test_log_lines_hold_no_prompt_text_response_text_or_key(self, caplog):
        marker = "PRIVATE-SCHEMA-MARKER"
        backend = RecordingBackend(fail=ConnectionError(f"{SECRET} {marker}"))
        keepwarm = warmup.KeepWarm(
            marker + SYSTEM_PROMPT, lambda: _router(backend), INTERVAL,
            activity=PrefixActivity(clock=FakeClock()),
        )
        with caplog.at_level(logging.DEBUG):
            keepwarm.run_once()
            keepwarm.run_once()
            backend.fail = None
            keepwarm.run_once()
        assert SECRET not in caplog.text
        assert marker not in caplog.text
        assert "BUSINESS RULES" not in caplog.text

    def test_a_skip_is_logged_once_per_streak_too(self, caplog):
        keepwarm = _keepwarm(MockBackend(), FakeClock())
        with caplog.at_level(logging.DEBUG, logger="llm.warmup"):
            for _ in range(4):
                keepwarm.run_once()
        infos = [r for r in _warmup_records(caplog) if r.levelno == logging.INFO]
        assert len(infos) == 1

    def test_a_pass_that_crashes_is_a_log_line_not_an_exception(self, monkeypatch, caplog):
        def boom(*args, **kwargs):
            raise RuntimeError(SECRET)

        monkeypatch.setattr(warmup, "warm_prefix_cache", boom)
        keepwarm = _keepwarm(RecordingBackend(), FakeClock())
        with caplog.at_level(logging.INFO, logger="llm.warmup"):
            assert keepwarm.run_once() is None
            assert keepwarm.run_once() is None
        assert keepwarm.status()["last_run_outcome"] == "failed"
        assert SECRET not in caplog.text
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_the_thread_survives_a_failing_pass(self):
        clock = FakeClock()
        backend = RecordingBackend(fail=TimeoutError())
        keepwarm = _keepwarm(backend, clock, wait=FakeWait(clock, passes=3))
        keepwarm.start().join(5)
        assert keepwarm.status()["runs"] == 3
        assert len(backend.calls) == 3


def requests_error() -> BaseException:
    import requests

    response = requests.Response()
    response.status_code = 503
    return requests.HTTPError("503", response=response)


# ---------------------------------------------------------------------------
# Status: the last run and its outcome
# ---------------------------------------------------------------------------

class TestStatus:
    def test_before_the_first_pass(self):
        status = _keepwarm(RecordingBackend(), FakeClock()).status()
        assert status["enabled"] is True
        assert status["interval_seconds"] == INTERVAL
        assert status["runs"] == 0
        assert status["last_run_at"] is None and status["last_run_outcome"] is None
        assert status["last_run"] == []

    def test_after_a_pass(self):
        keepwarm = _keepwarm(RecordingBackend(), FakeClock())
        keepwarm.run_once()
        status = keepwarm.status()
        assert status["runs"] == 1
        assert status["last_run_at"] == "2030-01-01T00:00:00+00:00"
        assert status["last_run_outcome"] == "warmed"
        (entry,) = status["last_run"]
        assert entry["status"] == "warmed" and entry["prompt_tokens"] == 4321
        assert "BUSINESS RULES" not in json.dumps(status)

    def test_the_outcome_names_the_worst_thing_that_happened(self):
        from tests._source_fixtures import SOURCES, configured_sources

        class OneFails(RecordingBackend):
            def warm_prefix(self, prefix, *, timeout=None):
                self.calls.append(prefix)
                if len(self.calls) == 2:
                    raise TimeoutError
                return {"prompt_tokens": 1}

        with configured_sources():
            keepwarm = _keepwarm(OneFails(), FakeClock())
            results = keepwarm.run_once()
        assert [r.status for r in results][:2] == ["warmed", "failed"]
        assert len(results) == len(SOURCES)
        assert keepwarm.status()["last_run_outcome"] == "failed"


# ---------------------------------------------------------------------------
# The server: start-up wiring, shutdown, and the admin routes
# ---------------------------------------------------------------------------

_LIFESPAN_SETTINGS = dict(
    db_connection_url=(
        "mssql+pyodbc://prod-db-host:1433/RealDB?driver=ODBC+Driver+17+for+SQL+Server"
    ),
    auth_required=False,
)


@pytest.fixture()
def fake_agent():
    def install(backend: LLMBackend) -> None:
        runner._reset_agent_for_testing(SQLAgent(router=_router(backend)))

    yield install
    runner._reset_agent_for_testing(None)


def _thread_alive() -> bool:
    return any(t.name == "llm-prefix-keepwarm" and t.is_alive() for t in threading.enumerate())


class TestLifespan:
    @pytest.fixture(autouse=True)
    def _clean(self):
        warmup.stop_keepwarm()
        yield
        warmup.stop_keepwarm()

    def _run(self, **overrides):
        import api.server as server_module
        from unittest.mock import patch

        return (
            override_settings(**{**_LIFESPAN_SETTINGS, "llm_prefix_warmup_on_startup": False, **overrides}),
            patch("retrieval.dimension_vocabulary.warm_all", return_value={}),
            server_module.app,
        )

    def test_it_is_not_started_by_default(self, fake_agent):
        fake_agent(RecordingBackend())
        settings_cm, vocab_cm, app = self._run(llm_prefix_keepwarm_seconds=0)
        with settings_cm, vocab_cm:
            with TestClient(app):
                assert not _thread_alive()

    def test_it_runs_while_the_server_runs_and_stops_with_it(self, fake_agent):
        fake_agent(RecordingBackend())
        settings_cm, vocab_cm, app = self._run(llm_prefix_keepwarm_seconds=3600, llm_provider="openai")
        with settings_cm, vocab_cm:
            with TestClient(app) as client:
                assert _thread_alive()
                assert client.get("/health").status_code == 200
            # The lifespan's shutdown stopped it.
            assert not _thread_alive()
            assert warmup.keepwarm_status()["enabled"] is False

    def test_a_keepwarm_that_cannot_start_does_not_stop_the_server(self, fake_agent, monkeypatch):
        fake_agent(RecordingBackend())

        def boom(*args, **kwargs):
            raise RuntimeError(SECRET)

        monkeypatch.setattr(warmup, "start_keepwarm", boom)
        settings_cm, vocab_cm, app = self._run(llm_prefix_keepwarm_seconds=60, llm_provider="openai")
        with settings_cm, vocab_cm:
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_OPS_KEY = "a" * 40
_SECURITY_KEY = "b" * 40
_ANALYST_KEY = "c" * 40
_KEYS_JSON = json.dumps([
    {"id": "ops-admin", "name": "Ops", "key_sha256": _sha256(_OPS_KEY), "operations": True},
    {"id": "sec-admin", "name": "Sec", "key_sha256": _sha256(_SECURITY_KEY), "security": True},
    {"id": "analyst-1", "name": "Analyst", "key_sha256": _sha256(_ANALYST_KEY)},
])


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture()
def admin_client(tmp_path, fake_agent):
    import api.admin_ops_routes as ops_routes
    import api.server as server_module
    import api.v2_routes as v2_routes

    warmup.stop_keepwarm()
    with override_settings(
        app_db_url=f"sqlite:///{tmp_path / 'appdb.db'}", api_keys_json=_KEYS_JSON,
        auth_required=True,
    ):
        dispose_app_engine()
        invalidate_cache()
        server_module._system_prompt = SYSTEM_PROMPT
        ops_routes._system_prompt = SYSTEM_PROMPT
        v2_routes._reset_for_testing()
        yield TestClient(server_module.app, raise_server_exceptions=False)
        ops_routes._system_prompt = ""
        v2_routes._reset_for_testing()
    warmup.stop_keepwarm()
    dispose_app_engine()
    invalidate_cache()


class TestAdminRoutes:
    def test_status_with_the_keepwarm_off(self, admin_client):
        resp = admin_client.get("/admin/llm/warmup", headers=_auth(_OPS_KEY))
        assert resp.status_code == 200
        keepwarm = resp.json()["keepwarm"]
        assert keepwarm["enabled"] is False
        assert "LLM_PREFIX_KEEPWARM_SECONDS" in keepwarm["reason"]
        assert keepwarm["last_run"] == []

    def test_status_shows_the_last_run_and_its_outcome(self, admin_client):
        keepwarm = warmup.KeepWarm(
            SYSTEM_PROMPT, lambda: _router(RecordingBackend()), 120,
            activity=PrefixActivity(clock=FakeClock()),
        )
        keepwarm.run_once()
        warmup._keepwarm = keepwarm
        resp = admin_client.get("/admin/llm/warmup", headers=_auth(_OPS_KEY))
        body = resp.json()["keepwarm"]
        assert body["enabled"] is True
        assert body["interval_seconds"] == 120
        assert body["runs"] == 1
        assert body["last_run_outcome"] == "warmed"
        assert body["last_run_at"]
        assert body["last_run"][0]["status"] == "warmed"
        assert "DATABASE SCHEMA" not in resp.text

    @pytest.mark.parametrize("key", [_OPS_KEY, _SECURITY_KEY])
    def test_both_admin_roles_can_read_it(self, admin_client, key):
        assert admin_client.get("/admin/llm/warmup", headers=_auth(key)).status_code == 200

    def test_an_analyst_cannot(self, admin_client):
        assert admin_client.get("/admin/llm/warmup", headers=_auth(_ANALYST_KEY)).status_code == 403

    def test_it_needs_a_key(self, admin_client):
        assert admin_client.get("/admin/llm/warmup").status_code in (401, 403)

    def test_the_manual_warmup_reports_the_keepwarm_beside_its_own_results(self, admin_client, fake_agent):
        fake_agent(RecordingBackend())
        resp = admin_client.post("/admin/llm/warmup", headers=_auth(_OPS_KEY))
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"][0]["status"] == "warmed"
        assert body["keepwarm"]["enabled"] is False

    def test_the_manual_warmup_counts_as_a_use_for_the_keepwarm(self, admin_client, fake_agent):
        backend = RecordingBackend()
        fake_agent(backend)
        prefix_activity.activity.clear()
        admin_client.post("/admin/llm/warmup", headers=_auth(_OPS_KEY))
        assert prefix_activity.activity.seconds_since_use(_prefix()) is not None
        prefix_activity.activity.clear()
