# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``classify_prompt_size`` and the over-budget warning on the retrieval path."""

from __future__ import annotations

import dataclasses
import logging

import pytest

import config as cfg
from core.models import RetrievalContext
from llm.router import build_prompt_segments
from prompt_engine.builder import PromptBuilder
from prompt_engine.prompt_size import PromptSize, classify_prompt_size
from prompt_engine.static_prefix import estimate_tokens

SYSTEM_PROMPT = "You are a T-SQL expert."
ROUTER_LOGGER = "llm.router"


class TestClassifyPromptSize:
    def test_under_budget(self):
        size = classify_prompt_size("a" * 396, 100)
        assert size == PromptSize(estimated_tokens=99, budget=100, over_budget=False, overshoot=0)

    def test_exactly_at_budget_is_within(self):
        size = classify_prompt_size("a" * 400, 100)
        assert size.estimated_tokens == 100
        assert not size.over_budget
        assert size.overshoot == 0

    def test_over_budget_reports_the_overshoot(self):
        size = classify_prompt_size("a" * 440, 100)
        assert size == PromptSize(estimated_tokens=110, budget=100, over_budget=True, overshoot=10)

    def test_empty_prompt(self):
        size = classify_prompt_size("", 100)
        assert size == PromptSize(estimated_tokens=0, budget=100, over_budget=False, overshoot=0)

    @pytest.mark.parametrize("budget", [0, -1, -6000])
    def test_non_positive_budget_is_never_over(self, budget):
        size = classify_prompt_size("a" * 4000, budget)
        assert size.estimated_tokens == 1000
        assert size.budget == budget
        assert not size.over_budget
        assert size.overshoot == 0

    def test_uses_the_shared_estimator(self):
        text = "پرسش فارسی " * 50
        assert classify_prompt_size(text, 1).estimated_tokens == estimate_tokens(text)

    def test_result_is_frozen(self):
        size = classify_prompt_size("abc", 10)
        with pytest.raises(dataclasses.FrozenInstanceError):
            size.over_budget = True  # type: ignore[misc]


def _retrieval_context() -> RetrievalContext:
    return RetrievalContext(entities=["Customer"], facts=["Contract"], filters={"A": 1})


def _retrieval_prompt_tokens() -> int:
    with cfg.override_settings(prompt_retrieval_token_budget=1):
        return estimate_tokens(PromptBuilder.build("q", SYSTEM_PROMPT, _retrieval_context()))


def _over_budget_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.name == ROUTER_LOGGER and r.levelno == logging.WARNING
        and "over budget" in r.getMessage()
    ]


class TestRouterWarning:
    def test_over_budget_retrieval_prompt_logs_one_warning(self, caplog):
        tokens = _retrieval_prompt_tokens()
        assert tokens > 1
        with cfg.override_settings(prompt_retrieval_token_budget=1):
            with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
                build_prompt_segments("q", SYSTEM_PROMPT, _retrieval_context())
        records = _over_budget_warnings(caplog)
        assert len(records) == 1
        assert records[0].args == (tokens, 1, tokens - 1, None)
        message = records[0].getMessage()
        assert f"estimated {tokens} tokens" in message
        assert "PROMPT_RETRIEVAL_TOKEN_BUDGET 1" in message
        assert f"over by {tokens - 1}" in message

    def test_warning_names_the_source(self, caplog):
        from tests._source_fixtures import SOURCES, configured_sources

        with configured_sources():
            with cfg.override_settings(prompt_retrieval_token_budget=1):
                with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
                    build_prompt_segments(
                        "q", SYSTEM_PROMPT, _retrieval_context(), source=SOURCES[0],
                    )
        records = _over_budget_warnings(caplog)
        assert len(records) == 1
        assert records[0].args[3] == SOURCES[0]
        assert repr(SOURCES[0]) in records[0].getMessage()

    def test_in_budget_retrieval_prompt_logs_nothing(self, caplog):
        # The retrieval path is reached because the whole static prefix is far
        # larger than this budget, yet the narrow retrieval prompt fits it.
        from prompt_engine.static_prefix import static_prefix_token_estimate

        budget = _retrieval_prompt_tokens() + 10
        assert static_prefix_token_estimate(SYSTEM_PROMPT) > budget
        with cfg.override_settings(prompt_retrieval_token_budget=budget):
            with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
                seg = build_prompt_segments("q", SYSTEM_PROMPT, _retrieval_context())
        assert seg.static_prefix == ""
        assert _over_budget_warnings(caplog) == []

    @pytest.mark.parametrize("budget", [0, -1])
    def test_forced_retrieval_logs_nothing(self, caplog, budget):
        with cfg.override_settings(prompt_retrieval_token_budget=budget):
            with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
                seg = build_prompt_segments("q", SYSTEM_PROMPT, _retrieval_context())
        assert seg.static_prefix == ""
        assert _over_budget_warnings(caplog) == []

    def test_static_path_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
            seg = build_prompt_segments("q", SYSTEM_PROMPT, _retrieval_context())
        assert seg.static_prefix != ""
        assert _over_budget_warnings(caplog) == []

    def test_segments_are_unchanged_by_the_measurement(self, caplog):
        ctx = _retrieval_context()
        with cfg.override_settings(prompt_retrieval_token_budget=1):
            expected = PromptBuilder.build("q", SYSTEM_PROMPT, ctx)
            with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
                seg = build_prompt_segments("q", SYSTEM_PROMPT, ctx)
        assert len(_over_budget_warnings(caplog)) == 1
        assert seg.static_prefix == ""
        assert seg.question == expected
