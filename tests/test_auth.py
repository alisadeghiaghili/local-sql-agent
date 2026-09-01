"""Phase 8 — authentication: the ten required contracts from the spec.

Covers, in order:
 1. Every protected route rejects an unauthenticated request with 401.
 2. A malformed/unknown/correct-prefix-wrong-tail key is rejected.
 3. A valid key authenticates.
 4. The query cache partitions on principal scope (real QueryCache,
    real hit/miss counters -- no mock at the cache boundary).
 5. A session is 404, never 403, to a non-owning principal.
 6. GET /health stays open and omits `model` when unauthenticated.
 7. AUTH_REQUIRED=true + empty API_KEYS_JSON -> lifespan raises RuntimeError.
 8. AUTH_REQUIRED=false logs a WARNING on every startup.
 9. An authenticated query's audit record carries principal_id.
10. The rate limiter buckets on principal id, not IP, for authenticated callers.

Also covers the four ``security.auth._parse_api_keys`` hardening fixes
(bare-string denied_columns, duplicate key_sha256, malformed key_sha256,
non-string name) flagged during review of the initial implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.models import HealthResponse
from config import override_settings
from security.auth import ApiKeyConfigError, Principal, _parse_api_keys

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RAW_KEY_A = "a" * 40
RAW_KEY_B = "b" * 40


def _entry(principal_id: str, name: str, raw_key: str, denied_columns=None) -> dict:
    entry = {"id": principal_id, "name": name, "key_sha256": _sha256(raw_key)}
    if denied_columns is not None:
        entry["denied_columns"] = denied_columns
    return entry


TWO_PRINCIPALS_JSON = json.dumps([
    _entry("u1", "User One", RAW_KEY_A),
    _entry("u2", "User Two", RAW_KEY_B),
])


@pytest.fixture(autouse=True)
def _reset_shared_state():
    """Isolate this module's tests from the session store / query cache."""
    import api.v2_routes as v2_routes
    from api.query_cache import query_cache

    v2_routes._reset_for_testing()
    query_cache.reconfigure(ttl_seconds=300, max_size=256)
    query_cache.clear()
    yield
    v2_routes._reset_for_testing()
    query_cache.clear()


@pytest.fixture()
def app_and_client():
    """Mirrors tests/test_api_endpoints.py's fixture -- lifespan skipped,
    run_query fully mocked. Deliberately does NOT bake an Authorization
    header into the client (unlike the shared ``auth_settings`` fixture in
    tests/conftest.py) -- these tests need to control auth headers
    per-request."""
    import api.runner as runner_module
    import api.server as server_module

    server_module._system_prompt = "stub system prompt"
    with patch.object(runner_module, "run_query") as mock_run:
        client = TestClient(server_module.app, raise_server_exceptions=False)
        yield server_module.app, client, mock_run


def _mock_agent():
    from llm.base import SQLGenerationResult

    agent = MagicMock()
    agent._backend.name = "test"
    df = pd.DataFrame({"x": [1]})
    agent.run.return_value = (df, SQLGenerationResult(sql="SELECT 1", raw_response="SELECT 1", attempt=1))
    return agent


# ---------------------------------------------------------------------------
# 1. Every protected route -> 401 when unauthenticated
# ---------------------------------------------------------------------------

_BODY_FOR = {
    ("POST", "/query"): {"question": "hi"},
    ("POST", "/query/stream"): {"question": "hi"},
    ("POST", "/cache/invalidate"): {"question": "hi", "mode": "full"},
    ("POST", "/v2/sessions/{session_id}/turns"): {"question": "hi"},
    ("PATCH", "/v2/sessions/{session_id}/turns/{turn_id}/assumptions"): {"assumptions": []},
}
_PATH_FILL = {"session_id": "s_doesnotexist", "turn_id": "t_doesnotexist"}


def _protected_route_cases() -> list[tuple[str, str, dict | None]]:
    """Every (method, concrete_path, body) pair in ``app.routes`` except
    ``/health`` -- discovered from the live route table, not hand-copied,
    so a route added later without auth fails this test automatically."""
    import api.server as server_module

    cases: list[tuple[str, str, dict | None]] = []
    for route in server_module.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or path == "/health":
            continue
        concrete = path
        for k, v in _PATH_FILL.items():
            concrete = concrete.replace("{" + k + "}", v)
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            cases.append((method, concrete, _BODY_FOR.get((method, path))))
    return cases


_PROTECTED_CASES = _protected_route_cases()


class TestEveryProtectedRouteRequiresAuth:
    def test_route_discovery_found_something(self):
        # Guards against the parametrize below going vacuously green if
        # route discovery itself silently returns nothing.
        assert len(_PROTECTED_CASES) >= 8

    @pytest.mark.parametrize(
        "method,path,body", _PROTECTED_CASES,
        ids=[f"{m}:{p}" for m, p, _ in _PROTECTED_CASES],
    )
    def test_unauthenticated_request_gets_401(self, app_and_client, method, path, body):
        _, client, _ = app_and_client
        with override_settings(auth_required=True, api_keys_json=TWO_PRINCIPALS_JSON):
            resp = client.request(method, path, json=body)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code} (expected 401)"
        assert resp.json()["error"]["code"] == "UNAUTHENTICATED"
        assert resp.headers.get("www-authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# 2. Malformed / unknown / correct-prefix-wrong-tail key -> 401
# ---------------------------------------------------------------------------

class TestInvalidCredentialsAreRejected:
    @pytest.mark.parametrize("bad_header", [
        "Bearer not-a-real-key",
        "Bearer " + RAW_KEY_A[:-1] + "X",  # correct prefix, wrong tail
        "Basic dXNlcjpwYXNz",               # wrong scheme entirely
        "Bearer",                            # malformed: scheme with no token
        "Bearer    ",                        # malformed: whitespace-only token
        RAW_KEY_A,                           # missing the "Bearer " prefix
    ])
    def test_rejected(self, app_and_client, bad_header):
        _, client, _ = app_and_client
        with override_settings(auth_required=True, api_keys_json=TWO_PRINCIPALS_JSON):
            resp = client.get("/cache/stats", headers={"Authorization": bad_header})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


# ---------------------------------------------------------------------------
# 3. Valid key -> 200
# ---------------------------------------------------------------------------

class TestValidKeyAuthenticates:
    def test_valid_key_returns_200(self, app_and_client):
        _, client, _ = app_and_client
        with override_settings(auth_required=True, api_keys_json=TWO_PRINCIPALS_JSON):
            resp = client.get("/cache/stats", headers={"Authorization": f"Bearer {RAW_KEY_A}"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 4. Cache isolation by principal scope -- real QueryCache, real counters
# ---------------------------------------------------------------------------

class TestCacheIsolationByPrincipalScope:
    def test_different_denied_columns_same_question_is_a_miss(self):
        from api.query_cache import query_cache
        from api.runner import run_query

        principal_a = Principal(id="a", name="A", denied_columns=("NationalID",))
        principal_b = Principal(id="b", name="B", denied_columns=("Phone",))

        agent = _mock_agent()
        with patch("api.runner.agent", agent):
            run_query("سوال", "stub", mode="full", principal=principal_a)
            misses_before = query_cache.stats()["misses"]
            hits_before = query_cache.stats()["hits"]
            run_query("سوال", "stub", mode="full", principal=principal_b)

        stats = query_cache.stats()
        assert stats["misses"] == misses_before + 1
        assert stats["hits"] == hits_before
        assert agent.run.call_count == 2  # neither call reused the other's entry

    def test_identical_denied_columns_same_question_is_a_hit(self):
        from api.query_cache import query_cache
        from api.runner import run_query

        principal_a = Principal(id="a", name="A", denied_columns=("NationalID",))
        principal_c = Principal(id="c", name="C", denied_columns=("NationalID",))

        agent = _mock_agent()
        with patch("api.runner.agent", agent):
            run_query("سوال", "stub", mode="full", principal=principal_a)
            # query_cache is a process-wide singleton whose hit/miss counters
            # are never reset by clear() (only the entries are) -- so this
            # must assert on the DELTA this test itself produced, not an
            # absolute count, or it becomes order-dependent on whatever else
            # in the session already touched the same counters.
            hits_before = query_cache.stats()["hits"]
            run_query("سوال", "stub", mode="full", principal=principal_c)

        stats = query_cache.stats()
        assert stats["hits"] == hits_before + 1
        assert agent.run.call_count == 1  # second principal reused the first's entry


# ---------------------------------------------------------------------------
# 5. Cross-principal session access -> 404, never 403
# ---------------------------------------------------------------------------

class TestCrossPrincipalSessionAccess:
    def test_get_owned_by_another_principal_returns_404_not_403(self, app_and_client):
        _, client, _ = app_and_client
        with override_settings(auth_required=True, api_keys_json=TWO_PRINCIPALS_JSON):
            created = client.post("/v2/sessions", headers={"Authorization": f"Bearer {RAW_KEY_A}"})
            assert created.status_code == 201
            sid = created.json()["session_id"]

            resp = client.get(f"/v2/sessions/{sid}", headers={"Authorization": f"Bearer {RAW_KEY_B}"})
        assert resp.status_code == 404
        assert resp.status_code != 403

    def test_owner_can_still_read_their_own_session(self, app_and_client):
        _, client, _ = app_and_client
        with override_settings(auth_required=True, api_keys_json=TWO_PRINCIPALS_JSON):
            created = client.post("/v2/sessions", headers={"Authorization": f"Bearer {RAW_KEY_A}"})
            sid = created.json()["session_id"]

            resp = client.get(f"/v2/sessions/{sid}", headers={"Authorization": f"Bearer {RAW_KEY_A}"})
        assert resp.status_code == 200

    def test_delete_owned_by_another_principal_returns_404(self, app_and_client):
        _, client, _ = app_and_client
        with override_settings(auth_required=True, api_keys_json=TWO_PRINCIPALS_JSON):
            created = client.post("/v2/sessions", headers={"Authorization": f"Bearer {RAW_KEY_A}"})
            sid = created.json()["session_id"]

            resp = client.delete(f"/v2/sessions/{sid}", headers={"Authorization": f"Bearer {RAW_KEY_B}"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6. GET /health -- open, omits `model` when unauthenticated
# ---------------------------------------------------------------------------

class TestHealthOpenNoModelWhenUnauthenticated:
    def _healthy(self) -> HealthResponse:
        return HealthResponse(status="ok", openai=True, database=True, model="some-model")

    def test_no_credentials_returns_200_without_model(self, app_and_client):
        _, client, _ = app_and_client
        with override_settings(auth_required=True, api_keys_json=TWO_PRINCIPALS_JSON), \
                patch("api.health.check_health", return_value=self._healthy()):
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json().get("model") is None

    def test_valid_credentials_returns_model(self, app_and_client):
        _, client, _ = app_and_client
        with override_settings(auth_required=True, api_keys_json=TWO_PRINCIPALS_JSON), \
                patch("api.health.check_health", return_value=self._healthy()):
            resp = client.get("/health", headers={"Authorization": f"Bearer {RAW_KEY_A}"})
        assert resp.status_code == 200
        assert resp.json().get("model") == "some-model"


# ---------------------------------------------------------------------------
# 7. AUTH_REQUIRED=true + empty API_KEYS_JSON -> lifespan raises RuntimeError
# ---------------------------------------------------------------------------

class TestLifespanFailsClosedWithoutKeys:
    def test_raises_when_no_keys_configured(self):
        import asyncio

        import api.server as server_module

        async def _start():
            async with server_module.lifespan(server_module.app):
                pass  # pragma: no cover - must not be reached

        with override_settings(
            openai_model="llama3",
            db_connection_url=(
                "mssql+pyodbc://prod-db-host:1433/RealDB"
                "?driver=ODBC+Driver+17+for+SQL+Server"
            ),
            auth_required=True,
            api_keys_json="",
        ):
            with pytest.raises(RuntimeError, match="API_KEYS_JSON"):
                asyncio.run(_start())


# ---------------------------------------------------------------------------
# 8. AUTH_REQUIRED=false -> WARNING on every startup, not just the first
# ---------------------------------------------------------------------------

class TestLifespanWarnsOnEveryStartupWhenAuthDisabled:
    def test_warning_logged_on_each_startup(self, caplog):
        import asyncio

        import api.server as server_module

        async def _start():
            async with server_module.lifespan(server_module.app):
                pass

        with override_settings(
            openai_model="llama3",
            db_connection_url=(
                "mssql+pyodbc://prod-db-host:1433/RealDB"
                "?driver=ODBC+Driver+17+for+SQL+Server"
            ),
            auth_required=False,
            api_keys_json="",
        ):
            with caplog.at_level(logging.WARNING, logger="api.server"):
                asyncio.run(_start())
            first = sum(1 for r in caplog.records if "AUTH_REQUIRED" in r.message)
            caplog.clear()

            with caplog.at_level(logging.WARNING, logger="api.server"):
                asyncio.run(_start())
            second = sum(1 for r in caplog.records if "AUTH_REQUIRED" in r.message)

        assert first >= 1
        assert second >= 1  # not deduplicated across the two startups


# ---------------------------------------------------------------------------
# 9. Audit record for an authenticated query carries principal_id
# ---------------------------------------------------------------------------

class TestAuditRecordCarriesPrincipalId:
    def test_principal_id_written_to_audit_record(self, tmp_path):
        import api.runner as runner_module

        agent = _mock_agent()
        log_file = tmp_path / "audit_log.jsonl"
        principal = Principal(id="p_audit_1", name="Audit Tester")

        with patch("api.runner.agent", agent), \
                patch("observability.audit._AUDIT_LOG_FILE", str(log_file)):
            runner_module.run_query("سوال", "stub", mode="full", principal=principal)

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines
        record = json.loads(lines[-1])
        assert record["principal_id"] == "p_audit_1"
        assert "rows" not in record  # existing hard rule: never row data

    def test_no_principal_writes_null_principal_id(self, tmp_path):
        import api.runner as runner_module

        agent = _mock_agent()
        log_file = tmp_path / "audit_log.jsonl"

        with patch("api.runner.agent", agent), \
                patch("observability.audit._AUDIT_LOG_FILE", str(log_file)):
            runner_module.run_query("سوال", "stub", mode="full")

        record = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert record["principal_id"] is None


# ---------------------------------------------------------------------------
# 10. Rate-limit bucket is per principal, not per (shared) IP
# ---------------------------------------------------------------------------

class TestRateLimitBucketsPerPrincipal:
    def test_two_principals_same_ip_do_not_share_a_bucket(self):
        from api.auth import AuthMiddleware
        from api.middleware import RateLimitMiddleware

        app = FastAPI()
        # RateLimitMiddleware must run AFTER AuthMiddleware has resolved
        # request.state.principal -- add_middleware() applies in reverse,
        # so AuthMiddleware (added second) is the outer layer here,
        # matching api/server.py's real ordering.
        app.add_middleware(RateLimitMiddleware, requests_per_window=1, window_seconds=60, burst=0)
        app.add_middleware(AuthMiddleware)

        @app.get("/protected")
        def protected():
            return {"ok": True}

        client = TestClient(app, raise_server_exceptions=False)

        with override_settings(api_keys_json=TWO_PRINCIPALS_JSON):
            r1 = client.get("/protected", headers={"Authorization": f"Bearer {RAW_KEY_A}"})
            r2 = client.get("/protected", headers={"Authorization": f"Bearer {RAW_KEY_A}"})
            r3 = client.get("/protected", headers={"Authorization": f"Bearer {RAW_KEY_B}"})

        assert r1.status_code == 200
        assert r2.status_code == 429  # principal A's own single-token bucket is spent
        assert r3.status_code == 200  # principal B has an independent bucket, same IP


# ---------------------------------------------------------------------------
# security.auth._parse_api_keys hardening (flagged during review)
# ---------------------------------------------------------------------------

class TestApiKeyConfigValidation:
    def test_bare_string_denied_columns_is_rejected(self):
        raw = json.dumps([_entry("analyst", "Analyst", RAW_KEY_A, denied_columns="Price")])
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys(raw)

    def test_denied_columns_is_never_silently_split_into_characters(self):
        """Pre-fix behaviour: tuple("Price") -> ('P','r','i','c','e'), and
        'Price' being denied would read as False. Prove that shape is gone."""
        raw = json.dumps([_entry("analyst", "Analyst", RAW_KEY_A, denied_columns="Price")])
        try:
            keys = _parse_api_keys(raw)
        except ApiKeyConfigError:
            return  # rejected outright -- the desired, non-silent outcome
        principal = next(iter(keys.values()))
        assert principal.denied_columns != tuple("Price")

    def test_duplicate_key_sha256_is_rejected(self):
        dup_hash_entries = [
            _entry("restricted", "Restricted", RAW_KEY_A, denied_columns=["Price"]),
            {"id": "wide_open", "name": "Wide Open", "key_sha256": _sha256(RAW_KEY_A)},
        ]
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys(json.dumps(dup_hash_entries))

    def test_restricted_principal_is_never_silently_replaced(self):
        """Pre-fix behaviour: the second (wide-open) entry silently won,
        so a restricted principal's ACL vanished with no error. Prove
        that either the config is rejected, or the restriction survives."""
        dup_hash_entries = [
            _entry("restricted", "Restricted", RAW_KEY_A, denied_columns=["Price"]),
            {"id": "wide_open", "name": "Wide Open", "key_sha256": _sha256(RAW_KEY_A)},
        ]
        try:
            keys = _parse_api_keys(json.dumps(dup_hash_entries))
        except ApiKeyConfigError:
            return  # rejected outright -- the desired, non-silent outcome
        principal = keys[_sha256(RAW_KEY_A)]
        assert principal.denied_columns == ("Price",)

    def test_malformed_key_sha256_is_rejected(self):
        """A raw key pasted into key_sha256 by mistake must fail loudly at
        parse time, not silently become a principal nobody can ever
        authenticate as."""
        raw = json.dumps([{"id": "a", "name": "A", "key_sha256": RAW_KEY_A}])
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys(raw)

    def test_non_string_name_is_rejected(self):
        raw = json.dumps([{"id": "a", "name": 123, "key_sha256": _sha256(RAW_KEY_A)}])
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys(raw)


# ---------------------------------------------------------------------------
# Column-level ACL, end to end (Principal.denied_columns -> validate_sql)
# ---------------------------------------------------------------------------
# Everything above proves the plumbing (config parsing, 401s, cache scope,
# session ownership). None of it proves denied_columns actually reaches
# security.sql_guard.validate_sql -- a principal could get its own cache
# partition and STILL be able to SELECT a column it is supposed to be
# denied. These tests exercise the real guard (a real SQLAgent / TurnEngine,
# not a mocked run_query) so a dropped `denied_columns=` at any call site
# fails them for the right reason.

_ACL_SQL = "SELECT TOP 5 NationalID FROM Customer"  # Customer.NationalID is a real column


def _real_sql_agent():
    """A real SQLAgent -- MockBackend for the LLM, an in-memory execute_fn --
    so clean_sql/validate_sql/ensure_top all run for real. Deterministic:
    MockBackend returns the same SQL regardless of prompt content, so the
    only thing that can differ between two calls is the principal's
    denied_columns."""
    from llm.providers import MockBackend
    from llm.sql_agent import SQLAgent

    df = pd.DataFrame({"NationalID": ["0012345678"]})
    return SQLAgent(backend=MockBackend(response=_ACL_SQL), execute_fn=lambda sql: df.copy())


@pytest.fixture()
def unmocked_app_and_client():
    """Like ``app_and_client``, but does NOT patch ``api.runner.run_query``
    -- these tests need the real pipeline (including the real guard) to
    run, only the LLM/DB boundary is stubbed (via ``api.runner.agent``,
    patched per-test)."""
    import api.server as server_module

    server_module._system_prompt = "stub system prompt"
    client = TestClient(server_module.app, raise_server_exceptions=False)
    yield server_module.app, client


class TestColumnLevelACLThroughQuery:
    """POST /query: a principal's denied_columns must actually be enforced
    by the guard, not just used to partition the cache."""

    def test_restricted_principal_gets_guard_rejection(self, unmocked_app_and_client):
        import api.runner as runner_module

        _, client = unmocked_app_and_client
        keys_json = json.dumps([_entry("restricted", "Restricted", RAW_KEY_A, denied_columns=["NationalID"])])
        with override_settings(auth_required=True, api_keys_json=keys_json), \
                patch.object(runner_module, "agent", _real_sql_agent()):
            resp = client.post(
                "/query",
                json={"question": "کد ملی مشتریان را نشان بده", "mode": "full"},
                headers={"Authorization": f"Bearer {RAW_KEY_A}"},
            )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "FORBIDDEN_SQL"

    def test_all_access_principal_succeeds_on_the_same_sql(self, unmocked_app_and_client):
        """Same generated SQL (MockBackend is deterministic), same
        question, only the principal's denied_columns differs -- proving
        the restriction is per-principal, not a blanket guard block."""
        import api.runner as runner_module

        _, client = unmocked_app_and_client
        keys_json = json.dumps([_entry("wide-open", "Wide Open", RAW_KEY_B)])  # no denied_columns
        with override_settings(auth_required=True, api_keys_json=keys_json), \
                patch.object(runner_module, "agent", _real_sql_agent()):
            resp = client.post(
                "/query",
                json={"question": "کد ملی مشتریان را نشان بده", "mode": "full"},
                headers={"Authorization": f"Bearer {RAW_KEY_B}"},
            )
        assert resp.status_code == 200
        assert resp.json()["sql"] == _ACL_SQL


class TestColumnLevelACLThroughSessionTurns:
    """POST /v2/sessions/{id}/turns: the session engine is a separate
    validate_sql call site (session/engine.py) from the /query path
    (api/runner.py -> llm/sql_agent.py) -- prove it independently.

    Per docs/api-contract-v2.md's "answer, then declare" policy, a guard
    rejection here is never an HTTP-level error -- the endpoint always
    returns 200, and the rejection shows up as the turn's own
    guard.verdict. That is the taxonomy this endpoint already uses for a
    guard rejection; asserting anything else would not match the contract.
    """

    def _engine_for(self, sql: str) -> "TurnEngine":  # noqa: F821 - imported below
        from llm.providers import MockBackend
        from llm.router import LLMRouter
        from session.engine import TurnEngine

        df = pd.DataFrame({"NationalID": ["0012345678"]})
        return TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response=sql)]),
            execute_fn=lambda s: df.copy(),
        )

    def test_restricted_principal_gets_guard_rejection(self, app_and_client):
        import api.v2_routes as v2_routes

        _, client, _ = app_and_client
        v2_routes._system_prompt = "stub system prompt"
        v2_routes._turn_engine = self._engine_for(_ACL_SQL)
        keys_json = json.dumps([_entry("restricted", "Restricted", RAW_KEY_A, denied_columns=["NationalID"])])
        try:
            with override_settings(auth_required=True, api_keys_json=keys_json):
                sid = client.post(
                    "/v2/sessions", headers={"Authorization": f"Bearer {RAW_KEY_A}"},
                ).json()["session_id"]
                resp = client.post(
                    f"/v2/sessions/{sid}/turns",
                    json={"question": "کد ملی مشتریان را نشان بده"},
                    headers={"Authorization": f"Bearer {RAW_KEY_A}"},
                )
        finally:
            v2_routes._reset_for_testing()

        assert resp.status_code == 200  # per contract: never an HTTP error
        turn = resp.json()
        assert turn["guard"]["verdict"] == "rejected"

    def test_all_access_principal_succeeds_on_the_same_sql(self, app_and_client):
        import api.v2_routes as v2_routes

        _, client, _ = app_and_client
        v2_routes._system_prompt = "stub system prompt"
        v2_routes._turn_engine = self._engine_for(_ACL_SQL)
        keys_json = json.dumps([_entry("wide-open", "Wide Open", RAW_KEY_B)])  # no denied_columns
        try:
            with override_settings(auth_required=True, api_keys_json=keys_json):
                sid = client.post(
                    "/v2/sessions", headers={"Authorization": f"Bearer {RAW_KEY_B}"},
                ).json()["session_id"]
                resp = client.post(
                    f"/v2/sessions/{sid}/turns",
                    json={"question": "کد ملی مشتریان را نشان بده"},
                    headers={"Authorization": f"Bearer {RAW_KEY_B}"},
                )
        finally:
            v2_routes._reset_for_testing()

        assert resp.status_code == 200
        turn = resp.json()
        assert turn["guard"]["verdict"] == "allowed"
        assert turn["sql"] == _ACL_SQL
