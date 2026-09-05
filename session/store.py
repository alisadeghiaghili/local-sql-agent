# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
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

Persistence (§9, §10) is attached as an *optional* constructor argument
(:class:`~session.persistence.SessionPersistence`, only type-checked here
to avoid a circular import — that module imports :class:`TurnMemory` from
this one). ``SessionStore()`` with no backend behaves exactly as it
always has, including TTL expiry *deleting* the in-memory record;
rehydration is only ever attempted when a backend is attached. When one
is attached, TTL expiry instead *demotes* a session out of the hot set —
:meth:`get` transparently rebuilds it from disk on the very next lookup,
resetting the in-memory TTL clock, while ``session_retention_days``
(``purge_expired``) governs the separate, much longer question of when a
conversation stops being listable/reopenable at all.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import config as cfg
from session.memory import truncate_at_word_boundary
from session.models import Turn

if TYPE_CHECKING:  # pragma: no cover - import-cycle avoidance only
    from session.persistence import SessionPersistence


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
    owner_id: str | None = None
    """The :class:`~security.auth.Principal.id` that created this session
    (Phase 8), or ``None`` for a session created with no principal at all
    (``AUTH_REQUIRED=false``, or a pre-Phase-8-shaped direct call).
    ``None`` is treated as "no ownership restriction" by the v2 routes'
    ownership check — never as "owned by nobody" in a way that would
    make the session unreachable. A non-``None`` owner that doesn't
    match the requesting principal makes the session behave as if it
    does not exist (404, never 403 — see ``api/v2_routes.py``): a 403
    would itself confirm the session's existence to a caller who has no
    business knowing that."""
    title: str | None = None
    """Derived from the first turn's question (truncated at a word
    boundary to ``session_title_max_length``), or explicitly renamed via
    ``PATCH /v2/sessions/{sid}``. ``None`` for a session with no turns
    yet. Presentation only — never enters a prompt (§3)."""

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

    def __init__(
        self,
        ttl_seconds: int = 1800,
        max_size: int = 500,
        max_turns: int = 50,
        *,
        persistence: SessionPersistence | None = None,
        retention_days: int = 30,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._max_turns = max_turns
        self._persistence = persistence
        self._retention_days = retention_days
        self._sessions: OrderedDict[str, SessionRecord] = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create(self, owner_id: str | None = None) -> SessionRecord:
        """Create and register a new, empty session.

        Parameters
        ----------
        owner_id:
            The creating principal's id (Phase 8), or ``None`` — see
            :attr:`SessionRecord.owner_id`.
        """
        now = time.monotonic()
        session_id = f"s_{uuid.uuid4().hex[:10]}"
        created_at = datetime.now(timezone.utc)
        record = SessionRecord(
            session_id=session_id,
            created_at=created_at,
            last_active=now,
            owner_id=owner_id,
        )
        with self._lock:
            while len(self._sessions) >= self._max_size:
                self._sessions.popitem(last=False)  # evict least-recently-active
            self._sessions[session_id] = record
        if self._persistence is not None:
            iso = created_at.isoformat()
            self._persistence.upsert_session(session_id, owner_id, None, iso, iso)
        return record

    def get(self, session_id: str) -> SessionRecord | None:
        """Return the live session, or ``None`` if unknown / expired-and-not-
        persisted / disabled.

        Touches the session's ``last_active`` timestamp and moves it to
        the "most recently used" end of the eviction order on every
        successful lookup. With no persistence backend attached, TTL
        expiry deletes the record exactly as before this phase. With one
        attached, TTL expiry instead *demotes* the record out of the hot
        set — this same call transparently rehydrates it from disk
        (a genuinely different, rebuilt object) and resets its in-memory
        TTL clock, per §9/§10's "demote, not delete".
        """
        if self._ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            record = self._sessions.get(session_id)
            if record is not None:
                if now - record.last_active > self._ttl:
                    del self._sessions[session_id]  # demote (or delete, with no backend)
                    record = None
                else:
                    record.last_active = now
                    self._sessions.move_to_end(session_id)
                    return record

        if self._persistence is not None:
            return self._rehydrate(session_id, now)
        return None

    def _rehydrate(self, session_id: str, now: float) -> SessionRecord | None:
        """Rebuild a :class:`SessionRecord` from the attached persistence
        backend, or ``None`` if it has no row for *session_id* (never
        persisted, or permanently deleted/purged)."""
        assert self._persistence is not None
        row = self._persistence.load_session_row(session_id)
        if row is None:
            return None
        turns, memories = self._persistence.load_turns(session_id)
        record = SessionRecord(
            session_id=session_id,
            created_at=datetime.fromisoformat(row["created_at"]),
            last_active=now,
            turns=turns,
            memory=memories,
            owner_id=row["owner_id"],
            title=row["title"],
        )
        with self._lock:
            while len(self._sessions) >= self._max_size:
                self._sessions.popitem(last=False)
            self._sessions[session_id] = record
        return record

    def require(self, session_id: str) -> SessionRecord:
        """Like :meth:`get`, but raises :class:`SessionNotFoundError` on a miss."""
        record = self.get(session_id)
        if record is None:
            raise SessionNotFoundError(f"Unknown or expired session: {session_id!r}")
        return record

    def delete(self, session_id: str) -> bool:
        """Remove *session_id* from the hot set AND, if attached, permanently
        from the persistence backend. Returns ``True`` if it existed in
        either place. A deleted session is never rehydrated back by a
        later :meth:`get`."""
        with self._lock:
            existed_hot = session_id in self._sessions
            if existed_hot:
                del self._sessions[session_id]
        existed_persisted = False
        if self._persistence is not None:
            existed_persisted = self._persistence.load_session_row(session_id) is not None
            self._persistence.delete_session(session_id)
        return existed_hot or existed_persisted

    def clear(self) -> None:
        """Test/admin helper: drop every (hot) session."""
        with self._lock:
            self._sessions.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"size": len(self._sessions), "max_size": self._max_size}

    @property
    def persistence(self) -> SessionPersistence | None:
        """The attached backend, or ``None`` — read-only; other modules
        (``api.v2_routes``'s cross-session memory endpoints) reuse this
        same backend rather than opening a second connection to the same
        file."""
        return self._persistence

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

    def sync_turn(self, record: SessionRecord, turn: Turn) -> None:
        """Persist *turn* — already appended to *record* by
        ``session.engine.TurnEngine.ask`` — to the attached backend.

        Derives ``record.title`` from *turn*'s question (§3) the first
        time a session gets a turn, regardless of whether persistence is
        attached, so ``GET /v2/sessions`` shows a title even with
        persistence disabled. Beyond that, a no-op with no backend
        attached.
        """
        if record.title is None and turn.index == 1:
            record.title = truncate_at_word_boundary(
                turn.question, cfg.settings.session_title_max_length,
            )

        if self._persistence is None:
            return

        memory = record.memory.get(turn.turn_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        self._persistence.upsert_session(
            record.session_id, record.owner_id, record.title,
            record.created_at.isoformat(), now_iso,
        )
        self._persistence.save_turn(record.session_id, turn, memory)

    def rename(self, record: SessionRecord, title: str) -> None:
        """Set *record*'s title (§3's ``PATCH /v2/sessions/{sid}``),
        persisting it immediately when a backend is attached."""
        record.title = title
        if self._persistence is not None:
            self._persistence.set_title(record.session_id, title)

    # ------------------------------------------------------------------
    # Conversation index (§3) and retention (§9, §10)
    # ------------------------------------------------------------------

    def list_sessions(self, owner_id: str | None) -> list[dict]:
        """The frozen §3 index shape for *owner_id* (or every ownerless
        session, when *owner_id* is ``None``).

        Sourced from the persistence backend when one is attached — so a
        TTL-demoted session still appears — otherwise from the in-memory
        hot set alone (the pre-persistence behaviour).
        """
        if self._persistence is not None:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=self._retention_days)
            ).isoformat()
            rows = self._persistence.list_sessions(owner_id or "", cutoff)
            return [
                {
                    "session_id": r["session_id"],
                    "title": r["title"],
                    "created_at": r["created_at"],
                    "last_active_at": r["last_active_at"],
                    "turn_count": r["turn_count"],
                    "expires_at": self._expires_at(r["last_active_at"]),
                }
                for r in rows
            ]

        with self._lock:
            records = [
                r for r in self._sessions.values()
                if r.owner_id == owner_id or r.owner_id is None
            ]
        now_iso = datetime.now(timezone.utc).isoformat()
        return [
            {
                "session_id": r.session_id,
                "title": r.title,
                "created_at": r.created_at.isoformat(),
                "last_active_at": now_iso,
                "turn_count": len(r.turns),
                "expires_at": self._expires_at(now_iso),
            }
            for r in records
        ]

    def _expires_at(self, last_active_iso: str) -> str | None:
        try:
            last_active = datetime.fromisoformat(last_active_iso)
        except ValueError:  # pragma: no cover - defensive only
            return None
        return (last_active + timedelta(days=self._retention_days)).isoformat()

    def purge_expired(self) -> int:
        """Permanently delete every persisted session whose last activity is
        older than ``session_retention_days`` (§9's retention purge — run
        once at start-up, not on a second daemon thread). Returns the
        number removed. A no-op (returns ``0``) with no persistence
        backend attached — TTL alone already bounds the hot set there.
        """
        if self._persistence is None:
            return 0
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        ).isoformat()
        return self._persistence.purge_sessions_older_than(cutoff)
