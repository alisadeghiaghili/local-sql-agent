# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Proves the sql_guard exception taxonomy actually changes correction-loop
BEHAVIOUR, not just internal classification.

Before this module's counterpart change (``security/sql_guard.py``'s
``CorrectableRejection``/``PolicyRejection`` split, wired into
``llm/sql_agent.py`` and ``session/engine.py``), every guard rejection --
fixable or not -- was retried up to ``max_corrections`` times. A denied
column or a forbidden statement can never be fixed by re-prompting (the
policy that caused it is not in the prompt), so that cost three LLM round
trips to return the exact same 400 a single call would have produced.

Counting the number of times the model backend was actually called is the
whole point of every test below: a test that only asserts the HTTP status
code or error code would pass identically before and after this fix (the
external contract is deliberately unchanged -- see the module docstring of
``security/sql_guard.py`` and the PR this accompanies). Only the call
count proves the retry loop actually stopped early.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from llm.base import LLMBackend
from llm.router import LLMRouter
from llm.sql_agent import MAX_CORRECTION_ATTEMPTS, SQLAgent
from security.sql_guard import (
    CorrectableRejection,
    PolicyRejection,
    SqlGuardRejection,
    validate_sql,
)
from session.engine import TurnEngine

# SELECT ... INTO is a write disguised as a read -- the guard refuses it by
# node type (rule 5 in validate_sql's docstring), and no rewrite of the
# query can make "write into a new table" into something a read-only guard
# will ever allow. Classified PolicyRejection: retrying can't help.
POLICY_SQL = "SELECT * INTO NewTbl FROM Contract"

# A table name this schema doesn't recognise -- exactly the kind of small-
# model hallucination a corrected retry can plausibly fix by naming a real
# table instead. Classified CorrectableRejection: today's retry behaviour
# is preserved.
CORRECTABLE_SQL = "SELECT * FROM HR_Payroll"

EMPTY_DF = pd.DataFrame()


def _execute_ok(sql: str) -> pd.DataFrame:  # noqa: ARG001 - never reached; both SQLs are guard-rejected
    return EMPTY_DF.copy()


class _CountingBackend(LLMBackend):
    """Counts real calls into the model -- deliberately not a MagicMock.

    A ``MagicMock``'s ``.call_count`` is easy to misread here: several
    ``LLMBackend`` wrapper methods (``generate``, ``generate_with_meta``,
    ``generate_with_meta_segments``) could each register a hit for what is
    logically ONE round trip to the model, depending on which layer a mock
    happens to intercept. This subclass instead implements only the single
    abstract method (``generate``); every default wrapper in
    ``llm.base.LLMBackend`` funnels down to it, so ``call_count`` always
    equals the true number of model invocations, regardless of which
    wrapper the router or ``SQLAgent``/``TurnEngine`` happens to call
    through.
    """

    def __init__(self, response: str) -> None:
        self._response = response
        self.call_count = 0

    @property
    def name(self) -> str:
        return "counting:test"

    def generate(self, prompt: str) -> str:  # noqa: ARG002
        self.call_count += 1
        return self._response


@pytest.fixture(autouse=True)
def _clear_query_cache():
    """A successful earlier test's cache entry must never serve a later one."""
    from api.query_cache import query_cache

    query_cache.clear()
    yield
    query_cache.clear()


# ---------------------------------------------------------------------------
# 1-2: POST /query (api/server.py + api/runner.py's SQLAgent-based loop)
# ---------------------------------------------------------------------------

@pytest.fixture()
def query_client(auth_settings):
    """A real TestClient against /query -- run_query() itself is NOT mocked.

    Unlike ``tests/test_api_endpoints.py``'s fixture (which replaces
    ``api.runner.run_query`` wholesale), this fixture only replaces the
    SHARED SQLAgent singleton (``api.runner.agent``) with a real one built
    from a counting backend, so the actual guard + correction loop in
    ``llm/sql_agent.py`` runs for real on every request.
    """
    import api.server as server_module

    server_module._system_prompt = "stub system prompt"
    return TestClient(server_module.app, raise_server_exceptions=False, headers=auth_settings)


class TestQueryEndpointCorrectionBudget:
    def test_policy_rejection_calls_backend_exactly_once(self, query_client):
        backend = _CountingBackend(POLICY_SQL)
        agent = SQLAgent(
            backend=backend, execute_fn=_execute_ok, max_corrections=MAX_CORRECTION_ATTEMPTS,
        )
        with patch("api.runner.agent", agent):
            resp = query_client.post("/query", json={"question": "چند مشتری داریم؟", "mode": "result"})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "FORBIDDEN_SQL"
        assert backend.call_count == 1

    def test_correctable_rejection_calls_backend_max_corrections_plus_one_times(self, query_client):
        """Today's behaviour, preserved: a fixable rejection still spends
        the whole budget before giving up."""
        backend = _CountingBackend(CORRECTABLE_SQL)
        agent = SQLAgent(
            backend=backend, execute_fn=_execute_ok, max_corrections=MAX_CORRECTION_ATTEMPTS,
        )
        with patch("api.runner.agent", agent):
            resp = query_client.post("/query", json={"question": "چند مشتری داریم؟", "mode": "result"})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "FORBIDDEN_SQL"
        assert backend.call_count == MAX_CORRECTION_ATTEMPTS + 1


# ---------------------------------------------------------------------------
# 3: POST /v2/sessions/{id}/turns (session/engine.py's TurnEngine-based loop)
# ---------------------------------------------------------------------------

@pytest.fixture()
def turns_client(auth_settings):
    """Mirrors ``tests/test_v2_endpoints.py``'s fixture style, minus the
    fixed MockBackend -- each test below injects its own counting backend
    into ``v2_routes._turn_engine``."""
    import api.server as server_module
    import api.v2_routes as v2_routes

    server_module._system_prompt = "stub system prompt"
    v2_routes._system_prompt = "stub system prompt"
    v2_routes._reset_for_testing()

    client = TestClient(server_module.app, raise_server_exceptions=False, headers=auth_settings)
    yield client, v2_routes
    v2_routes._reset_for_testing()


class TestTurnsEndpointCorrectionBudget:
    def test_policy_rejection_calls_backend_exactly_once(self, turns_client):
        client, v2_routes = turns_client
        backend = _CountingBackend(POLICY_SQL)
        v2_routes._turn_engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]),
            execute_fn=_execute_ok,
            max_corrections=MAX_CORRECTION_ATTEMPTS,
        )

        sid = client.post("/v2/sessions").json()["session_id"]
        resp = client.post(f"/v2/sessions/{sid}/turns", json={"question": "چند مشتری داریم؟"})

        # §5's "answer, then declare -- never block": a guard rejection is
        # still a 200 with turn.guard.verdict == "rejected", never an HTTP
        # error -- unchanged by this fix.
        assert resp.status_code == 200
        body = resp.json()
        assert body["guard"]["verdict"] == "rejected"
        assert backend.call_count == 1

    def test_correctable_rejection_calls_backend_max_corrections_plus_one_times(self, turns_client):
        client, v2_routes = turns_client
        backend = _CountingBackend(CORRECTABLE_SQL)
        v2_routes._turn_engine = TurnEngine(
            router=LLMRouter(default_chain=[backend]),
            execute_fn=_execute_ok,
            max_corrections=MAX_CORRECTION_ATTEMPTS,
        )

        sid = client.post("/v2/sessions").json()["session_id"]
        resp = client.post(f"/v2/sessions/{sid}/turns", json={"question": "چند مشتری داریم؟"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["guard"]["verdict"] == "rejected"
        assert backend.call_count == MAX_CORRECTION_ATTEMPTS + 1


# ---------------------------------------------------------------------------
# 4: correction_attempts honestly reports 1 for a policy rejection
# ---------------------------------------------------------------------------

class TestCorrectionAttemptsReportedOnPolicyRejection:
    """``SQLAgent.run`` attaches ``.attempt`` to the exception it raises
    (mirroring the existing ``.llm_meta``/``.candidate_sql`` attributes it
    already attaches for the audit trail), and ``api/runner.py``'s
    ``_carry_exception_meta`` copies it onto the translated NLQError --
    see both docstrings. This is what keeps the audit-facing attempt count
    honest: 1 for a policy rejection, never the full retry budget."""

    def test_sql_agent_attaches_attempt_one_on_policy_rejection(self):
        from api.errors import ForbiddenSQLError
        from api.runner import _safe_run
        from observability.timing import StageTimer

        backend = _CountingBackend(POLICY_SQL)
        agent = SQLAgent(
            backend=backend, execute_fn=_execute_ok, max_corrections=MAX_CORRECTION_ATTEMPTS,
        )
        with pytest.raises(ForbiddenSQLError) as exc_info:
            _safe_run(agent, "چند مشتری داریم؟", system_prompt="sp", timer=StageTimer())

        assert exc_info.value.attempt == 1
        assert backend.call_count == 1

    def test_sql_agent_attempt_is_full_budget_on_correctable_rejection(self):
        from api.errors import ForbiddenSQLError
        from api.runner import _safe_run
        from observability.timing import StageTimer

        backend = _CountingBackend(CORRECTABLE_SQL)
        agent = SQLAgent(
            backend=backend, execute_fn=_execute_ok, max_corrections=MAX_CORRECTION_ATTEMPTS,
        )
        with pytest.raises(ForbiddenSQLError) as exc_info:
            _safe_run(agent, "چند مشتری داریم؟", system_prompt="sp", timer=StageTimer())

        assert exc_info.value.attempt == MAX_CORRECTION_ATTEMPTS + 1
        assert backend.call_count == MAX_CORRECTION_ATTEMPTS + 1


# ---------------------------------------------------------------------------
# 5: backward compatibility -- every rejection is still catchable as a
# bare ValueError, and the subclass relationship is pinned explicitly.
# ---------------------------------------------------------------------------

class TestBackwardCompatibleValueErrorSubclassing:
    def test_policy_rejection_is_a_value_error_subclass(self):
        assert issubclass(PolicyRejection, ValueError)
        assert issubclass(PolicyRejection, SqlGuardRejection)

    def test_correctable_rejection_is_a_value_error_subclass(self):
        assert issubclass(CorrectableRejection, ValueError)
        assert issubclass(CorrectableRejection, SqlGuardRejection)

    def test_plain_except_value_error_still_catches_a_policy_rejection(self):
        """Every pre-existing ``except ValueError`` call site across the
        codebase (api/, llm/, session/, app.py, and the test suite) must
        keep working unchanged -- this is a refinement of the exception
        type, not a replacement for it."""
        try:
            validate_sql(POLICY_SQL)
        except ValueError as exc:
            assert isinstance(exc, PolicyRejection)
        else:
            pytest.fail("validate_sql should have rejected SELECT ... INTO")

    def test_plain_except_value_error_still_catches_a_correctable_rejection(self):
        try:
            validate_sql(CORRECTABLE_SQL)
        except ValueError as exc:
            assert isinstance(exc, CorrectableRejection)
        else:
            pytest.fail("validate_sql should have rejected an unknown table")


# ---------------------------------------------------------------------------
# 6: the HTTP status for a policy rejection is chosen by TYPE, not by message
# ---------------------------------------------------------------------------

class TestPolicyRejectionMapsByTypeNotMessage:
    """A PolicyRejection must become 400 FORBIDDEN_SQL whatever its message says.

    ``api/runner.py`` used to pick the status with
    ``if "Forbidden keyword" in str(exc)``. Any policy rejection whose
    message does not open with that exact phrase therefore reported 502
    INVALID_SQL_RESPONSE -- which tells the caller the model returned
    unparseable garbage, when it actually returned syntactically valid SQL
    that we refuse to run. ``SELECT * FROM sys.objects`` raises
    ``System catalogue forbidden: SYS`` and is the case that exposed it.

    Parametrised over policy rejections with deliberately dissimilar message
    text, so reintroducing any substring test fails here rather than passing
    on the one phrase that happens to match.
    """

    @pytest.mark.parametrize("sql,message_starts_with", [
        ("SELECT * FROM sys.objects", "System catalogue"),
        ("SELECT * INTO NewTbl FROM Contract", "Forbidden keyword"),
        ("SELECT 1;DROP TABLE Contract", "Forbidden keyword"),
    ])
    def test_policy_rejection_is_400_regardless_of_message(
        self, query_client, sql, message_starts_with
    ):
        # Guard the premise twice over: the SQL must really be a policy
        # rejection, and its message must really differ across cases --
        # otherwise this passes for the wrong reason.
        with pytest.raises(PolicyRejection) as caught:
            validate_sql(sql)
        assert str(caught.value).startswith(message_starts_with)

        backend = _CountingBackend(sql)
        agent = SQLAgent(
            backend=backend, execute_fn=_execute_ok, max_corrections=MAX_CORRECTION_ATTEMPTS,
        )
        with patch("api.runner.agent", agent):
            resp = query_client.post(
                "/query", json={"question": "پرسش نگاشت وضعیت", "mode": "result"}
            )

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "FORBIDDEN_SQL"
        assert backend.call_count == 1
