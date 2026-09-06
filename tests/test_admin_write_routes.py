# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 2 -- the write foundation. Frozen spec.

Covers every escalation path named in the spec's §2.1, plus the two
halves of the key-cache mitigation (§3.2) at the HTTP boundary (a real
FastAPI app, a real ``TestClient``, a real application database on a real
temp SQLite file -- no mock at the boundary under test).
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import config as cfg
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache
from security.auth import ANONYMOUS


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RAW_OPS_KEY = "1" * 40
RAW_SECURITY_KEY = "2" * 40
RAW_DUAL_KEY = "3" * 40
RAW_ANALYST_KEY = "4" * 40

_KEYS_JSON = json.dumps([
    {"id": "ops-admin", "name": "Ops Admin", "key_sha256": _sha256(RAW_OPS_KEY), "operations": True},
    {
        "id": "security-admin", "name": "Security Admin",
        "key_sha256": _sha256(RAW_SECURITY_KEY), "security": True,
    },
    {
        "id": "dual-admin", "name": "Dual Admin", "key_sha256": _sha256(RAW_DUAL_KEY),
        "operations": True, "security": True,
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
    db_path = tmp_path / "appdb.db"
    with cfg.override_settings(app_db_url=f"sqlite:///{db_path}", api_keys_json=_KEYS_JSON):
        dispose_app_engine()
        invalidate_cache()
        yield db_path
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
# Route discovery -- mirrors tests/test_admin.py's own _iter_live_routes,
# needed here too since app.routes does not flatten included routers in
# this FastAPI version (see that module's docstring for the full story).
# ---------------------------------------------------------------------------

def _iter_admin_routes(app):
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            if path.startswith("/admin"):
                yield route
            continue
        nested = getattr(route, "original_router", None)
        if nested is None:
            continue
        for nested_route in nested.routes:
            nested_path = getattr(nested_route, "path", None)
            if nested_path and nested_path.startswith("/admin"):
                yield nested_route


def _dependency_callables(dependant) -> set:
    """Every callable in *dependant*'s dependency tree, recursively."""
    out = set()
    stack = [dependant]
    while stack:
        current = stack.pop()
        if current is None:
            continue
        call = getattr(current, "call", None)
        if call is not None:
            out.add(call)
        stack.extend(getattr(current, "dependencies", None) or [])
    return out


# ---------------------------------------------------------------------------
# §2.1 escalation path #1: an operations admin cannot choose a new key's ACL
# ---------------------------------------------------------------------------

class TestOperationsCannotChooseANewKeysAcl:
    def test_issue_request_with_denied_columns_field_is_rejected_structurally(self, client):
        """The request schema itself has no such field -- Pydantic's
        extra='forbid' turns an attempt to smuggle one in into a 422
        before the route body ever runs."""
        resp = client.post(
            "/admin/keys",
            json={"principal_id": "sneaky", "name": "Sneaky", "denied_columns": []},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 422

    def test_issued_key_gets_the_restrictive_default_regardless(self, client):
        resp = client.post(
            "/admin/keys",
            json={"principal_id": "new-analyst", "name": "New Analyst"},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["denied_columns"], (
            "a freshly issued key must have a non-empty (maximally "
            "restrictive) denied_columns -- an empty list here would mean "
            "the operations admin walked around the ACL restriction"
        )

    def test_only_a_security_admin_can_loosen_the_acl(self, client):
        issued = client.post(
            "/admin/keys",
            json={"principal_id": "acl-target", "name": "ACL Target"},
            headers=_auth(RAW_OPS_KEY),
        ).json()
        key_hash = issued["key_sha256"]

        ops_attempt = client.patch(
            f"/admin/keys/{key_hash}/acl",
            json={"denied_columns": []},
            headers=_auth(RAW_OPS_KEY),
        )
        assert ops_attempt.status_code == 403

        security_attempt = client.patch(
            f"/admin/keys/{key_hash}/acl",
            json={"denied_columns": []},
            headers=_auth(RAW_SECURITY_KEY),
        )
        assert security_attempt.status_code == 200
        assert security_attempt.json()["denied_columns"] == []


# ---------------------------------------------------------------------------
# §2.1 escalation path #2: an operations admin cannot grant any role
# ---------------------------------------------------------------------------

class TestOperationsCannotGrantAnyRole:
    def test_operations_key_gets_403_granting_a_role(self, client):
        resp = client.post(
            "/admin/roles/analyst-1",
            json={"capability": "operations", "grant": True},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 403

    def test_operations_key_cannot_grant_itself_security(self, client):
        resp = client.post(
            "/admin/roles/ops-admin",
            json={"capability": "security", "grant": True},
            headers=_auth(RAW_OPS_KEY),
        )
        assert resp.status_code == 403

    def test_security_key_can_grant_operations(self, client):
        resp = client.post(
            "/admin/roles/analyst-1",
            json={"capability": "operations", "grant": True},
            headers=_auth(RAW_SECURITY_KEY),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# §2.1 escalation path #3: an operations admin cannot reach security-only
# endpoints -- discovered from the live route table
# ---------------------------------------------------------------------------

class TestOperationsCannotReachSecurityOnlyEndpoints:
    def test_every_security_gated_route_rejects_the_operations_key(self, client):
        import api.server as server_module
        from api.auth import require_security

        found_any = False
        for route in _iter_admin_routes(server_module.app):
            dependant = getattr(route, "dependant", None)
            if dependant is None or require_security not in _dependency_callables(dependant):
                continue
            found_any = True
            for method in route.methods:
                if method in ("HEAD", "OPTIONS"):
                    continue
                path = route.path.replace("{key_sha256}", "deadbeef").replace(
                    "{principal_id}", "analyst-1"
                )
                resp = client.request(
                    method, path, headers=_auth(RAW_OPS_KEY),
                    json={"denied_columns": [], "capability": "operations", "grant": True},
                )
                assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"
        assert found_any, "route discovery found no security-gated route to check"


# ---------------------------------------------------------------------------
# §2.1 escalation path #4: AUTH_REQUIRED=false confers neither role
# ---------------------------------------------------------------------------

class TestAuthDisabledConfersNeitherRole:
    def test_anonymous_has_neither_capability(self):
        assert ANONYMOUS.is_operations is False
        assert ANONYMOUS.is_security is False

    def test_auth_required_false_still_403s_on_operations_route(self, app_db):
        import api.server as server_module

        server_module._system_prompt = "stub system prompt"
        client = TestClient(server_module.app, raise_server_exceptions=False)
        with cfg.override_settings(auth_required=False, api_keys_json="", app_db_url=str(cfg.settings.app_db_url)):
            resp = client.get("/admin/keys")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "OPERATIONS_REQUIRED"

    def test_auth_required_false_still_403s_on_security_route(self, app_db):
        import api.server as server_module

        server_module._system_prompt = "stub system prompt"
        client = TestClient(server_module.app, raise_server_exceptions=False)
        with cfg.override_settings(auth_required=False, api_keys_json="", app_db_url=str(cfg.settings.app_db_url)):
            resp = client.post(
                "/admin/roles/anyone", json={"capability": "security", "grant": True},
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "SECURITY_REQUIRED"


# ---------------------------------------------------------------------------
# Immediate revocation, and no per-request re-query -- both halves, at the
# HTTP boundary (spec §3.2, §7).
# ---------------------------------------------------------------------------

class TestRevocationIsImmediateAtTheHttpBoundary:
    def test_revoked_key_is_rejected_on_the_very_next_request(self, client):
        issued = client.post(
            "/admin/keys",
            json={"principal_id": "soon-revoked", "name": "Soon Revoked"},
            headers=_auth(RAW_OPS_KEY),
        ).json()

        still_good = client.get("/cache/stats", headers=_auth(issued["raw_key"]))
        assert still_good.status_code == 200

        revoke_resp = client.post(
            f"/admin/keys/{issued['key_sha256']}/revoke", headers=_auth(RAW_OPS_KEY),
        )
        assert revoke_resp.status_code == 200

        rejected = client.get("/cache/stats", headers=_auth(issued["raw_key"]))
        assert rejected.status_code == 401


class TestNoPerRequestRequeryWhenNothingChanged:
    def test_unchanged_key_set_is_not_requeried_per_request(self, client, monkeypatch):
        from appdb import key_store as key_store_module

        call_count = {"n": 0}
        real_load = key_store_module._load_db_rows

        def _counting_load():
            call_count["n"] += 1
            return real_load()

        with cfg.override_settings(key_cache_ttl_seconds=30.0):
            invalidate_cache()
            monkeypatch.setattr(key_store_module, "_load_db_rows", _counting_load)
            for _ in range(5):
                resp = client.get("/cache/stats", headers=_auth(RAW_ANALYST_KEY))
                assert resp.status_code == 200

        assert call_count["n"] == 1, (
            f"expected exactly one application-database read across 5 "
            f"authenticated requests inside the TTL window, got {call_count['n']}"
        )


# ---------------------------------------------------------------------------
# Last admin cannot be removed, for either role, via the HTTP route
# ---------------------------------------------------------------------------

#: A key set with exactly one holder of each capability -- unlike
#: _KEYS_JSON module-wide, which also grants "dual-admin" both
#: capabilities and would make "security-admin"/"ops-admin" NOT the last
#: remaining holder of their own capability (a real, if easy to miss, test
#: hazard: asserting "the only holder" while a fixture quietly grants a
#: second one to someone else).
_SOLE_ADMIN_KEYS_JSON = json.dumps([
    {"id": "ops-admin", "name": "Ops Admin", "key_sha256": _sha256(RAW_OPS_KEY), "operations": True},
    {
        "id": "security-admin", "name": "Security Admin",
        "key_sha256": _sha256(RAW_SECURITY_KEY), "security": True,
    },
])


class TestLastAdminCannotBeRemovedViaTheRoute:
    def test_revoking_the_only_security_admin_is_refused(self, client):
        with cfg.override_settings(api_keys_json=_SOLE_ADMIN_KEYS_JSON):
            resp = client.post(
                "/admin/roles/security-admin",
                json={"capability": "security", "grant": False},
                headers=_auth(RAW_SECURITY_KEY),
            )
        assert resp.status_code == 409

    def test_revoking_the_only_operations_admin_is_refused(self, client):
        with cfg.override_settings(api_keys_json=_SOLE_ADMIN_KEYS_JSON):
            resp = client.post(
                "/admin/roles/ops-admin",
                json={"capability": "operations", "grant": False},
                headers=_auth(RAW_SECURITY_KEY),
            )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Mutual visibility (spec §2.4/§5): either role can read the admin-action
# log; neither can edit or delete it (no such route exists at all).
# ---------------------------------------------------------------------------

class TestMutualVisibility:
    def test_operations_admin_can_read_that_security_admin_acted(self, client):
        client.post(
            "/admin/roles/analyst-1",
            json={"capability": "operations", "grant": True},
            headers=_auth(RAW_SECURITY_KEY),
        )
        resp = client.get("/admin/actions", headers=_auth(RAW_OPS_KEY))
        assert resp.status_code == 200
        actions = resp.json()["actions"]
        assert any(
            a["authorised_by"] == "security" and a["action"] == "role.grant" for a in actions
        )

    def test_security_admin_can_read_that_operations_admin_acted(self, client):
        client.post(
            "/admin/keys",
            json={"principal_id": "x", "name": "X"},
            headers=_auth(RAW_OPS_KEY),
        )
        resp = client.get("/admin/actions", headers=_auth(RAW_SECURITY_KEY))
        assert resp.status_code == 200
        actions = resp.json()["actions"]
        assert any(a["authorised_by"] == "operations" and a["action"] == "key.issue" for a in actions)

    def test_dual_capability_actions_are_distinguishable_in_the_log(self, client):
        """spec §2.3: one principal holding both roles must have each
        action attributed to the capability that actually authorised it,
        not collapsed into one indistinguishable entry."""
        client.post(
            "/admin/keys", json={"principal_id": "y", "name": "Y"}, headers=_auth(RAW_DUAL_KEY),
        )
        client.post(
            "/admin/roles/y", json={"capability": "operations", "grant": True},
            headers=_auth(RAW_DUAL_KEY),
        )
        actions = client.get("/admin/actions", headers=_auth(RAW_OPS_KEY)).json()["actions"]
        dual_actions = [a for a in actions if a["actor_principal_id"] == "dual-admin"]
        authorised_by = {a["authorised_by"] for a in dual_actions}
        assert authorised_by == {"operations", "security"}, (
            "the same principal's operations-authorised and security-"
            "authorised actions must both be visible AND distinguishable"
        )

# Note: "every mutating /admin route declares a role dependency" -- the
# rule that replaces phase 1's obsolete "no /admin route accepts a
# mutating method" -- lives in tests/test_admin.py
# (TestEveryMutatingAdminRouteDeclaresARoleDependency), as the literal
# replacement for that phase 1 test the frozen phase 2 spec calls for,
# rather than duplicated here.
