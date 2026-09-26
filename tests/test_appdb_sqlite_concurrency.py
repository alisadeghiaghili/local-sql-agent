# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The FILE-backed application database must survive real concurrent writers.

Reproduces, with real ``threading.Thread`` workers against a real SQLite
FILE on a real ``tmp_path`` (never a mock, never ``:memory:`` -- an
in-memory database is a single process-wide connection by construction,
which would prove nothing about the file-locking/pooling bug this module
guards against), the exact failure this suite exists to close: FastAPI
runs this codebase's synchronous admin/analyst routes in a thread pool, so
two requests writing the application database at the same moment used to
share one ``sqlite3.Connection`` (``appdb.engine.build_engine``'s old,
unconditional ``StaticPool``) and race each other's commits and
rollbacks -- ``sqlite3.InterfaceError``, ``OperationalError: cannot commit
-- no transaction is active``, and rows left inconsistent.

Eight real threads, started together with a ``threading.Barrier`` (never a
``sleep``-based approximation of "at the same time"), each doing several
writes through the real, unmocked ``appdb`` functions an admin request
actually calls -- ``appdb.key_store.issue_key`` and
``appdb.access_requests.approve_request``/``deny_request`` racing on the
very same request row, the exact pairing the original bug report
reproduced from two real threads. Verified, while building this fix, to
fail reliably if ``appdb.engine.build_engine`` is reverted to giving a
file-backed SQLite URL ``poolclass=StaticPool`` again.
"""

from __future__ import annotations

import shutil
import threading
from datetime import datetime
from pathlib import Path

import pytest

import config as cfg
from appdb.access_requests import (
    AlreadyResolvedError,
    approve_request,
    deny_request,
    submit_request,
)
from appdb.engine import dispose_app_engine
from appdb.key_store import issue_key, list_keys
from observability.audit import AuditRecord, save_audit_record

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"

#: 4 racing pairs (one thread approving, one denying) -- 8 threads total,
#: matching the requirement's own "8 threads started together with a
#: threading.Barrier".
_N_PAIRS = 4
#: 4 pairs * 15 rounds = 60 independent approve-vs-deny races -- the same
#: iteration count the bug this suite guards against was first reproduced
#: with (two threads, 60 iterations), spread here across 4 concurrent
#: pairs instead of one.
_N_ROUNDS = 15
_N_THREADS = _N_PAIRS * 2

#: Generous but bounded -- a hang here (the exact failure mode the first,
#: pool-event-based attempt at the in-memory fix produced while this suite
#: was being built) must be reported as a test failure, never left to hang
#: CI forever.
_BARRIER_TIMEOUT_SECONDS = 30
_JOIN_TIMEOUT_SECONDS = 60


@pytest.fixture()
def app_env(tmp_path):
    """A real, FILE-backed application database on an isolated temp path,
    a real audit-log directory, and ``project_config.example/`` copied in
    -- mirrors ``tests/test_appdb_access_requests.py``'s own fixture
    exactly, since this test exercises the same real functions.
    """
    db_path = tmp_path / "app.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    project_dir = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE_CONFIG_DIR, project_dir)
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}",
        log_dir=str(log_dir),
        api_keys_json="[]",
        project_config_dir=str(project_dir),
    ):
        dispose_app_engine()
        yield {"db_path": db_path}
    # Windows keeps the database (and its -wal/-shm sidecars) locked for as
    # long as any connection from this engine is open -- dispose here,
    # before pytest tears down `tmp_path`, is what lets that directory
    # actually be deleted afterwards (requirement 4's own concern).
    dispose_app_engine()


def _any_real_column() -> str:
    """A real, schema-backed column name -- resolved lazily, after
    ``app_env`` has already pointed ``project_config_dir`` at a real
    schema, the same idiom ``tests/test_appdb_access_requests.py`` uses.
    A freshly issued key's ``denied_columns`` always starts as *every*
    such column (``appdb.key_store``'s restrictive default), which is
    exactly what makes "the winning side's key state matches" a
    meaningful, checkable invariant below.
    """
    from schema_data.columns import TABLE_COLUMNS

    any_table = next(iter(TABLE_COLUMNS))
    return next(iter(TABLE_COLUMNS[any_table]))


class TestApproveVsDenyRaceUnderRealThreads:
    """The mandated concurrency test (requirement 5): 8 real threads, a
    real ``threading.Barrier``, several real writes each, against a real
    file-backed application database."""

    def test_eight_threads_racing_key_issuance_and_approve_deny_survive(self, app_env):
        column = _any_real_column()

        # ------------------------------------------------------------
        # Setup (sequential -- the race under test is the concurrent
        # phase below, not this): one open access request per (pair,
        # round), each for its own, never-reused principal so that one
        # round's approval can never retroactively touch another
        # round's key (appdb.access_requests.approve_request updates
        # EVERY live key of the requesting principal that still denies
        # the column -- a shared principal across rounds would let a
        # later round's approval widen an earlier round's already-
        # resolved "denied" key, which would make this test's own
        # invariant check meaningless rather than testing anything about
        # concurrency).
        # ------------------------------------------------------------
        request_id = {}
        for p in range(_N_PAIRS):
            for r in range(_N_ROUNDS):
                principal = f"racer-{p}-{r}"
                session_id, turn_id = f"s-{p}-{r}", f"t-{p}-{r}"
                save_audit_record(AuditRecord(
                    timestamp=datetime(2026, 1, 1, 12, 0, 0),
                    request_id=f"audit-{p}-{r}",
                    question="پرسش آزمایشی همزمانی",
                    generated_sql="SELECT 1",
                    guard={"verdict": "rejected", "reason": "denied_column", "subject": column},
                    row_count=0,
                    session_id=session_id,
                    turn_id=turn_id,
                ))
                row, created = submit_request(
                    session_id=session_id, turn_id=turn_id, requester_principal_id=principal,
                )
                assert created is True
                request_id[p, r] = row["request_id"]

        # ------------------------------------------------------------
        # Concurrent phase: 8 real threads (4 pairs), started together
        # with a threading.Barrier, each doing several real writes --
        # one appdb.key_store.issue_key call and one
        # appdb.access_requests.approve_request/deny_request call, per
        # round, for _N_ROUNDS rounds.
        # ------------------------------------------------------------
        start_barrier = threading.Barrier(_N_THREADS)
        race_barrier = threading.Barrier(_N_THREADS)

        errors: list[BaseException] = []
        outcomes: dict[tuple[int, int], dict[str, str]] = {
            (p, r): {"approve": "", "deny": ""} for p in range(_N_PAIRS) for r in range(_N_ROUNDS)
        }
        key_hashes: dict[tuple[int, int], dict[str, str]] = {
            (p, r): {"approve": None, "deny": None} for p in range(_N_PAIRS) for r in range(_N_ROUNDS)
        }

        def _worker(pair: int, role: str) -> None:
            for r in range(_N_ROUNDS):
                try:
                    start_barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
                except threading.BrokenBarrierError as exc:
                    errors.append(exc)
                    return

                principal = f"racer-{pair}-{r}"
                try:
                    _, entry = issue_key(principal, f"Key-{pair}-{r}-{role}")
                    key_hashes[pair, r][role] = entry["key_sha256"]
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

                try:
                    race_barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
                except threading.BrokenBarrierError as exc:
                    errors.append(exc)
                    return

                try:
                    if role == "approve":
                        approve_request(request_id[pair, r], actor_principal_id="security-1")
                    else:
                        deny_request(
                            request_id[pair, r],
                            actor_principal_id="security-1",
                            reason="denied for a concurrency test",
                        )
                    outcomes[pair, r][role] = "won"
                except AlreadyResolvedError:
                    outcomes[pair, r][role] = "lost"
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                    outcomes[pair, r][role] = "error"

        threads = [
            threading.Thread(target=_worker, args=(p, role), daemon=True)
            for p in range(_N_PAIRS)
            for role in ("approve", "deny")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_JOIN_TIMEOUT_SECONDS)

        still_alive = [t.name for t in threads if t.is_alive()]
        assert not still_alive, f"thread(s) never finished (a hang): {still_alive}"

        # ------------------------------------------------------------
        # Invariant 1: no unexpected exception. AlreadyResolvedError is
        # the EXPECTED shape of a race's losing side (appdb.access_
        # requests._require_open's own, correct refusal to resolve an
        # already-resolved request) -- it is recorded as an outcome
        # above, never appended to `errors`. Anything else here --
        # sqlite3.InterfaceError, OperationalError, a BrokenBarrierError
        # from a stuck thread -- is exactly the corruption/hang this
        # module exists to prevent.
        # ------------------------------------------------------------
        assert not errors, f"unexpected exception(s) from concurrent appdb writers: {errors!r}"

        # ------------------------------------------------------------
        # Invariant 2: for every round, exactly one side won and the
        # other lost -- never both (a lost update), never neither (a
        # stuck/failed resolution).
        # ------------------------------------------------------------
        for (p, r), sides in outcomes.items():
            wins = [role for role, outcome in sides.items() if outcome == "won"]
            assert len(wins) == 1, (
                f"pair={p} round={r}: expected exactly one winner between "
                f"approve/deny, got {sides!r}"
            )
            loser = "deny" if wins[0] == "approve" else "approve"
            assert sides[loser] == "lost", (
                f"pair={p} round={r}: the losing side must fail with "
                f"AlreadyResolvedError, got {sides!r}"
            )

        # ------------------------------------------------------------
        # Invariant 3 ("keys match the winner"): every key issued for a
        # round reflects that round's real outcome -- approved means the
        # column was actually removed from it, denied means it is still
        # there. Read back through the real appdb.key_store.list_keys,
        # once, after every thread has finished.
        # ------------------------------------------------------------
        denied_columns_by_hash = {row["key_sha256"]: set(row["denied_columns"]) for row in list_keys()}
        for (p, r), sides in outcomes.items():
            winner = "approve" if sides["approve"] == "won" else "deny"
            for role in ("approve", "deny"):
                key_hash = key_hashes[p, r][role]
                assert key_hash is not None, f"pair={p} round={r} role={role}: issue_key never recorded a hash"
                denied = denied_columns_by_hash[key_hash]
                if winner == "approve":
                    assert column not in denied, (
                        f"pair={p} round={r} role={role}: request was approved, "
                        f"but key {key_hash!r} still denies {column!r}"
                    )
                else:
                    assert column in denied, (
                        f"pair={p} round={r} role={role}: request was denied, "
                        f"but key {key_hash!r} no longer denies {column!r}"
                    )
