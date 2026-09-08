# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A completion cut off at the token cap is named as such, and not retried.

A reasoning model reasons before it answers. With ``LLM_NUM_PREDICT`` too
low for the reasoning it does, every token goes to thinking and the answer
never starts: ``finish_reason="length"`` with empty content.

Two things used to happen then, and both were wrong in the same direction —
they described the symptom and hid the cause:

1. The correction loop treated it as bad SQL and re-prompted twice with
   "the SQL query you generated failed / --- FAILED SQL --- (nothing) /
   --- ERROR --- LLM returned an empty response". Nothing about the
   truncation depends on the question, so all three rounds hit the same
   ceiling; the only effect was to triple the latency of a certain
   failure.
2. What surfaced was ``EMPTY_SQL_RESPONSE`` — "the model returned
   nothing", which sends the reader to the model, the prompt and the
   schema. The cause was a number in ``.env``.

The pair of conditions is what identifies it: empty output alone is a
model with nothing to say, and truncation alone is a partial answer the
guard rejects on its own terms.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from observability.llm_status import (
    TRUNCATED_OUTPUT_ERROR_CODE,
    is_truncated_empty_completion,
    truncated_output_message,
)


class TestTheSignatureIsBothConditionsTogether:
    def test_empty_and_length_is_a_truncation(self):
        assert is_truncated_empty_completion("", "length")

    def test_whitespace_only_counts_as_empty(self):
        assert is_truncated_empty_completion("  \n\t ", "length")

    def test_empty_but_stopped_cleanly_is_not_a_truncation(self):
        """A model that finished and said nothing is a different failure
        with a different fix; calling it a token-limit problem would send
        the reader to change a setting that is not the cause."""
        assert not is_truncated_empty_completion("", "stop")

    def test_truncated_but_non_empty_is_not_this_case(self):
        """Partial SQL is a partial answer -- the guard rejects it on its
        own terms, and it says something real about the model's output
        that this error code would paper over."""
        assert not is_truncated_empty_completion("SELECT TOP 100 cc.ID FR", "length")

    @pytest.mark.parametrize("reason", ["error", "content_filter", "tool_calls", None, ""])
    def test_other_finish_reasons_are_not_truncations(self, reason):
        assert not is_truncated_empty_completion("", reason)


class TestTheMessageNamesTheFix:
    """The symptom points nowhere near the cause, so the message has to
    carry the whole route from one to the other."""

    def test_it_names_the_setting_and_its_current_value(self):
        message = truncated_output_message(512)
        assert "LLM_NUM_PREDICT" in message
        assert "512" in message, (
            "the limit that was actually in force is the one fact the "
            "reader cannot look up from the error alone"
        )

    def test_it_names_the_other_way_out(self):
        message = truncated_output_message(512)
        assert "LLM_EXTRA_BODY" in message
        assert "enable_thinking" in message, (
            "raising the cap pays for the reasoning tokens every request; "
            "the message should offer the option that does not"
        )

    def test_it_says_retrying_will_not_help(self):
        assert "Retrying will not help" in truncated_output_message(512)


class TestTheCorrectionLoopStops:
    """The v2 conversational path (session/engine.py) — what the web UI
    uses, and where the three-round retry was observed."""

    def test_it_returns_on_the_first_round_without_retrying(self, monkeypatch):
        from llm.router import RouteResult
        from session import engine as engine_module

        calls: list[str] = []

        class _TruncatingRouter:
            def generate_for_task(self, task, segments):
                calls.append(segments.question)
                return RouteResult(
                    text="",
                    structured=None,
                    meta={
                        "raw": {
                            "choices": [{"finish_reason": "length"}],
                            "usage": {"prompt_tokens": 6517, "completion_tokens": 512},
                        },
                        "finish_reason": "length",
                        "endpoint_status": 200,
                        "attempts": 1,
                    },
                    provider="openai:Qwen3",
                    fallback_used=False,
                )

        eng = engine_module.TurnEngine.__new__(engine_module.TurnEngine)
        eng._router = _TruncatingRouter()
        eng._max_corrections = 2

        from llm.router import PromptSegments
        from observability.timing import StageTimer

        segments = PromptSegments(
            static_prefix="prefix", session_context="", question="show me trades",
        )
        with cfg.override_settings(llm_num_predict=512):
            outcome = eng._generate_validate_execute(segments, "prefix", StageTimer())

        assert len(calls) == 1, (
            f"the model was called {len(calls)} times for a failure that "
            "cannot change between attempts -- each retry spends the same "
            "budget on the same reasoning to reach the same ceiling"
        )
        assert outcome.error is not None
        assert outcome.error.code == TRUNCATED_OUTPUT_ERROR_CODE
        assert "LLM_NUM_PREDICT" in outcome.error.message

    def test_an_ordinary_empty_response_still_retries(self, monkeypatch):
        """The short-circuit must not swallow the case it sits beside: an
        empty completion that stopped cleanly may well differ on a retry,
        which is what the correction loop is for."""
        from llm.router import RouteResult
        from session import engine as engine_module

        calls: list[str] = []

        class _EmptyButCleanRouter:
            def generate_for_task(self, task, segments):
                calls.append(segments.question)
                return RouteResult(
                    text="",
                    structured=None,
                    meta={
                        "raw": {"choices": [{"finish_reason": "stop"}]},
                        "finish_reason": "stop",
                        "endpoint_status": 200,
                        "attempts": 1,
                    },
                    provider="openai:m",
                    fallback_used=False,
                )

        eng = engine_module.TurnEngine.__new__(engine_module.TurnEngine)
        eng._router = _EmptyButCleanRouter()
        eng._max_corrections = 2

        from llm.router import PromptSegments
        from observability.timing import StageTimer

        segments = PromptSegments(
            static_prefix="prefix", session_context="", question="show me trades",
        )
        outcome = eng._generate_validate_execute(segments, "prefix", StageTimer())

        assert len(calls) == 3, "the ordinary empty-response retry path was removed"
        assert outcome.error is not None
        assert outcome.error.code == "EMPTY_SQL_RESPONSE"


class TestTheErrorClassIsCountedAsACompletedCall:
    def test_a_truncation_reports_finish_reason_length_not_error(self):
        """The endpoint answered. Classifying this as a transport failure
        would rewrite finish_reason to "error" in the audit log, erasing
        the one field that identifies the token cap as the cause."""
        from api.errors import TruncatedSQLResponseError
        from api.runner import _LLM_COMPLETED_ERRORS

        assert TruncatedSQLResponseError in _LLM_COMPLETED_ERRORS

    def test_the_code_is_distinct_from_the_empty_one(self):
        from api.errors import EmptySQLResponseError, TruncatedSQLResponseError

        assert TruncatedSQLResponseError.error_code == TRUNCATED_OUTPUT_ERROR_CODE
        assert TruncatedSQLResponseError.error_code != EmptySQLResponseError.error_code
