# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``prefix_cache_hit`` says what the model server says, when it says anything.

The audit ``llm`` block's ``prefix_cache_hit`` used to be one rule:
``prompt_tokens < 0.5 x the prefix estimate``. That was a cache signal when a
backend reported only the tokens it computed. An OpenAI-compatible server
reports the whole prompt in ``usage.prompt_tokens`` whether or not it came
from the cache, so the rule really said "the prompt was short": a short
prompt that was fully computed read as a hit, and a full-size prompt served
from the cache in a second read as a miss.

Now the server's own ``usage.prompt_tokens_details.cached_tokens`` decides
(``prefix_cache_hit_source == "server"``, the number kept as
``cached_tokens``), and the old rule is only the fallback for a server that
does not report it (``"estimate"``). These tests pin both paths, the records
written before the fields existed, and the analysis that groups latency by
the real answer.
"""

from __future__ import annotations

from typing import Any

import pytest

from llm.providers import _StreamAssembler
from observability.llm_status import build_llm_status, server_cached_tokens
from observability.quality import compute_metrics, render_quality_text
from scripts.analyze_audit_log import (
    cache_behaviour,
    latency_by_cache,
    render_text,
)

PREFIX = 24500  # a large static prefix, in estimated tokens


def _status(usage: dict[str, Any] | None, *, prefix: int = PREFIX, **kwargs: Any) -> dict[str, Any]:
    raw = None if usage is None else {"usage": usage}
    return dict(build_llm_status(
        raw, model="m", endpoint_status=200, finish_reason="stop",
        static_prefix_tokens=prefix, **kwargs,
    ))


def _usage(prompt: int, cached: Any = "absent") -> dict[str, Any]:
    usage: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": 20}
    if cached != "absent":
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return usage


class TestServerReportsCachedTokens:
    def test_a_full_size_prompt_served_from_the_cache_is_a_hit(self):
        # The old rule called this a miss: the prompt is as large as the prefix.
        status = _status(_usage(24600, 24576))
        assert status["prefix_cache_hit"] is True
        assert status["prefix_cache_hit_source"] == "server"
        assert status["cached_tokens"] == 24576

    def test_a_full_size_prompt_computed_from_scratch_is_a_miss(self):
        status = _status(_usage(24600, 0))
        assert status["prefix_cache_hit"] is False
        assert status["prefix_cache_hit_source"] == "server"
        assert status["cached_tokens"] == 0

    def test_a_short_prompt_computed_from_scratch_is_not_a_hit(self):
        # The old rule called this a hit: 2k tokens is under half of the prefix.
        status = _status(_usage(2000, 0))
        assert status["prefix_cache_hit"] is False
        assert status["prefix_cache_hit_source"] == "server"

    def test_a_short_prompt_mostly_cached_is_a_hit(self):
        assert _status(_usage(2000, 1900))["prefix_cache_hit"] is True

    def test_less_than_half_of_the_prefix_cached_is_a_miss(self):
        assert _status(_usage(24600, 8000))["prefix_cache_hit"] is False

    def test_exactly_half_counts(self):
        assert _status(_usage(24600, 12250))["prefix_cache_hit"] is True

    def test_without_a_known_prefix_it_is_the_share_of_the_whole_prompt(self):
        assert _status(_usage(1000, 600), prefix=0)["prefix_cache_hit"] is True
        assert _status(_usage(1000, 400), prefix=0)["prefix_cache_hit"] is False

    def test_zero_prompt_tokens_is_never_a_hit(self):
        status = _status(_usage(0, 0))
        assert status["prefix_cache_hit"] is False
        assert status["prefix_cache_hit_source"] == "estimate"

    def test_the_server_word_wins_over_the_size_rule(self):
        small_and_cached = _status(_usage(1500, 0), prefix=4000)
        assert small_and_cached["prefix_cache_hit"] is False  # the size rule says True
        assert small_and_cached["prefix_cache_hit_source"] == "server"


class TestServerSilentFallsBackToTheEstimate:
    def test_no_details_means_the_old_rule_and_says_so(self):
        small = _status(_usage(2000))
        assert (small["prefix_cache_hit"], small["prefix_cache_hit_source"]) == (True, "estimate")
        assert small["cached_tokens"] is None
        big = _status(_usage(24600))
        assert (big["prefix_cache_hit"], big["prefix_cache_hit_source"]) == (False, "estimate")

    def test_details_without_cached_tokens_is_silent_too(self):
        usage = _usage(2000)
        usage["prompt_tokens_details"] = {"audio_tokens": 0}
        status = _status(usage)
        assert status["prefix_cache_hit_source"] == "estimate" and status["cached_tokens"] is None

    @pytest.mark.parametrize("bad", [None, True, "12", -5, [3]])
    def test_a_malformed_value_is_not_believed(self, bad):
        status = _status(_usage(2000, bad))
        assert status["prefix_cache_hit_source"] == "estimate"
        assert status["cached_tokens"] is None

    def test_a_transport_failure_is_not_a_hit(self):
        status = _status(None)
        assert status["prefix_cache_hit"] is False
        assert status["prefix_cache_hit_source"] == "estimate"
        assert status["cached_tokens"] is None

    def test_no_prefix_and_no_report_is_not_a_hit(self):
        assert _status(_usage(2000), prefix=0)["prefix_cache_hit"] is False

    def test_server_cached_tokens_reader(self):
        assert server_cached_tokens(None) is None
        assert server_cached_tokens({}) is None
        assert server_cached_tokens({"prompt_tokens_details": {"cached_tokens": 7.0}}) == 7


class TestStreamedResponsesCarryIt:
    def test_the_reassembled_body_keeps_cached_tokens(self):
        assembler = _StreamAssembler()
        assembler.feed({"choices": [{"delta": {"content": "SELECT 1"}, "finish_reason": "stop"}]}, 1.0)
        assembler.feed({"choices": [], "usage": _usage(24600, 24576)}, 1.1)
        status = dict(build_llm_status(
            assembler.body(), model="m", endpoint_status=200, finish_reason="stop",
            static_prefix_tokens=PREFIX,
        ))
        assert status["cached_tokens"] == 24576 and status["prefix_cache_hit_source"] == "server"


class TestTheRunnerBlock:
    def test_the_block_a_request_audits_carries_the_new_fields(self):
        from api.runner import _llm_status_block
        from llm.providers import MockBackend

        meta = {"raw": {"usage": _usage(24600, 24576)}, "endpoint_status": 200}
        block = _llm_status_block(
            meta, MockBackend(), finish_reason="stop", static_prefix_tokens=PREFIX,
        )
        assert block["prefix_cache_hit"] is True
        assert block["prefix_cache_hit_source"] == "server"
        assert block["cached_tokens"] == 24576


# ---------------------------------------------------------------------------
# The analysis groups by the real answer, and old records stay readable
# ---------------------------------------------------------------------------

def _record(llm_ms: int, *, hit: bool, source: str | None, cached: int | None = None,
            prompt: int = 24600) -> dict[str, Any]:
    block: dict[str, Any] = {"prefix_cache_hit": hit, "prompt_tokens": prompt, "total_ms": llm_ms}
    if source is not None:
        block["prefix_cache_hit_source"] = source
        block["cached_tokens"] = cached
    return {"timings": {"llm_ms": llm_ms, "total_ms": llm_ms + 10}, "llm": block}


@pytest.fixture()
def mixed() -> list[dict[str, Any]]:
    return [
        _record(900, hit=True, source="server", cached=24576),
        _record(1100, hit=True, source="server", cached=24576),
        _record(61000, hit=False, source="server", cached=0),
        _record(2000, hit=True, source=None, prompt=2000),     # written before the fields existed
        _record(3000, hit=True, source="estimate", prompt=2000),
        {"timings": {"llm_ms": 5}, "error_code": "OUT_OF_SCOPE"},  # no llm block
    ]


class TestLatencyByRealHitOrMiss:
    def test_groups_by_the_server_word_only(self, mixed):
        out = latency_by_cache(mixed)
        assert (out["hit"]["count"], out["miss"]["count"], out["unreported"]["count"]) == (2, 1, 2)
        assert out["hit"]["llm_ms"]["p50"] == 1000
        assert out["miss"]["llm_ms"]["p50"] == 61000
        assert out["hit"]["cached_tokens"]["p50"] == 24576
        assert out["miss"]["cached_tokens"]["p50"] == 0

    def test_an_estimate_is_never_put_in_hit_or_miss(self, mixed):
        out = latency_by_cache([r for r in mixed if r.get("llm", {}).get("prefix_cache_hit_source") != "server"])
        assert out["hit"]["count"] == 0 and out["miss"]["count"] == 0

    def test_an_old_log_is_all_unreported_and_does_not_raise(self):
        old = [{"timings": {"llm_ms": 10}, "llm": {"prefix_cache_hit": True, "prompt_tokens": 5}}]
        out = latency_by_cache(old)
        assert out["unreported"]["count"] == 1 and out["hit"]["count"] == 0
        assert latency_by_cache([]) ["miss"]["llm_ms"]["count"] == 0

    def test_cache_behaviour_reports_the_real_rate_beside_the_estimate(self, mixed):
        cb = cache_behaviour(mixed)
        assert cb["server_reported_count"] == 3
        assert cb["server_hit_count"] == 2
        assert cb["server_hit_rate"] == round(2 / 3, 4)
        # The estimate-bearing rate still counts every llm record, as before.
        assert cb["llm_call_count"] == 5 and cb["prefix_cache_hit_count"] == 4

    def test_cache_behaviour_on_an_old_log_has_no_real_rate(self):
        cb = cache_behaviour([{"llm": {"prefix_cache_hit": True}}])
        assert cb["server_reported_count"] == 0 and cb["server_hit_rate"] is None

    def test_the_quality_report_carries_the_split(self, mixed):
        m = compute_metrics(mixed, confirmed_keys=None, flagged_keys=None)
        by_cache = m["latency"]["llm_by_cache"]
        assert by_cache["miss"]["llm_ms"]["p50"] == 61000
        assert by_cache["hit"]["count"] == 2

    def test_the_quality_text_shows_it(self, mixed):
        from observability.quality import _render_metrics

        text = "\n".join(_render_metrics("all", compute_metrics(mixed, confirmed_keys=None, flagged_keys=None)))
        assert "cache hit" in text and "cache miss" in text and "cache unreported" in text

    def test_the_text_report_says_when_nothing_was_reported(self):
        from scripts.analyze_audit_log import build_report

        text = render_text(build_report([_record(10, hit=True, source=None)]))
        assert "n/a (no call reported cached_tokens" in text

    def test_the_text_report_shows_the_real_rate_and_the_split(self, mixed):
        from scripts.analyze_audit_log import build_report

        text = render_text(build_report(mixed))
        assert "real hit rate (server) : 0.6667" in text
        assert "LLM stage by real cache hit" in text
