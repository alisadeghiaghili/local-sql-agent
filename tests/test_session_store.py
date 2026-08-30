"""Tests for ``session.store.SessionStore`` — §9's TTL/count/turn caps."""

from __future__ import annotations

import time

import pytest

from session.models import Turn
from session.store import SessionNotFoundError, SessionStore, TurnMemory


class TestLifecycle:
    def test_create_then_get_round_trips(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        assert store.get(record.session_id) is record

    def test_get_unknown_session_is_none(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        assert store.get("s_nope") is None

    def test_require_raises_on_unknown(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        with pytest.raises(SessionNotFoundError):
            store.require("s_nope")

    def test_delete_removes_session(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        assert store.delete(record.session_id) is True
        assert store.get(record.session_id) is None
        assert store.delete(record.session_id) is False


class TestTTL:
    def test_ttl_zero_disables_sessions_entirely(self):
        store = SessionStore(ttl_seconds=0, max_size=10, max_turns=10)
        record = store.create()
        assert store.get(record.session_id) is None

    def test_expired_session_is_evicted_on_access(self):
        store = SessionStore(ttl_seconds=0.05, max_size=10, max_turns=10)
        record = store.create()
        time.sleep(0.1)
        assert store.get(record.session_id) is None
        assert store.stats()["size"] == 0


class TestCountCap:
    def test_oldest_session_evicted_beyond_max_size(self):
        store = SessionStore(ttl_seconds=60, max_size=2, max_turns=10)
        first = store.create()
        store.create()
        store.create()  # evicts `first`
        assert store.get(first.session_id) is None
        assert store.stats()["size"] == 2

    def test_recently_used_session_survives_eviction(self):
        store = SessionStore(ttl_seconds=60, max_size=2, max_turns=10)
        first = store.create()
        store.create()
        store.get(first.session_id)  # touch -- moves to "most recent"
        store.create()  # should evict the OTHER (untouched) session, not `first`
        assert store.get(first.session_id) is not None


class TestTurnCap:
    def test_oldest_turn_dropped_beyond_max_turns(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=3)
        record = store.create()
        for i in range(5):
            turn = Turn(turn_id=f"t_{i}", session_id=record.session_id, index=i + 1, question=f"q{i}")
            store.add_turn(record, turn, TurnMemory(turn_id=f"t_{i}"))
        assert len(record.turns) == 3
        assert [t.turn_id for t in record.turns] == ["t_2", "t_3", "t_4"]
        assert "t_0" not in record.memory
        assert "t_4" in record.memory

    def test_find_turn(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        turn = Turn(turn_id="t_1", session_id=record.session_id, index=1, question="q")
        store.add_turn(record, turn, TurnMemory(turn_id="t_1"))
        assert store.find_turn(record, "t_1") is turn
        assert store.find_turn(record, "t_missing") is None
