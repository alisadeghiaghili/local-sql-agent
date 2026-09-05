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
    """Every (method, path) pair under ``/admin`` in the live route table
    -- discovered, not hand-copied, so a route added later without
    ``Depends(require_admin)`` fails this test automatically (mirrors the
    intent of ``tests/test_auth.py``'s ``_protected_route_cases``; see
    :func:`_iter_live_routes` for why this cannot reuse that function's own
    flat ``app.routes`` walk verbatim in this environment)."""
    import api.server as server_module

    return [
        (method, path)
        for method, path in _iter_live_routes(server_module.app)
        if path.startswith("/admin") and method not in ("HEAD", "OPTIONS")
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
# 4. No /admin route accepts a mutating method
# ---------------------------------------------------------------------------

class TestNoAdminRouteAcceptsAMutatingMethod:
    def test_every_admin_route_is_get_only(self):
        by_path: dict[str, set[str]] = {}
        for method, path in _ADMIN_CASES:
            by_path.setdefault(path, set()).add(method)

        assert by_path, "route discovery found no /admin routes at all"
        for path, methods in by_path.items():
            assert methods == {"GET"}, (
                f"{path} accepts {sorted(methods)} -- phase 1 admin routes "
                "must be GET-only"
            )
