# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 6 -- the operational tier's HTTP surface
(api/admin_ops_routes.py). Frozen spec.

Schema drift, vocabulary freshness/manual-refresh, per-analyst usage, and
cache controls, each exercised against the real FastAPI app and a real
TestClient. Only ``retrieval.dimension_vocabulary``'s ``execute_fn`` and
the audit-log file contents are ever faked -- never the routes, the role
gating, or the admin-audit trail under test.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import config as cfg
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache
from retrieval.dimension_vocabulary import PREFETCH_COLUMNS, clear_vocabulary_cache


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RAW_OPS_KEY = "5" * 40
RAW_SECURITY_KEY = "6" * 40
RAW_ANALYST_KEY = "7" * 40

_KEYS_JSON = json.dumps([
    {"id": "ops-admin", "name": "Ops Admin", "key_sha256": _sha256(RAW_OPS_KEY), "operations": True},
    {
        "id": "security-admin", "name": "Security Admin",
        "key_sha256": _sha256(RAW_SECURITY_KEY), "security": True,
    },
    {"id": "analyst-1", "name": "Analyst", "key_sha256": _sha256(RAW_ANALYST_KEY)},
])


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


@pytest.fixture(autouse=True)
def _clean_vocab_cache():
    clear_vocabulary_cache()
    yield
    clear_vocabulary_cache()


@pytest.fixture()
def app_db(tmp_path):
    db_path = tmp_path / "appdb.db"
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}", api_keys_json=_KEYS_JSON, auth_required=True,
    ):
        dispose_app_engine()
        invalidate_cache()
        yield db_path
    dispose_app_engine()
    invalidate_cache()


@pytest.fixture()
def client(app_db):
    import api.server as server_module
    import api.v2_routes as v2_routes
    from api.query_cache import query_cache

    server_module._system_prompt = "stub system prompt"
    v2_routes._reset_for_testing()
    query_cache.reconfigure(ttl_seconds=300, max_size=256)
    query_cache.clear()
    yield TestClient(server_module.app, raise_server_exceptions=False)
    v2_routes._reset_for_testing()
    query_cache.clear()


# ---------------------------------------------------------------------------
# Schema drift -- read-only; §2's "must not need/accept elevated
# credentials" also means the route has no engine-injection seam at all,
# so under this suite's _no_real_database fixture it must fail closed
# with a clear 503, never a crash.
# ---------------------------------------------------------------------------

class TestSchemaDriftRoute:
    def test_unreachable_warehouse_is_a_clear_503_not_a_crash(self, client):
        resp = client.get("/admin/schema-drift", headers=_auth(RAW_SECURITY_KEY))
        assert resp.status_code == 503
        assert "warehouse" in resp.json()["detail"].lower()

    def test_requires_operations_or_security(self, client):
        resp = client.get("/admin/schema-drift", headers=_auth(RAW_ANALYST_KEY))
        assert resp.status_code == 403


def _fake_drift_report(detail: str = "ok"):
    class _FakeReport:
        def as_dict(self):
            return {
                "checked_at": detail, "schemas_scanned": [], "warehouse_only": [],
                "schema_only": [], "type_changed": [], "unverifiable_tables": [],
                "baseline_available": True,
            }

    return _FakeReport()


# ---------------------------------------------------------------------------
# Schema drift -- result caching (Required item 1, 2026 warehouse-load
# audit). ``check_schema_drift`` reflects the entire warehouse catalogue;
# these tests fake THAT call (via api.admin_ops_routes' own imported
# name, never the route/auth machinery) so they are about the cache, not
# about schema_data.drift itself (covered by tests/test_schema_drift.py).
# ---------------------------------------------------------------------------

class TestSchemaDriftCaching:
    def test_second_call_inside_the_ttl_does_not_reflect_again(self, client, monkeypatch):
        import api.admin_ops_routes as admin_ops_routes_module

        calls = {"n": 0}

        def fake_check_schema_drift():
            calls["n"] += 1
            return _fake_drift_report()

        monkeypatch.setattr(admin_ops_routes_module, "check_schema_drift", fake_check_schema_drift)

        with cfg.override_settings(admin_expensive_cache_ttl_seconds=300):
            first = client.get("/admin/schema-drift", headers=_auth(RAW_SECURITY_KEY))
            second = client.get("/admin/schema-drift", headers=_auth(RAW_SECURITY_KEY))

        assert first.status_code == 200 and second.status_code == 200
        assert calls["n"] == 1, (
            f"check_schema_drift ran {calls['n']} times for 2 calls inside the TTL"
        )
        assert first.json()["cache"]["cached"] is False
        assert second.json()["cache"]["cached"] is True

    def test_refresh_bypasses_the_cache(self, client, monkeypatch):
        import api.admin_ops_routes as admin_ops_routes_module

        calls = {"n": 0}

        def fake_check_schema_drift():
            calls["n"] += 1
            return _fake_drift_report()

        monkeypatch.setattr(admin_ops_routes_module, "check_schema_drift", fake_check_schema_drift)

        with cfg.override_settings(admin_expensive_cache_ttl_seconds=300):
            client.get("/admin/schema-drift", headers=_auth(RAW_SECURITY_KEY))
            refreshed = client.get(
                "/admin/schema-drift?refresh=1", headers=_auth(RAW_SECURITY_KEY),
            )

        assert calls["n"] == 2, "?refresh=1 must force a fresh reflection"
        assert refreshed.json()["cache"]["cached"] is False

    def test_cache_expires_after_the_ttl(self, monkeypatch):
        """Direct function call, not through TestClient -- see
        ``tests/test_admin.py::TestHealthChecksCaching.test_cache_expires_after_the_ttl``
        for why patching the process-wide ``time.monotonic`` under a real
        HTTP round trip (which also drives the rate limiter) is avoided."""
        import api.admin_ops_routes as admin_ops_routes_module
        import api.admin_result_cache as admin_result_cache_module
        from api.admin_ops_routes import admin_schema_drift
        from security.auth import OPERATIONS_CAPABILITY, Principal

        calls = {"n": 0}

        def fake_check_schema_drift():
            calls["n"] += 1
            return _fake_drift_report()

        monkeypatch.setattr(admin_ops_routes_module, "check_schema_drift", fake_check_schema_drift)

        clock = {"t": 1000.0}
        monkeypatch.setattr(admin_result_cache_module.time, "monotonic", lambda: clock["t"])

        ops_principal = Principal(id="ops-1", name="Ops", capabilities=frozenset({OPERATIONS_CAPABILITY}))

        with cfg.override_settings(admin_expensive_cache_ttl_seconds=5):
            admin_schema_drift(refresh=False, principal=ops_principal)
            clock["t"] += 6
            expired = admin_schema_drift(refresh=False, principal=ops_principal)

        assert calls["n"] == 2, "the cached result must expire once the TTL elapses"
        assert expired["cache"]["cached"] is False

    def test_a_failed_reflection_is_never_cached(self, client, monkeypatch):
        import api.admin_ops_routes as admin_ops_routes_module

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("warehouse briefly unreachable")
            return _fake_drift_report()

        monkeypatch.setattr(admin_ops_routes_module, "check_schema_drift", flaky)

        with cfg.override_settings(admin_expensive_cache_ttl_seconds=300):
            first = client.get("/admin/schema-drift", headers=_auth(RAW_SECURITY_KEY))
            second = client.get("/admin/schema-drift", headers=_auth(RAW_SECURITY_KEY))

        assert first.status_code == 503
        assert second.status_code == 200
        assert calls["n"] == 2, "a failed reflection must not be cached as a false success"


# ---------------------------------------------------------------------------
# Vocabulary freshness + manual refresh
# ---------------------------------------------------------------------------

class TestVocabularyRoutes:
    def _one_pair(self):
        table = next(iter(PREFETCH_COLUMNS))
        column = PREFETCH_COLUMNS[table][0]
        return table, column

    def test_status_lists_every_prefetch_column(self, client):
        resp = client.get("/admin/vocabulary", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200
        columns = resp.json()["columns"]
        expected = sum(len(cols) for cols in PREFETCH_COLUMNS.values())
        assert len(columns) == expected

    def test_refresh_unknown_pair_is_404(self, client):
        resp = client.post(
            "/admin/vocabulary/NotATable/NotAColumn/refresh", headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 404

    def test_refresh_requires_operations(self, client):
        table, column = self._one_pair()
        resp = client.post(
            f"/admin/vocabulary/{table}/{column}/refresh", headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 403

    def test_successful_refresh_is_recorded_in_the_admin_action_trail(self, client, monkeypatch):
        table, column = self._one_pair()
        import pandas as pd

        def fake_execute_sql_params(sql, params):
            return pd.DataFrame({column: ["value one"]})

        monkeypatch.setattr(
            "database.executor.execute_sql_params", fake_execute_sql_params,
        )
        resp = client.post(
            f"/admin/vocabulary/{table}/{column}/refresh", headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        actions = client.get("/admin/actions", headers=_auth(RAW_OPS_KEY)).json()["actions"]
        assert any(a["action"] == "vocabulary.refresh" for a in actions)

    def test_failing_refresh_reports_failure_not_success(self, client, monkeypatch):
        table, column = self._one_pair()

        def failing_execute(sql, params):
            raise RuntimeError("simulated outage")

        monkeypatch.setattr("database.executor.execute_sql_params", failing_execute)
        resp = client.post(
            f"/admin/vocabulary/{table}/{column}/refresh", headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "simulated outage" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Per-analyst usage
# ---------------------------------------------------------------------------

class TestUsageRoute:
    def test_figures_match_audit_log_directly(self, client, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "audit_log.jsonl").write_text(
            "\n".join([
                json.dumps({
                    "timestamp": "2026-01-01T00:00:00+00:00", "principal_id": "analyst-1",
                    "error_code": None, "timings": {"total_ms": 100},
                }),
                json.dumps({
                    "timestamp": "2026-01-01T00:00:01+00:00", "principal_id": "analyst-1",
                    "error_code": "QUERY_EXECUTION_ERROR", "timings": {"total_ms": 200},
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        with cfg.override_settings(log_dir=str(log_dir)):
            resp = client.get("/admin/usage", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200
        body = resp.json()
        assert body["principals"]["analyst-1"]["queries"] == 2
        assert body["principals"]["analyst-1"]["failures"] == 1
        assert body["rate_limit_never_triggered"] is True

    def test_requires_operations_or_security(self, client):
        resp = client.get("/admin/usage", headers=_auth(RAW_ANALYST_KEY))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Cache controls
# ---------------------------------------------------------------------------

class TestCacheRoutes:
    def test_clear_is_recorded_and_echoes_the_pre_clear_snapshot(self, client):
        from api.query_cache import query_cache
        from api.models import QueryResponse

        query_cache.set(
            "سوال", "full",
            QueryResponse(question="سوال", sql="SELECT 1", result=None, row_count=0,
                           correction_attempts=0, elapsed_seconds=0.0),
            prefix_version="v1",
        )
        assert query_cache.stats()["size"] == 1

        resp = client.post("/admin/cache/clear", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200
        body = resp.json()
        assert body["stats_before_clear"]["size"] == 1
        assert query_cache.stats()["size"] == 0

        actions = client.get("/admin/actions", headers=_auth(RAW_OPS_KEY)).json()["actions"]
        assert any(a["action"] == "cache.clear" for a in actions)

    def test_clear_requires_operations(self, client):
        resp = client.post("/admin/cache/clear", headers=_auth(RAW_SECURITY_KEY))
        assert resp.status_code == 403

    def test_invalidate_missing_entry_is_404(self, client):
        resp = client.post(
            "/admin/cache/invalidate", json={"question": "no such question", "mode": "full"},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Failed-authentication visibility
# ---------------------------------------------------------------------------

class TestAuthFailuresRoute:
    def test_route_requires_operations_or_security(self, client):
        resp = client.get("/admin/security/auth-failures", headers=_auth(RAW_ANALYST_KEY))
        assert resp.status_code == 403

    def test_reports_a_bad_key_attempt(self, client, tmp_path):
        import security.auth_failures as auth_failures_module

        path = tmp_path / "auth_failure_log.jsonl"
        auth_failures_module._AUTH_FAILURE_LOG_FILE = str(path)
        try:
            client.get("/admin/summary", headers={"Authorization": "Bearer totally-wrong"})
            resp = client.get("/admin/security/auth-failures", headers=_auth(RAW_OPS_KEY))
        finally:
            auth_failures_module._AUTH_FAILURE_LOG_FILE = ""
        assert resp.status_code == 200
        assert resp.json()["total"] == 1
