# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 6, §9 (resolved) -- a small, separate rate-limit
bucket for auth FAILURES, and visibility over them.

Real middleware stack (AuthMiddleware -> RateLimitMiddleware, the same
order api/server.py registers them in), a real TestClient -- no mocking
at the boundary under test.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config as cfg
from api.auth import AuthMiddleware
from api.middleware import RateLimitMiddleware
import security.auth_failures as auth_failures_module
from security.auth_failures import iter_auth_failures, summarize_auth_failures


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RAW_KEY = "z" * 40
_KEYS_JSON = json.dumps([
    {"id": "analyst-1", "name": "Analyst", "key_sha256": _sha256(RAW_KEY)},
])


def _make_app(**rl_kwargs) -> FastAPI:
    app = FastAPI()
    # Registration order mirrors api/server.py: AuthMiddleware must run
    # before RateLimitMiddleware, and add_middleware applies in reverse.
    app.add_middleware(RateLimitMiddleware, **rl_kwargs)
    app.add_middleware(AuthMiddleware)

    @app.get("/limited")
    def limited():
        return {"ok": True}

    return app


@pytest.fixture(autouse=True)
def _isolate_auth_failure_log(tmp_path):
    path = tmp_path / "auth_failure_log.jsonl"
    auth_failures_module._AUTH_FAILURE_LOG_FILE = str(path)
    yield path
    auth_failures_module._AUTH_FAILURE_LOG_FILE = ""


class TestAuthFailureHasItsOwnBucket:
    def test_repeated_bad_key_does_not_exhaust_the_shared_unauthenticated_budget(self):
        """The whole point of the fix: a client looping on a stale key
        must not starve genuinely unauthenticated traffic (e.g. a health
        probe) sharing the same IP."""
        with cfg.override_settings(
            auth_required=True, api_keys_json=_KEYS_JSON,
        ):
            app = _make_app(
                requests_per_window=2, window_seconds=60, burst=0,  # tiny SHARED budget
                auth_failure_requests_per_window=1000, auth_failure_window_seconds=60, auth_failure_burst=0,
            )
            client = TestClient(app)

            # Exhaust the auth-failure bucket's own (huge, for this test)
            # allowance with a bad key -- must NOT touch the shared budget.
            for _ in range(5):
                resp = client.get("/limited", headers={"Authorization": "Bearer wrong-key"})
                assert resp.status_code == 200

            # The shared budget (capacity 2) is still fully intact for a
            # genuinely unauthenticated caller from the same IP.
            assert client.get("/limited").status_code == 200
            assert client.get("/limited").status_code == 200
            assert client.get("/limited").status_code == 429  # shared budget itself still works

    def test_auth_failure_bucket_itself_can_still_be_exhausted(self):
        with cfg.override_settings(auth_required=True, api_keys_json=_KEYS_JSON):
            app = _make_app(
                requests_per_window=1000, window_seconds=60, burst=0,
                auth_failure_requests_per_window=2, auth_failure_window_seconds=60, auth_failure_burst=0,
            )
            client = TestClient(app)

            assert client.get("/limited", headers={"Authorization": "Bearer wrong-key"}).status_code == 200
            assert client.get("/limited", headers={"Authorization": "Bearer wrong-key"}).status_code == 200
            resp = client.get("/limited", headers={"Authorization": "Bearer wrong-key"})
            assert resp.status_code == 429

    def test_a_valid_key_never_touches_the_auth_failure_bucket(self):
        with cfg.override_settings(auth_required=True, api_keys_json=_KEYS_JSON):
            app = _make_app(
                requests_per_window=1000, window_seconds=60, burst=0,
                auth_failure_requests_per_window=1, auth_failure_window_seconds=60, auth_failure_burst=0,
            )
            client = TestClient(app)
            for _ in range(5):
                resp = client.get("/limited", headers={"Authorization": f"Bearer {RAW_KEY}"})
                assert resp.status_code == 200


class TestAuthFailureVisibility:
    def test_failed_attempts_are_recorded(self, _isolate_auth_failure_log):
        with cfg.override_settings(auth_required=True, api_keys_json=_KEYS_JSON):
            app = _make_app(requests_per_window=1000, window_seconds=60, burst=0)
            client = TestClient(app)
            client.get("/limited", headers={"Authorization": "Bearer wrong-key"})
            client.get("/limited", headers={"Authorization": "Bearer another-wrong-key"})
            client.get("/limited")  # no header at all -- NOT a failure

        records = iter_auth_failures()
        assert len(records) == 2
        assert all(r["path"] == "/limited" for r in records)

    def test_summary_counts_and_breaks_down_by_source(self, _isolate_auth_failure_log):
        with cfg.override_settings(auth_required=True, api_keys_json=_KEYS_JSON):
            app = _make_app(requests_per_window=1000, window_seconds=60, burst=0)
            client = TestClient(app)
            for _ in range(3):
                client.get("/limited", headers={"Authorization": "Bearer wrong-key"})

        summary = summarize_auth_failures(iter_auth_failures())
        assert summary["total"] == 3
        assert summary["admin_path_total"] == 0  # /limited is not under /admin
        assert sum(summary["by_source_ip"].values()) == 3
