# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Findings 4, 6, 8, 10 and 20 — the smaller items, each with its own reason.

Grouped because each is a handful of assertions, not because they are
related. Read them independently.

**4 — the audit trail fails open, silently.** ``observability/audit.py``
swallows any ``OSError`` while writing, on the stated principle that a
broken audit log must never fail a user's query. For an internal tool that
is right. For a venue where "who asked what" is itself an obligation, the
part that is wrong is not the fail-open -- it is that it is *silent*. A
full disk, a permissions change, or someone deliberately making the file
unwritable produces queries that execute with no record and nothing
anywhere saying so. Keep the fail-open; make it observable.

**6 — the two key sources disagree on the default ACL.** A key issued
through the panel gets ``_maximally_restrictive_denied_columns()``. A key
in ``API_KEYS_JSON`` with no ``denied_columns`` gets ``()`` -- no
restriction at all. Flipping the env default to deny-all would silently
revoke access from every existing deployment mid-upgrade, so that is not
the fix. Being loud about it is.

**8 — a security parameter nobody passes.** ``scope_key`` accepts
``memory_used`` and no call site supplies it. Harmless today only because
the path that uses memory does not cache; ``session/engine.py`` carries a
``# reserved for a future T0 cache tier`` marker at the exact seam where
that stops being true. Whoever wires that tier will copy the existing call
and inherit the default, and two analysts with different memory will share
a cache entry. A default that is only safe by accident should not be a
default.

**10 — identifier quoting does not double inner quotes.**
``f'"{column_name}"'`` breaks on a name containing ``"``. Low risk, since
names come from the database catalogue rather than from a question -- an
attacker would need DDL rights already. Worth fixing as correctness. Note
the remedy is *not* ``bindparams``: bind parameters carry values, never
identifiers, so the advice an earlier report gave here would not have run.

**20 — one principal can evict every other principal's sessions.** The
store is a single 500-entry LRU with no per-owner quota. Persistence is on
by default, so eviction costs a rehydration rather than data -- a
performance problem, not a loss. It becomes a real one if persistence is
ever disabled.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Finding 4 -- a silent audit failure is the part that must change
# ---------------------------------------------------------------------------

def _record(request_id: str):
    from datetime import datetime

    from observability.audit import AuditRecord

    return AuditRecord(
        timestamp=datetime.now(),
        request_id=request_id,
        question="q",
        generated_sql="SELECT 1",
        guard={"verdict": "allowed"},
    )


class TestAuditWriteFailuresAreObservable:
    def test_a_failed_write_increments_a_counter(self, monkeypatch):
        import observability.audit as audit

        monkeypatch.setattr(
            audit, "append_jsonl",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        before = audit.audit_write_failures()
        audit.save_audit_record(_record("r_1"))
        assert audit.audit_write_failures() == before + 1, (
            "a failed audit write left no trace anywhere. The query still ran; "
            "nothing recorded that it ran unaudited"
        )

    def test_the_query_still_succeeds(self, monkeypatch):
        """The fail-open contract is deliberate and stays. Observability is
        the addition, not a replacement."""
        import observability.audit as audit

        monkeypatch.setattr(
            audit, "append_jsonl",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        audit.save_audit_record(_record("r_2"))  # must not raise

    def test_the_count_is_surfaced_to_an_operator(self):
        """A counter nobody can read is not observability."""
        src = (_REPO_ROOT / "api" / "admin_routes.py").read_text(encoding="utf-8")
        assert "audit_write_failures" in src, (
            "the admin summary does not report audit write failures, so a "
            "severed audit trail is invisible in the one place an operator looks"
        )


# ---------------------------------------------------------------------------
# Finding 6 -- the asymmetry is defensible; the silence is not
# ---------------------------------------------------------------------------

class TestTheEnvKeyAclDefaultIsAnnounced:
    def test_an_env_key_without_denied_columns_warns(self, caplog):
        import logging

        from config import override_settings
        from security.auth import load_api_keys

        payload = (
            '[{"id":"analyst-9","name":"No ACL","key_sha256":"'
            + "a" * 64 + '"}]'
        )
        with caplog.at_level(logging.WARNING), override_settings(api_keys_json=payload):
            load_api_keys()

        assert any(
            "denied_columns" in r.message for r in caplog.records
        ), (
            "a key configured with no denied_columns loaded silently with "
            "unrestricted column access, while a panel-issued key would have "
            "been created deny-all. Same system, opposite defaults, no notice"
        )

    def test_an_explicit_empty_list_does_not_warn(self, caplog):
        """Someone who wrote `"denied_columns": []` has made the decision.
        Warning at them trains people to ignore the warning."""
        import logging

        from config import override_settings
        from security.auth import load_api_keys

        payload = (
            '[{"id":"analyst-9","name":"Explicit","key_sha256":"'
            + "a" * 64 + '","denied_columns":[]}]'
        )
        with caplog.at_level(logging.WARNING), override_settings(api_keys_json=payload):
            load_api_keys()
        assert not any("denied_columns" in r.message for r in caplog.records)

    def test_the_asymmetry_is_documented(self):
        docs = list((_REPO_ROOT / "docs").rglob("*.md"))
        text = "\n".join(p.read_text(encoding="utf-8") for p in docs)
        assert "denied_columns" in text, (
            "no document explains that omitting denied_columns from "
            "API_KEYS_JSON grants full column access"
        )


# ---------------------------------------------------------------------------
# Finding 8 -- remove the accidental default before it is inherited
# ---------------------------------------------------------------------------

class TestScopeKeyForcesAMemoryDecision:
    def test_memory_used_has_no_silent_default(self):
        import inspect

        from security.auth import scope_key

        param = inspect.signature(scope_key).parameters["memory_used"]
        assert param.default is inspect.Parameter.empty, (
            "scope_key still defaults memory_used, so the future T0 cache "
            "tier will silently omit it and two analysts with different "
            "memory will share a cache entry"
        )

    def test_existing_callers_pass_it_explicitly(self):
        """Making it required is only meaningful if the call sites were
        updated to state their answer rather than to satisfy the compiler."""
        for module in ("api/runner.py", "retrieval/value_resolver.py"):
            src = (_REPO_ROOT / module).read_text(encoding="utf-8")
            for call in re.findall(r"scope_key\w*\([^)]*\)", src):
                if "def " in call:
                    continue
                assert "memory_used" in call, (
                    f"{module} calls {call} without naming memory_used"
                )

    def test_the_cache_seam_carries_a_warning(self):
        src = (_REPO_ROOT / "session" / "engine.py").read_text(encoding="utf-8")
        if "future T0 cache tier" in src:
            line = next(ln for ln in src.splitlines() if "future T0 cache tier" in ln)
            idx = src.splitlines().index(line)
            nearby = "\n".join(src.splitlines()[max(0, idx - 6): idx + 3])
            assert "SECURITY" in nearby or "memory_used" in nearby, (
                "the reserved cache seam does not warn that wiring it without "
                "memory_used leaks between principals"
            )


# ---------------------------------------------------------------------------
# Finding 10 -- correctness, with the right remedy
# ---------------------------------------------------------------------------

class TestIdentifierQuotingIsDelegatedToTheDialect:
    def test_the_inspector_does_not_hand_roll_quoting(self):
        src = (_REPO_ROOT / "database" / "schema_inspector.py").read_text(encoding="utf-8")
        assert not re.search(r'f\'"\{(column_name|table_name|schema)\}"\'', src), (
            "schema_inspector still builds quoted identifiers with an f-string, "
            "which does not double an embedded quote"
        )

    def test_it_uses_the_dialect_preparer(self):
        src = (_REPO_ROOT / "database" / "schema_inspector.py").read_text(encoding="utf-8")
        assert "identifier_preparer" in src, (
            "the fix is engine.dialect.identifier_preparer.quote(). It is NOT "
            "bindparams -- bind parameters carry values, never identifiers"
        )


# ---------------------------------------------------------------------------
# Finding 20 -- one principal must not evict another's working set
# ---------------------------------------------------------------------------

class TestSessionsAreQuotedPerPrincipal:
    def test_one_principal_cannot_evict_anothers_session(self):
        from session.store import SessionStore

        store = SessionStore(ttl_seconds=1800, max_size=8, max_turns=10)
        victim = store.create(owner_id="analyst-victim")

        for _ in range(20):
            store.create(owner_id="noisy-neighbour")

        assert store.get(victim.session_id) is not None, (
            "a single principal creating sessions evicted another principal's "
            "session from the shared cache. Eviction should fall on the "
            "owner who caused it"
        )

    def test_a_principal_still_evicts_its_own_oldest(self):
        """The quota must bound one owner, not stop them working."""
        from session.store import SessionStore

        store = SessionStore(ttl_seconds=1800, max_size=8, max_turns=10)
        first = store.create(owner_id="analyst-1")
        for _ in range(20):
            store.create(owner_id="analyst-1")

        assert store.get(first.session_id) is None or store.stats()["size"] <= 8, (
            "the per-owner quota is not bounding anything"
        )
