# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The conversational path can summarise its own result, on request.

It never could. ``session/engine.py`` set ``interpretation=None`` as a
literal from the commit that introduced the file, and never ran an
``interpret`` stage at all — so the web UI's fifth pipeline step, labelled
"تفسیر", could only ever sit at "waiting". That was invisible while none
of the five steps moved, and conspicuous the moment the other four started
ticking.

Opt-in per request, not per deployment
--------------------------------------
An interpretation sends up to twenty rows of **real query results** to the
model. ``/query`` has treated that as opt-in since phase 2
(``QueryRequest.interpret`` defaults to ``False``), and this path now
matches: the flag rides on the request, the web UI exposes it as a toggle
each analyst sets for themselves, and the deployment is not forced into
one posture for everyone.

The governance gate that refuses to send rows to a *remote* backend
without ``LLM_ALLOW_REMOTE`` is unchanged and shared — it moved to
``llm/interpret.py`` with the rest of the function rather than being
copied, because two copies of a gate is one copy that gets fixed and one
that does not. ``tests/test_runner_interpret_gate.py`` still covers it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm.interpret import format_numbers, interpret_rows
from llm.providers import MockBackend
from llm.router import LLMRouter
from session.engine import TurnEngine
from session.models import AskTurnRequest
from session.store import SessionStore


@pytest.fixture()
def session_record():
    return SessionStore(ttl_seconds=1800, max_size=10, max_turns=50).create()


_SQL = "SELECT TOP 10 c.Name AS CustomerName FROM Customer c"
_DF = pd.DataFrame({"CustomerName": ["A", "B"]})


def _engine(summary: str = "خلاصهٔ نتیجه") -> TurnEngine:
    """An engine that generates SQL, then this *summary*.

    The two tasks reach the backend by different methods and that is what
    lets one stub serve both: SQL generation goes through
    ``generate_with_meta_segments`` (a prompt with a static prefix worth
    caching), while interpretation goes through plain ``generate`` (all
    per-request data, nothing to cache). Overriding only ``generate``
    therefore answers exactly the interpretation call.
    """
    class _SummaryBackend(MockBackend):
        def generate(self, prompt: str) -> str:  # noqa: D102
            return summary

    return TurnEngine(
        router=LLMRouter(default_chain=[_SummaryBackend(response=_SQL)]),
        execute_fn=lambda sql: _DF.copy(),
    )


class TestTheRequestCarriesTheChoice:
    def test_the_field_defaults_to_off(self):
        """Matching /query. Rows do not go to the model because a caller
        forgot to say otherwise."""
        assert AskTurnRequest(question="q").interpret is False

    def test_the_field_can_be_turned_on(self):
        assert AskTurnRequest(question="q", interpret=True).interpret is True


class TestTheEngineHonoursIt:
    def test_no_interpretation_when_not_asked(self, session_record):
        turn = _engine().ask(session_record, "چند مشتری داریم؟", "prompt")
        assert turn.interpretation is None

    def test_an_interpretation_when_asked(self, session_record):
        turn = _engine().ask(
            session_record, "چند مشتری داریم؟", "prompt", interpret=True,
        )
        assert turn.interpretation == "خلاصهٔ نتیجه"

    def test_asking_runs_the_interpret_stage(self, session_record):
        """The stage is what the web UI's fifth step listens for. Producing
        the text without timing it would leave the step at "waiting" while
        the summary sat on screen beside it."""
        seen: list[tuple[str, str]] = []
        _engine().ask(
            session_record, "چند مشتری داریم؟", "prompt",
            interpret=True, on_stage=lambda n, s: seen.append((n, s)),
        )
        assert ("interpret", "running") in seen
        assert ("interpret", "done") in seen

    def test_not_asking_runs_no_interpret_stage(self, session_record):
        seen: list[tuple[str, str]] = []
        _engine().ask(
            session_record, "چند مشتری داریم؟", "prompt",
            on_stage=lambda n, s: seen.append((n, s)),
        )
        assert not [s for s in seen if s[0] == "interpret"]

    def test_an_empty_summary_stays_None_rather_than_empty_string(self, session_record):
        """`interpret_rows` returns "" when it could not produce one. A
        turn carrying an empty interpretation would render as an empty
        panel that looks like a failure; absent is the honest shape."""
        turn = _engine(summary="   ").ask(
            session_record, "چند مشتری داریم؟", "prompt", interpret=True,
        )
        assert turn.interpretation is None


class TestAFailedInterpretationDoesNotCostTheAnswer:
    def test_a_raising_backend_still_returns_the_turn(self, session_record):
        """The summary is an addition to a result the caller already has.
        Losing the whole turn because the prose did not come back would
        trade something for nothing."""
        class _InterpretationRaises(MockBackend):
            def generate(self, prompt: str) -> str:
                raise RuntimeError("interpretation backend is down")

        engine = TurnEngine(
            router=LLMRouter(default_chain=[_InterpretationRaises(response=_SQL)]),
            execute_fn=lambda sql: _DF.copy(),
        )
        turn = engine.ask(session_record, "چند مشتری داریم؟", "prompt", interpret=True)
        assert turn.interpretation is None
        assert turn.result is not None and turn.result.row_count == 2


class TestTheSharedHelpersMovedIntact:
    """`api/runner` now delegates here; these are the behaviours its own
    tests depended on."""

    def test_large_numbers_are_separated(self):
        assert format_numbers("total 12000000000 rial") == "total 12,000,000,000 rial"

    def test_persian_years_are_left_alone(self):
        assert format_numbers("in 1402") == "in 1402"

    def test_toman_is_rewritten_to_rial(self):
        router = LLMRouter(default_chain=[MockBackend(response="مبلغ ۵ تومان بود")])
        assert "ریال" in interpret_rows(router, "q", [{"a": 1}])
        assert "تومان" not in interpret_rows(router, "q", [{"a": 1}])

    def test_a_failing_router_returns_empty_rather_than_raising(self):
        class _Broken(MockBackend):
            def generate(self, prompt: str) -> str:
                raise RuntimeError("down")

        assert interpret_rows(LLMRouter(default_chain=[_Broken(response="x")]), "q", []) == ""
