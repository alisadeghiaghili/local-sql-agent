# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel, phase 1 — read-only observability. Frozen spec.

Covers the four Python-side contracts the spec names (a fifth, the panel
attaching ``Authorization`` on every call, is a browser-JS boundary and
lives in ``tests/web_ui/test_web_ui_admin_auth_boundary.py`` instead —
same split ``tests/test_auth.py``/``tests/web_ui/test_web_ui_auth_boundary.py``
already use):

1. A non-admin key gets 403 on every ``/admin`` route, discovered from
   the live route table rather than hand-listed.
2. ``AUTH_REQUIRED=false`` does not confer admin — verified against the
   real :data:`~security.auth.ANONYMOUS`, not a mock.
3. ``GET /admin/summary`` defaults to the aggregate-safe mode, and the
   response carries ``mode``.
4. No ``/admin`` route accepts a mutating method — every one is ``GET``.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from config import override_settings
from security.auth import ANONYMOUS

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RAW_ADMIN_KEY = "d" * 40
RAW_ANALYST_KEY = "e" * 40

_KEYS_JSON = json.dumps([
    {"id": "admin-1", "name": "Admin One", "key_sha256": _sha256(RAW_ADMIN_KEY), "admin": True},
    {"id": "analyst-1", "name": "Analyst One", "key_sha256": _sha256(RAW_ANALYST_KEY)},
])


@pytest.fixture(autouse=True)
def _reset_shared_state():
    """Isolate this module's tests from the session store / query cache,
    mirroring tests/test_auth.py's own fixture of the same name."""
    import api.v2_routes as v2_routes
    from api.query_cache import query_cache

    v2_routes._reset_for_testing()
    query_cache.reconfigure(ttl_seconds=300, max_size=256)
    query_cache.clear()
    yield
    v2_routes._reset_for_testing()
    query_cache.clear()


@pytest.fixture(autouse=True)
def _fast_deployment_checks(monkeypatch):
    """Every test in this module asks "is this route 403/GET-only", not
    "did verify_deployment's live database/LLM-endpoint checks pass" --
    that question already has its own real-boundary tests in
    tests/test_verify_deployment.py. Swapping in one trivial, deterministic
    check keeps GET /admin/health/checks fast and independent of whatever
    network/DB reachability happens to exist in the environment this suite
    runs in, without mocking the thing any of the four tests below is
    actually about (the admin-capability gate and the route table shape).
    """
    import scripts.verify_deployment as verify_deployment_module
    from scripts.verify_deployment import CheckResult

    def _stub_check() -> CheckResult:
        return CheckResult("stub check", "PASS", "stubbed for test speed")

    monkeypatch.setattr(verify_deployment_module, "_CHECKS", [_stub_check])
    yield


@pytest.fixture()
def app_and_client():
    import api.server as server_module

    server_module._system_prompt = "stub system prompt"
    client = TestClient(server_module.app, raise_server_exceptions=False)
    yield server_module.app, client


# ---------------------------------------------------------------------------
# 1. Every /admin route -> 403 for a non-admin key, discovered from the
#    live route table
# ---------------------------------------------------------------------------

def _iter_live_routes(app):
    """Yield ``(method, path)`` for every route *app* actually serves,
    recursing into an ``app.include_router(...)``-included sub-router.

    This FastAPI installation (0.141.1) represents each included router as
    one opaque ``_IncludedRouter`` entry in ``app.routes`` — its own
    ``.path`` is ``None`` — rather than flattening every sub-route into
    the top-level list the way older FastAPI/Starlette did (the shape
    ``tests/test_auth.py``'s own ``_protected_route_cases`` assumes).
    A naive walk of ``app.routes`` therefore silently stops seeing every
    ``/v2/*`` and ``/admin/*`` route the moment this FastAPI version is
    installed. Recursing into ``.original_router.routes`` — the actual
    ``APIRouter`` object the ``include_router()`` call was given — restores
    exactly what a flat ``app.routes`` walk used to expose directly, so
    this test discovers from the route table FastAPI actually dispatches
    through rather than silently degrading to "whatever was registered
    directly on ``app``".
    """
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path:
            for method in methods:
                yield method, path
            continue
        nested_router = getattr(route, "original_router", None)
        if nested_router is None:
            continue
        for nested_route in nested_router.routes:
            nested_path = getattr(nested_route, "path", None)
            nested_methods = getattr(nested_route, "methods", None) or set()
            if not nested_path:
                continue
            for method in nested_methods:
                yield method, nested_path


def _admin_route_cases() -> list[tuple[str, str]]:
    """Every (method, path) pair phase 1's OWN router
    (``api.admin_routes.router``) serves -- discovered from that router
    object directly, not hand-copied, so a route added later to
    ``api/admin_routes.py`` without ``Depends(require_admin)`` fails this
    test automatically (mirrors the intent of ``tests/test_auth.py``'s
    ``_protected_route_cases``).

    Scoped to this one router object, not "every path starting with
    /admin" -- admin panel phase 2 (``docs/admin-panel-architecture.md``;
    the frozen phase 2 spec) deliberately adds its OWN routes under
    ``/admin`` (``api/admin_write_routes.py``) gated on the ``operations``/
    ``security`` capabilities instead of phase 1's single ``admin``
    capability, per the architecture's two-role split. A prefix-based
    "every /admin path requires admin" would now be simply false for those
    routes by design, not a regression this test should catch -- see
    ``tests/test_admin_write_routes.py`` for phase 2's own equivalent
    coverage (every phase 2 route requires the RIGHT role) and this
    module's ``TestEveryMutatingAdminRouteDeclaresARoleDependency`` for
    the app-wide "every mutating /admin route requires *some* role" rule
    that replaces phase 1's obsolete "no /admin route accepts a mutating
    method".
    """
    import api.admin_routes as admin_routes_module

    return [
        (method, route.path)
        for route in admin_routes_module.router.routes
        for method in (getattr(route, "methods", None) or set())
        if method not in ("HEAD", "OPTIONS")
    ]


_ADMIN_CASES = _admin_route_cases()


class TestEveryAdminRouteRequiresAdminCapability:
    def test_route_discovery_found_something(self):
        # Guards against the parametrize below going vacuously green if
        # route discovery itself silently returns nothing.
        assert len(_ADMIN_CASES) >= 4

    @pytest.mark.parametrize(
        "method,path", _ADMIN_CASES, ids=[f"{m}:{p}" for m, p in _ADMIN_CASES],
    )
    def test_non_admin_key_gets_403(self, app_and_client, method, path):
        _, client = app_and_client
        with override_settings(auth_required=True, api_keys_json=_KEYS_JSON):
            resp = client.request(
                method, path, headers={"Authorization": f"Bearer {RAW_ANALYST_KEY}"},
            )
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code} (expected 403)"
        assert resp.json()["error"]["code"] == "ADMIN_REQUIRED"

    @pytest.mark.parametrize(
        "method,path", _ADMIN_CASES, ids=[f"{m}:{p}" for m, p in _ADMIN_CASES],
    )
    def test_admin_key_is_not_rejected(self, app_and_client, method, path):
        _, client = app_and_client
        with override_settings(auth_required=True, api_keys_json=_KEYS_JSON):
            resp = client.request(
                method, path, headers={"Authorization": f"Bearer {RAW_ADMIN_KEY}"},
            )
        assert resp.status_code not in (401, 403), (
            f"{method} {path} -> {resp.status_code} for a genuine admin key"
        )

    @pytest.mark.parametrize(
        "method,path", _ADMIN_CASES, ids=[f"{m}:{p}" for m, p in _ADMIN_CASES],
    )
    def test_unauthenticated_gets_401_not_403(self, app_and_client, method, path):
        # A missing credential must fail as "you never authenticated"
        # (401), not "you authenticated but lack a capability" (403) --
        # require_admin depends on require_principal so AUTH_REQUIRED's
        # own enforcement always runs first.
        _, client = app_and_client
        with override_settings(auth_required=True, api_keys_json=_KEYS_JSON):
            resp = client.request(method, path)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 2. AUTH_REQUIRED=false does not confer admin -- verified against the
#    real ANONYMOUS principal
# ---------------------------------------------------------------------------

class TestAuthDisabledDoesNotConferAdmin:
    def test_anonymous_principal_is_never_admin(self):
        assert ANONYMOUS.is_admin is False
        assert ANONYMOUS.capabilities == frozenset()

    def test_auth_required_false_still_gets_403_on_admin_route(self, app_and_client):
        _, client = app_and_client
        with override_settings(auth_required=False, api_keys_json=""):
            resp = client.get("/admin/cache")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ADMIN_REQUIRED"


# ---------------------------------------------------------------------------
# 3. GET /admin/summary defaults to aggregate-safe, and carries `mode`
# ---------------------------------------------------------------------------

class TestAdminSummaryDefaultsToAggregateSafe:
    def _write_log(self, tmp_path):
        log_file = tmp_path / "audit_log.jsonl"
        log_file.write_text(
            "\n".join([
                json.dumps({
                    "question": "پرسش واقعی یک تحلیلگر",
                    "error_code": None,
                    "timings": {"total_ms": 42},
                }),
                json.dumps({
                    "question": "پرسش دوم که رد شد",
                    "error_code": "FORBIDDEN_SQL",
                    "error_message": "denied column",
                    "timings": {},
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        return log_file

    def test_default_mode_is_aggregate_safe_and_hides_questions(self, app_and_client, tmp_path):
        _, client = app_and_client
        self._write_log(tmp_path)
        with override_settings(
            auth_required=True, api_keys_json=_KEYS_JSON, log_dir=str(tmp_path),
        ):
            resp = client.get(
                "/admin/summary", headers={"Authorization": f"Bearer {RAW_ADMIN_KEY}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "aggregate_safe"
        assert body["record_count"] == 2
        assert "examples_by_error_code" not in body["failure_taxonomy"]
        raw = resp.text
        assert "پرسش واقعی یک تحلیلگر" not in raw
        assert "پرسش دوم که رد شد" not in raw

    def test_include_examples_true_switches_mode(self, app_and_client, tmp_path):
        _, client = app_and_client
        self._write_log(tmp_path)
        with override_settings(
            auth_required=True, api_keys_json=_KEYS_JSON, log_dir=str(tmp_path),
        ):
            resp = client.get(
                "/admin/summary?include_examples=true",
                headers={"Authorization": f"Bearer {RAW_ADMIN_KEY}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "aggregate_with_examples"
        assert "examples_by_error_code" in body["failure_taxonomy"]


# ---------------------------------------------------------------------------
# 4. [Superseded by admin panel phase 2] Every mutating /admin route
#    declares a role dependency.
# ---------------------------------------------------------------------------
# Phase 1's rule here used to be "no /admin route accepts a mutating
# method at all" -- true only because phase 1 never added a write path.
# Admin panel phase 2 (docs/admin-panel-architecture.md; the frozen phase 2
# spec) adds real writes under /admin (key issue/disable/enable/revoke,
# ACL changes, role grants -- see api/admin_write_routes.py), so that rule
# is now obsolete by construction: it would fail the moment phase 2's own
# routes exist, for reasons that have nothing to do with a security
# regression. The protection this test exists for was never really "no
# writes exist" -- it was "no write is ungated" -- so this is a
# replacement, not a deletion, of the same underlying guarantee: every
# route discovered under /admin that accepts a mutating method must
# declare one of require_admin/require_operations/require_security,
# discovered from the route's own dependant tree (the actual FastAPI
# dependency graph Starlette dispatches through), not hand-listed.

def _dependency_callables(dependant) -> set:
    """Every callable in *dependant*'s dependency tree, recursively --
    e.g. for a route depending on ``require_operations`` (which itself
    depends on ``require_principal``), this returns both."""
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


def _admin_route_objects():
    """Like :func:`_admin_route_cases`, but yielding the live route
    OBJECTS (not just method/path) -- needed here to reach each route's
    own ``.dependant`` tree."""
    import api.server as server_module

    return list(_iter_live_route_objects(server_module.app, "/admin"))


def _iter_live_route_objects(app, prefix: str):
    """Mirrors :func:`_iter_live_routes` above, but yields route objects
    (recursing into an ``app.include_router(...)``-included sub-router the
    same way, for the same FastAPI-version reason documented on that
    function) instead of bare ``(method, path)`` tuples."""
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            if path.startswith(prefix):
                yield route
            continue
        nested_router = getattr(route, "original_router", None)
        if nested_router is None:
            continue
        for nested_route in nested_router.routes:
            nested_path = getattr(nested_route, "path", None)
            if nested_path and nested_path.startswith(prefix):
                yield nested_route


class TestEveryMutatingAdminRouteDeclaresARoleDependency:
    def test_every_non_get_admin_route_requires_a_role(self):
        from api.auth import require_admin, require_operations, require_security

        allowed = {require_admin, require_operations, require_security}
        checked = 0
        for route in _admin_route_objects():
            dependant = getattr(route, "dependant", None)
            deps = _dependency_callables(dependant) if dependant is not None else set()
            for method in getattr(route, "methods", None) or set():
                if method in ("HEAD", "OPTIONS", "GET"):
                    continue
                checked += 1
                assert deps & allowed, (
                    f"{method} {route.path} is a mutating /admin route with no "
                    "require_admin/require_operations/require_security dependency "
                    "-- every write under /admin must declare a role"
                )
        assert checked > 0, (
            "route discovery found no mutating /admin route to check -- phase 2's "
            "write routes (api/admin_write_routes.py) should have added some"
        )
