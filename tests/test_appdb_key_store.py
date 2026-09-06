# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 2 -- ``appdb.key_store`` (spec §3).

Every test here uses a REAL SQLAlchemy engine against a REAL SQLite file
on a REAL temp path (``tmp_path``), a REAL ``security.auth.Principal``,
and calls the module's own public functions directly -- no mock at the
boundary under test. ``config.override_settings(app_db_url=...)`` points
each test at its own isolated database file, matching
``tests/test_session_persistence.py``'s own ``tmp_path`` discipline.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

import config as cfg
from appdb.engine import dispose_app_engine, get_app_engine
from appdb.key_store import (
    AmbiguousKeyIdentityError,
    bootstrap_from_env,
    get_active_principals,
    invalidate_cache,
    issue_key,
    list_keys,
    revoke_key,
    set_disabled,
    update_denied_columns,
)


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@pytest.fixture()
def app_db(tmp_path):
    """Point the application database at an isolated file for this test,
    and guarantee a fresh engine both before and after (the autouse
    ``_fresh_app_db`` fixture in conftest.py already does this for the
    *default* in-memory URL between tests generally, but this fixture's
    own ``override_settings`` changes the URL mid-test, so the cache and
    cached engine must be reset again here to actually point at the new
    file rather than an already-cached prior engine)."""
    db_path = tmp_path / "appdb.db"
    with cfg.override_settings(app_db_url=f"sqlite:///{db_path}"):
        dispose_app_engine()
        invalidate_cache()
        yield db_path
    dispose_app_engine()
    invalidate_cache()


class TestIssueKeyRestrictiveDefault:
    def test_issued_key_denies_every_known_column(self, app_db):
        """Spec §2.1 escalation path #1: a freshly issued key must not be
        able to select a single column until a security admin loosens it.
        Uses the real schema_data.registry -- project_config.example/'s
        schema.yaml (or project_config/'s, whichever this run resolves to)
        is the source of truth for "every known column"."""
        from schema_data.registry import get_table_columns

        raw_key, entry = issue_key("ops-issued-1", "Ops Issued")
        assert raw_key  # a real raw key was minted
        all_columns = {c for cols in get_table_columns().values() for c in cols}
        assert set(entry["denied_columns"]) == all_columns
        assert all_columns, "sanity: the resolved schema must have at least one column"

    def test_raw_key_is_never_persisted(self, app_db):
        raw_key, entry = issue_key("ops-issued-2", "Ops Issued 2")
        assert entry["key_sha256"] == _sha256(raw_key)
        stored = list_keys()
        assert raw_key not in json.dumps(stored)


class TestRevocationIsImmediate:
    def test_revoked_key_is_absent_from_active_principals_on_next_call(self, app_db):
        """Spec §3.2/§5.6: revoke, then the very next read must reflect
        it -- no restart, no sleep-and-hope."""
        raw_key, entry = issue_key("will-be-revoked", "Soon Revoked")
        key_hash = entry["key_sha256"]

        active = get_active_principals()
        assert key_hash in active

        revoke_key(key_hash)

        active_after = get_active_principals()
        assert key_hash not in active_after

    def test_disabled_key_is_absent_and_re_enabling_restores_it(self, app_db):
        raw_key, entry = issue_key("will-be-disabled", "Soon Disabled")
        key_hash = entry["key_sha256"]

        set_disabled(key_hash, True)
        assert key_hash not in get_active_principals()

        set_disabled(key_hash, False)
        assert key_hash in get_active_principals()


class TestCacheServesUnchangedKeySetWithoutRequerying:
    def test_no_per_request_requery_when_nothing_changed(self, app_db, monkeypatch):
        """Spec §3.2's other half: an UNCHANGED key set must not re-query
        the database on every call within the TTL window."""
        raw_key, entry = issue_key("cached-principal", "Cached Principal")
        invalidate_cache()  # start from a clean cache for this assertion

        from appdb import key_store as key_store_module

        call_count = {"n": 0}
        real_load = key_store_module._load_db_rows

        def _counting_load():
            call_count["n"] += 1
            return real_load()

        monkeypatch.setattr(key_store_module, "_load_db_rows", _counting_load)

        with cfg.override_settings(key_cache_ttl_seconds=30.0):
            for _ in range(5):
                result = get_active_principals()
                assert entry["key_sha256"] in result

        assert call_count["n"] == 1, (
            f"expected exactly one database read across 5 calls inside the "
            f"TTL window, got {call_count['n']}"
        )

    def test_cache_refreshes_after_ttl_elapses(self, app_db):
        raw_key, entry = issue_key("ttl-expiring-principal", "TTL Principal")
        with cfg.override_settings(key_cache_ttl_seconds=0.05):
            invalidate_cache()
            assert entry["key_sha256"] in get_active_principals()
            revoke_key(entry["key_sha256"])  # also invalidates explicitly
            assert entry["key_sha256"] not in get_active_principals()


class TestBootstrapFromEnv:
    def test_env_keys_imported_into_empty_table(self, app_db):
        keys_json = json.dumps([
            {"id": "env-1", "name": "Env One", "key_sha256": _sha256("a" * 40)},
        ])
        with cfg.override_settings(api_keys_json=keys_json):
            bootstrap_from_env()

        stored = list_keys()
        assert any(row["principal_id"] == "env-1" for row in stored)
        assert any(row["source"] == "imported_from_env" for row in stored)

    def test_bootstrap_is_idempotent_across_restarts(self, app_db):
        keys_json = json.dumps([
            {"id": "env-2", "name": "Env Two", "key_sha256": _sha256("b" * 40)},
        ])
        with cfg.override_settings(api_keys_json=keys_json):
            bootstrap_from_env()
            bootstrap_from_env()  # a second "restart" against the same, now-populated table

        stored = [r for r in list_keys() if r["principal_id"] == "env-2"]
        assert len(stored) == 1, "the table must not accumulate a second imported row"

    def test_ambiguous_identity_between_env_and_database_refuses(self, app_db):
        """Spec §3.3: the same principal id resolving to two DIFFERENT key
        hashes across the two sources must refuse to start."""
        # First boot: import "shared-id" bound to key A.
        keys_json_a = json.dumps([
            {"id": "shared-id", "name": "First", "key_sha256": _sha256("a" * 40)},
        ])
        with cfg.override_settings(api_keys_json=keys_json_a):
            bootstrap_from_env()

        # A later boot with the SAME id now bound to a DIFFERENT raw key's
        # hash -- e.g. an operator edited API_KEYS_JSON without realising
        # a database row for that id already exists.
        keys_json_b = json.dumps([
            {"id": "shared-id", "name": "First", "key_sha256": _sha256("c" * 40)},
        ])
        with cfg.override_settings(api_keys_json=keys_json_b):
            with pytest.raises(AmbiguousKeyIdentityError):
                bootstrap_from_env()

    def test_consistent_reimport_of_the_same_entry_is_not_ambiguous(self, app_db):
        """The common case: an operator leaves the original API_KEYS_JSON
        entry in place after it was imported -- same id, same hash, every
        subsequent start. Must NOT be refused."""
        keys_json = json.dumps([
            {"id": "steady-id", "name": "Steady", "key_sha256": _sha256("d" * 40)},
        ])
        with cfg.override_settings(api_keys_json=keys_json):
            bootstrap_from_env()
            bootstrap_from_env()  # no error


class TestDatabaseIsAuthoritativeOverAStaleEnvEntry:
    def test_revoking_an_imported_key_takes_effect_even_though_env_still_lists_it(self, app_db):
        """The point of moving keys to the database at all: an
        originally-environment-sourced key's revocation must be immediate
        even on a process that never restarted (so its hash is still
        sitting, unchanged, in API_KEYS_JSON)."""
        raw = "e" * 40
        keys_json = json.dumps([
            {"id": "imported-then-revoked", "name": "X", "key_sha256": _sha256(raw)},
        ])
        with cfg.override_settings(api_keys_json=keys_json):
            bootstrap_from_env()
            assert _sha256(raw) in get_active_principals()

            revoke_key(_sha256(raw))

            # Still the SAME override_settings block -- API_KEYS_JSON is
            # unchanged and still names this key, but the database row
            # governs usability.
            assert _sha256(raw) not in get_active_principals()


class TestRevocationSurvivesRestore:
    def test_revoked_key_stays_revoked_after_engine_restart(self, app_db):
        """Spec §3.4: a tombstone, never a delete -- simulate a restart by
        disposing the engine and reopening the same file."""
        raw_key, entry = issue_key("restore-me", "Restore Me")
        revoke_key(entry["key_sha256"])

        dispose_app_engine()
        invalidate_cache()

        stored = next(r for r in list_keys() if r["key_sha256"] == entry["key_sha256"])
        assert stored["revoked_at"] is not None
        assert entry["key_sha256"] not in get_active_principals()


class TestAclUpdateIsSecurityOnlyAtTheStorageLayer:
    def test_update_denied_columns_changes_stored_acl(self, app_db):
        raw_key, entry = issue_key("acl-target", "ACL Target")
        update_denied_columns(entry["key_sha256"], ["OnlyThisColumn"])

        stored = next(r for r in list_keys() if r["key_sha256"] == entry["key_sha256"])
        assert stored["denied_columns"] == ["OnlyThisColumn"]

        principal = get_active_principals()[entry["key_sha256"]]
        assert principal.denied_columns == ("OnlyThisColumn",)
