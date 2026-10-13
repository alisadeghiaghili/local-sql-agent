# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``GET /admin/quality`` -- the real-use quality report's HTTP surface.

A thin, read-only surface over ``observability.quality.build_quality_report``
(the same function ``scripts/analyze_audit_log.py`` prints), gated on
operations-or-security like ``/admin/usage``. Exercised against the real
FastAPI app, a real ``TestClient``, a real audit-log file and a real
application database on ``tmp_path``; nothing under test is faked.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import config as cfg
from appdb.confirmations import confirm_turn
from appdb.engine import dispose_app_engine
from appdb.feedback import submit_flag
from appdb.key_store import invalidate_cache
from observability.audit import AuditRecord, save_audit_record
from scripts.analyze_audit_log import iter_records, resolve_log_paths
from observability.quality import build_quality_report

SECRET = "پرسش-محرمانه-۶۶۳"


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RAW_OPS_KEY = "a" * 40
RAW_SECURITY_KEY = "b" * 40
RAW_ANALYST_KEY = "c" * 40

_KEYS_JSON = json.dumps([
    {"id": "ops-admin", "name": "Ops Admin", "key_sha256": _sha256(RAW_OPS_KEY), "operations": True},
    {"id": "security-admin", "name": "Security Admin",
     "key_sha256": _sha256(RAW_SECURITY_KEY), "security": True},
    {"id": "analyst-1", "name": "Analyst", "key_sha256": _sha256(RAW_ANALYST_KEY)},
])


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


@pytest.fixture()
def env(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    with cfg.override_settings(
        app_db_url=f"sqlite:///{tmp_path / 'appdb.db'}", api_keys_json=_KEYS_JSON,
        auth_required=True, log_dir=str(log_dir), admin_expensive_cache_ttl_seconds=300,
    ):
        dispose_app_engine()
        invalidate_cache()
        yield {"log_dir": log_dir}
    dispose_app_engine()
    invalidate_cache()


@pytest.fixture()
def client(env):
    import api.server as server_module
    import api.v2_routes as v2_routes

    server_module._system_prompt = "stub system prompt"
    v2_routes._reset_for_testing()
    yield TestClient(server_module.app, raise_server_exceptions=False)
    v2_routes._reset_for_testing()


def _answer(turn, *, ago_days=1, ds="sales", code=None, total=100, question=SECRET, ttft=None):
    llm = {"model": "openai:real", "corrections": 0}
    if ttft is not None:
        llm.update(ttft_ms=ttft, generation_ms=total - ttft, total_ms=total)
    save_audit_record(AuditRecord(
        timestamp=datetime.now() - timedelta(days=ago_days),
        request_id=f"r_{turn}", question=question,
        generated_sql="" if code else "SELECT TOP 1 LeakedColumn FROM LeakedTable",
        guard={"verdict": "allowed"}, row_count=1, error_code=code,
        columns=None if code else ["n"], session_id="s", turn_id=turn, datasource=ds,
        timings={"total_ms": total, "llm_ms": total // 2}, llm=llm, principal_id="alice-the-analyst",
    ))


class TestQualityRoute:
    def test_figures_match_the_shared_computation(self, client, env):
        _answer("t1", total=100, ttft=40)
        _answer("t2", total=300, ttft=60)
        _answer("t3", code="OUT_OF_SCOPE", total=50)
        _answer("t_old", ago_days=40, ds="stock")
        confirm_turn(session_id="s", turn_id="t1", principal_id="analyst-1")
        submit_flag(session_id="s", turn_id="t2", reporter_principal_id="analyst-1", category="other")

        resp = client.get("/admin/quality", headers=_auth(RAW_OPS_KEY))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        recent, all_time = body["recent"]["overall"], body["all_time"]["overall"]
        assert (recent["questions"], all_time["questions"]) == (3, 4)
        assert recent["executed"] == 2 and recent["out_of_scope"] == 1
        assert recent["ratings"] == {
            "available": True, "confirmed": 1, "flagged": 1, "rated": 2, "confirmed_share_of_rated": 0.5,
        }
        assert recent["latency"]["total_ms"]["p50"] == 100
        assert recent["latency"]["llm_split"]["streamed_call_count"] == 2
        assert set(body["all_time"]["by_datasource"]) == {"sales", "stock"}
        assert body["window_days"] == 7
        assert "not accuracy" in body["caveats"]["confirmed_share"]

        # Identical to calling the function directly on the same files.
        direct = build_quality_report(
            list(iter_records(resolve_log_paths([f"{env['log_dir']}/audit_log.jsonl*"]))),
            confirmations=[{"session_id": "s", "turn_id": "t1"}],
            flags=[{"session_id": "s", "turn_id": "t2"}],
        )
        assert recent == direct["recent"]["overall"]

    def test_days_sets_the_window(self, client):
        _answer("t1", ago_days=1)
        _answer("t2", ago_days=20)

        week = client.get("/admin/quality", headers=_auth(RAW_OPS_KEY)).json()
        month = client.get("/admin/quality?days=30", headers=_auth(RAW_OPS_KEY)).json()

        assert week["recent"]["overall"]["questions"] == 1
        assert month["window_days"] == 30 and month["recent"]["overall"]["questions"] == 2

    @pytest.mark.parametrize("days", ["0", "-3", "abc", "100000"])
    def test_an_invalid_window_is_rejected(self, client, days):
        assert client.get(f"/admin/quality?days={days}", headers=_auth(RAW_OPS_KEY)).status_code == 422

    def test_an_empty_log_is_a_valid_report_not_an_error(self, client):
        resp = client.get("/admin/quality", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200
        overall = resp.json()["recent"]["overall"]
        assert overall["questions"] == 0 and overall["execution_success_rate"] is None

    def test_no_question_text_sql_or_principal_leaves_the_server(self, client):
        _answer("t1")
        confirm_turn(session_id="s", turn_id="t1", principal_id="analyst-secret-id")

        text = client.get("/admin/quality", headers=_auth(RAW_OPS_KEY)).text

        for private in (SECRET, "LeakedColumn", "alice-the-analyst", "analyst-secret-id"):
            assert private not in text

    def test_it_is_read_only(self, client, env):
        _answer("t1")
        before = {p.name: p.read_bytes() for p in env["log_dir"].iterdir()}
        client.get("/admin/quality", headers=_auth(RAW_OPS_KEY))
        assert {p.name: p.read_bytes() for p in env["log_dir"].iterdir()} == before


class TestAuthorisation:
    def test_operations_may_read_it(self, client):
        assert client.get("/admin/quality", headers=_auth(RAW_OPS_KEY)).status_code == 200

    def test_security_may_read_it(self, client):
        assert client.get("/admin/quality", headers=_auth(RAW_SECURITY_KEY)).status_code == 200

    def test_an_analyst_key_is_403(self, client):
        assert client.get("/admin/quality", headers=_auth(RAW_ANALYST_KEY)).status_code == 403

    def test_no_key_is_401(self, client):
        assert client.get("/admin/quality").status_code == 401

    def test_only_get_exists(self, client):
        assert client.post("/admin/quality", headers=_auth(RAW_OPS_KEY)).status_code == 405


class TestCaching:
    """The audit log is read in full per computation, so the panel's 30-second
    auto-refresh is served from a cache (like the schema-drift card)."""

    def test_a_second_call_is_served_from_the_cache_until_refresh(self, client):
        _answer("t1")
        first = client.get("/admin/quality", headers=_auth(RAW_OPS_KEY)).json()
        assert first["cache"]["cached"] is False

        _answer("t2")
        second = client.get("/admin/quality", headers=_auth(RAW_OPS_KEY)).json()
        assert second["cache"]["cached"] is True
        assert second["recent"]["overall"]["questions"] == 1, "served from the cache"

        forced = client.get("/admin/quality?refresh=1", headers=_auth(RAW_OPS_KEY)).json()
        assert forced["cache"]["cached"] is False
        assert forced["recent"]["overall"]["questions"] == 2

    def test_each_window_has_its_own_cache_entry(self, client):
        _answer("t1", ago_days=20)
        week = client.get("/admin/quality", headers=_auth(RAW_OPS_KEY)).json()
        month = client.get("/admin/quality?days=30", headers=_auth(RAW_OPS_KEY)).json()
        assert week["recent"]["overall"]["questions"] == 0
        assert month["recent"]["overall"]["questions"] == 1

    def test_a_zero_ttl_disables_the_cache(self, client):
        _answer("t1")
        with cfg.override_settings(admin_expensive_cache_ttl_seconds=0):
            client.get("/admin/quality", headers=_auth(RAW_OPS_KEY))
            _answer("t2")
            again = client.get("/admin/quality", headers=_auth(RAW_OPS_KEY)).json()
        assert again["cache"]["cached"] is False
        assert again["recent"]["overall"]["questions"] == 2


class TestDegradation:
    def test_an_unreadable_application_database_leaves_the_rest_of_the_report(
        self, client, monkeypatch,
    ):
        import observability.quality as quality

        _answer("t1")
        monkeypatch.setattr(
            quality, "load_ratings",
            lambda: quality.Ratings(confirmations=None, flags=None, error="OperationalError"),
        )

        resp = client.get("/admin/quality", headers=_auth(RAW_OPS_KEY))

        assert resp.status_code == 200
        overall = resp.json()["recent"]["overall"]
        assert overall["questions"] == 1
        assert overall["ratings"]["available"] is False
