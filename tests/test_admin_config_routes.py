# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 3 -- ``/admin/config/*`` at the HTTP boundary.

A real FastAPI app, a real ``TestClient``, a real application database on a
real temp SQLite file, and a real (copied) ``project_config.example/`` --
no mock at the boundary under test. ``tests/test_config_versions.py``
covers :mod:`appdb.config_versions` itself in depth; this module covers
the thin HTTP surface over it: role gating, status codes, and that the
route layer does not itself perform the schema.yaml draft/approve
decision (that lives one layer down, and is proven directly there).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config as cfg
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"
_EXAMPLE_GOLDEN_PATH = _REPO_ROOT / "eval_data.example" / "golden.jsonl"


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
    {"id": "analyst-1", "name": "Analyst One", "key_sha256": _sha256(RAW_ANALYST_KEY)},
])


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
    db_path = tmp_path / "appdb.db"
    export_dir = tmp_path / "export"
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}",
        api_keys_json=_KEYS_JSON,
        project_config_dir=str(project_dir),
        eval_golden_path=str(_EXAMPLE_GOLDEN_PATH),
        config_export_dir=str(export_dir),
    ):
        dispose_app_engine()
        invalidate_cache()
        yield {"project_dir": project_dir, "export_dir": export_dir}
    dispose_app_engine()
    invalidate_cache()


@pytest.fixture()
def client(app_db):
    import api.server as server_module

    server_module._system_prompt = "stub system prompt"
    return TestClient(server_module.app, raise_server_exceptions=False)


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


# ---------------------------------------------------------------------------
# Reads: either role, never an analyst key
# ---------------------------------------------------------------------------

class TestReadsRequireEitherAdminRole:
    def test_analyst_key_gets_403_on_active(self, client):
        resp = client.get("/admin/config/active", headers=_auth(RAW_ANALYST_KEY))
        assert resp.status_code == 403

    def test_operations_key_can_read_active(self, client):
        resp = client.get("/admin/config/active", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200
        assert resp.json()["version_id"] == 1

    def test_security_key_can_read_active(self, client):
        resp = client.get("/admin/config/active", headers=_auth(RAW_SECURITY_KEY))
        assert resp.status_code == 200

    def test_unknown_version_is_404(self, client):
        resp = client.get("/admin/config/versions/999999", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /admin/config/versions -- role split enforced one layer down
# ---------------------------------------------------------------------------

class TestSaveVersionRoleSplit:
    def test_operations_key_edit_of_a_non_schema_file_applies(self, client):
        active = client.get("/admin/config/active", headers=_auth(RAW_OPS_KEY)).json()
        resp = client.post(
            "/admin/config/versions",
            json={
                "based_on_version": active["version_id"],
                "files": {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# via http\n"},
            },
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "applied"
        assert body["created_by_capability"] == "operations"

    def test_operations_key_editing_schema_yaml_creates_a_draft_not_an_applied_version(self, client):
        active = client.get("/admin/config/active", headers=_auth(RAW_OPS_KEY)).json()
        import yaml

        doc = yaml.safe_load(active["files"]["schema.yaml"])
        del doc["tables"]["Customer"]
        new_schema = yaml.dump(doc, allow_unicode=True, sort_keys=False)

        resp = client.post(
            "/admin/config/versions",
            json={"based_on_version": active["version_id"], "files": {"schema.yaml": new_schema}},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "draft"
        assert body["created_by_capability"] == "operations"

        still_active = client.get("/admin/config/active", headers=_auth(RAW_OPS_KEY)).json()
        assert still_active["version_id"] == active["version_id"]

    def test_security_key_editing_schema_yaml_applies_directly(self, client):
        active = client.get("/admin/config/active", headers=_auth(RAW_SECURITY_KEY)).json()
        import yaml

        doc = yaml.safe_load(active["files"]["schema.yaml"])
        del doc["tables"]["Customer"]
        new_schema = yaml.dump(doc, allow_unicode=True, sort_keys=False)

        resp = client.post(
            "/admin/config/versions",
            json={"based_on_version": active["version_id"], "files": {"schema.yaml": new_schema}},
            headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert resp.json()["created_by_capability"] == "security"

    def test_analyst_key_cannot_save_a_version_at_all(self, client):
        resp = client.post(
            "/admin/config/versions",
            json={"based_on_version": 1, "files": {"metrics.yaml": "metrics: {}"}},
            headers=_auth(RAW_ANALYST_KEY),
        )
        assert resp.status_code == 403

    def test_stale_based_version_is_409(self, client):
        active = client.get("/admin/config/active", headers=_auth(RAW_OPS_KEY)).json()
        client.post(
            "/admin/config/versions",
            json={
                "based_on_version": active["version_id"],
                "files": {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# first\n"},
            },
            headers=_auth(RAW_OPS_KEY),
        )
        resp = client.post(
            "/admin/config/versions",
            json={
                "based_on_version": active["version_id"],  # already stale
                "files": {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# second\n"},
            },
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 409

    def test_malformed_file_is_422(self, client):
        active = client.get("/admin/config/active", headers=_auth(RAW_OPS_KEY)).json()
        resp = client.post(
            "/admin/config/versions",
            json={
                "based_on_version": active["version_id"],
                "files": {"business_rules.yaml": "not: [valid"},
            },
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Approve / reject -- security only
# ---------------------------------------------------------------------------

class TestApproveRejectRequireSecurity:
    def _make_draft(self, client) -> dict:
        active = client.get("/admin/config/active", headers=_auth(RAW_OPS_KEY)).json()
        import yaml

        doc = yaml.safe_load(active["files"]["schema.yaml"])
        del doc["tables"]["Customer"]
        new_schema = yaml.dump(doc, allow_unicode=True, sort_keys=False)
        resp = client.post(
            "/admin/config/versions",
            json={"based_on_version": active["version_id"], "files": {"schema.yaml": new_schema}},
            headers=_auth(RAW_OPS_KEY),
        )
        return resp.json()

    def test_operations_key_cannot_approve(self, client):
        draft = self._make_draft(client)
        resp = client.post(
            f"/admin/config/versions/{draft['version_id']}/approve", headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 403

    def test_operations_key_cannot_reject(self, client):
        draft = self._make_draft(client)
        resp = client.post(
            f"/admin/config/versions/{draft['version_id']}/reject",
            json={"reason": "no"},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 403

    def test_security_key_can_approve(self, client):
        draft = self._make_draft(client)
        resp = client.post(
            f"/admin/config/versions/{draft['version_id']}/approve",
            headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"

    def test_security_key_can_reject(self, client):
        draft = self._make_draft(client)
        resp = client.post(
            f"/admin/config/versions/{draft['version_id']}/reject",
            json={"reason": "not yet"},
            headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    def test_approving_a_non_draft_is_409(self, client):
        active = client.get("/admin/config/active", headers=_auth(RAW_OPS_KEY)).json()
        resp = client.post(
            f"/admin/config/versions/{active['version_id']}/approve",
            headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

class TestRestoreRoute:
    def test_restore_creates_a_new_version_and_keeps_history(self, client):
        v1 = client.get("/admin/config/active", headers=_auth(RAW_OPS_KEY)).json()
        client.post(
            "/admin/config/versions",
            json={
                "based_on_version": v1["version_id"],
                "files": {"metrics.yaml": v1["files"]["metrics.yaml"] + "\n# v2\n"},
            },
            headers=_auth(RAW_OPS_KEY),
        )
        resp = client.post(
            "/admin/config/restore",
            json={"from_version_id": v1["version_id"], "filename": "metrics.yaml"},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["version_id"] == 3
        assert body["restored_from_version"] == v1["version_id"]
        assert body["restored_file"] == "metrics.yaml"
        # version 2 is untouched
        v2 = client.get("/admin/config/versions/2", headers=_auth(RAW_OPS_KEY)).json()
        assert v2["status"] == "applied"

    def test_restore_from_unknown_version_is_404(self, client):
        resp = client.post(
            "/admin/config/restore",
            json={"from_version_id": 999999, "filename": None},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Export / import -- always via CONFIG_EXPORT_DIR
# ---------------------------------------------------------------------------

class TestExportImport:
    def test_export_then_import_round_trips(self, client, app_db):
        active = client.get("/admin/config/active", headers=_auth(RAW_OPS_KEY)).json()

        export_resp = client.post("/admin/config/export", headers=_auth(RAW_OPS_KEY))
        assert export_resp.status_code == 200
        assert export_resp.json()["version_id"] == active["version_id"]

        # Import re-applies the same content on top of the (unchanged)
        # active version -- a no-op diff, applied cleanly.
        import_resp = client.post("/admin/config/import", headers=_auth(RAW_OPS_KEY))
        assert import_resp.status_code == 200
        assert import_resp.json()["files"] == active["files"]

    def test_export_without_config_export_dir_is_400(self, client):
        with cfg.override_settings(config_export_dir=""):
            resp = client.post("/admin/config/export", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 400
