"""In-memory, TTL-bounded, count-capped conversational session store.

Same operational discipline as ``api.query_cache.QueryCache`` — an
``OrderedDict`` for insertion/recency order, a single ``threading.Lock``
guarding every mutation, TTL expiry checked at access time, and a hard cap
on how many entries may exist at once — applied here to *sessions* instead
of cached query responses (``docs/api-contract-v2.md`` §9, §10: "in-memory,
same discipline as the existing query cache").

A session holds two parallel pieces of state per turn:

* :class:`~session.models.Turn` — the public, contract-shaped record
  returned to clients and listed by ``GET /v2/sessions/{sid}``.
* :class:`TurnMemory` — a private sidecar (never serialised to a client)
  that the refinement composer and ambiguity logic need to resolve a
  follow-up turn: the filters that were in effect, the result's column
  names (never row values), the executed SQL, and the row cap that was
  applied. Keeping this separate from ``Turn`` means the public contract
  shape never has to grow fields whose only purpose is internal
  bookkeeping.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from session.models import Turn


@dataclass
class TurnMemory:
    """Private, non-serialised bookkeeping for one turn — see module docstring."""

    turn_id: str
    filters: dict[str, object] = field(default_factory=dict)
    result_columns: list[str] = field(default_factory=list)
    sql: str | None = None
    injected_top: int | None = None
    row_count: int = 0


@dataclass
class SessionRecord:
    """One live conversation: its transcript plus the private memory sidecar."""

    session_id: str
    created_at: datetime
    last_active: float  # monotonic seconds — TTL bookkeeping only
    turns: list[Turn] = field(default_factory=list)
    memory: dict[str, TurnMemory] = field(default_factory=dict)

    def last_turn(self) -> Turn | None:
        """The most recently added turn, or ``None`` for a brand-new session."""
        return self.turns[-1] if self.turns else None

    def memory_for(self, turn_id: str | None) -> TurnMemory | None:
        return self.memory.get(turn_id) if turn_id else None


class SessionNotFoundError(LookupError):
    """Raised by :meth:`SessionStore.require` when *session_id* is unknown or expired."""


class SessionStore:
    """Thread-safe, TTL + count + per-session turn-cap bounded session store.

    Parameters
    ----------
    ttl_seconds:
        Idle-expiry window (§9 ``session_ttl_seconds``). ``0`` disables
        sessions entirely — every lookup behaves as a miss.
    max_count:
        Maximum concurrent sessions (§9 ``session_max_count``). Beyond
        this, the least-recently-active session is evicted.
    max_turns:
        Transcript cap per session (§9 ``session_max_turns``). Beyond
        this, the oldest turn (and its memory sidecar) is dropped from
        the session.
    """

    def __init__(self, ttl_seconds: int = 1800, max_size: int = 500, max_turns: int = 50) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._max_turns = max_turns
        self._sessions: OrderedDict[str, SessionRecord] = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create(self) -> SessionRecord:
        """Create and register a new, empty session."""
        now = time.monotonic()
        session_id = f"s_{uuid.uuid4().hex[:10]}"
        record = SessionRecord(
            session_id=session_id,
            created_at=datetime.now(timezone.utc),
            last_active=now,
        )
        with self._lock:
            while len(self._sessions) >= self._max_size:
                self._sessions.popitem(last=False)  # evict least-recently-active
            self._sessions[session_id] = record
        return record

    def get(self, session_id: str) -> SessionRecord | None:
        """Return the live session, or ``None`` if unknown / TTL-expired / disabled.

        Touches the session's ``last_active`` timestamp and moves it to
        the "most recently used" end of the eviction order on every
        successful lookup.
        """
        if self._ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return None
            if now - record.last_active > self._ttl:
                del self._sessions[session_id]
                return None
            record.last_active = now
            self._sessions.move_to_end(session_id)
            return record

    def require(self, session_id: str) -> SessionRecord:
        """Like :meth:`get`, but raises :class:`SessionNotFoundError` on a miss."""
        record = self.get(session_id)
        if record is None:
            raise SessionNotFoundError(f"Unknown or expired session: {session_id!r}")
        return record

    def delete(self, session_id: str) -> bool:
        """Remove *session_id*. Returns ``True`` if it was present."""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def clear(self) -> None:
        """Test/admin helper: drop every session."""
        with self._lock:
            self._sessions.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"size": len(self._sessions), "max_size": self._max_size}

    # ------------------------------------------------------------------
    # Turn bookkeeping
    # ------------------------------------------------------------------

    def add_turn(self, record: SessionRecord, turn: Turn, memory: TurnMemory) -> None:
        """Append *turn*/*memory* to *record*, enforcing the per-session turn cap.

        Beyond ``max_turns``, the oldest turn (and its memory sidecar) is
        dropped — a session's *transcript* is capped, distinct from the
        smaller ``session_prompt_turns`` window used to build a prompt
        (see ``session.engine``).
        """
        record.turns.append(turn)
        record.memory[turn.turn_id] = memory
        while len(record.turns) > self._max_turns:
            oldest = record.turns.pop(0)
            record.memory.pop(oldest.turn_id, None)

    def find_turn(self, record: SessionRecord, turn_id: str) -> Turn | None:
        for t in record.turns:
            if t.turn_id == turn_id:
                return t
        return None
