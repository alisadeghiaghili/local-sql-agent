# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 4 -- ``/admin/feedback/*`` at the HTTP boundary. Frozen spec.

A real FastAPI app, a real ``TestClient``, a real application database on a
real temp SQLite file, a real (copied) ``project_config.example/`` and
``eval_data.example/golden.jsonl``, a real session store, and a real
(injected-backend) ``TurnEngine`` -- no mock at the boundary under test.
``tests/test_appdb_feedback.py`` covers :mod:`appdb.feedback` itself in
depth; this module covers the thin HTTP surface over it: role gating and
the end-to-end triage flow, including promotion to a golden case.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import config as cfg
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache
from eval.runner import load_golden_cases, make_offline_executor, make_offline_generator, run_golden_set
from llm.providers import MockBackend
from llm.router import LLMRouter
from session.engine import TurnEngine

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"
_EXAMPLE_GOLDEN_PATH = _REPO_ROOT / "eval_data.example" / "golden.jsonl"

SIMPLE_SQL = "SELECT TOP 10 c.Name AS CustomerName FROM Customer c"
SIMPLE_DF = pd.DataFrame({"CustomerName": ["A", "B"]})


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RAW_OPS_KEY = "8" * 40
RAW_SECURITY_KEY = "9" * 40
RAW_ANALYST_KEY = "a" * 40

_KEYS_JSON = json.dumps([
    {"id": "ops-admin", "name": "Ops Admin", "key_sha256": _sha256(RAW_OPS_KEY), "operations": True},
    {
        "id": "security-admin", "name": "Security Admin",
        "key_sha256": _sha256(RAW_SECURITY_KEY), "security": True,
    },
    {"id": "analyst-1", "name": "Analyst One", "key_sha256": _sha256(RAW_ANALYST_KEY)},
])


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


@pytest.fixture(autouse=True)
def _reset_shared_state():
    import api.v2_routes as v2_routes
    from api.query_cache import query_cache

    v2_routes._reset_for_testing()
    query_cache.reconfigure(ttl_seconds=300, max_size=256)
    query_cache.clear()
    yield
    v2_routes._reset_for_testing()
    query_cache.clear()


@pytest.fixture()
def app_db(tmp_path):
    project_dir = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE_CONFIG_DIR, project_dir)
    golden_path = tmp_path / "golden.jsonl"
    shutil.copyfile(_EXAMPLE_GOLDEN_PATH, golden_path)  # a real, but disposable, copy
    db_path = tmp_path / "appdb.db"
    export_dir = tmp_path / "export"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}",
        api_keys_json=_KEYS_JSON,
        project_config_dir=str(project_dir),
        eval_golden_path=str(golden_path),
        config_export_dir=str(export_dir),
        session_store_path=str(tmp_path / "sessions.db"),
        log_dir=str(log_dir),
    ):
        dispose_app_engine()
        invalidate_cache()
        yield {"project_dir": project_dir, "golden_path": golden_path, "log_dir": log_dir}
    dispose_app_engine()
    invalidate_cache()


@pytest.fixture()
def client(app_db):
    import api.server as server_module
    import api.v2_routes as v2_routes

    server_module._system_prompt = "stub system prompt"
    v2_routes._system_prompt = "stub system prompt"
    engine = TurnEngine(
        router=LLMRouter(default_chain=[MockBackend(response=SIMPLE_SQL)]),
        execute_fn=lambda sql: SIMPLE_DF.copy(),
    )
    v2_routes._turn_engine = engine
    return TestClient(server_module.app, raise_server_exceptions=False)


def _ask_and_flag(client) -> dict:
    """Create a session, ask one question (with real analyst auth,
    producing a real audit record), flag it, and return the stored flag."""
    sid = client.post("/v2/sessions", headers=_auth(RAW_ANALYST_KEY)).json()["session_id"]
    turn = client.post(
        f"/v2/sessions/{sid}/turns", json={"question": "این یک پرسش آزمایشی برای بازخورد است؟"},
        headers=_auth(RAW_ANALYST_KEY),
    ).json()
    resp = client.post(
        f"/v2/sessions/{sid}/turns/{turn['turn_id']}/feedback",
        json={"category": "wrong_number", "note": "عدد اشتباه به نظر می‌رسد"},
        headers=_auth(RAW_ANALYST_KEY),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Role gating -- both admin roles may triage (architecture §2 table)
# ---------------------------------------------------------------------------


class TestRoleGating:
    def test_analyst_key_gets_403_on_the_triage_queue(self, client):
        _ask_and_flag(client)
        resp = client.get("/admin/feedback", headers=_auth(RAW_ANALYST_KEY))
        assert resp.status_code == 403

    def test_operations_key_can_read_the_triage_queue(self, client):
        _ask_and_flag(client)
        resp = client.get("/admin/feedback", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200

    def test_security_key_can_read_the_triage_queue(self, client):
        _ask_and_flag(client)
        resp = client.get("/admin/feedback", headers=_auth(RAW_SECURITY_KEY))
        assert resp.status_code == 200

    def test_operations_key_can_resolve_a_flag(self, client):
        flag = _ask_and_flag(client)
        resp = client.post(
            f"/admin/feedback/{flag['feedback_id']}/resolve",
            json={"outcome": "not_a_defect", "note": "Confirmed correct with the analyst."},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 200

    def test_security_key_can_resolve_a_flag(self, client):
        flag = _ask_and_flag(client)
        resp = client.post(
            f"/admin/feedback/{flag['feedback_id']}/resolve",
            json={"outcome": "not_a_defect", "note": "Confirmed correct with the analyst."},
            headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# The triage queue joins to the audit record (spec §3)
# ---------------------------------------------------------------------------


class TestTriageQueueJoinsTheAuditRecord:
    def test_list_shows_question_sql_and_guard_verdict(self, client):
        _ask_and_flag(client)
        resp = client.get("/admin/feedback", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200
        rows = resp.json()["feedback"]
        assert len(rows) == 1
        assert rows[0]["audit"]["question"] == "این یک پرسش آزمایشی برای بازخورد است؟"
        assert rows[0]["audit"]["generated_sql"]
        assert rows[0]["audit"]["guard"]["verdict"] == "allowed"

    def test_get_one_flag_carries_the_same_join(self, client):
        flag = _ask_and_flag(client)
        resp = client.get(f"/admin/feedback/{flag['feedback_id']}", headers=_auth(RAW_SECURITY_KEY))
        assert resp.status_code == 200
        assert resp.json()["audit"]["question"] == "این یک پرسش آزمایشی برای بازخورد است؟"

    def test_unknown_feedback_id_is_404(self, client):
        resp = client.get("/admin/feedback/999999", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Triage requires an outcome
# ---------------------------------------------------------------------------


class TestResolveRequiresAnOutcome:
    def test_unknown_outcome_is_rejected(self, client):
        flag = _ask_and_flag(client)
        resp = client.post(
            f"/admin/feedback/{flag['feedback_id']}/resolve",
            json={"outcome": "ignore_it"},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 422

    def test_not_a_defect_without_a_note_is_rejected(self, client):
        flag = _ask_and_flag(client)
        resp = client.post(
            f"/admin/feedback/{flag['feedback_id']}/resolve",
            json={"outcome": "not_a_defect"},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Promoting to a golden case (spec §4) -- pending_expected, ignored by the
# gate -- and nothing auto-applies (spec §3.2)
# ---------------------------------------------------------------------------


class TestGoldenCasePromotionAndNoAutoApply:
    def test_resolving_as_golden_case_writes_pending_expected_and_leaves_config_untouched(
        self, client, app_db,
    ):
        active_before = client.get(
            "/admin/config/active", headers=_auth(RAW_OPS_KEY),
        ).json()

        flag = _ask_and_flag(client)
        resp = client.post(
            f"/admin/feedback/{flag['feedback_id']}/resolve",
            json={"outcome": "golden_case", "note": "Worth regression-testing."},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["resolution_outcome"] == "golden_case"
        golden_case_id = body["resolution_golden_case_id"]
        assert golden_case_id

        cases = load_golden_cases(app_db["golden_path"])
        new_case = next(c for c in cases if c.id == golden_case_id)
        assert new_case.status == "pending_expected"
        assert new_case.expected_sql is None
        assert new_case.question == "این یک پرسش آزمایشی برای بازخورد است؟"

        active_after = client.get(
            "/admin/config/active", headers=_auth(RAW_OPS_KEY),
        ).json()
        assert active_after["version_id"] == active_before["version_id"], (
            "resolving a flag as a golden case must never create or apply "
            "a configuration version"
        )

    def test_the_gate_ignores_the_pending_case(self, client, app_db):
        flag = _ask_and_flag(client)
        client.post(
            f"/admin/feedback/{flag['feedback_id']}/resolve",
            json={"outcome": "golden_case", "note": "Worth regression-testing."},
            headers=_auth(RAW_SECURITY_KEY),
        )

        cases = load_golden_cases(app_db["golden_path"])
        generate_fn = make_offline_generator(cases)
        execute_fn = make_offline_executor(cases)
        results = run_golden_set(cases, generate_fn, execute_fn)

        pending_ids = {c.id for c in cases if c.status == "pending_expected"}
        assert pending_ids, "the promoted case must exist and be pending_expected"
        result_ids = {r.case_id for r in results}
        assert not (pending_ids & result_ids), (
            "the regression gate must produce no pass/fail result at all "
            "for a pending_expected case"
        )


# ---------------------------------------------------------------------------
# Stats (spec §5)
# ---------------------------------------------------------------------------


class TestStatsEndpoint:
    def test_stats_reports_flag_and_golden_set_counts(self, client):
        _ask_and_flag(client)
        resp = client.get("/admin/feedback/stats", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200
        body = resp.json()
        assert body["flags_total"] == 1
        assert body["flags_open"] == 1
        assert body["golden_set_size"] >= 1
