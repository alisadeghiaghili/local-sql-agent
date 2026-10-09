# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Routing a question to one data source, and the single OUT_OF_SCOPE retry.

Covers, end to end through the real code of both engines:

* ``llm.source_routing`` itself (the shared helper);
* ``llm.sql_agent.SQLAgent`` and ``api.runner.run_query`` (``/query``,
  result and SQL-only modes), including the audit record's
  ``datasource_selection``;
* ``session.engine.TurnEngine`` (conversations), including session
  continuity and what the turn memory persists.

The model is a scripted backend that records the prompt it is shown, so
"the model saw only that source's tables" is checked on the prompt itself.
Sources are ``sales``, ``inventory`` and ``archive`` over whichever schema
is loaded (``tests/_source_fixtures``); the question text that steers the
selection is a keyword configured for the test.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd
import pytest

from api.errors import OutOfScopeError
from llm.base import LLMBackend
from llm.router import LLMRouter, PromptSegments, RouteResult
from llm.source_routing import (
    SourceRouting,
    choose_source,
    continuity_audit,
    generate_with_source_fallback,
    is_out_of_scope_failure,
)
from llm.sql_agent import SQLAgent
from observability.audit import AuditRecord
from prompt_engine.static_prefix import build_static_prefix, static_prefix_token_estimate
from retrieval.source_selector import SourceSelection
from session.engine import TurnEngine
from session.persistence import SessionPersistence
from session.store import SessionStore
from tests._source_fixtures import SOURCES, configured_sources, tables_of

SYSTEM_PROMPT = "You are a T-SQL expert."
GOOD_SQL = "SELECT TOP 10 * FROM [sales].[Customer]"
#: A statement the guard refuses outright (a write disguised as a read).
POLICY_SQL = "SELECT * INTO NewTbl FROM [sales].[Customer]"
KEYWORDS = {"inventory": ["stock level"], "archive": ["historical"], "sales": ["revenue"]}
INVENTORY_QUESTION = "show the stock level"
DF = pd.DataFrame({"Id": [1]})

#: The question that selects ``sales`` while the model sees ``sales``.
SALES_QUESTION = "show the revenue"


def _ok(sql: str) -> pd.DataFrame:
    return DF.copy()


class _Scripted(LLMBackend):
    """Answers each call with the next scripted item (SQL text, or an
    exception to raise) and records the segments it was shown."""

    name = "scripted:stub"

    def __init__(self, script, *, prompt_tokens: int = 100) -> None:
        self._script = list(script)
        self._prompt_tokens = prompt_tokens
        self.seen: list[PromptSegments] = []

    def generate(self, prompt: str) -> str:  # pragma: no cover - segment path only
        raise NotImplementedError

    def generate_with_meta_segments(self, segments: PromptSegments):
        self.seen.append(segments)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item, {"raw": {"usage": {"prompt_tokens": self._prompt_tokens, "completion_tokens": 5}},
                      "endpoint_status": 200, "attempts": 1}

    @property
    def calls(self) -> int:
        return len(self.seen)

    def source_of(self, index: int) -> str | None:
        """The source named in the prompt shown on call *index*."""
        text = self.seen[index].static_prefix or self.seen[index].question
        for source in SOURCES:
            if f"Data source: {source}" in text:
                return source
        return None


def _oos() -> ValueError:
    return ValueError("OUT_OF_SCOPE")


@pytest.fixture()
def sources():
    with configured_sources(keywords=KEYWORDS) as sets:
        yield sets


# ---------------------------------------------------------------------------
# llm.source_routing
# ---------------------------------------------------------------------------


class TestSourceRoutingHelper:
    def _routing(self, chosen="sales", candidates=("sales", "inventory", "archive")):
        built: list[str] = []

        def build(source: str) -> PromptSegments:
            built.append(source)
            return PromptSegments(static_prefix=f"prefix:{source}", question="q")

        routing = SourceRouting(SourceSelection(chosen, "keyword", candidates), build)
        return routing, built

    def _result(self, text="SELECT 1") -> RouteResult:
        return RouteResult(text=text, structured=None, meta={}, provider="p", fallback_used=False)

    def test_one_source_means_no_routing(self):
        assert SourceRouting.plan("q", object(), build=lambda s: PromptSegments()) is None
        assert choose_source("q", object()) is None
        assert continuity_audit("sales") is None

    def test_plan_selects_and_builds_for_the_chosen_source(self, sources):
        built = []
        routing = SourceRouting.plan(
            INVENTORY_QUESTION, object(),
            build=lambda s: built.append(s) or PromptSegments(question=s),
        )
        assert routing.current == "inventory"
        assert routing.selection.reason == "keyword"
        assert built == ["inventory"]

    def test_a_follow_up_starts_from_the_previous_source(self, sources):
        routing = SourceRouting.plan(
            "and last year?", object(), build=lambda s: PromptSegments(question=s),
            previous_source="archive",
        )
        assert (routing.current, routing.selection.reason) == ("archive", "session")

    def test_out_of_scope_retries_exactly_once_on_the_next_candidate(self):
        routing, built = self._routing()
        calls = []

        def generate(segments):
            calls.append(segments.static_prefix)
            raise _oos()

        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            generate_with_source_fallback(generate, routing, routing.segments)
        assert calls == ["prefix:sales", "prefix:inventory"]  # two attempts, never three
        assert built == ["sales", "inventory"]
        assert routing.audit()["fallback_from"] == "sales"
        assert routing.audit()["chosen"] == "inventory"
        assert not routing.can_fall_back()

    def test_a_second_decline_is_raised_unchanged(self):
        routing, _ = self._routing()
        second = _oos()
        raises = iter([_oos(), second])

        def generate(segments):
            raise next(raises)

        with pytest.raises(ValueError) as caught:
            generate_with_source_fallback(generate, routing, routing.segments)
        assert caught.value is second

    def test_other_failures_are_not_retried(self):
        routing, built = self._routing()

        def generate(segments):
            raise RuntimeError("connection refused")

        with pytest.raises(RuntimeError, match="connection refused"):
            generate_with_source_fallback(generate, routing, routing.segments)
        assert built == ["sales"]
        assert routing.audit()["fallback_from"] is None

    def test_a_wrapped_decline_counts_as_a_decline(self):
        routing, _ = self._routing()
        wrapper = RuntimeError("Every backend in the chain failed")
        wrapper.__cause__ = _oos()
        calls = []

        def generate(segments):
            calls.append(segments.static_prefix)
            if len(calls) == 1:
                raise wrapper
            return self._result()

        result, replaced = generate_with_source_fallback(generate, routing, routing.segments)
        assert result.text == "SELECT 1"
        assert replaced.static_prefix == "prefix:inventory"
        assert is_out_of_scope_failure(wrapper)

    def test_the_last_candidate_has_nothing_to_fall_back_to(self):
        routing, built = self._routing(chosen="archive", candidates=("archive",))
        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            generate_with_source_fallback(
                lambda s: (_ for _ in ()).throw(_oos()), routing, routing.segments,
            )
        assert built == ["archive"]

    def test_success_on_the_first_attempt_returns_no_replacement(self):
        routing, _ = self._routing()
        result, replaced = generate_with_source_fallback(
            lambda s: self._result(), routing, routing.segments,
        )
        assert replaced is None
        assert routing.audit()["fallback_from"] is None

    def test_no_routing_calls_the_model_once_and_never_retries(self):
        calls = []

        def generate(segments):
            calls.append(1)
            raise _oos()

        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            generate_with_source_fallback(generate, None, PromptSegments(question="q"))
        assert calls == [1]

    def test_fall_back_without_a_candidate_is_an_error(self):
        routing, _ = self._routing(chosen="archive", candidates=("archive",))
        with pytest.raises(RuntimeError, match="no data source left"):
            routing.fall_back()

    def test_continuity_names_only_the_previous_source(self, sources):
        assert continuity_audit("inventory") == {
            "chosen": "inventory", "reason": "session",
            "candidates": ["inventory"], "fallback_from": None,
        }
        assert continuity_audit(None) is None


# ---------------------------------------------------------------------------
# SQLAgent (the /query engine)
# ---------------------------------------------------------------------------


class TestSqlAgentRoutesAndRetriesOnce:
    def _run(self, script, question=INVENTORY_QUESTION, max_corrections=2):
        backend = _Scripted(script)
        agent = SQLAgent(backend=backend, execute_fn=_ok, max_corrections=max_corrections)
        return backend, agent

    def test_the_model_sees_only_the_chosen_sources_prompt(self, sources):
        backend, agent = self._run([GOOD_SQL])
        df, result = agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert backend.calls == 1
        assert backend.seen[0].static_prefix == build_static_prefix(SYSTEM_PROMPT, "inventory")
        shown = [row[7:] for row in backend.seen[0].static_prefix.splitlines() if row.startswith("Table: ")]
        assert shown == tables_of("inventory", sources)
        assert result.datasource_selection == {
            "chosen": "inventory", "reason": "keyword",
            "candidates": result.datasource_selection["candidates"], "fallback_from": None,
        }
        assert result.datasource_selection["candidates"][0] == "inventory"
        assert sorted(result.datasource_selection["candidates"]) == sorted(SOURCES)

    def test_out_of_scope_retries_once_with_the_next_candidate(self, sources):
        backend, agent = self._run([_oos(), GOOD_SQL])
        df, result = agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)

        assert backend.calls == 2
        selection = result.datasource_selection
        second = selection["candidates"][1]
        assert backend.seen[0].static_prefix == build_static_prefix(SYSTEM_PROMPT, "inventory")
        assert backend.seen[1].static_prefix == build_static_prefix(SYSTEM_PROMPT, second)
        assert selection["chosen"] == second
        assert selection["fallback_from"] == "inventory"
        assert result.sql == GOOD_SQL

    def test_a_second_out_of_scope_is_returned_as_before_after_exactly_two_calls(self, sources):
        backend, agent = self._run([_oos(), _oos(), GOOD_SQL, GOOD_SQL])
        with pytest.raises(ValueError, match="OUT_OF_SCOPE") as caught:
            agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert backend.calls == 2  # three sources, but one extra attempt at most
        selection = caught.value.datasource_selection
        assert selection["fallback_from"] == "inventory"
        assert selection["chosen"] == selection["candidates"][1]

    def test_the_retry_does_not_use_up_the_correction_budget(self, sources):
        backend, agent = self._run([_oos(), "not sql at all", "still not sql", GOOD_SQL], max_corrections=2)
        df, result = agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert backend.calls == 4  # 1 declined + 3 attempts (1 + 2 corrections)
        assert result.attempt == 3

    def test_with_no_corrections_allowed_the_retry_still_happens(self, sources):
        backend, agent = self._run([_oos(), GOOD_SQL], max_corrections=0)
        df, result = agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert backend.calls == 2 and result.sql == GOOD_SQL

    def test_corrections_keep_the_same_sources_prefix(self, sources):
        backend, agent = self._run(["not sql at all", "also not sql", GOOD_SQL])
        df, result = agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        prefixes = [s.static_prefix for s in backend.seen]
        assert backend.calls == 3
        assert len(set(prefixes)) == 1
        assert prefixes[0] == build_static_prefix(SYSTEM_PROMPT, "inventory")
        assert "not sql at all" not in prefixes[1]
        assert "not sql at all" in backend.seen[1].question
        assert result.datasource_selection["fallback_from"] is None

    def test_a_decline_after_a_correction_drops_the_correction_history(self, sources):
        backend, agent = self._run(["not sql at all", _oos(), GOOD_SQL])
        df, result = agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert backend.calls == 3
        retry = backend.seen[2]
        assert "not sql at all" not in retry.question  # described the other source
        assert retry.static_prefix == build_static_prefix(
            SYSTEM_PROMPT, result.datasource_selection["chosen"],
        )
        assert result.datasource_selection["fallback_from"] == "inventory"
        assert result.correction_prompts == []

    def test_corrections_after_the_retry_keep_the_new_sources_prefix(self, sources):
        backend, agent = self._run([_oos(), "not sql at all", GOOD_SQL])
        df, result = agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert backend.calls == 3
        assert backend.seen[1].static_prefix == backend.seen[2].static_prefix
        assert backend.seen[1].static_prefix != backend.seen[0].static_prefix
        assert "not sql at all" in backend.seen[2].question
        assert result.attempt == 2

    def test_other_model_failures_are_not_retried_on_another_source(self, sources):
        backend, agent = self._run([RuntimeError("connection refused"), GOOD_SQL])
        with pytest.raises(RuntimeError):
            agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert backend.calls == 1

    def test_a_policy_rejection_is_not_retried_on_another_source(self, sources):
        backend, agent = self._run([POLICY_SQL, GOOD_SQL])
        with pytest.raises(ValueError):
            agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert backend.calls == 1

    def test_the_selection_rides_on_a_raised_exception_too(self, sources):
        backend, agent = self._run([POLICY_SQL])
        with pytest.raises(ValueError) as caught:
            agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert caught.value.datasource_selection["chosen"] == "inventory"

    def test_the_guard_still_decides_what_runs_where(self, sources):
        # The model was shown ``archive`` but wrote SQL over a table that
        # lives elsewhere: the selection decides what the model SEES, the
        # guard and router decide the rest. No new refusal.
        archive_tables = tables_of("archive", sources)
        assert "Customer" not in archive_tables
        backend, agent = self._run([GOOD_SQL])
        df, result = agent.run("show the historical data", SYSTEM_PROMPT)
        assert result.datasource_selection["chosen"] == "archive"
        assert result.sql == GOOD_SQL
        assert backend.calls == 1

    def test_with_one_source_nothing_is_routed_or_retried(self):
        backend, agent = self._run([_oos(), GOOD_SQL])
        with pytest.raises(ValueError, match="OUT_OF_SCOPE") as caught:
            agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert backend.calls == 1
        assert not hasattr(caught.value, "datasource_selection")

    def test_with_one_source_the_prompt_is_the_whole_schema_and_the_result_has_no_selection(self):
        backend, agent = self._run([GOOD_SQL])
        df, result = agent.run(INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert backend.seen[0].static_prefix == build_static_prefix(SYSTEM_PROMPT)
        assert result.datasource_selection is None


# ---------------------------------------------------------------------------
# api.runner.run_query: the audit record
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_file(tmp_path):
    path = tmp_path / "audit_log.jsonl"
    with patch("observability.audit._AUDIT_LOG_FILE", str(path)):
        yield path


@pytest.fixture(autouse=True)
def _clear_query_cache():
    from api.query_cache import query_cache

    query_cache.clear()
    yield
    query_cache.clear()


def _records(path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _run_query(script, question=INVENTORY_QUESTION, **kwargs):
    from api import runner

    backend = _Scripted(script)
    agent = SQLAgent(backend=backend, execute_fn=_ok, max_corrections=0)
    with patch.object(runner, "agent", agent):
        try:
            response = runner.run_query(question, SYSTEM_PROMPT, **kwargs)
        except Exception as exc:  # noqa: BLE001 - returned for the test to inspect
            return backend, exc
    return backend, response


class TestRunnerAuditsTheSelection:
    def test_result_mode_records_the_selection(self, sources, audit_file):
        backend, response = _run_query([GOOD_SQL], mode="full")
        assert response.sql == GOOD_SQL
        (record,) = _records(audit_file)
        selection = record["datasource_selection"]
        assert selection["chosen"] == "inventory"
        assert selection["reason"] == "keyword"
        assert selection["fallback_from"] is None
        assert selection["candidates"][0] == "inventory"
        # The statement's own routing is a separate, authoritative field.
        assert record["datasource"] == "default"

    def test_the_retry_is_recorded(self, sources, audit_file):
        backend, response = _run_query([_oos(), GOOD_SQL], mode="full")
        assert backend.calls == 2
        (record,) = _records(audit_file)
        selection = record["datasource_selection"]
        assert selection["fallback_from"] == "inventory"
        assert selection["chosen"] == selection["candidates"][1]

    def test_a_second_decline_is_out_of_scope_and_still_audited_with_the_selection(self, sources, audit_file):
        backend, exc = _run_query([_oos(), _oos()], mode="full")
        assert isinstance(exc, OutOfScopeError)
        assert backend.calls == 2
        (record,) = _records(audit_file)
        assert record["error_code"] == "OUT_OF_SCOPE"
        assert record["datasource_selection"]["fallback_from"] == "inventory"

    def test_a_guard_rejection_is_audited_with_the_selection(self, sources, audit_file):
        backend, exc = _run_query([POLICY_SQL], mode="full")
        assert exc.__class__.__name__ == "ForbiddenSQLError"
        (record,) = _records(audit_file)
        assert record["datasource_selection"]["chosen"] == "inventory"

    def test_sql_only_mode_routes_and_retries_too(self, sources, audit_file):
        backend, response = _run_query([_oos(), GOOD_SQL], mode="sql")
        assert response.sql == GOOD_SQL
        assert backend.calls == 2
        assert backend.seen[0].static_prefix == build_static_prefix(SYSTEM_PROMPT, "inventory")
        assert backend.seen[1].static_prefix != backend.seen[0].static_prefix
        (record,) = _records(audit_file)
        assert record["datasource_selection"]["fallback_from"] == "inventory"

    def test_sql_only_mode_second_decline(self, sources, audit_file):
        backend, exc = _run_query([_oos(), _oos(), GOOD_SQL], mode="sql")
        assert isinstance(exc, OutOfScopeError)
        assert backend.calls == 2
        (record,) = _records(audit_file)
        assert record["error_code"] == "OUT_OF_SCOPE"
        assert record["datasource_selection"]["fallback_from"] == "inventory"

    def test_a_single_source_deployment_records_no_selection(self, audit_file):
        backend, response = _run_query([GOOD_SQL], mode="full")
        assert response.sql == GOOD_SQL
        (record,) = _records(audit_file)
        assert record["datasource_selection"] is None

    def test_a_single_source_deployment_does_not_retry_out_of_scope(self, audit_file):
        backend, exc = _run_query([_oos(), GOOD_SQL], mode="full")
        assert isinstance(exc, OutOfScopeError)
        assert backend.calls == 1
        (record,) = _records(audit_file)
        assert record["datasource_selection"] is None

    def test_a_cache_hit_runs_no_selection(self, sources, audit_file):
        _run_query([GOOD_SQL], mode="full")
        backend, response = _run_query([GOOD_SQL], mode="full")
        assert backend.calls == 0
        first, second = _records(audit_file)
        assert first["datasource_selection"]["chosen"] == "inventory"
        assert second["tier"] == "T0"
        assert second["datasource_selection"] is None

    def test_the_cache_key_prefix_version_does_not_depend_on_the_source(self, sources):
        from prompt_engine.static_prefix import prefix_version

        before = prefix_version(SYSTEM_PROMPT)
        build_static_prefix(SYSTEM_PROMPT, "inventory")
        assert prefix_version(SYSTEM_PROMPT) == before

    def test_prefix_tokens_follow_the_chosen_source(self, sources):
        from api.runner import _prefix_tokens_for

        whole = static_prefix_token_estimate(SYSTEM_PROMPT)
        assert _prefix_tokens_for(SYSTEM_PROMPT, None, whole) == whole
        for source in SOURCES:
            assert _prefix_tokens_for(SYSTEM_PROMPT, {"chosen": source}, whole) == (
                static_prefix_token_estimate(SYSTEM_PROMPT, source)
            )

    def test_the_audit_cache_hit_flag_is_measured_against_the_chosen_sources_prefix(
        self, sources, audit_file,
    ):
        # prefix_cache_hit is "prompt_tokens < static_prefix_tokens * 0.5".
        # Pick a prompt size that is below that line for the biggest source's
        # prefix but above it for the smallest's: the flag then says which
        # prefix the request was measured against.
        questions = {
            "sales": SALES_QUESTION,
            "inventory": INVENTORY_QUESTION,
            "archive": "show the historical data",
        }
        estimates = {s: static_prefix_token_estimate(SYSTEM_PROMPT, s) for s in SOURCES}
        big, small = max(estimates, key=estimates.get), min(estimates, key=estimates.get)
        prompt_tokens = int(estimates[big] * 0.5) - 1
        assert prompt_tokens >= estimates[small] * 0.5

        flags = {}
        for source in (big, small):
            backend = _Scripted([GOOD_SQL], prompt_tokens=prompt_tokens)
            agent = SQLAgent(backend=backend, execute_fn=_ok, max_corrections=0)
            from api import runner
            from api.query_cache import query_cache

            query_cache.clear()
            with patch.object(runner, "agent", agent):
                response = runner.run_query(questions[source], SYSTEM_PROMPT, mode="full")
            flags[source] = response.llm["prefix_cache_hit"]
        assert flags == {big: True, small: False}


class TestAuditRecordField:
    def test_the_field_is_optional_and_additive(self):
        from datetime import datetime

        record = AuditRecord(
            timestamp=datetime(2026, 1, 1), request_id="r", question="q",
            generated_sql="SELECT 1", guard={"verdict": "allowed"},
        )
        assert record.datasource_selection is None
        assert record.as_dict()["datasource_selection"] is None

    def test_the_block_is_written_as_given(self):
        from datetime import datetime

        block = {"chosen": "sales", "reason": "keyword", "candidates": ["sales", "inventory"],
                 "fallback_from": None}
        record = AuditRecord(
            timestamp=datetime(2026, 1, 1), request_id="r", question="q",
            generated_sql="SELECT 1", guard={"verdict": "allowed"}, datasource_selection=block,
        )
        assert json.loads(json.dumps(record.as_dict()))["datasource_selection"] == block


# ---------------------------------------------------------------------------
# session.engine.TurnEngine (conversations)
# ---------------------------------------------------------------------------

FOLLOW_UP = "همین را برای سال قبل"
CTE_FOLLOW_UP = "از بین آن‌ها ۱۰ مشتری برتر"


@pytest.fixture()
def captured_audit():
    records: list[AuditRecord] = []
    with patch("session.engine.save_audit_record", records.append):
        yield records


def _engine(script, max_corrections=2):
    backend = _Scripted(script)
    engine = TurnEngine(
        router=LLMRouter(default_chain=[backend]), execute_fn=_ok, max_corrections=max_corrections,
    )
    return backend, engine


def _record(store=None):
    return (store or SessionStore(ttl_seconds=60, max_size=10, max_turns=10)).create()


class TestTurnEngineRoutesAndRetriesOnce:
    def test_the_prompt_is_the_chosen_sources(self, sources, captured_audit):
        backend, engine = _engine([GOOD_SQL])
        turn = engine.ask(_record(), INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert turn.error is None
        assert backend.seen[0].static_prefix == build_static_prefix(SYSTEM_PROMPT, "inventory")
        (audit,) = captured_audit
        assert audit.datasource_selection["chosen"] == "inventory"
        assert audit.datasource_selection["reason"] == "keyword"
        assert audit.datasource_selection["fallback_from"] is None

    def test_out_of_scope_retries_once_and_the_answer_is_the_retrys(self, sources, captured_audit):
        backend, engine = _engine([_oos(), GOOD_SQL])
        turn = engine.ask(_record(), INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert turn.error is None and turn.sql
        assert backend.calls == 2
        (audit,) = captured_audit
        selection = audit.datasource_selection
        assert selection["fallback_from"] == "inventory"
        assert backend.seen[1].static_prefix == build_static_prefix(SYSTEM_PROMPT, selection["chosen"])

    def test_a_second_decline_is_out_of_scope_after_exactly_two_calls(self, sources, captured_audit):
        backend, engine = _engine([_oos(), _oos(), GOOD_SQL, GOOD_SQL])
        turn = engine.ask(_record(), INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert turn.error is not None and turn.error.code == "OUT_OF_SCOPE"
        assert backend.calls == 2
        (audit,) = captured_audit
        assert audit.error_code == "OUT_OF_SCOPE"
        assert audit.datasource_selection["fallback_from"] == "inventory"

    def test_corrections_keep_the_prefix_and_the_retry_resets_them(self, sources, captured_audit):
        backend, engine = _engine(["not sql at all", _oos(), GOOD_SQL])
        turn = engine.ask(_record(), INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert turn.error is None
        assert backend.seen[0].static_prefix == backend.seen[1].static_prefix
        assert "not sql at all" in backend.seen[1].question
        assert backend.seen[2].static_prefix != backend.seen[1].static_prefix
        assert "not sql at all" not in backend.seen[2].question

    def test_one_source_never_retries_and_records_no_selection(self, captured_audit):
        backend, engine = _engine([_oos(), GOOD_SQL])
        turn = engine.ask(_record(), INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert turn.error.code == "OUT_OF_SCOPE"
        assert backend.calls == 1
        (audit,) = captured_audit
        assert audit.datasource_selection is None
        assert backend.seen[0].static_prefix == build_static_prefix(SYSTEM_PROMPT)

    def test_one_source_records_no_source_in_the_turn_memory(self, captured_audit):
        backend, engine = _engine([GOOD_SQL])
        record = _record()
        turn = engine.ask(record, INVENTORY_QUESTION, SYSTEM_PROMPT)
        assert record.memory[turn.turn_id].datasource is None


class TestSessionContinuity:
    def _first_turn(self, record, captured_audit):
        backend, engine = _engine([GOOD_SQL, GOOD_SQL, GOOD_SQL])
        first = engine.ask(record, "show the historical data", SYSTEM_PROMPT)
        assert first.error is None
        return backend, engine, first

    def test_the_turn_memory_keeps_the_source_the_turn_was_answered_from(self, sources, captured_audit):
        record = _record()
        backend, engine, first = self._first_turn(record, captured_audit)
        assert record.memory[first.turn_id].datasource == "archive"

    def test_a_follow_up_stays_on_the_previous_source(self, sources, captured_audit):
        record = _record()
        backend, engine, first = self._first_turn(record, captured_audit)
        second = engine.ask(record, FOLLOW_UP, SYSTEM_PROMPT)
        assert second.basis.kind == "refines"
        assert backend.seen[1].static_prefix == build_static_prefix(SYSTEM_PROMPT, "archive")
        selection = captured_audit[-1].datasource_selection
        assert (selection["chosen"], selection["reason"]) == ("archive", "session")

    def test_a_keyword_for_another_source_beats_continuity(self, sources, captured_audit):
        record = _record()
        backend, engine, first = self._first_turn(record, captured_audit)
        engine.ask(record, FOLLOW_UP + " stock level", SYSTEM_PROMPT)
        selection = captured_audit[-1].datasource_selection
        assert (selection["chosen"], selection["reason"]) == ("inventory", "keyword")

    def test_a_fresh_question_is_not_a_follow_up(self, sources, captured_audit):
        record = _record()
        backend, engine, first = self._first_turn(record, captured_audit)
        turn = engine.ask(record, "something unrelated to anything", SYSTEM_PROMPT)
        assert turn.basis.kind == "fresh"
        assert captured_audit[-1].datasource_selection["reason"] != "session"

    def test_a_cte_refinement_stays_on_the_previous_source_with_no_selection_of_its_own(
        self, sources, captured_audit,
    ):
        backend, engine = _engine([GOOD_SQL, "SELECT TOP 10 c_Name FROM _prev"])
        record = _record()
        engine.ask(record, "show the stock level", SYSTEM_PROMPT)
        with patch("session.engine.check_scan_truncated", return_value=False):
            second = engine.ask(record, CTE_FOLLOW_UP, SYSTEM_PROMPT)
        assert second.basis.composition == "cte"
        assert captured_audit[-1].datasource_selection == {
            "chosen": "inventory", "reason": "session",
            "candidates": ["inventory"], "fallback_from": None,
        }
        assert record.memory[second.turn_id].datasource == "inventory"

    def test_a_turn_stored_before_the_field_existed_continues_from_its_sql(self, sources):
        from session.engine import _previous_turn_source
        from session.models import ResultColumn, Turn, TurnResult
        from session.store import TurnMemory

        turn = Turn(
            turn_id="t_old", session_id="s", index=1, question="q", sql=GOOD_SQL,
            result=TurnResult(columns=[ResultColumn(name="Id", type="number")], row_count=1),
        )
        with patch("session.engine.target_datasource_or_none", return_value="archive") as routed:
            # The memory names a source: it wins, the SQL is not parsed.
            assert _previous_turn_source(turn, TurnMemory(turn_id="t_old", datasource="inventory")) == "inventory"
            routed.assert_not_called()
            # Stored before the field existed: the source its SQL routes to.
            assert _previous_turn_source(turn, TurnMemory(turn_id="t_old", sql=GOOD_SQL)) == "archive"
            assert _previous_turn_source(turn, None) == "archive"
            # No previous turn, or one that produced no SQL: nothing to continue.
            assert _previous_turn_source(None, None) is None
            assert _previous_turn_source(turn.model_copy(update={"sql": None}), None) is None

    def test_with_one_source_the_previous_source_is_never_looked_up(self):
        from session.engine import _previous_turn_source
        from session.models import Turn
        from session.store import TurnMemory

        turn = Turn(turn_id="t_old", session_id="s", index=1, question="q", sql=GOOD_SQL)
        with patch("session.engine.target_datasource_or_none") as routed:
            assert _previous_turn_source(turn, TurnMemory(turn_id="t_old", datasource="x")) is None
            routed.assert_not_called()

    def test_the_source_survives_persistence(self, sources, captured_audit, tmp_path):
        persistence = SessionPersistence(str(tmp_path / "sessions.db"))
        store = SessionStore(
            ttl_seconds=0.05, max_size=10, max_turns=10,
            persistence=persistence, retention_days=30,
        )
        record = store.create(owner_id="analyst-1")
        backend, engine = _engine([GOOD_SQL, GOOD_SQL])
        first = engine.ask(record, "show the historical data", SYSTEM_PROMPT)
        store.sync_turn(record, first)

        turns, memories = persistence.load_turns(record.session_id)
        assert memories[first.turn_id].datasource == "archive"

        # ...so a conversation reopened later still continues from it.
        import time

        time.sleep(0.15)
        reopened = store.get(record.session_id)
        engine.ask(reopened, FOLLOW_UP, SYSTEM_PROMPT)
        assert captured_audit[-1].datasource_selection["reason"] == "session"
        assert captured_audit[-1].datasource_selection["chosen"] == "archive"

    def test_a_single_source_turn_stores_its_memory_exactly_as_before(self, tmp_path):
        persistence = SessionPersistence(str(tmp_path / "sessions.db"))
        store = SessionStore(
            ttl_seconds=60, max_size=10, max_turns=10, persistence=persistence, retention_days=30,
        )
        record = store.create(owner_id="analyst-1")
        backend, engine = _engine([GOOD_SQL])
        with patch("session.engine.save_audit_record"):
            turn = engine.ask(record, INVENTORY_QUESTION, SYSTEM_PROMPT)
        store.sync_turn(record, turn)
        import sqlite3

        with sqlite3.connect(str(tmp_path / "sessions.db")) as conn:
            (memory_json,) = conn.execute("SELECT memory_json FROM turns").fetchone()
        assert set(json.loads(memory_json)) == {
            "filters", "result_columns", "sql", "injected_top", "row_count",
        }
