"""Unit tests for api/query_cache.py — no external I/O needed."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from api.models import QueryResponse
from api.query_cache import QueryCache


def _resp(question: str = "سوال", mode: str = "full") -> QueryResponse:
    return QueryResponse(
        question=question,
        sql="SELECT 1",
        result=[{"n": 1}],
        row_count=1,
        model="ollama:test",
    )


class TestQueryCacheBasic:
    def test_miss_returns_none(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        assert c.get("سوال", "full") is None

    def test_set_then_get_returns_response(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        r = _resp()
        c.set("سوال", "full", r)
        assert c.get("سوال", "full") is r

    def test_different_mode_is_separate_entry(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        r1, r2 = _resp(mode="full"), _resp(mode="result")
        c.set("سوال", "full", r1)
        c.set("سوال", "result", r2)
        assert c.get("سوال", "full") is r1
        assert c.get("سوال", "result") is r2

    def test_different_question_is_separate_entry(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        r1, r2 = _resp("سوال ۱"), _resp("سوال ۲")
        c.set("سوال ۱", "full", r1)
        c.set("سوال ۲", "full", r2)
        assert c.get("سوال ۱", "full") is r1
        assert c.get("سوال ۲", "full") is r2

    def test_question_stripped_before_keying(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        r = _resp()
        c.set("  سوال  ", "full", r)
        assert c.get("سوال", "full") is r


class TestQueryCacheTTL:
    def test_expired_entry_returns_none(self):
        c = QueryCache(ttl_seconds=1, max_size=10)
        c.set("سوال", "full", _resp())
        # Fake time past TTL
        with patch("api.query_cache.time.monotonic", return_value=time.monotonic() + 2):
            assert c.get("سوال", "full") is None

    def test_non_expired_entry_still_returned(self):
        c = QueryCache(ttl_seconds=300, max_size=10)
        r = _resp()
        c.set("سوال", "full", r)
        assert c.get("سوال", "full") is r

    def test_expired_entry_increments_evictions(self):
        c = QueryCache(ttl_seconds=1, max_size=10)
        c.set("سوال", "full", _resp())
        with patch("api.query_cache.time.monotonic", return_value=time.monotonic() + 2):
            c.get("سوال", "full")
        assert c.stats()["evictions"] == 1


class TestQueryCacheLRU:
    def test_lru_evicts_oldest_when_full(self):
        c = QueryCache(ttl_seconds=300, max_size=3)
        for i in range(3):
            c.set(f"سوال {i}", "full", _resp(f"سوال {i}"))
        # Adding a 4th evicts سوال 0 (oldest)
        c.set("سوال 3", "full", _resp("سوال 3"))
        assert c.get("سوال 0", "full") is None
        assert c.get("سوال 3", "full") is not None

    def test_accessed_entry_not_evicted_first(self):
        c = QueryCache(ttl_seconds=300, max_size=3)
        for i in range(3):
            c.set(f"سوال {i}", "full", _resp(f"سوال {i}"))
        # Access سوال 0 → moves to MRU position
        c.get("سوال 0", "full")
        # Add 4th → should evict سوال 1 (now oldest)
        c.set("سوال 3", "full", _resp("سوال 3"))
        assert c.get("سوال 0", "full") is not None
        assert c.get("سوال 1", "full") is None


class TestQueryCacheDisabled:
    def test_disabled_when_ttl_zero(self):
        c = QueryCache(ttl_seconds=0, max_size=10)
        assert not c.enabled

    def test_get_returns_none_when_disabled(self):
        c = QueryCache(ttl_seconds=0, max_size=10)
        c.set("سوال", "full", _resp())  # no-op
        assert c.get("سوال", "full") is None


class TestQueryCacheStats:
    def test_hits_and_misses_counted(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.get("سوال", "full")  # miss
        c.set("سوال", "full", _resp())
        c.get("سوال", "full")  # hit
        s = c.stats()
        assert s["hits"] == 1
        assert s["misses"] == 1

    def test_clear_empties_store(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.set("سوال", "full", _resp())
        c.clear()
        assert c.stats()["size"] == 0
        assert c.get("سوال", "full") is None

    def test_invalidate_removes_single_entry(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.set("سوال ۱", "full", _resp())
        c.set("سوال ۲", "full", _resp())
        removed = c.invalidate("سوال ۱", "full")
        assert removed is True
        assert c.get("سوال ۱", "full") is None
        assert c.get("سوال ۲", "full") is not None

    def test_invalidate_missing_returns_false(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        assert c.invalidate("نیست", "full") is False


class TestQueryCacheRunnerIntegration:
    """Smoke-test: cache is consulted/populated by run_query.

    Strategy
    --------
    1. Pre-populate the module-level ``query_cache`` singleton with a
       known ``QueryResponse``.
    2. Patch ``api.runner.agent`` so any accidental fall-through to the
       real Ollama backend raises immediately (no network required).
    3. Call ``run_query`` with the same (question, mode) key.
    4. Assert the returned object is the exact cached instance and that
       ``agent.run`` was never called.

    This proves run_query() consults the cache before touching the LLM,
    without coupling the test to reconfigure() semantics.
    """

    @pytest.fixture(autouse=True)
    def _isolate(self):
        from api.query_cache import query_cache
        query_cache.clear()
        yield
        query_cache.clear()

    def test_second_call_hits_cache(self):
        """Pre-populate cache → run_query must return cached entry without
        calling agent.run."""
        import api.runner as runner_module
        from api.query_cache import query_cache

        cached_resp = QueryResponse(
            question="سوال", sql="SELECT 1", result=[{"n": 1}],
            row_count=1, model="test",
        )
        query_cache.set("سوال", "full", cached_resp)

        # Patch agent so any real LLM call raises instantly
        mock_agent = MagicMock()
        mock_agent.run.side_effect = AssertionError("agent.run must NOT be called on cache hit")
        mock_agent._backend.name = "test"

        with patch("api.runner.agent", mock_agent):
            result = runner_module.run_query("سوال", "stub", mode="full", interpret=False)

        assert result is cached_resp
        mock_agent.run.assert_not_called()

    def test_cache_miss_calls_agent_and_stores_result(self):
        """On a cache miss run_query calls agent.run and stores the result."""
        import api.runner as runner_module
        from api.query_cache import query_cache
        from llm.base import SQLGenerationResult
        import pandas as pd

        df = pd.DataFrame({"x": [1]})
        sql_result = SQLGenerationResult(
            sql="SELECT 1", raw_response="SELECT 1", attempt=1
        )

        mock_agent = MagicMock()
        mock_agent.run.return_value = (df, sql_result)
        mock_agent._backend.name = "test"

        with patch("api.runner.agent", mock_agent):
            result = runner_module.run_query("سوال", "stub", mode="full", interpret=False)

        mock_agent.run.assert_called_once()
        assert query_cache.get("سوال", "full") is result
