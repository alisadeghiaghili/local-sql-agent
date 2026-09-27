# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Error-path coverage for ``session.engine.TurnEngine`` not exercised by
``tests/test_session_engine.py``'s happy-path §2 proof.

Every branch here is a case §5 explicitly calls out: a failure must
produce a ``Turn`` with ``error``/``guard`` populated, never an
exception that escapes ``TurnEngine.ask`` — see that method's docstring.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from config import override_settings
from core.models import RetrievalContext
from llm.providers import MockBackend
from llm.router import LLMRouter, PromptSegments
from prompt_engine.untrusted import UNTRUSTED_INSTRUCTION
from session import engine as engine_module
from session.engine import TurnEngine
from session.models import GuardVerdict, ResultColumn, Turn, TurnResult
from session.store import SessionStore, TurnMemory

SYSTEM_PROMPT = "You are a T-SQL expert."


class _CountingBackend:
    """Returns a different response each call, from a fixed list; raises past the end."""

    name = "counting"

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = 0

    def generate_with_meta_segments(self, segments: PromptSegments):
        response = self._responses[self.calls]
        self.calls += 1
        return response, {"raw": {}, "endpoint_status": 200, "attempts": 1}


class _AlwaysBrokenBackend:
    name = "broken"

    def generate_with_meta_segments(self, segments):
        raise RuntimeError("connection refused")


class _EmptyBackend:
    name = "empty"

    def generate_with_meta_segments(self, segments):
        return "", {"raw": {}, "endpoint_status": 200, "attempts": 1}


def _seed_previous_turn(record, *, sql: str | None, filters: dict | None = None) -> Turn:
    turn = Turn(
        turn_id="t_prev", session_id=record.session_id, index=1, question="q1", sql=sql,
        result=TurnResult(columns=[ResultColumn(name="X", type="string")], row_count=0) if sql else None,
    )
    record.turns.append(turn)
    record.memory["t_prev"] = TurnMemory(turn_id="t_prev", filters=filters or {})
    return turn


class TestCteRefinementErrorPaths:
    def test_no_previous_sql_yields_no_previous_turn_error(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        _seed_previous_turn(record, sql=None)  # a previous turn that itself failed

        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response="SELECT 1")]),
            execute_fn=lambda sql: pd.DataFrame(),
        )
        turn = engine.ask(record, "از بین آن‌ها ۱۰ مشتری برتر به لحاظ حجم معامله", SYSTEM_PROMPT)

        assert turn.error is not None
        assert turn.error.code == "NO_PREVIOUS_TURN"

    def test_router_failure_during_outer_query_generation(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        _seed_previous_turn(record, sql="SELECT TOP 10 Name FROM Customer", filters={"Ring": "x"})

        engine = TurnEngine(router=LLMRouter(default_chain=[_AlwaysBrokenBackend()]), execute_fn=lambda sql: pd.DataFrame())
        turn = engine.ask(record, "از بین آن‌ها ۱۰ مشتری برتر", SYSTEM_PROMPT)

        assert turn.error is not None
        assert turn.error.code == "MODEL_UNAVAILABLE"

    def test_execute_failure_after_successful_composition(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        _seed_previous_turn(record, sql="SELECT TOP 10 c.Name AS Name FROM Customer c", filters={"Ring": "x"})

        def broken_execute(sql: str) -> pd.DataFrame:
            if "COUNT(*)" in sql:
                return pd.DataFrame({"Cnt": [0]})  # the truncation-check probe
            raise RuntimeError("db is down")

        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response="SELECT TOP 10 c_Name FROM _prev")]),
            execute_fn=broken_execute,
        )
        with override_settings(refinement_scan_cap=1000, default_top_n=1000):
            turn = engine.ask(record, "از بین آن‌ها ۱۰ مشتری برتر", SYSTEM_PROMPT)

        assert turn.error is not None
        assert turn.error.code == "QUERY_EXECUTION_ERROR"
        assert turn.guard.verdict == "allowed"  # it passed the guard; the DB is what failed


class TestGenerativeRetryLoop:
    def test_empty_response_exhausts_corrections_then_errors(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[_EmptyBackend()]), execute_fn=lambda sql: pd.DataFrame(),
            max_corrections=1,
        )
        turn = engine.ask(record, "لیست مشتریان", SYSTEM_PROMPT)
        assert turn.error is not None
        assert turn.error.code == "EMPTY_SQL_RESPONSE"

    def test_invalid_sql_exhausts_corrections_then_guard_rejects(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        backend = _CountingBackend(["not sql at all", "still not sql"])
        engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]), execute_fn=lambda sql: pd.DataFrame(),
            max_corrections=1,
        )
        turn = engine.ask(record, "لیست مشتریان", SYSTEM_PROMPT)
        assert turn.guard.verdict == "rejected"
        assert backend.calls == 2  # first attempt + one correction round

    def test_execution_fails_then_succeeds_on_correction(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        backend = _CountingBackend([
            "SELECT TOP 10 Name FROM Customer",
            "SELECT TOP 10 Name FROM Customer WHERE 1=1",
        ])
        attempts: list[str] = []

        def flaky_execute(sql: str) -> pd.DataFrame:
            attempts.append(sql)
            if len(attempts) == 1:
                raise RuntimeError("transient failure")
            return pd.DataFrame({"Name": ["A"]})

        engine = TurnEngine(router=LLMRouter(default_chain=[backend]), execute_fn=flaky_execute, max_corrections=2)
        turn = engine.ask(record, "لیست مشتریان", SYSTEM_PROMPT)

        assert turn.error is None
        assert turn.result.row_count == 1
        assert len(attempts) == 2

    def test_execution_fails_until_corrections_exhausted(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response="SELECT TOP 10 Name FROM Customer")]),
            execute_fn=lambda sql: (_ for _ in ()).throw(RuntimeError("db is down")),
            max_corrections=0,
        )
        turn = engine.ask(record, "لیست مشتریان", SYSTEM_PROMPT)
        assert turn.error is not None
        assert turn.error.code == "QUERY_EXECUTION_ERROR"


class TestCarryForwardPeriodDelta:
    def test_period_delta_with_no_inherited_year_uses_current_year_default(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        _seed_previous_turn(record, sql="SELECT TOP 10 Name FROM Customer", filters={"Ring": "تالار سیمان"})
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response="SELECT TOP 10 Name FROM Customer")]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        turn = engine.ask(record, "همین را برای سال قبل", SYSTEM_PROMPT)
        assert turn.error is None
        period = next(a for a in turn.ambiguity.assumptions if a.field == "period")
        assert period.value.isdigit()

    def test_period_delta_reaches_the_actual_prompt_sent_to_the_model(self):
        """Regression: the computed period must reach the LLM's DETECTED
        FILTERS section, not just the displayed assumption -- an earlier
        version of this code path popped ``PersianYear`` back out of the
        filters right after computing it, so a real model would never
        see the year it was supposed to filter on at all."""
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        _seed_previous_turn(
            record, sql="SELECT TOP 10 Name FROM Customer", filters={"Ring": "x", "PersianYear": 1404},
        )
        captured: list[str] = []

        class _RecordingBackend:
            name = "recording"

            def generate_with_meta_segments(self, segments):
                captured.append(segments.flatten())
                return "SELECT TOP 10 Name FROM Customer", {"raw": {}, "endpoint_status": 200, "attempts": 1}

        engine = TurnEngine(router=LLMRouter(default_chain=[_RecordingBackend()]), execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}))
        engine.ask(record, "همین را برای سال قبل", SYSTEM_PROMPT)

        assert len(captured) == 1
        assert "PersianYear: 1403" in captured[0]

    def test_period_delta_with_inherited_year_shifts_it(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        _seed_previous_turn(
            record, sql="SELECT TOP 10 Name FROM Customer", filters={"Ring": "x", "PersianYear": 1404},
        )
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response="SELECT TOP 10 Name FROM Customer")]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        turn = engine.ask(record, "همین را برای سال قبل", SYSTEM_PROMPT)
        period = next(a for a in turn.ambiguity.assumptions if a.field == "period")
        assert period.value == "1403"


# Synthetic (never a real deployment's) hall names for the overridden-refinement and filter-enforcement tests below --
# "تالار" ("تالار"/"hall") + a distinctive suffix.
_HALL_A = "تالار اول"
_HALL_B = "تالار دوم"


class TestCteRefinementOverrideDemotion:
    """Overridden filter on a refinement: an ``assumption_overrides`` PATCH that
    changes a filter a §2 CTE refinement would otherwise inherit unchanged
    must never be silently composed over ``_prev`` (the old value is baked
    into the previous turn's own SQL) -- it must route through fresh
    generation instead, with the overridden value reaching the model, the
    chip, and ``resolved_question`` all in agreement."""

    def test_ring_override_on_a_cte_refinement_turn_demotes_to_fresh_generation(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        _seed_previous_turn(
            record,
            sql=f"SELECT TOP 10 Name FROM Customer WHERE Name = N'{_HALL_A}'",
            filters={"Ring": _HALL_A, "PersianYear": 1403},
        )
        captured: list[str] = []

        class _RecordingBackend:
            name = "recording"

            def generate_with_meta_segments(self, segments):
                captured.append(segments.flatten())
                return (
                    f"SELECT TOP 10 Name FROM Customer WHERE Name = N'{_HALL_B}'",
                    {"raw": {}, "endpoint_status": 200, "attempts": 1},
                )

        engine = TurnEngine(
            router=LLMRouter(default_chain=[_RecordingBackend()]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        turn = engine.ask(
            record,
            "از بین آن‌ها ۱۰ مشتری برتر به لحاظ حجم معامله",
            SYSTEM_PROMPT,
            assumption_overrides={"ring": _HALL_B},
        )

        assert turn.error is None
        # Composition must be relabelled honestly -- this turn no longer
        # reuses the previous turn's SQL at all.
        assert turn.basis.kind == "refines"
        assert turn.basis.composition == "none"
        # The SQL actually executed must be the fresh generation, never a
        # `_prev`-composed statement built over the OLD value.
        assert "_prev" not in turn.sql
        assert _HALL_B in turn.sql
        # The chip and resolved_question must both show the NEW value.
        ring_assumption = next(a for a in turn.ambiguity.assumptions if a.field == "ring")
        assert ring_assumption.value == _HALL_B
        assert ring_assumption.source == "question"
        assert _HALL_B in turn.resolved_question
        assert _HALL_A not in turn.resolved_question
        # The model must have been told the NEW value, in the DETECTED
        # FILTERS section specifically, and never the old one there --
        # the OLD value legitimately still appears elsewhere, in the
        # SESSION CONTEXT section's verbatim record of the previous
        # turn's own SQL, which is unrelated to this defect.
        assert len(captured) == 1
        detected_filters_section = captured[0][
            captured[0].index("DETECTED FILTERS") : captured[0].index("RESOLVED WAREHOUSE VALUES")
        ]
        assert _HALL_B in detected_filters_section
        assert _HALL_A not in detected_filters_section

    def test_override_on_an_unrelated_field_does_not_demote(self):
        """An override that touches a field OTHER than the inherited
        scope filter (e.g. "measure") changes nothing this §2 composition
        actually bakes in -- composing over `_prev` is still safe, so this
        must stay a "cte" refinement."""
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        _seed_previous_turn(
            record,
            sql=f"SELECT TOP 10 c.Name AS Name FROM Customer c WHERE c.Name = N'{_HALL_A}'",
            filters={"Ring": _HALL_A},
        )
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response="SELECT TOP 10 c_Name AS Name FROM _prev")]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        turn = engine.ask(
            record,
            "از بین آن‌ها ۱۰ مشتری برتر به لحاظ حجم معامله",
            SYSTEM_PROMPT,
            assumption_overrides={"measure": "something else"},
        )
        assert turn.error is None
        assert turn.basis.composition == "cte"

    def test_no_override_at_all_still_composes_as_cte(self):
        """Sanity check on the gate itself: an ordinary CTE refinement with
        no assumption_overrides is completely unaffected by the overridden-refinement handling."""
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        _seed_previous_turn(
            record,
            sql=f"SELECT TOP 10 c.Name AS Name FROM Customer c WHERE c.Name = N'{_HALL_A}'",
            filters={"Ring": _HALL_A},
        )
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response="SELECT TOP 10 c_Name AS Name FROM _prev")]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        turn = engine.ask(record, "از بین آن‌ها ۱۰ مشتری برتر به لحاظ حجم معامله", SYSTEM_PROMPT)
        assert turn.error is None
        assert turn.basis.composition == "cte"


class _CountingRecordingBackend:
    """Like ``_CountingBackend`` above, but also records every
    ``PromptSegments`` it is asked to generate from -- used by the filter-enforcement
    tests below to assert exactly how many generation attempts a missing
    filter costs."""

    name = "counting-recording"

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = 0
        self.segments: list[PromptSegments] = []

    def generate_with_meta_segments(self, segments: PromptSegments):
        self.segments.append(segments)
        response = self._responses[self.calls]
        self.calls += 1
        return response, {"raw": {}, "endpoint_status": 200, "attempts": 1}


def _ask_with_context(engine: TurnEngine, record, question: str, context: RetrievalContext) -> Turn:
    with patch("session.engine.ContextRetriever.retrieve", return_value=context):
        return engine.ask(record, question, SYSTEM_PROMPT)


class TestFilterEnforcementAfterGeneration:
    """Filter-enforcement check: a string-valued dimension filter this
    turn presents as applied must actually be referenced by the generated
    SQL -- checked once, after the guard, with one bounded regeneration
    attempt before falling back to a Persian warning."""

    def test_sql_omitting_the_filter_triggers_exactly_one_regeneration_then_the_warning(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        backend = _CountingRecordingBackend(
            responses=["SELECT TOP 10 Name FROM Customer", "SELECT TOP 10 Name FROM Customer"],
        )
        engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        context = RetrievalContext(entities=["Ring"], filters={"Ring": _HALL_A})

        turn = _ask_with_context(engine, record, "قیمت " + _HALL_A + " چند بود", context)

        assert turn.error is None
        assert backend.calls == 2, "exactly one regeneration attempt, not more"
        assert any(_HALL_A in w for w in turn.warnings)

    def test_sql_that_includes_the_filter_triggers_neither_regeneration_nor_warning(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        backend = _CountingRecordingBackend(
            responses=[f"SELECT TOP 10 Name FROM Customer WHERE Name = N'{_HALL_A}'"],
        )
        engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        context = RetrievalContext(entities=["Ring"], filters={"Ring": _HALL_A})

        turn = _ask_with_context(engine, record, "قیمت " + _HALL_A + " چند بود", context)

        assert turn.error is None
        assert backend.calls == 1, "an SQL that already references the filter must not trigger a retry"
        assert not any(_HALL_A in w for w in turn.warnings)

    def test_sql_referencing_the_filter_via_an_in_clause_is_accepted(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        backend = _CountingRecordingBackend(
            responses=[f"SELECT TOP 10 Name FROM Customer WHERE Name IN (N'{_HALL_A}', N'other')"],
        )
        engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        context = RetrievalContext(entities=["Ring"], filters={"Ring": _HALL_A})

        turn = _ask_with_context(engine, record, "قیمت " + _HALL_A + " چند بود", context)

        assert turn.error is None
        assert backend.calls == 1
        assert not any(_HALL_A in w for w in turn.warnings)

    def test_token_tier_match_referenced_via_like_triggers_no_warning(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        distinctive_token = "اول"  # "اول"
        backend = _CountingRecordingBackend(
            responses=[f"SELECT TOP 10 Name FROM Customer WHERE Name LIKE N'%{distinctive_token}%'"],
        )
        engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        context = RetrievalContext(
            entities=["Ring"], filters={"Ring": _HALL_A},
            token_tier_filters={"Ring": (distinctive_token,)},
        )

        turn = _ask_with_context(engine, record, "قیمت اول چند بود", context)

        assert turn.error is None
        assert backend.calls == 1
        assert turn.warnings == []

    def test_token_tier_match_not_referenced_triggers_regeneration_then_warning(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        distinctive_token = "اول"  # "اول"
        backend = _CountingRecordingBackend(
            responses=["SELECT TOP 10 Name FROM Customer", "SELECT TOP 10 Name FROM Customer"],
        )
        engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        context = RetrievalContext(
            entities=["Ring"], filters={"Ring": _HALL_A},
            token_tier_filters={"Ring": (distinctive_token,)},
        )

        turn = _ask_with_context(engine, record, "قیمت اول چند بود", context)

        assert turn.error is None
        assert backend.calls == 2
        assert any(_HALL_A in w for w in turn.warnings)

    def test_numeric_filters_are_never_enforced(self):
        """PersianYear (or any non-string filter) is out of the filter-enforcement check's scope --
        a fixed SQL naming no year at all must never trigger a
        regeneration or a warning over it."""
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        backend = _CountingRecordingBackend(responses=["SELECT TOP 10 Name FROM Customer"])
        engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        context = RetrievalContext(entities=[], filters={"PersianYear": 1403})

        turn = _ask_with_context(engine, record, "چند مشتری داریم", context)

        assert turn.error is None
        assert backend.calls == 1
        assert turn.warnings == []


class TestResolvedValuesReachThePrompt:
    """Resolved-values pass-through: ``_handle_generative`` must pass
    ``context.resolved_values`` through when it builds the fresh-path
    ``RetrievalContext`` for the prompt, so the "RESOLVED WAREHOUSE
    VALUES" section is no longer always empty."""

    def test_resolved_values_populate_the_fenced_prompt_section(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        captured: list[str] = []

        class _RecordingBackend:
            name = "recording"

            def generate_with_meta_segments(self, segments):
                captured.append(segments.flatten())
                return (
                    f"SELECT TOP 10 Name FROM Customer WHERE Name = N'{_HALL_A}'",
                    {"raw": {}, "endpoint_status": 200, "attempts": 1},
                )

        engine = TurnEngine(
            router=LLMRouter(default_chain=[_RecordingBackend()]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        context = RetrievalContext(
            entities=["Ring"], filters={"Ring": _HALL_A}, resolved_values={"Ring": [_HALL_A]},
        )

        _ask_with_context(engine, record, "قیمت " + _HALL_A + " چند بود", context)

        assert len(captured) == 1
        # Without the resolved-values pass-through, RetrievalContext.resolved_values never reaches the
        # prompt builder on this path, so this section (and its own
        # untrusted-data instruction) would never render at all.
        assert UNTRUSTED_INSTRUCTION in captured[0]
        assert _HALL_A in captured[0]

    def test_no_resolved_values_leaves_the_section_empty(self):
        """Sanity check on the other side of the resolved-values pass-through contract: a question with
        nothing warehouse-resolved must not fabricate a fenced section."""
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        captured: list[str] = []

        class _RecordingBackend:
            name = "recording"

            def generate_with_meta_segments(self, segments):
                captured.append(segments.flatten())
                return "SELECT TOP 10 Name FROM Customer", {"raw": {}, "endpoint_status": 200, "attempts": 1}

        engine = TurnEngine(
            router=LLMRouter(default_chain=[_RecordingBackend()]),
            execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
        )
        context = RetrievalContext(entities=[], filters={})

        _ask_with_context(engine, record, "چند مشتری داریم", context)

        assert len(captured) == 1
        assert UNTRUSTED_INSTRUCTION not in captured[0]


class TestRouterAndExecuteSingletons:
    def test_get_router_builds_and_caches(self):
        engine_module._reset_router_for_testing(None)
        with patch("llm.router.LLMRouter.from_settings") as mock_from_settings:
            mock_from_settings.return_value = "sentinel-router"
            first = engine_module._get_router()
            second = engine_module._get_router()
        assert first == "sentinel-router"
        assert first is second
        mock_from_settings.assert_called_once()
        engine_module._reset_router_for_testing(None)

    def test_default_execute_delegates_to_database_executor(self):
        with patch("database.executor.execute_query") as mock_exec:
            mock_exec.return_value = pd.DataFrame({"X": [1]})
            df = engine_module._default_execute("SELECT 1")
        mock_exec.assert_called_once_with("SELECT 1")
        assert df.iloc[0]["X"] == 1


class TestOutOfScopeClassification:
    def test_out_of_scope_signal_is_classified_correctly(self):
        class _OutOfScopeBackend:
            name = "oos"

            def generate_with_meta_segments(self, segments):
                raise ValueError("OUT_OF_SCOPE")

        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(router=LLMRouter(default_chain=[_OutOfScopeBackend()]), execute_fn=lambda sql: pd.DataFrame())
        turn = engine.ask(record, "چیزی خارج از حوزه", SYSTEM_PROMPT)
        assert turn.error is not None
        assert turn.error.code == "OUT_OF_SCOPE"

    def test_timeout_signal_is_classified_correctly(self):
        class _TimeoutBackend:
            name = "slow"

            def generate_with_meta_segments(self, segments):
                raise TimeoutError("backend exceeded latency budget")

        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(router=LLMRouter(default_chain=[_TimeoutBackend()]), execute_fn=lambda sql: pd.DataFrame())
        turn = engine.ask(record, "چیزی", SYSTEM_PROMPT)
        assert turn.error is not None
        assert turn.error.code == "MODEL_TIMEOUT"
