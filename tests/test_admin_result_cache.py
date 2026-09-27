# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``api.admin_result_cache.TTLCache`` -- the admin panel's expensive-card
cache (Required item 1, 2026 warehouse-load audit).

Uses a fake monotonic clock (mirroring
``tests/security_audit/test_health_probe_cache.py``'s own pattern) rather
than real ``time.sleep`` calls, so the TTL-expiry test is deterministic
and fast.
"""

from __future__ import annotations

from api.admin_result_cache import TTLCache


class TestSecondCallInsideTtlDoesNotRecompute:
    def test_a_burst_computes_once(self, monkeypatch):
        cache = TTLCache()
        clock = {"t": 1000.0}
        monkeypatch.setattr("api.admin_result_cache.time.monotonic", lambda: clock["t"])

        calls = {"n": 0}

        def compute():
            calls["n"] += 1
            return "result"

        for _ in range(5):
            result = cache.get_or_compute("k", 300, compute)
            assert result.value == "result"

        assert calls["n"] == 1, (
            f"compute() ran {calls['n']} times for 5 calls inside the TTL"
        )

    def test_cached_result_reports_its_age(self, monkeypatch):
        cache = TTLCache()
        clock = {"t": 1000.0}
        monkeypatch.setattr("api.admin_result_cache.time.monotonic", lambda: clock["t"])

        cache.get_or_compute("k", 300, lambda: "result")
        clock["t"] += 42
        second = cache.get_or_compute("k", 300, lambda: "result")

        assert second.cached is True
        assert second.age_seconds == 42


class TestRefreshForcesRecompute:
    def test_force_true_recomputes_even_inside_the_ttl(self, monkeypatch):
        cache = TTLCache()
        clock = {"t": 1000.0}
        monkeypatch.setattr("api.admin_result_cache.time.monotonic", lambda: clock["t"])

        calls = {"n": 0}

        def compute():
            calls["n"] += 1
            return calls["n"]

        first = cache.get_or_compute("k", 300, compute)
        second = cache.get_or_compute("k", 300, compute, force=True)

        assert first.value == 1
        assert second.value == 2
        assert second.cached is False
        assert calls["n"] == 2


class TestExpiry:
    def test_recomputes_after_the_ttl_elapses(self, monkeypatch):
        cache = TTLCache()
        clock = {"t": 1000.0}
        monkeypatch.setattr("api.admin_result_cache.time.monotonic", lambda: clock["t"])

        calls = {"n": 0}

        def compute():
            calls["n"] += 1
            return calls["n"]

        cache.get_or_compute("k", 300, compute)
        clock["t"] += 301
        result = cache.get_or_compute("k", 300, compute)

        assert calls["n"] == 2
        assert result.cached is False

    def test_zero_ttl_disables_caching(self, monkeypatch):
        cache = TTLCache()
        clock = {"t": 1000.0}
        monkeypatch.setattr("api.admin_result_cache.time.monotonic", lambda: clock["t"])

        calls = {"n": 0}

        def compute():
            calls["n"] += 1
            return calls["n"]

        cache.get_or_compute("k", 0, compute)
        cache.get_or_compute("k", 0, compute)

        assert calls["n"] == 2


class TestFailureIsNeverCached:
    def test_a_raising_compute_leaves_nothing_cached(self, monkeypatch):
        cache = TTLCache()
        clock = {"t": 1000.0}
        monkeypatch.setattr("api.admin_result_cache.time.monotonic", lambda: clock["t"])

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("warehouse unreachable")
            return "ok"

        try:
            cache.get_or_compute("k", 300, flaky)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the RuntimeError to propagate")

        result = cache.get_or_compute("k", 300, flaky)
        assert result.value == "ok"
        assert calls["n"] == 2, "a failed computation must not be cached"


class TestIndependentKeys:
    def test_two_keys_do_not_share_a_slot(self):
        cache = TTLCache()
        a = cache.get_or_compute("a", 300, lambda: "A")
        b = cache.get_or_compute("b", 300, lambda: "B")
        assert a.value == "A"
        assert b.value == "B"


class TestReset:
    def test_reset_one_key_leaves_the_other_cached(self):
        cache = TTLCache()
        cache.get_or_compute("a", 300, lambda: "A")
        cache.get_or_compute("b", 300, lambda: "B")
        cache.reset("a")

        calls = {"a": 0, "b": 0}
        a = cache.get_or_compute("a", 300, lambda: (calls.__setitem__("a", calls["a"] + 1), "A2")[1])
        b = cache.get_or_compute("b", 300, lambda: (calls.__setitem__("b", calls["b"] + 1), "B2")[1])

        assert a.value == "A2" and calls["a"] == 1
        assert b.value == "B" and calls["b"] == 0

    def test_reset_with_no_key_clears_everything(self):
        cache = TTLCache()
        cache.get_or_compute("a", 300, lambda: "A")
        cache.reset()
        result = cache.get_or_compute("a", 300, lambda: "A2")
        assert result.value == "A2"
        assert result.cached is False
