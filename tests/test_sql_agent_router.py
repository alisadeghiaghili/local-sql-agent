"""SQLAgent's SQL-generation hot path routed through llm.router.LLMRouter.

Closes the Phase 2 debt: "the LLM router is built, tested, and routes no
production traffic." Before this, ``SQLAgent`` hardwired
a hardwired ``LLMBackend`` and called
``LLMBackend.generate_with_meta(prompt)`` with a single flat prompt string
(``llm/sql_agent.py``, previously lines 139-143 and 254) -- bypassing
``llm.router.LLMRouter``'s task-based routing, fallback chains, per-task
budgets, and remote-provider governance entirely, and making
provider-side prefix caching (the whole point of
``llm.router.PromptSegments``) unreachable the moment a non-Ollama
provider was configured.

Exit criteria this file is directly responsible for
----------------------------------------------------
1. Prefix invariance across correction rounds -- the criterion most
   likely to silently regress: :class:`TestPrefixInvarianceAcrossCorrections`.
2. Three different questions share a byte-identical static prefix:
   :class:`TestPrefixInvarianceAcrossQuestions`.
3. A second provider (mock) serves SQL generation with no call-site
   change: :class:`TestSecondProviderServesSqlGeneration`.
4. Fallback chain fires on primary failure, and ``fallback_used`` appears
   in the llm status (``SQLGenerationResult.llm_meta``):
   :class:`TestFallbackChain`.
6. Determinism preserved: same question twice -> byte-identical SQL:
   :class:`TestDeterminism`.

:class:`TestConstructorRouting` covers the two new (additive,
backward-compatible) construction paths -- ``router=`` given directly,
and neither ``backend=`` nor ``router=`` given (falls back to
``LLMRouter.from_settings()``, exactly mirroring the pre-refactor default
of constructing a bare backend directly).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import config as cfg
from llm.base import LLMBackend
from llm.providers import MockBackend
from llm.router import LLMRouter, PromptSegments, TaskType
from llm.sql_agent import SQLAgent
from prompt_engine.static_prefix import build_static_prefix

SYSTEM_PROMPT = "You are a T-SQL expert for the Auction domain."
GOOD_SQL = "SELECT TOP 10 * FROM [Auction_Dim].[Customer]"
SIMPLE_DF = pd.DataFrame({"Id": [1]})


def _ok_execute(sql: str) -> pd.DataFrame:
    return SIMPLE_DF.copy()


def _common_prefix_len(a: bytes, b: bytes) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


class _SegmentSpyBackend(LLMBackend):
    """Records every ``PromptSegments`` it is asked to answer, replaying a
    scripted sequence of raw SQL responses -- one per call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.received_segments: list[PromptSegments] = []

    @property
    def name(self) -> str:
        return "spy:stub"

    def generate(self, prompt: str) -> str:  # pragma: no cover - unused; the segment path is exercised instead
        raise NotImplementedError

    def generate_with_meta_segments(self, segments: PromptSegments):  # noqa: ANN001
        self.received_segments.append(segments)
        return next(self._responses), {}


class _FailingBackend(LLMBackend):
    """A backend whose every call raises, to exercise the fallback chain."""

    name = "failing:stub"

    def generate(self, prompt: str) -> str:
        raise RuntimeError("simulated backend failure")


class _OutOfScopeBackend(LLMBackend):
    """A backend that DECLINES -- the terminal ``OUT_OF_SCOPE`` signal.

    Mirrors ``OpenAIBackend``, which attaches an ``llm_meta`` attribute to
    the raised ``ValueError`` so a caller further up (``api/runner.py``)
    can still recover call metadata for the audit trail.
    """

    name = "out-of-scope:stub"

    def __init__(self) -> None:
        self.llm_meta = {"endpoint_status": 200, "attempts": 1}
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        exc = ValueError("OUT_OF_SCOPE")
        exc.llm_meta = self.llm_meta
        raise exc


# ---------------------------------------------------------------------------
# Exit criterion 1 -- the one the debt calls out as silently regressing
# ---------------------------------------------------------------------------


class TestPrefixInvarianceAcrossCorrections:
    """Correction text must land in the *question* segment, never
    ``static_prefix`` -- leaking it forward would silently defeat KV-cache
    reuse on every retry (see ``llm/sql_agent.py``'s module docstring)."""

    def test_static_prefix_is_byte_identical_across_every_correction_round(self):
        spy = _SegmentSpyBackend(
            ["not sql at all", "also not sql", GOOD_SQL]
        )
        agent = SQLAgent(backend=spy, execute_fn=_ok_execute, max_corrections=2)

        df, result = agent.run("چند مشتری فعال داریم؟", system_prompt=SYSTEM_PROMPT)

        # Three rounds: two validation failures, then success.
        assert len(spy.received_segments) == 3
        assert result.attempt == 3
        assert result.sql == GOOD_SQL

        prefixes = [s.static_prefix for s in spy.received_segments]
        assert all(p == prefixes[0] for p in prefixes), (
            "static_prefix must be byte-identical across every correction round"
        )
        assert prefixes[0] == build_static_prefix(SYSTEM_PROMPT)
        assert len(prefixes[0].encode("utf-8")) > 0

        # The trap: correction text (the failed SQL / error text) must
        # appear only in the *question* segment of later rounds, never in
        # static_prefix.
        round2, round3 = spy.received_segments[1], spy.received_segments[2]
        assert "not sql at all" not in round2.static_prefix
        assert "not sql at all" in round2.question
        assert "also not sql" not in round3.static_prefix
        assert "also not sql" in round3.question
        # Every prior round's correction text accumulates in later rounds.
        assert "not sql at all" in round3.question

    def test_session_context_is_also_preserved_unchanged_across_rounds(self):
        """static_prefix is the one that must never move, but
        session_context (empty for SQLAgent.run today -- it has no session
        parameter) should be equally stable across rounds for the same
        reason."""
        spy = _SegmentSpyBackend(["still not sql", GOOD_SQL])
        agent = SQLAgent(backend=spy, execute_fn=_ok_execute, max_corrections=1)

        agent.run("q", system_prompt=SYSTEM_PROMPT)

        session_contexts = [s.session_context for s in spy.received_segments]
        assert all(sc == "" for sc in session_contexts)


# ---------------------------------------------------------------------------
# Exit criterion 2
# ---------------------------------------------------------------------------


class TestPrefixInvarianceAcrossQuestions:
    """Three different questions (two Persian, one English) share a
    byte-identical static prefix against today's real, production
    ``prompts/system_prompt.md`` and full knowledge base -- not a stub."""

    def test_three_questions_share_the_static_prefix(self):
        system_prompt = Path("prompts/system_prompt.md").read_text(encoding="utf-8")
        questions = [
            "چند مشتری فعال داریم؟",
            "میانگین قیمت معاملات چقدر است؟",
            "How many active customers are there?",
        ]
        spy = _SegmentSpyBackend([GOOD_SQL, GOOD_SQL, GOOD_SQL])
        agent = SQLAgent(backend=spy, execute_fn=_ok_execute)

        for q in questions:
            agent.run(q, system_prompt=system_prompt)

        assert len(spy.received_segments) == 3
        prefixes = [s.static_prefix for s in spy.received_segments]
        assert all(p == prefixes[0] for p in prefixes)
        prefix_bytes = len(prefixes[0].encode("utf-8"))
        assert prefix_bytes == len(build_static_prefix(system_prompt).encode("utf-8"))

        flattened = [s.flatten().encode("utf-8") for s in spy.received_segments]
        shared_01 = _common_prefix_len(flattened[0], flattened[1])
        shared_02 = _common_prefix_len(flattened[0], flattened[2])
        # At minimum, the static prefix itself is shared leading bytes.
        assert shared_01 >= prefix_bytes
        assert shared_02 >= prefix_bytes


# ---------------------------------------------------------------------------
# Exit criterion 3
# ---------------------------------------------------------------------------


class TestSecondProviderServesSqlGeneration:
    """A second provider (mock) answers SQL generation with NO change at
    the call site -- only the constructor argument differs from any other
    ``SQLAgent(backend=..., execute_fn=...)`` in this suite."""

    def test_mock_backend_answers_through_sql_agent(self):
        backend = MockBackend(response=GOOD_SQL)
        agent = SQLAgent(backend=backend, execute_fn=_ok_execute)

        df, result = agent.run("q", system_prompt=SYSTEM_PROMPT)

        assert result.sql == GOOD_SQL
        assert result.llm_meta["provider"] == "mock:stub"
        assert result.llm_meta["fallback_used"] is False


# ---------------------------------------------------------------------------
# Exit criterion 4
# ---------------------------------------------------------------------------


class TestFallbackChain:
    """``_FailingBackend`` and ``MockBackend`` are both recognised as local
    (neither is in ``llm.router._REMOTE_BACKENDS``), so no
    ``llm_allow_remote`` opt-in is needed here -- this is purely about
    fallback-on-failure, not governance (see ``tests/test_llm_router.py``'s
    own ``TestFallbackChain`` for that same split)."""

    def test_fallback_fires_and_is_recorded_in_llm_meta(self):
        primary = _FailingBackend()
        secondary = MockBackend(response=GOOD_SQL)
        router = LLMRouter(default_chain=[primary, secondary])
        agent = SQLAgent(router=router, execute_fn=_ok_execute)

        df, result = agent.run("q", system_prompt=SYSTEM_PROMPT)

        assert result.sql == GOOD_SQL
        assert result.llm_meta["fallback_used"] is True
        assert result.llm_meta["provider"] == "mock:stub"

    def test_out_of_scope_is_never_overridden_by_a_later_backend(self):
        """A decline is a terminal domain decision, not a backend failure.

        With a multi-entry chain the router must NOT move on: letting the
        second backend answer would silently discard the first model's
        correct refusal and hand the user SQL for a question already judged
        outside the Auction domain.
        """
        declining = _OutOfScopeBackend()
        answering = MockBackend(response=GOOD_SQL)
        router = LLMRouter(default_chain=[declining, answering])
        agent = SQLAgent(router=router, execute_fn=_ok_execute, max_corrections=0)

        with pytest.raises(ValueError) as exc_info:
            agent.run("q", system_prompt=SYSTEM_PROMPT)

        assert str(exc_info.value) == "OUT_OF_SCOPE"
        assert declining.calls == 1

    def test_out_of_scope_reaches_run_caller_unwrapped_with_its_llm_meta(self):
        """``run`` unwraps with ``exc.__cause__ or exc``; a directly
        re-raised OUT_OF_SCOPE has ``__cause__ is None``, so the SAME
        exception object -- and the ``llm_meta`` the audit trail reads off
        it -- reaches ``api/runner.py``'s translation logic."""
        declining = _OutOfScopeBackend()
        router = LLMRouter(default_chain=[declining, MockBackend(response=GOOD_SQL)])
        agent = SQLAgent(router=router, execute_fn=_ok_execute, max_corrections=0)

        with pytest.raises(ValueError) as exc_info:
            agent.run("q", system_prompt=SYSTEM_PROMPT)

        raised = exc_info.value
        assert raised.llm_meta is declining.llm_meta
        assert raised.__cause__ is None
        assert "Every backend in the chain failed" not in str(raised)

    def test_out_of_scope_is_not_retried_by_the_correction_loop(self):
        """``max_corrections`` must not turn a decline into another attempt
        (``SQLAgent.run``'s docstring: "Never retried -- this is a terminal
        signal, not a fixable mistake")."""
        declining = _OutOfScopeBackend()
        router = LLMRouter(default_chain=[declining, MockBackend(response=GOOD_SQL)])
        agent = SQLAgent(router=router, execute_fn=_ok_execute, max_corrections=2)

        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            agent.run("q", system_prompt=SYSTEM_PROMPT)

        assert declining.calls == 1

    def test_every_backend_failing_raises_and_is_unwrapped(self):
        """RuntimeError from a real DB/transport failure must still reach
        the caller as a bare RuntimeError (not the router's internal
        "every backend in the chain failed" wrapper) -- api/runner.py's
        exception translation switches on the ORIGINAL exception type."""
        router = LLMRouter(default_chain=[_FailingBackend(), _FailingBackend()])
        agent = SQLAgent(router=router, execute_fn=_ok_execute, max_corrections=0)

        with pytest.raises(RuntimeError) as exc_info:
            agent.run("q", system_prompt=SYSTEM_PROMPT)

        assert "Every backend in the chain failed" not in str(exc_info.value)
        assert "simulated backend failure" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Exit criterion 6
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_question_twice_yields_byte_identical_sql(self):
        backend = MockBackend(response=GOOD_SQL)
        agent = SQLAgent(backend=backend, execute_fn=_ok_execute)

        _, r1 = agent.run("چند مشتری فعال داریم؟", system_prompt=SYSTEM_PROMPT)
        _, r2 = agent.run("چند مشتری فعال داریم؟", system_prompt=SYSTEM_PROMPT)

        assert r1.sql == r2.sql


# ---------------------------------------------------------------------------
# Constructor routing -- additive, backward-compatible construction paths
# ---------------------------------------------------------------------------


class TestConstructorRouting:
    def test_explicit_router_is_used_directly(self):
        router = LLMRouter(default_chain=[MockBackend(response=GOOD_SQL)])
        agent = SQLAgent(router=router, execute_fn=_ok_execute)

        assert agent._router is router
        assert agent._backend is router._chain_for(TaskType.SQL_GENERATION)[0]

    def test_router_takes_precedence_over_backend_when_both_given(self):
        router = LLMRouter(default_chain=[MockBackend(response=GOOD_SQL)])
        ignored_backend = MockBackend(response="SELECT 999")
        agent = SQLAgent(backend=ignored_backend, router=router, execute_fn=_ok_execute)

        assert agent._router is router

    def test_default_construction_uses_router_from_settings(self):
        """Mirrors the pre-refactor default of constructing a bare
        a bare backend directly when neither ``backend`` nor ``router`` is
        given -- now expressed as ``LLMRouter.from_settings()``."""
        with cfg.override_settings(llm_provider="mock"):
            agent = SQLAgent(execute_fn=_ok_execute)

        assert agent._backend.name == "mock:stub"
        chain = agent._router._chain_for(TaskType.SQL_GENERATION)
        assert len(chain) == 1
        assert chain[0] is agent._backend
