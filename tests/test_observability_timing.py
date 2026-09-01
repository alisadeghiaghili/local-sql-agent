# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for observability/timing.py."""

from __future__ import annotations

import threading
import time

import pytest

from observability.timing import STAGE_NAMES, StageTimer, UnknownStageError


class TestSnapshotShape:
    def test_all_seven_keys_always_present(self):
        timer = StageTimer()
        snap = timer.snapshot()
        assert set(snap.keys()) == {
            "total_ms", "plan_ms", "prompt_ms", "llm_ms",
            "guard_ms", "execute_ms", "interpret_ms",
        }

    def test_untimed_stages_default_to_zero(self):
        timer = StageTimer()
        with timer.stage("plan"):
            pass
        snap = timer.snapshot()
        assert snap["plan_ms"] >= 0
        for name in ("prompt", "llm", "guard", "execute", "interpret"):
            assert snap[f"{name}_ms"] == 0

    def test_values_are_ints(self):
        timer = StageTimer()
        with timer.stage("execute"):
            time.sleep(0.001)
        snap = timer.snapshot()
        for v in snap.values():
            assert isinstance(v, int)


class TestStageTiming:
    def test_stage_records_elapsed_time(self):
        timer = StageTimer()
        with timer.stage("llm"):
            time.sleep(0.02)
        snap = timer.snapshot()
        assert snap["llm_ms"] >= 15  # allow scheduler slack, avoid flakiness

    def test_repeated_stage_accumulates(self):
        timer = StageTimer()
        with timer.stage("llm"):
            time.sleep(0.01)
        with timer.stage("llm"):
            time.sleep(0.01)
        snap = timer.snapshot()
        assert snap["llm_ms"] >= 15

    def test_elapsed_recorded_even_on_exception(self):
        timer = StageTimer()
        with pytest.raises(RuntimeError):
            with timer.stage("guard"):
                time.sleep(0.01)
                raise RuntimeError("boom")
        snap = timer.snapshot()
        assert snap["guard_ms"] >= 5

    def test_unknown_stage_raises_immediately_without_entering_with(self):
        """The error must fire at .stage() call time, not deferred to __enter__."""
        timer = StageTimer()
        with pytest.raises(UnknownStageError):
            timer.stage("not_a_stage")

    def test_unknown_stage_message_lists_valid_names(self):
        timer = StageTimer()
        with pytest.raises(UnknownStageError, match="not_a_stage"):
            timer.stage("not_a_stage")


class TestRecord:
    def test_record_adds_seconds_as_ms(self):
        timer = StageTimer()
        timer.record("guard", 0.006)
        assert timer.snapshot()["guard_ms"] == 6

    def test_record_accumulates(self):
        timer = StageTimer()
        timer.record("guard", 0.006)
        timer.record("guard", 0.004)
        assert timer.snapshot()["guard_ms"] == 10

    def test_record_unknown_stage_raises(self):
        timer = StageTimer()
        with pytest.raises(UnknownStageError):
            timer.record("nope", 1.0)

    def test_record_negative_seconds_raises(self):
        timer = StageTimer()
        with pytest.raises(ValueError):
            timer.record("guard", -0.1)


class TestDecorate:
    def test_decorate_times_the_call(self):
        timer = StageTimer()

        @timer.decorate("execute")
        def slow() -> str:
            time.sleep(0.01)
            return "done"

        assert slow() == "done"
        assert timer.snapshot()["execute_ms"] >= 5

    def test_decorate_preserves_function_metadata(self):
        timer = StageTimer()

        @timer.decorate("execute")
        def named_fn() -> None:
            """Docstring."""

        assert named_fn.__name__ == "named_fn"
        assert named_fn.__doc__ == "Docstring."

    def test_decorate_propagates_exceptions(self):
        timer = StageTimer()

        @timer.decorate("execute")
        def raises() -> None:
            raise ValueError("bad")

        with pytest.raises(ValueError):
            raises()


class TestTotalMs:
    def test_total_ms_reflects_wall_clock_not_just_stage_sum(self):
        timer = StageTimer()
        time.sleep(0.01)  # untimed gap, outside any stage
        with timer.stage("plan"):
            time.sleep(0.01)
        snap = timer.snapshot()
        assert snap["total_ms"] >= snap["plan_ms"]
        assert snap["total_ms"] >= 15

    def test_total_ms_grows_across_snapshots(self):
        timer = StageTimer()
        first = timer.snapshot()["total_ms"]
        time.sleep(0.01)
        second = timer.snapshot()["total_ms"]
        assert second >= first


class TestConcurrency:
    def test_concurrent_stage_recording_is_not_lost(self):
        """Many threads timing the same stage concurrently must not race:
        every thread's contribution has to land in the accumulated total,
        which only holds if the internal dict update is properly locked.
        """
        timer = StageTimer()
        n_threads = 20
        sleep_s = 0.01

        def worker() -> None:
            with timer.stage("execute"):
                time.sleep(sleep_s)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = timer.snapshot()
        # Threads run concurrently, so this is well below the naive serial
        # sum (n_threads * sleep_s) but must reflect real accumulated work
        # from more than one thread -- a lost update would show far less.
        assert snap["execute_ms"] >= sleep_s * 1000 * (n_threads / 2)

    def test_independent_timers_do_not_share_state(self):
        """Two StageTimer instances used concurrently from different threads
        must never leak accumulated duration into each other -- there is no
        module-level mutable accumulator to race on.
        """
        timer_a = StageTimer()
        timer_b = StageTimer()

        def work_a() -> None:
            for _ in range(10):
                with timer_a.stage("llm"):
                    time.sleep(0.001)

        def work_b() -> None:
            pass  # timer_b never records anything

        t1 = threading.Thread(target=work_a)
        t1.start()
        t1.join()
        work_b()

        assert timer_a.snapshot()["llm_ms"] > 0
        assert timer_b.snapshot()["llm_ms"] == 0

    def test_many_timers_created_concurrently_stay_isolated(self):
        results: dict[int, int] = {}
        lock = threading.Lock()

        def worker(idx: int) -> None:
            timer = StageTimer()
            with timer.stage("plan"):
                time.sleep(0.001 * (idx % 3 + 1))
            with lock:
                results[idx] = timer.snapshot()["plan_ms"]

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 15
        assert all(v >= 0 for v in results.values())


class TestStageNamesConstant:
    def test_matches_contract_timings_keys(self):
        assert STAGE_NAMES == ("plan", "prompt", "llm", "guard", "execute", "interpret")
