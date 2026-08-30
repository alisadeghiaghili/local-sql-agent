"""Tests for the observability wiring inside ``api/runner.py`` (Debt 1).

Exercises the REAL pipeline end to end (a fake backend implementing
``generate_with_meta`` wired into a real ``SQLAgent``, not a fully mocked
``agent``/``_safe_run``), so these tests both prove the audit trail's
behaviour and cover ``_safe_generate_sql_only``/``_safe_run``'s exception
translation, which the pre-existing test suite only ever exercised through
mocks.

Contracts under test
---------------------
* Exactly one :class:`~observability.audit.AuditRecord` is written per
  ``run_query()`` call — success, cache hit, guard rejection, out-of-scope,
  LLM transport failure, and database error.
* A result row VALUE never reaches the audit file, even though the column
  NAME it lives under does.
* ``request_id`` passed to ``run_query()`` ends up verbatim on the record;
  omitting it still produces a well-formed record.
* Auditing itself can never fail the user's query.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pandas as pd
import pytest
import requests

from llm.base import LLMBackend
from llm.sql_agent import SQLAgent

SIMPLE_SQL = "SELECT TOP 5 * FROM [Auction_Dim].[Customer]"
POISON_VALUE = "ACME-SECRET-ROW-VALUE-9c1f2a"


def _raw_meta(prompt_tokens: int = 100, completion_tokens: int = 20) -> dict:
    return {
        "raw": {
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
        "endpoint_status": 200,
        "attempts": 1,
    }


class FakeMetaBackend(LLMBackend):
    """A stand-in for OpenAIBackend: exposes ``generate_with_meta`` for real,
    so the runner's real telemetry-building code path is exercised instead
    of being bypassed by a mock.

    ``responses`` is a queue of either a raw SQL string or an ``Exception``
    instance to raise (mirroring how OpenAIBackend can raise
    ``ValueError("OUT_OF_SCOPE")``, ``requests.Timeout``, etc).
    """

    def __init__(self, responses: list, model: str = "fake-model") -> None:
        self._responses = list(responses)
        self._model = model
        self._retries = 3

    @property
    def name(self) -> str:
        return f"openai:{self._model}"

    def generate(self, prompt: str) -> str:
        return self.generate_with_meta(prompt)[0]

    def generate_with_meta(self, prompt: str):
        value = self._responses.pop(0)
        if isinstance(value, Exception):
            raise value
        meta = _raw_meta()
        if value.strip().upper() == "OUT_OF_SCOPE":
            exc = ValueError("OUT_OF_SCOPE")
            exc.llm_meta = meta  # type: ignore[attr-defined]
            raise exc
        return value, meta


def _execute_ok(sql: str) -> pd.DataFrame:
    return pd.DataFrame({"CustomerName": ["Ali"], "SecretBalance": [POISON_VALUE]})


def _execute_fail(sql: str) -> pd.DataFrame:
    raise RuntimeError("Database error: Invalid column name 'Foo'")


@pytest.fixture()
def audit_file(tmp_path):
    """Redirect the audit trail to an isolated temp file for this test."""
    path = tmp_path / "audit_log.jsonl"
    with patch("observability.audit._AUDIT_LOG_FILE", str(path)):
        yield path


def _records(path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _clear_cache():
    from api.query_cache import query_cache
    query_cache.clear()
    yield
    query_cache.clear()


def _agent(backend: LLMBackend, execute_fn=_execute_ok, max_corrections: int = 0) -> SQLAgent:
    return SQLAgent(backend=backend, execute_fn=execute_fn, max_corrections=max_corrections)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

class TestSuccessPathAudit:
    def test_exactly_one_record_written(self, audit_file):
        from api.runner import run_query

        with patch("api.runner.agent", _agent(FakeMetaBackend([SIMPLE_SQL]))):
            run_query("چند مشتری فعال داریم؟", system_prompt="sp", mode="full",
                       request_id="r_success_1")

        records = _records(audit_file)
        assert len(records) == 1

    def test_record_shape(self, audit_file):
        from api.runner import run_query

        with patch("api.runner.agent", _agent(FakeMetaBackend([SIMPLE_SQL]))):
            run_query("چند مشتری فعال داریم؟", system_prompt="sp", mode="full",
                       request_id="r_success_2")

        rec = _records(audit_file)[0]
        assert rec["request_id"] == "r_success_2"
        assert rec["question"] == "چند مشتری فعال داریم؟"
        assert rec["generated_sql"] == SIMPLE_SQL
        assert rec["row_count"] == 1
        assert rec["tier"] == "T2"
        assert rec["error_code"] is None
        assert rec["guard"]["verdict"] == "allowed"
        assert rec["guard"]["tables_touched"] == ["Customer"]
        # SIMPLE_SQL already carries "TOP 5" -- ensure_top has nothing to
        # inject, so injected_top correctly stays None.
        assert rec["guard"]["injected_top"] is None
        assert rec["columns"] == ["CustomerName", "SecretBalance"]
        assert rec["llm"]["finish_reason"] == "stop"
        assert rec["llm"]["prompt_tokens"] == 100
        assert rec["llm"]["backend"] == "openai"
        assert rec["timings"]["total_ms"] >= 0
        assert set(rec["timings"].keys()) == {
            "total_ms", "plan_ms", "prompt_ms", "llm_ms",
            "guard_ms", "execute_ms", "interpret_ms",
        }

    def test_row_value_never_reaches_audit_file(self, audit_file):
        """The executed query's result contains a distinctive row VALUE.
        It must never appear anywhere in the audit file, even though the
        column NAME it lives under legitimately does (contract §8's
        column-names-yes/row-values-no rule, applied to the audit trail
        by observability/audit.py's module docstring)."""
        from api.runner import run_query

        with patch("api.runner.agent", _agent(FakeMetaBackend([SIMPLE_SQL]))):
            run_query("سوال", system_prompt="sp", mode="full", request_id="r_poison")

        raw_text = audit_file.read_text(encoding="utf-8")
        assert POISON_VALUE not in raw_text
        assert "SecretBalance" in raw_text  # the column NAME is fine to log

    def test_missing_request_id_still_produces_a_record(self, audit_file):
        from api.runner import run_query

        with patch("api.runner.agent", _agent(FakeMetaBackend([SIMPLE_SQL]))):
            run_query("سوال", system_prompt="sp", mode="full")  # no request_id

        records = _records(audit_file)
        assert len(records) == 1
        assert records[0]["request_id"]  # non-empty fallback id was minted


# ---------------------------------------------------------------------------
# sql-only mode — exercises _safe_generate_sql_only's real logic
# ---------------------------------------------------------------------------

class TestSqlOnlyModeAudit:
    def test_exactly_one_record_and_no_row_count(self, audit_file):
        from api.runner import run_query

        with patch("api.runner.agent", _agent(FakeMetaBackend([SIMPLE_SQL]))):
            run_query("سوال", system_prompt="sp", mode="sql", request_id="r_sql_1")

        records = _records(audit_file)
        assert len(records) == 1
        rec = records[0]
        assert rec["generated_sql"] == SIMPLE_SQL
        assert rec["row_count"] == 0  # never executed
        assert rec["llm"]["finish_reason"] == "stop"
        assert rec["guard"]["tables_touched"] == ["Customer"]
        # ensure_top is never applied in sql-only mode (nothing is
        # executed, so there is no row cap to enforce).
        assert rec["guard"]["injected_top"] is None

    def test_out_of_scope(self, audit_file):
        from api.runner import run_query
        from api.errors import OutOfScopeError

        with patch("api.runner.agent", _agent(FakeMetaBackend(["OUT_OF_SCOPE"]))):
            with pytest.raises(OutOfScopeError):
                run_query("سوال", system_prompt="sp", mode="sql", request_id="r_sql_oos")

        rec = _records(audit_file)[0]
        assert rec["error_code"] == "OUT_OF_SCOPE"
        assert rec["llm"]["finish_reason"] == "stop"

    def test_timeout(self, audit_file):
        from api.runner import run_query
        from api.errors import ModelTimeoutError

        backend = FakeMetaBackend([requests.Timeout("slow")])
        with patch("api.runner.agent", _agent(backend)):
            with pytest.raises(ModelTimeoutError):
                run_query("سوال", system_prompt="sp", mode="sql", request_id="r_sql_timeout")

        rec = _records(audit_file)[0]
        assert rec["error_code"] == "MODEL_TIMEOUT"

    def test_connection_error(self, audit_file):
        from api.runner import run_query
        from api.errors import ModelUnavailableError

        backend = FakeMetaBackend([requests.ConnectionError("no route")])
        with patch("api.runner.agent", _agent(backend)):
            with pytest.raises(ModelUnavailableError):
                run_query("سوال", system_prompt="sp", mode="sql", request_id="r_sql_conn")

        rec = _records(audit_file)[0]
        assert rec["error_code"] == "MODEL_UNAVAILABLE"
        assert rec["llm"]["finish_reason"] == "error"

    def test_backend_runtime_error(self, audit_file):
        from api.runner import run_query
        from api.errors import ModelUnavailableError

        backend = FakeMetaBackend([RuntimeError("OpenAI-compatible endpoint unreachable after 3 retries")])
        with patch("api.runner.agent", _agent(backend)):
            with pytest.raises(ModelUnavailableError):
                run_query("سوال", system_prompt="sp", mode="sql", request_id="r_sql_rt")

        assert _records(audit_file)[0]["error_code"] == "MODEL_UNAVAILABLE"

    def test_builtin_timeout_error(self, audit_file):
        """The BUILTIN ``TimeoutError`` (an ``OSError`` subclass -- NOT
        ``requests.Timeout``, which ``test_timeout`` above covers) must
        translate to ``ModelTimeoutError``, not escape as a raw
        non-``NLQError`` audited as ``INTERNAL_ERROR``.

        This is the mode="sql" half of the pair kept in lockstep with
        ``TestErrorPathsAudit.test_router_latency_budget_breach_is_a_model_timeout``.

        Two distinct triggers reach the same untranslated clause here, and
        both now matter. A socket-level timeout raised by the backend
        itself is one: ``socket.timeout`` *is* the builtin ``TimeoutError``
        from Python 3.10 on, so it never matched the ``requests.Timeout``
        clause. The operator's ``llm_task_budget_seconds`` is the other —
        and that one only became reachable on this path with commit
        ``49a0c91``, which moved ``_safe_generate_sql_only`` off a direct
        ``agent._backend.generate_with_meta`` call and onto
        ``LLMRouter.generate_for_task``, bringing ``_call_chain``'s budget
        check with it. Verified on the merged tree: with a tiny budget and
        a slow backend, mode="sql" and mode="full" both raised a raw
        ``builtins.TimeoutError`` before this fix.
        """
        from api.runner import run_query
        from api.errors import ModelTimeoutError

        backend = FakeMetaBackend([TimeoutError("socket timed out")])
        with patch("api.runner.agent", _agent(backend)):
            with pytest.raises(ModelTimeoutError):
                run_query("سوال", system_prompt="sp", mode="sql",
                          request_id="r_sql_builtin_timeout")

        rec = _records(audit_file)[0]
        assert rec["error_code"] == "MODEL_TIMEOUT"

    def test_empty_response(self, audit_file):
        from api.runner import run_query
        from api.errors import EmptySQLResponseError

        backend = FakeMetaBackend(["   "])
        with patch("api.runner.agent", _agent(backend)):
            with pytest.raises(EmptySQLResponseError):
                run_query("سوال", system_prompt="sp", mode="sql", request_id="r_sql_empty")

        rec = _records(audit_file)[0]
        assert rec["error_code"] == "EMPTY_SQL_RESPONSE"
        assert rec["llm"]["prompt_tokens"] == 100  # meta was recovered even on this failure

    def test_forbidden_guard_rejection(self, audit_file):
        from api.runner import run_query
        from api.errors import ForbiddenSQLError

        stacked_statement = "SELECT Price FROM Contract; DROP TABLE Contract"
        with patch("api.runner.agent", _agent(FakeMetaBackend([stacked_statement]))):
            with pytest.raises(ForbiddenSQLError):
                run_query("سوال", system_prompt="sp", mode="sql", request_id="r_sql_forbidden")

        rec = _records(audit_file)[0]
        assert rec["error_code"] == "FORBIDDEN_SQL"
        assert rec["guard"]["verdict"] == "rejected"

    def test_invalid_sql_response(self, audit_file):
        """A ValueError that is neither OUT_OF_SCOPE nor a forbidden-keyword
        rejection (e.g. clean_sql finding no SELECT/CTE at all) maps to
        InvalidSQLResponseError."""
        from api.runner import run_query
        from api.errors import InvalidSQLResponseError

        with patch("api.runner.agent", _agent(FakeMetaBackend(["not sql at all"]))):
            with pytest.raises(InvalidSQLResponseError):
                run_query("سوال", system_prompt="sp", mode="sql", request_id="r_sql_invalid")

        rec = _records(audit_file)[0]
        assert rec["error_code"] == "INVALID_SQL_RESPONSE"

    def test_non_sentinel_valueerror_from_llm_stage(self, audit_file):
        """A non-sentinel ``ValueError`` raised at the *llm* stage (not the
        guard stage, as ``test_invalid_sql_response`` covers) is translated
        too, instead of escaping ``run_query`` bare.

        ``run_query`` documents that it raises only ``NLQError`` subclasses.
        A bare ``ValueError`` has no ``error_code`` and no ``http_status``,
        so it fell through to ``run_query``'s generic ``except Exception``,
        was audited as ``INTERNAL_ERROR``, and was served as a generic 500
        that hid the real cause. ``result``/``full`` mode has always mapped
        this same case to ``InvalidSQLResponseError`` via ``_safe_run``; the
        two paths must not disagree.

        The concrete live source is a remote provider's unretried
        ``resp.json()`` on a truncated or non-JSON 200 body:
        ``requests.exceptions.JSONDecodeError`` subclasses ``ValueError``
        (``OllamaBackend`` is immune -- it catches that as the
        ``RequestException`` it also is, retries, and exhausts into a
        ``RuntimeError``). It is raised directly here rather than through a
        real provider so the test stays a unit test of the translation
        contract, with no HTTP layer to mock.
        """
        from api.runner import run_query
        from api.errors import InvalidSQLResponseError, NLQError

        backend = FakeMetaBackend([ValueError("model returned malformed JSON")])
        with patch("api.runner.agent", _agent(backend)):
            with pytest.raises(InvalidSQLResponseError) as excinfo:
                run_query("سوال", system_prompt="sp", mode="sql",
                          request_id="r_sql_llm_valueerror")

        err = excinfo.value
        assert isinstance(err, NLQError)
        assert err.http_status == 502
        assert "model returned malformed JSON" in str(err)

        rec = _records(audit_file)[0]
        assert rec["error_code"] == "INVALID_SQL_RESPONSE"  # not INTERNAL_ERROR


# ---------------------------------------------------------------------------
# sql-only mode is routed through LLMRouter, like the other two paths
# ---------------------------------------------------------------------------

class TestSqlOnlyModeUsesRouter:
    """``mode="sql"`` is the third request path into
    ``TaskType.SQL_GENERATION`` (after ``SQLAgent.run``'s result/full hot
    path and, for its own task, ``_interpret``). It goes through
    ``LLMRouter`` too, so it inherits the fallback chain, the governance
    gate, and the chain's decline semantics for free -- mirroring
    ``tests/test_sql_agent_router.py``'s
    ``TestSecondProviderServesSqlGeneration`` / ``TestFallbackChain``, but
    entered via ``run_query(..., mode="sql")`` rather than
    ``SQLAgent.run``.

    Except where a test says otherwise, no backend here is remote (see
    ``llm.router._REMOTE_BACKENDS``), so no ``llm_allow_remote`` opt-in is
    involved.

    The last two tests are the decline contract: ``OUT_OF_SCOPE`` is a
    terminal domain decision, not a transport failure, so
    ``llm/router.py``'s ``_call_chain`` short-circuits the chain on it
    rather than letting a second backend answer a question the first one
    correctly refused. Both modes are covered because both now reach
    ``_call_chain``: ``full`` through ``SQLAgent.run``, and ``sql``
    through ``_safe_generate_sql_only``, which used to call
    ``agent._backend`` (chain entry 0) directly -- when it did, the
    decline was terminal there for an unrelated second reason, and these
    tests are what kept it terminal once that call site moved onto the
    router.
    """

    def test_second_provider_serves_sql_only_mode_after_primary_fails(self, audit_file):
        """The primary backend fails; the second answers -- with NO change
        at the call site (the same ``run_query(..., mode="sql")`` every
        other test in this file makes), and the fallback is recorded in
        the audit trail's llm block."""
        from api.runner import run_query
        from llm.providers import MockBackend
        from llm.router import LLMRouter

        primary = FakeMetaBackend([RuntimeError("OpenAI-compatible endpoint unreachable after 3 retries")])
        router = LLMRouter(default_chain=[primary, MockBackend(response=SIMPLE_SQL)])
        agent = SQLAgent(router=router, execute_fn=_execute_ok, max_corrections=0)

        with patch("api.runner.agent", agent):
            response = run_query(
                "سوال", system_prompt="sp", mode="sql", request_id="r_sql_fallback",
            )

        assert response.sql == SIMPLE_SQL
        assert response.llm["provider"] == "mock:stub"
        assert response.llm["fallback_used"] is True

        rec = _records(audit_file)[0]
        assert rec["error_code"] is None
        assert rec["generated_sql"] == SIMPLE_SQL
        assert rec["llm"]["provider"] == "mock:stub"
        assert rec["llm"]["fallback_used"] is True

    def test_transport_timeout_survives_the_router_unwrap(self, audit_file):
        """The chain-exhausted wrapper's ``__cause__`` is a
        ``requests.Timeout`` raised by the LAST chain entry, and the
        request must still land on MODEL_TIMEOUT rather than being
        flattened into MODEL_UNAVAILABLE by the wrapping RuntimeError.

        This is the only remaining test that pins
        ``_safe_generate_sql_only``'s ``exc.__cause__ or exc`` unwrap: an
        OUT_OF_SCOPE decline can no longer arrive wrapped at all, since
        ``_call_chain`` re-raises it bare (``llm/router.py``, added in
        9a66604), so the transport types are what exercise the wrapper
        now. Delete this and the unwrap regresses silently."""
        from api.runner import run_query
        from api.errors import ModelTimeoutError
        from llm.router import LLMRouter

        router = LLMRouter(default_chain=[
            FakeMetaBackend([requests.ConnectionError("no route")]),
            FakeMetaBackend([requests.Timeout("slow")]),
        ])
        agent = SQLAgent(router=router, execute_fn=_execute_ok, max_corrections=0)

        with patch("api.runner.agent", agent):
            with pytest.raises(ModelTimeoutError):
                run_query("سوال", system_prompt="sp", mode="sql",
                          request_id="r_sql_timeout_chain")

        assert _records(audit_file)[0]["error_code"] == "MODEL_TIMEOUT"

    def test_remote_provider_is_refused_in_sql_only_mode(self, audit_file):
        """Governance now covers this mode too -- one of the reasons it was
        routed through LLMRouter at all. A hosted provider without the
        ``llm_allow_remote`` opt-in is refused by
        ``LLMRouter._governance_check`` BEFORE any backend method runs, so
        the question never leaves the deployment; the refusal
        (``RemoteProviderNotAllowedError``, a ``RuntimeError`` subclass)
        translates to MODEL_UNAVAILABLE. Compare
        ``tests/test_runner_interpret_gate.py``, which pins the same gate
        for the interpretation task."""
        from unittest.mock import MagicMock

        import config as cfg
        from api.runner import run_query
        from api.errors import ModelUnavailableError
        from llm.providers import OpenAIBackend

        backend = OpenAIBackend(model="gpt-4o-mini", api_key="sk-fake")
        backend.generate = MagicMock(side_effect=AssertionError("must not be called"))
        backend.generate_with_meta = MagicMock(side_effect=AssertionError("must not be called"))
        agent = SQLAgent(backend=backend, execute_fn=_execute_ok, max_corrections=0)

        with cfg.override_settings(llm_allow_remote=False):
            with patch("api.runner.agent", agent):
                with pytest.raises(ModelUnavailableError):
                    run_query("سوال", system_prompt="sp", mode="sql",
                              request_id="r_sql_remote_refused")

        backend.generate.assert_not_called()
        backend.generate_with_meta.assert_not_called()
        assert _records(audit_file)[0]["error_code"] == "MODEL_UNAVAILABLE"

    def test_out_of_scope_is_not_overridden_by_a_later_backend(self, audit_file):
        from llm.router import LLMRouter
        from api.runner import run_query
        from api.errors import OutOfScopeError

        declining = FakeMetaBackend(["OUT_OF_SCOPE"], model="declining")
        answering = FakeMetaBackend([SIMPLE_SQL], model="answering")
        router = LLMRouter(default_chain=[declining, answering])
        agent = SQLAgent(router=router, execute_fn=_execute_ok, max_corrections=0)

        with patch("api.runner.agent", agent):
            with pytest.raises(OutOfScopeError):
                run_query("سوال", system_prompt="sp", mode="sql",
                          request_id="r_sql_oos_chain")

        # The second backend must never have been consulted.
        assert answering._responses == [SIMPLE_SQL]

        rec = _records(audit_file)[0]
        assert rec["error_code"] == "OUT_OF_SCOPE"
        assert rec["generated_sql"] == ""
        # llm_meta rode along on the raised ValueError, so the audit trail
        # still describes a completed (declining) model call.
        assert rec["llm"]["finish_reason"] == "stop"
        assert rec["llm"]["prompt_tokens"] == 100

    def test_out_of_scope_in_full_mode_is_not_overridden_either(self, audit_file):
        from llm.router import LLMRouter
        from api.runner import run_query
        from api.errors import OutOfScopeError

        declining = FakeMetaBackend(["OUT_OF_SCOPE"], model="declining")
        answering = FakeMetaBackend([SIMPLE_SQL], model="answering")
        router = LLMRouter(default_chain=[declining, answering])
        agent = SQLAgent(router=router, execute_fn=_execute_ok, max_corrections=0)

        with patch("api.runner.agent", agent):
            with pytest.raises(OutOfScopeError):
                run_query("سوال", system_prompt="sp", mode="full",
                          request_id="r_full_oos_chain")

        assert answering._responses == [SIMPLE_SQL]

        records = _records(audit_file)
        assert len(records) == 1
        rec = records[0]
        assert rec["error_code"] == "OUT_OF_SCOPE"
        assert rec["guard"]["rule"] == "OUT_OF_SCOPE"
        assert rec["llm"]["prompt_tokens"] == 100


# ---------------------------------------------------------------------------
# Cache hit — still audited
# ---------------------------------------------------------------------------

class TestCacheHitAudit:
    def test_second_identical_call_still_writes_a_record(self, audit_file):
        from api.runner import run_query

        with patch("api.runner.agent", _agent(FakeMetaBackend([SIMPLE_SQL, SIMPLE_SQL]))):
            run_query("سوال یکسان", system_prompt="sp", mode="full", request_id="r_cache_1")
            run_query("سوال یکسان", system_prompt="sp", mode="full", request_id="r_cache_2")

        records = _records(audit_file)
        assert len(records) == 2
        assert records[0]["request_id"] == "r_cache_1"
        assert records[1]["request_id"] == "r_cache_2"
        # Second call was a cache hit, but SQL/columns were still recovered
        # from the cached response, not lost.
        assert records[1]["generated_sql"] == SIMPLE_SQL

    def test_first_call_is_t2_second_is_t0(self, audit_file):
        """The cache exists today and run_query already knows when it
        served from it -- tier must say so immediately, not wait for a
        later tiering phase to make T0 visible in the audit log."""
        from api.runner import run_query

        with patch("api.runner.agent", _agent(FakeMetaBackend([SIMPLE_SQL, SIMPLE_SQL]))):
            run_query("سوال یکسان دوباره", system_prompt="sp", mode="full", request_id="r_tier_1")
            run_query("سوال یکسان دوباره", system_prompt="sp", mode="full", request_id="r_tier_2")

        records = _records(audit_file)
        assert records[0]["tier"] == "T2"
        assert records[1]["tier"] == "T0"


# ---------------------------------------------------------------------------
# Row-cap injection (guard.injected_top)
# ---------------------------------------------------------------------------

class TestInjectedTopAudit:
    def test_injected_top_recorded_when_model_omits_a_cap(self, audit_file):
        """When the model's own SQL has no TOP/row-limit clause,
        ensure_top injects cfg.settings.default_top_n -- the audit record
        must say so, not silently leave injected_top as None."""
        import config as cfg
        from api.runner import run_query

        uncapped_sql = "SELECT * FROM Customer"
        with patch("api.runner.agent", _agent(FakeMetaBackend([uncapped_sql]))):
            run_query("سوال", system_prompt="sp", mode="full", request_id="r_injected_top")

        rec = _records(audit_file)[0]
        assert rec["guard"]["injected_top"] == cfg.settings.default_top_n
        assert rec["generated_sql"] == f"SELECT TOP {cfg.settings.default_top_n} * FROM Customer"


# ---------------------------------------------------------------------------
# Error paths — exactly one record each, with the right guard/llm shape
# ---------------------------------------------------------------------------

class TestErrorPathsAudit:
    def test_invalid_sql_response_full_mode(self, audit_file):
        """A ValueError that is neither OUT_OF_SCOPE nor a forbidden-keyword
        rejection (empty model output, in this case) maps to
        InvalidSQLResponseError via _safe_run's else branch."""
        from api.runner import run_query
        from api.errors import InvalidSQLResponseError

        with patch("api.runner.agent", _agent(FakeMetaBackend(["   "]))):
            with pytest.raises(InvalidSQLResponseError):
                run_query("سوال", system_prompt="sp", mode="full", request_id="r_full_invalid")

        rec = _records(audit_file)[0]
        assert rec["error_code"] == "INVALID_SQL_RESPONSE"

    def test_interpret_failure_is_non_fatal_but_still_audited(self, audit_file):
        """interpret=True's extra LLM call fails; the request still
        succeeds (interpretation degrades to ""), and exactly one audit
        record is written."""
        from api.runner import run_query

        # First generate_with_meta() call produces the SQL; the second
        # (interpret's plain generate()) finds an empty queue and raises.
        backend = FakeMetaBackend([SIMPLE_SQL])
        with patch("api.runner.agent", _agent(backend)):
            response = run_query("سوال", system_prompt="sp", mode="full",
                                   interpret=True, request_id="r_interpret_fail")

        assert response.interpretation == ""
        records = _records(audit_file)
        assert len(records) == 1
        assert records[0]["error_code"] is None

    def test_out_of_scope(self, audit_file):
        from api.runner import run_query
        from api.errors import OutOfScopeError

        with patch("api.runner.agent", _agent(FakeMetaBackend(["OUT_OF_SCOPE"]))):
            with pytest.raises(OutOfScopeError):
                run_query("پرسش نامرتبط", system_prompt="sp", mode="full",
                           request_id="r_oos")

        records = _records(audit_file)
        assert len(records) == 1
        rec = records[0]
        assert rec["error_code"] == "OUT_OF_SCOPE"
        assert rec["guard"]["rule"] == "OUT_OF_SCOPE"
        # The model DID respond (just declined) -- llm block reflects a
        # completed call, not a transport failure.
        assert rec["llm"]["finish_reason"] == "stop"
        assert rec["llm"]["prompt_tokens"] == 100

    def test_forbidden_sql_is_a_guard_rejection(self, audit_file):
        from api.runner import run_query
        from api.errors import ForbiddenSQLError

        stacked_statement = "SELECT Price FROM Contract; DROP TABLE Contract"
        with patch("api.runner.agent", _agent(FakeMetaBackend([stacked_statement]))):
            with pytest.raises(ForbiddenSQLError):
                run_query("سوال", system_prompt="sp", mode="full", request_id="r_forbidden")

        records = _records(audit_file)
        assert len(records) == 1
        rec = records[0]
        assert rec["error_code"] == "FORBIDDEN_SQL"
        assert rec["guard"]["verdict"] == "rejected"
        assert rec["guard"]["rule"] == "FORBIDDEN_SQL"
        assert rec["llm"]["finish_reason"] == "stop"
        # Even though the query as a whole was rejected (stacked
        # statements), the candidate text is recovered well enough to
        # report which known table it touched.
        assert rec["generated_sql"] == stacked_statement
        assert rec["guard"]["tables_touched"] == ["Contract"]

    def test_model_unavailable_after_transport_failure(self, audit_file):
        """Backend raises RuntimeError directly (as OpenAIBackend does
        after exhausting its own retries) -- a genuine transport failure,
        so finish_reason must be "error", not "stop"."""
        from api.runner import run_query
        from api.errors import ModelUnavailableError

        backend = FakeMetaBackend([RuntimeError("OpenAI-compatible endpoint unreachable after 3 retries: boom")])
        with patch("api.runner.agent", _agent(backend)):
            with pytest.raises(ModelUnavailableError):
                run_query("سوال", system_prompt="sp", mode="full", request_id="r_unavail")

        records = _records(audit_file)
        assert len(records) == 1
        rec = records[0]
        assert rec["error_code"] == "MODEL_UNAVAILABLE"
        assert rec["llm"]["finish_reason"] == "error"
        assert rec["llm"]["endpoint_status"] == 0

    def test_model_timeout(self, audit_file):
        from api.runner import run_query
        from api.errors import ModelTimeoutError

        backend = FakeMetaBackend([requests.Timeout("slow model")])
        with patch("api.runner.agent", _agent(backend)):
            with pytest.raises(ModelTimeoutError):
                run_query("سوال", system_prompt="sp", mode="full", request_id="r_timeout")

        records = _records(audit_file)
        assert len(records) == 1
        assert records[0]["error_code"] == "MODEL_TIMEOUT"
        assert records[0]["llm"]["finish_reason"] == "error"

    def test_router_latency_budget_breach_is_a_model_timeout(self, audit_file):
        """A backend that answers, but too slowly for the operator's
        ``LLM_TASK_BUDGET_SECONDS``, must surface as ``ModelTimeoutError``.

        ``LLMRouter._call_chain`` records a budget breach as the BUILTIN
        ``TimeoutError`` (an ``OSError`` subclass, unrelated to
        ``requests.Timeout``) and, once every backend in the chain has
        breached, raises its "Every backend in the chain failed"
        ``RuntimeError`` with that ``TimeoutError`` as ``__cause__``;
        ``SQLAgent.run`` unwraps and re-raises the cause, so ``_safe_run``
        sees a bare ``TimeoutError``. Before the matching clause existed it
        matched none of ``_safe_run``'s translations and escaped
        ``run_query`` as a non-``NLQError``, audited as ``INTERNAL_ERROR``
        -- an operator-configured timeout reading as a server bug.

        Only reachable when the budget is configured; it defaults to
        ``None`` (check disabled), which is why nothing else covers it.
        """
        import config as cfg
        from llm.router import LLMRouter, TaskType
        from api.runner import run_query
        from api.errors import ModelTimeoutError

        class SlowBackend(FakeMetaBackend):
            def generate_with_meta(self, prompt: str):
                time.sleep(0.01)
                return super().generate_with_meta(prompt)

        with cfg.override_settings(llm_task_budget_seconds=0.0001):
            # Mirrors LLMRouter.from_settings(): one budget, every task.
            budget = cfg.settings.llm_task_budget_seconds
            router = LLMRouter(
                default_chain=[SlowBackend([SIMPLE_SQL])],
                budgets={task: budget for task in TaskType},
            )
            agent = SQLAgent(router=router, execute_fn=_execute_ok, max_corrections=0)
            with patch("api.runner.agent", agent):
                with pytest.raises(ModelTimeoutError):
                    run_query("سوال", system_prompt="sp", mode="full",
                              request_id="r_budget_breach")

        records = _records(audit_file)
        assert len(records) == 1
        assert records[0]["error_code"] == "MODEL_TIMEOUT"

    def test_database_execution_error(self, audit_file):
        """SQL generation succeeds; execution fails -- the LLM call itself
        completed, so finish_reason is "stop", but the error is a DB one."""
        from api.runner import run_query
        from api.errors import QueryExecutionError

        backend = FakeMetaBackend([SIMPLE_SQL])
        with patch("api.runner.agent", _agent(backend, execute_fn=_execute_fail)):
            with pytest.raises(QueryExecutionError):
                run_query("سوال", system_prompt="sp", mode="full", request_id="r_dberr")

        records = _records(audit_file)
        assert len(records) == 1
        rec = records[0]
        assert rec["error_code"] == "QUERY_EXECUTION_ERROR"
        assert rec["llm"]["finish_reason"] == "stop"
        assert rec["row_count"] == 0
        # The SQL passed the guard before execution failed -- the audit
        # record should still say what was attempted and which known
        # table it touched, not leave generated_sql/tables_touched blank
        # just because the request ultimately failed.
        assert rec["generated_sql"] == SIMPLE_SQL
        assert rec["guard"]["tables_touched"] == ["Customer"]

    def test_unexpected_bug_still_writes_exactly_one_record(self, audit_file):
        """A non-NLQError bug (e.g. a KeyError deep in some helper) must
        still result in exactly one audit record, tagged INTERNAL_ERROR,
        and must still propagate to the caller unchanged."""
        from api.runner import run_query

        with patch("api.runner.agent", _agent(FakeMetaBackend([SIMPLE_SQL]))), \
                patch("api.runner._safe_run", side_effect=KeyError("boom")):
            with pytest.raises(KeyError):
                run_query("سوال", system_prompt="sp", mode="full", request_id="r_bug")

        records = _records(audit_file)
        assert len(records) == 1
        assert records[0]["error_code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Auditing must never fail the user's query
# ---------------------------------------------------------------------------

class TestAuditingNeverFailsTheQuery:
    def test_broken_save_audit_record_does_not_propagate(self, audit_file):
        from api.runner import run_query

        with patch("api.runner.agent", _agent(FakeMetaBackend([SIMPLE_SQL]))), \
                patch("api.runner.save_audit_record", side_effect=RuntimeError("disk full")):
            # Must return normally -- the broken audit writer must not
            # surface as an error to the caller.
            response = run_query("سوال", system_prompt="sp", mode="full")

        assert response.row_count == 1
