"""TDD tests for cache admin endpoints.

GET  /cache/stats
POST /cache/clear
POST /cache/invalidate

Contracts
---------
/cache/stats
  - Returns 200 with hits/misses/size/evictions/enabled when cache enabled
  - enabled=False when TTL=0
  - size reflects actual entry count

/cache/clear
  - Returns snapshot of stats *before* clearing (size > 0 in snapshot)
  - After call, GET /cache/stats returns size=0
  - Safe to call on empty cache

/cache/invalidate
  - Removes only the targeted (question, mode) entry
  - Other entries remain untouched
  - Returns 404 when entry does not exist
  - Returns 200 with updated stats on success
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.models import QueryResponse


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _resp(question: str = "سوال", mode: str = "full") -> QueryResponse:
    return QueryResponse(
        question=question,
        sql="SELECT 1",
        result=[{"n": 1}],
        row_count=1,
        model="test",
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    from api.query_cache import query_cache
    query_cache.reconfigure(ttl_seconds=300, max_size=256)
    query_cache.clear()
    yield
    query_cache.clear()


@pytest.fixture()
def client():
    import api.server as server_module
    server_module._system_prompt = "stub"
    return TestClient(server_module.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /cache/stats
# ---------------------------------------------------------------------------

class TestCacheStats:
    def test_returns_200(self, client):
        resp = client.get("/cache/stats")
        assert resp.status_code == 200

    def test_response_has_required_fields(self, client):
        body = client.get("/cache/stats").json()
        for field in ("hits", "misses", "size", "evictions", "enabled"):
            assert field in body, f"missing field: {field}"

    def test_enabled_true_when_ttl_positive(self, client):
        body = client.get("/cache/stats").json()
        assert body["enabled"] is True

    def test_enabled_false_when_ttl_zero(self, client):
        from api.query_cache import query_cache
        query_cache.reconfigure(ttl_seconds=0, max_size=256)
        body = client.get("/cache/stats").json()
        assert body["enabled"] is False
        query_cache.reconfigure(ttl_seconds=300, max_size=256)  # restore

    def test_size_reflects_entries(self, client):
        from api.query_cache import query_cache
        query_cache.set("سوال", "full", _resp())
        body = client.get("/cache/stats").json()
        assert body["size"] == 1

    def test_hits_increments_after_hit(self, client):
        from api.query_cache import query_cache
        query_cache.set("سوال", "full", _resp())
        query_cache.get("سوال", "full")  # trigger hit
        body = client.get("/cache/stats").json()
        assert body["hits"] >= 1

    def test_misses_increments_after_miss(self, client):
        from api.query_cache import query_cache
        query_cache.get("نیست", "full")  # trigger miss
        body = client.get("/cache/stats").json()
        assert body["misses"] >= 1


# ---------------------------------------------------------------------------
# POST /cache/clear
# ---------------------------------------------------------------------------

class TestCacheClear:
    def test_returns_200(self, client):
        resp = client.post("/cache/clear")
        assert resp.status_code == 200

    def test_response_has_stats_fields(self, client):
        body = client.post("/cache/clear").json()
        for field in ("hits", "misses", "size", "evictions", "enabled"):
            assert field in body

    def test_snapshot_contains_size_before_clear(self, client):
        from api.query_cache import query_cache
        query_cache.set("سوال یک", "full", _resp("سوال یک"))
        query_cache.set("سوال دو", "full", _resp("سوال دو"))
        snapshot = client.post("/cache/clear").json()
        # snapshot must reflect state *before* clear
        assert snapshot["size"] == 2

    def test_cache_empty_after_clear(self, client):
        from api.query_cache import query_cache
        query_cache.set("سوال", "full", _resp())
        client.post("/cache/clear")
        stats = client.get("/cache/stats").json()
        assert stats["size"] == 0

    def test_clear_on_empty_cache_is_safe(self, client):
        resp = client.post("/cache/clear")
        assert resp.status_code == 200
        assert resp.json()["size"] == 0

    def test_second_clear_returns_size_zero(self, client):
        client.post("/cache/clear")
        body = client.post("/cache/clear").json()
        assert body["size"] == 0


# ---------------------------------------------------------------------------
# POST /cache/invalidate
# ---------------------------------------------------------------------------

class TestCacheInvalidate:
    def test_returns_200_for_existing_entry(self, client):
        from api.query_cache import query_cache
        query_cache.set("سوال", "full", _resp())
        resp = client.post("/cache/invalidate", json={"question": "سوال", "mode": "full"})
        assert resp.status_code == 200

    def test_returns_404_for_missing_entry(self, client):
        resp = client.post("/cache/invalidate", json={"question": "نیست", "mode": "full"})
        assert resp.status_code == 404

    def test_target_entry_removed(self, client):
        from api.query_cache import query_cache
        query_cache.set("سوال", "full", _resp())
        client.post("/cache/invalidate", json={"question": "سوال", "mode": "full"})
        assert query_cache.get("سوال", "full") is None

    def test_other_entries_untouched(self, client):
        from api.query_cache import query_cache
        r1 = _resp("سوال الف")
        r2 = _resp("سوال ب")
        query_cache.set("سوال الف", "full", r1)
        query_cache.set("سوال ب", "full", r2)
        client.post("/cache/invalidate", json={"question": "سوال الف", "mode": "full"})
        assert query_cache.get("سوال ب", "full") is r2

    def test_different_mode_not_affected(self, client):
        from api.query_cache import query_cache
        query_cache.set("سوال", "full", _resp())
        query_cache.set("سوال", "result", _resp())
        client.post("/cache/invalidate", json={"question": "سوال", "mode": "full"})
        assert query_cache.get("سوال", "result") is not None

    def test_stats_size_decrements_after_invalidate(self, client):
        from api.query_cache import query_cache
        query_cache.set("سوال الف", "full", _resp("سوال الف"))
        query_cache.set("سوال ب", "full", _resp("سوال ب"))
        client.post("/cache/invalidate", json={"question": "سوال الف", "mode": "full"})
        assert client.get("/cache/stats").json()["size"] == 1

    def test_invalid_mode_returns_422(self, client):
        resp = client.post("/cache/invalidate", json={"question": "سوال", "mode": "bad"})
        assert resp.status_code == 422

    def test_missing_question_returns_422(self, client):
        resp = client.post("/cache/invalidate", json={"mode": "full"})
        assert resp.status_code == 422
