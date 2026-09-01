# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for llm/base.py's default generate_with_meta_segments / generate_structured.

These are the "no native support" fallback paths every backend gets for
free by inheriting from ``LLMBackend`` — exercised directly here via a
minimal concrete subclass, separately from any specific provider's own
(possibly overridden) behaviour.
"""

from __future__ import annotations

import pytest

from llm.base import LLMBackend
from llm.router import PromptSegments


class _EchoBackend(LLMBackend):
    """Returns whatever prompt it was given, for asserting on flattening."""

    name = "echo:stub"

    def __init__(self, response: str | None = None) -> None:
        self._response = response

    def generate(self, prompt: str) -> str:
        return self._response if self._response is not None else prompt


class TestGenerateWithMetaSegmentsDefault:
    def test_flattens_and_delegates_to_generate_with_meta(self):
        backend = _EchoBackend()
        segments = PromptSegments(static_prefix="PREFIX", question="QUESTION")
        text, meta = backend.generate_with_meta_segments(segments)
        assert text == "PREFIX\n\nQUESTION"
        assert meta == {}


class TestGenerateStructuredDefault:
    def test_parses_json_from_plain_text_response(self):
        backend = _EchoBackend(response='{"sql": "SELECT 1", "out_of_scope": false}')
        segments = PromptSegments(question="q")
        obj, meta = backend.generate_structured(segments, schema={"type": "object"})
        assert obj == {"sql": "SELECT 1", "out_of_scope": False}
        assert meta["structured_output"] is False

    def test_appends_schema_instruction_to_flattened_prompt(self):
        captured: dict[str, str] = {}

        class _CapturingBackend(LLMBackend):
            name = "capture:stub"

            def generate(self, prompt: str) -> str:
                captured["prompt"] = prompt
                return '{"sql": "SELECT 1", "out_of_scope": false}'

        backend = _CapturingBackend()
        segments = PromptSegments(static_prefix="PREFIX", question="Q")
        backend.generate_structured(segments, schema={"type": "object"})
        assert "PREFIX" in captured["prompt"]
        assert "Q" in captured["prompt"]
        assert "JSON object" in captured["prompt"]

    def test_non_dict_json_raises(self):
        backend = _EchoBackend(response="[1, 2, 3]")
        with pytest.raises(ValueError, match="Expected a JSON object"):
            backend.generate_structured(PromptSegments(question="q"), schema={})

    def test_unparsable_response_raises(self):
        backend = _EchoBackend(response="not json at all")
        with pytest.raises(ValueError):
            backend.generate_structured(PromptSegments(question="q"), schema={})
