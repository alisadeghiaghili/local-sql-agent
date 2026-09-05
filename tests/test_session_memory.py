# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``session.memory`` and its integration into ``session.engine``
— ``docs/api-contract-v2.md`` §5 "Memory".

Uses the real ``project_config/memory_policy.yaml`` (or
``project_config.example/memory_policy.yaml`` under CI) via
``knowledge.memory_policy.get_memory_keys`` -- no key/column/value name is
a literal in ``session/memory.py`` or ``session/engine.py`` itself, per
``tests/test_no_domain_literals.py``, so these tests read the real
declared key back out of config rather than hardcoding one.
"""

from __future__ import annotations

import pandas as pd
import pytest

from config import override_settings
from knowledge.memory_policy import get_memory_keys
from llm.providers import MockBackend
from llm.router import LLMRouter
from security.auth import Principal, scope_key
from session.engine import TurnEngine
from session.memory import (
    MemoryEntry,
    MemoryValidationError,
    apply_memory_to_assumptions,
    has_disallowed_chars,
    truncate_at_word_boundary,
    validate_memory_value,
)
from session.models import Assumption
from session.store import SessionStore

SYSTEM_PROMPT = "You are a T-SQL expert."


def _the_one_declared_key() -> tuple[str, object]:
    keys = get_memory_keys()
    key = next(iter(keys))
    return key, keys[key]


# ---------------------------------------------------------------------------
# Write-time validation
# ---------------------------------------------------------------------------


class TestValidateMemoryValue:
    def test_unknown_key_is_rejected(self):
        with pytest.raises(MemoryValidationError):
            validate_memory_value("no-such-key-declared-anywhere", "x")

    def test_newline_is_rejected_and_nothing_is_returned_as_valid(self):
        key, _ = _the_one_declared_key()
        with pytest.raises(MemoryValidationError):
            validate_memory_value(key, "line one\nline two")

    def test_control_character_is_rejected(self):
        key, _ = _the_one_declared_key()
        with pytest.raises(MemoryValidationError):
            validate_memory_value(key, "bad\x07value")

    def test_over_length_value_is_rejected(self):
        key, key_cfg = _the_one_declared_key()
        too_long = "x" * (key_cfg.max_length + 1)
        with pytest.raises(MemoryValidationError):
            validate_memory_value(key, too_long)

    def test_value_outside_closed_option_set_is_rejected(self):
        key, key_cfg = _the_one_declared_key()
        if not key_cfg.options:
            pytest.skip("declared key has no closed option set")
        with pytest.raises(MemoryValidationError):
            validate_memory_value(key, "a value that is definitely not one of the declared options")

    def test_a_declared_option_value_is_accepted(self):
        key, key_cfg = _the_one_declared_key()
        if not key_cfg.options:
            pytest.skip("declared key has no closed option set")
        validate_memory_value(key, key_cfg.options[0])  # must not raise


class TestTextSafetyHelpers:
    def test_has_disallowed_chars_true_for_newline(self):
        assert has_disallowed_chars("a\nb") is True

    def test_has_disallowed_chars_false_for_ordinary_text(self):
        assert has_disallowed_chars("ordinary text") is False

    def test_truncate_at_word_boundary_never_exceeds_cap(self):
        text = "one two three four five six seven eight nine ten"
        out = truncate_at_word_boundary(text, 20)
        assert len(out) <= 20
        assert not out.endswith(" ")


# ---------------------------------------------------------------------------
# apply_memory_to_assumptions — precedence + read-time ACL re-check
# ---------------------------------------------------------------------------


class TestApplyMemoryToAssumptions:
    def test_default_sourced_field_is_replaced_and_resourced_as_memory(self):
        key, key_cfg = _the_one_declared_key()
        assumptions = [Assumption(field=key_cfg.field_name, value="fallback", source="default")]
        entries = {key: MemoryEntry(key=key, field=key_cfg.field_name, value="پیش‌فرض من", updated_at="t")}

        result, warnings, used = apply_memory_to_assumptions(assumptions, entries, denied_columns=None)

        assert warnings == []
        assert used == {key: "پیش‌فرض من"}
        assert result[0].source == "memory"
        assert result[0].value == "پیش‌فرض من"

    def test_question_sourced_field_is_left_untouched(self):
        """Precedence: question > memory. A "question"-sourced assumption
        must never be overwritten by a stored preference."""
        key, key_cfg = _the_one_declared_key()
        assumptions = [Assumption(field=key_cfg.field_name, value="از متن پرسش", source="question")]
        entries = {key: MemoryEntry(key=key, field=key_cfg.field_name, value="پیش‌فرض من", updated_at="t")}

        result, warnings, used = apply_memory_to_assumptions(assumptions, entries, denied_columns=None)

        assert result[0].value == "از متن پرسش"
        assert result[0].source == "question"
        assert used == {}

    def test_denied_column_drops_the_entry_and_emits_a_warning(self):
        """§5 "re-check the ACL at read time" -- an entry naming a now-denied
        column is dropped for this turn, not applied, and not silently ignored."""
        key, key_cfg = _the_one_declared_key()
        assumptions = [Assumption(field=key_cfg.field_name, value="fallback", source="default")]
        entries = {key: MemoryEntry(key=key, field=key_cfg.field_name, value="ممنوعه", updated_at="t")}

        result, warnings, used = apply_memory_to_assumptions(
            assumptions, entries, denied_columns=(key_cfg.column,),
        )

        assert used == {}
        assert result[0].source == "default"  # NOT applied
        assert result[0].value == "fallback"
        assert len(warnings) == 1

    def test_no_entries_is_a_no_op(self):
        assumptions = [Assumption(field="x", value="v", source="default")]
        result, warnings, used = apply_memory_to_assumptions(assumptions, {}, denied_columns=None)
        assert result == assumptions
        assert warnings == []
        assert used == {}


# ---------------------------------------------------------------------------
# Integration: TurnEngine surfaces memory as a declared assumption
# ---------------------------------------------------------------------------


class TestTurnEngineMemoryIntegration:
    def _fresh_ranking_engine(self, sql: str) -> TurnEngine:
        router = LLMRouter(default_chain=[MockBackend(response=sql)])
        return TurnEngine(router=router, execute_fn=lambda _sql: pd.DataFrame({"Name": ["A"]}))

    def test_memory_surfaces_as_a_declared_assumption_never_silently(self):
        """§5: "It surfaces as a declared assumption, never silently." """
        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"
        entries = {key: MemoryEntry(key=key, field=key_cfg.field_name, value=value, updated_at="t")}
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=10)
        record = store.create()

        with override_settings(default_top_n=1000):
            turn = self._fresh_ranking_engine(
                "SELECT TOP 5 Name FROM Customer"
            ).ask(
                record, "۱۰ مشتری برتر را نشان بده", SYSTEM_PROMPT, memory_entries=entries,
            )

        assert turn.error is None
        memory_assumptions = [a for a in turn.ambiguity.assumptions if a.source == "memory"]
        assert len(memory_assumptions) == 1
        assert memory_assumptions[0].field == key_cfg.field_name
        assert memory_assumptions[0].value == value
        assert turn.ambiguity.is_ambiguous is True

    def test_memory_disabled_by_config_never_applies(self):
        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"
        entries = {key: MemoryEntry(key=key, field=key_cfg.field_name, value=value, updated_at="t")}
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=10)
        record = store.create()

        with override_settings(default_top_n=1000, memory_enabled=False):
            turn = self._fresh_ranking_engine(
                "SELECT TOP 5 Name FROM Customer"
            ).ask(
                record, "۱۰ مشتری برتر را نشان بده", SYSTEM_PROMPT, memory_entries=entries,
            )

        assert turn.error is None
        assert all(a.source != "memory" for a in turn.ambiguity.assumptions)

    def test_denied_column_warning_reaches_turn_warnings(self):
        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"
        entries = {key: MemoryEntry(key=key, field=key_cfg.field_name, value=value, updated_at="t")}
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=10)
        record = store.create()

        with override_settings(default_top_n=1000):
            turn = self._fresh_ranking_engine(
                "SELECT TOP 5 Name FROM Customer"
            ).ask(
                record, "۱۰ مشتری برتر را نشان بده", SYSTEM_PROMPT,
                denied_columns=(key_cfg.column,), memory_entries=entries,
            )

        assert turn.error is None
        assert any(turn.warnings)
        assert all(a.source != "memory" for a in turn.ambiguity.assumptions)


# ---------------------------------------------------------------------------
# §8: the static prefix must not move when memory is set vs unset.
# ---------------------------------------------------------------------------


class TestStaticPrefixInvarianceWithMemory:
    def test_static_prefix_byte_identical_with_memory_set_and_unset(self):
        from prompt_engine.static_prefix import build_static_prefix

        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"
        entries = {key: MemoryEntry(key=key, field=key_cfg.field_name, value=value, updated_at="t")}

        captured: list[str] = []

        class _RecordingBackend:
            name = "recording"

            def generate_with_meta_segments(self, segments):
                captured.append(segments.flatten())
                return "SELECT TOP 5 Name FROM Customer", {"raw": {}, "endpoint_status": 200, "attempts": 1}

        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=10)

        with override_settings(default_top_n=1000):
            record_without = store.create()
            TurnEngine(
                router=LLMRouter(default_chain=[_RecordingBackend()]),
                execute_fn=lambda _sql: pd.DataFrame({"Name": ["A"]}),
            ).ask(record_without, "۱۰ مشتری برتر را نشان بده", SYSTEM_PROMPT, memory_entries=None)

            record_with = store.create()
            TurnEngine(
                router=LLMRouter(default_chain=[_RecordingBackend()]),
                execute_fn=lambda _sql: pd.DataFrame({"Name": ["A"]}),
            ).ask(record_with, "۱۰ مشتری برتر را نشان بده", SYSTEM_PROMPT, memory_entries=entries)

        assert len(captured) == 2
        prefix = build_static_prefix(SYSTEM_PROMPT)
        assert captured[0].startswith(prefix)
        assert captured[1].startswith(prefix)
        assert captured[0][: len(prefix)] == captured[1][: len(prefix)]
        # The two prompts as a WHOLE differ (memory changed the suffix) --
        # otherwise this test would prove nothing about where memory landed.
        assert captured[0] != captured[1]
        assert value in captured[1][len(prefix):]
        assert value not in captured[0][len(prefix):]


# ---------------------------------------------------------------------------
# §5 "The cache trap" -- the query cache's scope key (security.auth.scope_key)
# must fold in exactly the memory entries that influenced a query, never the
# caller's whole stored memory set.
# ---------------------------------------------------------------------------


class TestScopeKeyCacheTrap:
    def test_principals_whose_memory_did_influence_the_query_do_not_share_a_scope_key(self):
        key, key_cfg = _the_one_declared_key()
        if not key_cfg.options or len(key_cfg.options) < 2:
            pytest.skip("declared key needs at least two closed options for this test")

        assumptions_a = [Assumption(field=key_cfg.field_name, value="fallback", source="default")]
        assumptions_b = [Assumption(field=key_cfg.field_name, value="fallback", source="default")]
        entries_a = {key: MemoryEntry(key=key, field=key_cfg.field_name, value=key_cfg.options[0], updated_at="t")}
        entries_b = {key: MemoryEntry(key=key, field=key_cfg.field_name, value=key_cfg.options[1], updated_at="t")}

        _, _, used_a = apply_memory_to_assumptions(assumptions_a, entries_a, denied_columns=None)
        _, _, used_b = apply_memory_to_assumptions(assumptions_b, entries_b, denied_columns=None)
        assert used_a and used_b  # sanity: memory really did influence both turns

        principal_a = Principal(id="analyst-1", name="A")
        principal_b = Principal(id="analyst-2", name="B")
        assert scope_key(principal_a, memory_used=used_a) != scope_key(principal_b, memory_used=used_b)

    def test_principals_whose_memory_did_not_influence_the_query_still_share_a_scope_key(self):
        key, key_cfg = _the_one_declared_key()
        value = key_cfg.options[0] if key_cfg.options else "یک مقدار"
        # Both already carry a "question"-sourced assumption for this
        # field -- precedence (question > memory) means the stored entry
        # never actually applies, even though it is present in `entries`.
        assumptions_a = [Assumption(field=key_cfg.field_name, value="از متن پرسش یک", source="question")]
        assumptions_b = [Assumption(field=key_cfg.field_name, value="از متن پرسش دو", source="question")]
        entries_a = {key: MemoryEntry(key=key, field=key_cfg.field_name, value=value, updated_at="t")}
        entries_b = {key: MemoryEntry(key=key, field=key_cfg.field_name, value=value, updated_at="t")}

        _, _, used_a = apply_memory_to_assumptions(assumptions_a, entries_a, denied_columns=None)
        _, _, used_b = apply_memory_to_assumptions(assumptions_b, entries_b, denied_columns=None)
        assert used_a == {} and used_b == {}  # sanity: memory never applied for either

        principal_a = Principal(id="analyst-1", name="A", denied_columns=("Z",))
        principal_b = Principal(id="analyst-2", name="B", denied_columns=("Z",))
        assert scope_key(principal_a, memory_used=used_a) == scope_key(principal_b, memory_used=used_b)
