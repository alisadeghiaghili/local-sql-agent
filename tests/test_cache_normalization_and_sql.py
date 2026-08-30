"""Phase 2 task 6 — question normalisation and cache-by-SQL.

Two independent behaviours added to api/query_cache.py:

1. Trivially-different phrasings of the same question (whitespace, Persian
   vs. Arabic-Indic digits, ZWNJ, ي/ك vs. ی/ک) now share one cache entry.
2. Two DIFFERENT questions that generate the SAME SQL share the execution
   result via QueryCache.get_by_sql / SQLAgent.run's sql_cache_lookup hook,
   instead of hitting the database twice.

Both are versioned by prefix_version so a knowledge-base change invalidates
old entries automatically (see TestPrefixVersioning below).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from api.models import QueryResponse
from api.query_cache import QueryCache, _normalize_question
from llm.base import SQLGenerationResult
from llm.sql_agent import SQLAgent


def _resp(question: str, sql: str = "SELECT 1") -> QueryResponse:
    return QueryResponse(question=question, sql=sql, result=[{"n": 1}], row_count=1, model="test")


class TestNormalization:
    def test_whitespace_collapsed(self):
        assert _normalize_question("خرید   مشتریان") == _normalize_question("خرید مشتریان")

    def test_persian_digits_fold_to_ascii(self):
        assert _normalize_question("سال ۱۴۰۲") == _normalize_question("سال 1402")

    def test_arabic_indic_digits_fold_to_ascii(self):
        assert _normalize_question("سال ١٤٠٢") == _normalize_question("سال 1402")

    def test_zwnj_stripped(self):
        assert _normalize_question("می‌خواهم") == _normalize_question("میخواهم")

    def test_arabic_yeh_and_kaf_folded(self):
        assert _normalize_question("علي") == _normalize_question("علی")
        assert _normalize_question("كارگزار") == _normalize_question("کارگزار")

    def test_ascii_case_insensitive(self):
        assert _normalize_question("Top Customers") == _normalize_question("top customers")


class TestCacheHitsOnNormalizedVariants:
    def test_digit_form_variant_hits_cache(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.set("خرید در ۱۴۰۲", "full", _resp("خرید در ۱۴۰۲"))
        assert c.get("خرید در 1402", "full") is not None

    def test_whitespace_variant_hits_cache(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.set("خرید   مشتریان", "full", _resp("خرید مشتریان"))
        assert c.get("خرید مشتریان", "full") is not None

    def test_normalization_disabled_keeps_variants_separate(self):
        from config import override_settings

        c = QueryCache(ttl_seconds=60, max_size=10)
        with override_settings(cache_normalize_questions=False):
            c.set("خرید در ۱۴۰۲", "full", _resp("خرید در ۱۴۰۲"))
            assert c.get("خرید در 1402", "full") is None


class TestPrefixVersioning:
    def test_different_version_is_a_miss(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.set("سوال", "full", _resp("سوال"), prefix_version="v1")
        assert c.get("سوال", "full", prefix_version="v2") is None
        assert c.get("سوال", "full", prefix_version="v1") is not None

    def test_invalidate_scoped_to_its_own_version(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.set("سوال", "full", _resp("سوال"), prefix_version="v1")
        removed = c.invalidate("سوال", "full", prefix_version="v2")
        assert removed is False
        assert c.get("سوال", "full", prefix_version="v1") is not None


class TestCacheBySQL:
    def test_get_by_sql_hits_after_set_with_sql(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.set("question A", "full", _resp("question A", sql="SELECT TOP 10 * FROM Contract"), sql="SELECT TOP 10 * FROM Contract")
        hit = c.get_by_sql("SELECT TOP 10 * FROM Contract", "full")
        assert hit is not None
        assert hit.result == [{"n": 1}]

    def test_different_question_same_sql_shares_result(self):
        """The whole point: question B never wrote its own cache entry,
        but its SQL matches question A's, so get_by_sql finds A's result."""
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.set(
            "question A", "full",
            _resp("question A", sql="SELECT COUNT(*) FROM Contract"),
            sql="SELECT COUNT(*) FROM Contract",
        )
        hit = c.get_by_sql("SELECT COUNT(*) FROM Contract", "full")
        assert hit is not None

    def test_no_sql_index_when_sql_not_supplied(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.set("question A", "full", _resp("question A"))  # sql=None
        assert c.get_by_sql("SELECT 1", "full") is None

    def test_sql_index_versioned_too(self):
        c = QueryCache(ttl_seconds=60, max_size=10)
        c.set("q", "full", _resp("q"), sql="SELECT 1", prefix_version="v1")
        assert c.get_by_sql("SELECT 1", "full", prefix_version="v2") is None
        assert c.get_by_sql("SELECT 1", "full", prefix_version="v1") is not None


class TestSQLAgentSqlCacheLookupHook:
    """SQLAgent.run's sql_cache_lookup hook: a cache hit skips execute_fn entirely."""

    def test_cache_hit_skips_execution(self):
        backend = MagicMock()
        backend.generate_with_meta.return_value = ("SELECT TOP 10 * FROM Contract", {})
        # SQLAgent now routes through LLMRouter.generate_for_task, which calls
        # generate_with_meta_segments(segments) -- a real LLMBackend subclass
        # gets a working default (flatten + generate_with_meta) for free, but
        # a bare MagicMock has no base class to inherit that from, so it must
        # be wired explicitly to keep emulating that same default behaviour.
        backend.generate_with_meta_segments.side_effect = (
            lambda segments: backend.generate_with_meta(segments.flatten())
        )
        execute_fn = MagicMock(side_effect=AssertionError("execute_fn must not be called on a sql-cache hit"))
        cached_df = pd.DataFrame({"n": [42]})

        agent = SQLAgent(backend=backend, execute_fn=execute_fn)
        df, result = agent.run(
            "question", "system prompt",
            sql_cache_lookup=lambda sql: cached_df,
        )

        execute_fn.assert_not_called()
        assert df.equals(cached_df)
        assert result.sql == "SELECT TOP 10 * FROM Contract"

    def test_cache_miss_falls_through_to_execution(self):
        backend = MagicMock()
        backend.generate_with_meta.return_value = ("SELECT TOP 10 * FROM Contract", {})
        # SQLAgent now routes through LLMRouter.generate_for_task, which calls
        # generate_with_meta_segments(segments) -- a real LLMBackend subclass
        # gets a working default (flatten + generate_with_meta) for free, but
        # a bare MagicMock has no base class to inherit that from, so it must
        # be wired explicitly to keep emulating that same default behaviour.
        backend.generate_with_meta_segments.side_effect = (
            lambda segments: backend.generate_with_meta(segments.flatten())
        )
        real_df = pd.DataFrame({"n": [1]})
        execute_fn = MagicMock(return_value=real_df)

        agent = SQLAgent(backend=backend, execute_fn=execute_fn)
        df, result = agent.run(
            "question", "system prompt",
            sql_cache_lookup=lambda sql: None,
        )

        execute_fn.assert_called_once()
        assert df.equals(real_df)

    def test_no_hook_behaves_exactly_as_before(self):
        backend = MagicMock()
        backend.generate_with_meta.return_value = ("SELECT TOP 10 * FROM Contract", {})
        # SQLAgent now routes through LLMRouter.generate_for_task, which calls
        # generate_with_meta_segments(segments) -- a real LLMBackend subclass
        # gets a working default (flatten + generate_with_meta) for free, but
        # a bare MagicMock has no base class to inherit that from, so it must
        # be wired explicitly to keep emulating that same default behaviour.
        backend.generate_with_meta_segments.side_effect = (
            lambda segments: backend.generate_with_meta(segments.flatten())
        )
        real_df = pd.DataFrame({"n": [1]})
        execute_fn = MagicMock(return_value=real_df)

        agent = SQLAgent(backend=backend, execute_fn=execute_fn)
        df, result = agent.run("question", "system prompt")

        execute_fn.assert_called_once()
        assert df.equals(real_df)
