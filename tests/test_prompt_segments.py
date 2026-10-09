# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``build_prompt_segments`` takes the static prefix and the per-request tail separately.

It used to concatenate them (``PromptBuilder.build_static``) and cut the
result apart again at ``len(prefix)``. It now asks for the two halves
directly (``PromptBuilder.build_static_suffix``). Nothing about the prompt
may change: the pair must concatenate to exactly the string
``PromptBuilder.build`` produces, for every input the tail depends on, and
the prefix must still be the cached string itself.
"""

from __future__ import annotations

import pytest

import config as cfg
from core.models import RetrievalContext
from llm.router import build_prompt_segments
from prompt_engine.builder import PromptBuilder
from prompt_engine.static_prefix import build_static_prefix
from tests._source_fixtures import SOURCES, configured_sources

SYSTEM_PROMPT = "You are a T-SQL expert."

#: (question, filters, resolved_values, session_context, access_notes)
CASES = [
    ("how many rows?", {}, None, "", ""),
    ("پرسش فارسی", {"Ring": "تالار پتروشیمی", "PersianYear": 1402}, None, "", ""),
    ("q", {}, {"Customer.Name": ["مشتری یک", "مشتری دو"]}, "", ""),
    ("q", {"A": 1}, None, "turn 1: asked about X\ncolumns: a, b", ""),
    ("q", {}, None, "", "Use COLUMN_X only in JOIN ... ON."),
    ("q", {"A": 1, "B": "x"}, {"T.C": ["v"]}, "prior turn", "Use COLUMN_X only in JOIN ... ON."),
    ("", {}, None, "", ""),
]


def _context(filters: dict) -> RetrievalContext:
    return RetrievalContext(filters=filters)


@pytest.mark.parametrize("question,filters,resolved,session,access", CASES)
def test_the_pair_concatenates_to_the_prompt_builder_output(question, filters, resolved, session, access):
    ctx = _context(filters)
    seg = build_prompt_segments(
        question, SYSTEM_PROMPT, ctx, session_context=session,
        denied_columns=None,
    )
    # Not every input is reachable through build_prompt_segments (resolved
    # values come from the context, access notes from the principal), so the
    # string comparison is made against the builder with the same inputs.
    assert seg.static_prefix + seg.question == PromptBuilder.build_static(
        question, SYSTEM_PROMPT, ctx, session_context=session,
        resolved_values=getattr(ctx, "resolved_values", None) or None,
    )
    assert seg.static_prefix + seg.question == PromptBuilder.build(
        question, SYSTEM_PROMPT, ctx, session_context=session,
    )


@pytest.mark.parametrize("question,filters,resolved,session,access", CASES)
def test_the_suffix_is_what_build_static_has_always_appended(question, filters, resolved, session, access):
    ctx = _context(filters)
    prefix = build_static_prefix(SYSTEM_PROMPT)
    full = PromptBuilder.build_static(
        question, SYSTEM_PROMPT, ctx, session_context=session,
        resolved_values=resolved, access_notes=access,
    )
    tail = PromptBuilder.build_static_suffix(
        question, ctx, session_context=session, resolved_values=resolved, access_notes=access,
    )
    assert full == prefix + tail


def test_the_prefix_is_the_cached_string_itself():
    seg = build_prompt_segments("q", SYSTEM_PROMPT, _context({}))
    # (system_prompt, None) is the key build_prompt_segments caches under;
    # lru_cache keys build_static_prefix(p) and build_static_prefix(p, None)
    # separately.
    assert seg.static_prefix is build_static_prefix(SYSTEM_PROMPT, None)


def test_the_prefix_does_not_change_with_the_question():
    a = build_prompt_segments("first question", SYSTEM_PROMPT, _context({"A": 1}))
    b = build_prompt_segments("second question", SYSTEM_PROMPT, _context({"B": 2}),
                              session_context="prior")
    assert a.static_prefix == b.static_prefix
    assert a.question != b.question
    assert "first question" in a.question and "first question" not in a.static_prefix


def test_session_context_and_filters_stay_in_the_tail():
    seg = build_prompt_segments(
        "q", SYSTEM_PROMPT, _context({"Ring": "R1"}), session_context="SESSION-MARKER",
    )
    assert "SESSION-MARKER" in seg.question and "Ring: R1" in seg.question
    assert "SESSION-MARKER" not in seg.static_prefix and "Ring: R1" not in seg.static_prefix


@pytest.mark.parametrize("source", SOURCES)
def test_each_data_sources_pair_concatenates_to_the_builders_output(source):
    ctx = _context({"A": 1})
    with configured_sources():
        seg = build_prompt_segments("q", SYSTEM_PROMPT, ctx, source=source)
        expected = PromptBuilder.build_static("q", SYSTEM_PROMPT, ctx, source=source)
        assert seg.static_prefix == build_static_prefix(SYSTEM_PROMPT, source)
    assert seg.static_prefix + seg.question == expected


def test_the_retrieval_path_is_untouched():
    ctx = RetrievalContext(entities=["T"], filters={"A": 1})
    with cfg.override_settings(prompt_retrieval_token_budget=1):
        seg = build_prompt_segments("q", SYSTEM_PROMPT, ctx)
        expected = PromptBuilder.build("q", SYSTEM_PROMPT, ctx)
    assert seg.static_prefix == ""
    assert seg.question == expected


def test_join_only_columns_reach_the_tail_never_the_prefix():
    from security.column_policy import join_only_prompt_line

    seg_plain = build_prompt_segments("q", SYSTEM_PROMPT, _context({}))
    seg_acl = build_prompt_segments("q", SYSTEM_PROMPT, _context({}), denied_columns=["T.Secret@join_only"])
    assert seg_plain.static_prefix == seg_acl.static_prefix
    # whatever line the policy produces for this entry (possibly none for a
    # source-less deployment) is in the tail, byte for byte
    line = join_only_prompt_line(["T.Secret@join_only"], None)
    if line:
        assert line in seg_acl.question and line not in seg_acl.static_prefix
