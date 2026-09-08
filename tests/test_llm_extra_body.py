# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``LLM_EXTRA_BODY`` — the passthrough for server-specific request fields.

The setting exists for one concrete failure, and these tests are written
against it rather than against the abstraction. A reasoning model
(Qwen3-class) spends its completion budget thinking before it answers, so
with ``LLM_NUM_PREDICT`` at its 512-token default it can consume every
token on reasoning and be cut off — ``finish_reason: "length"``,
``completion_tokens`` exactly 512, ``reasoning_detected: true``, and an
empty ``content``. That reaches the caller as ``EMPTY_SQL_RESPONSE``: a
description of the response, not of the cause.

Turning that reasoning off is not expressible in the OpenAI
chat-completions schema, and every server spells it differently — vLLM and
SGLang take ``chat_template_kwargs.enable_thinking``, Ollama takes
``think``, OpenAI takes ``reasoning_effort``. This project therefore passes
the fields through verbatim instead of encoding dialects it would sometimes
get wrong.

The reserved-key rule is the load-bearing part. A passthrough that could
set ``model`` or ``max_tokens`` would let one environment variable
contradict the settings the ``llm`` status block reports, making that block
describe a request that was never sent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from llm.providers import (
    RESERVED_PAYLOAD_KEYS,
    ExtraBodyConfigError,
    OpenAIBackend,
    load_extra_body,
)

_THINKING_OFF = '{"chat_template_kwargs": {"enable_thinking": false}}'


def _backend() -> OpenAIBackend:
    return OpenAIBackend(model="test-model", api_key="test-key")


class TestTheDefaultIsUnchangedBehaviour:
    def test_unset_sends_exactly_what_it_always_sent(self):
        with cfg.override_settings(llm_extra_body_json=""):
            payload = _backend()._build_payload("hello")
        assert sorted(payload) == [
            "max_tokens", "messages", "model", "seed", "temperature", "top_p",
        ]

    def test_whitespace_only_is_treated_as_unset(self):
        with cfg.override_settings(llm_extra_body_json="   \n  "):
            assert load_extra_body() == {}


class TestFieldsReachTheRequest:
    def test_the_qwen3_thinking_switch_is_merged_verbatim(self):
        """The value a Qwen3 deployment actually needs, unmodified — this
        project does not know what the field means and must not reshape
        it."""
        with cfg.override_settings(llm_extra_body_json=_THINKING_OFF):
            payload = _backend()._build_payload("hello")
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}

    def test_merging_does_not_disturb_the_standard_fields(self):
        with cfg.override_settings(llm_extra_body_json='{"top_k": 20}'):
            payload = _backend()._build_payload("hello")
        assert payload["model"] == "test-model"
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        assert payload["max_tokens"] == cfg.settings.llm_num_predict
        assert payload["top_k"] == 20

    def test_a_falsey_value_still_reaches_the_payload(self):
        """``false`` and ``0`` are the values most likely to be sent here
        (``think: false``, ``reasoning_effort: 0``); a truthiness test
        anywhere in the merge would drop exactly the fields this setting
        exists for."""
        with cfg.override_settings(llm_extra_body_json='{"think": false, "budget": 0}'):
            payload = _backend()._build_payload("hello")
        assert payload["think"] is False
        assert payload["budget"] == 0


class TestReservedKeysAreRefused:
    """Refused at parse time, not overridden at merge time: a setting the
    operator believes took effect and did not is the failure mode this
    whole feature exists to remove, so it must not introduce its own."""

    @pytest.mark.parametrize("key", sorted(RESERVED_PAYLOAD_KEYS))
    def test_every_reserved_key_is_rejected(self, key):
        with cfg.override_settings(llm_extra_body_json=json.dumps({key: "x"})):
            with pytest.raises(ExtraBodyConfigError) as excinfo:
                load_extra_body()
        assert key in str(excinfo.value)

    def test_the_refusal_names_the_setting_to_use_instead(self):
        with cfg.override_settings(llm_extra_body_json='{"max_tokens": 4096}'):
            with pytest.raises(ExtraBodyConfigError) as excinfo:
                load_extra_body()
        assert "LLM_NUM_PREDICT" in str(excinfo.value), (
            "the refusal says no without saying where the value belongs -- "
            "and max_tokens is the key an operator is most likely to reach "
            "for here, having just hit the reasoning-model truncation"
        )

    def test_a_reserved_key_alongside_a_valid_one_still_refuses(self):
        """Rejecting the whole value rather than the offending key: a
        partial application is another way to believe something took."""
        body = '{"chat_template_kwargs": {"enable_thinking": false}, "seed": 1}'
        with cfg.override_settings(llm_extra_body_json=body):
            with pytest.raises(ExtraBodyConfigError):
                load_extra_body()


class TestMalformedValuesFailLoudly:
    def test_invalid_json_is_refused_with_an_example(self):
        with cfg.override_settings(llm_extra_body_json="not json"):
            with pytest.raises(ExtraBodyConfigError) as excinfo:
                load_extra_body()
        assert "enable_thinking" in str(excinfo.value), (
            "a parse error that does not show the shape being asked for "
            "leaves the reader to guess it"
        )

    @pytest.mark.parametrize("value", ['["a"]', '"a"', "42", "true", "null"])
    def test_a_non_object_is_refused(self, value):
        with cfg.override_settings(llm_extra_body_json=value):
            with pytest.raises(ExtraBodyConfigError):
                load_extra_body()

    def test_a_malformed_value_fails_at_startup_not_at_first_question(self):
        """``Settings.validate()`` is what ``scripts/verify_deployment.py``
        and the server's own start-up gate run. Parsing this only at
        request time would mean the operator who set it — most likely
        while debugging an empty response — learns about their typo from
        an analyst."""
        with cfg.override_settings(
            llm_extra_body_json='{"model": "somewhere-else"}',
            openai_model="m",
            db_connection_url="sqlite:///x.db",
            sql_dialect="sqlite",
        ):
            with pytest.raises(ValueError, match="LLM_EXTRA_BODY"):
                cfg.settings.validate()

    def test_a_valid_value_does_not_break_startup_validation(self):
        with cfg.override_settings(
            llm_extra_body_json=_THINKING_OFF,
            openai_model="m",
            db_connection_url="sqlite:///x.db",
            sql_dialect="sqlite",
        ):
            cfg.settings.validate()  # does not raise
