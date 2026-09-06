# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 2 -- ``appdb.roles`` (spec §2.2's last-admin protection).

Real SQLAlchemy engine, real temp SQLite file, real
``security.auth.Principal`` capability flags -- no mock at the boundary
under test.
"""

from __future__ import annotations

import json

import pytest

import config as cfg
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache
from appdb.roles import LastAdminError, grant, holders, revoke
from security.auth import OPERATIONS_CAPABILITY, SECURITY_CAPABILITY


@pytest.fixture()
def app_db(tmp_path):
    db_path = tmp_path / "appdb.db"
    with cfg.override_settings(app_db_url=f"sqlite:///{db_path}"):
        dispose_app_engine()
        invalidate_cache()
        yield db_path
    dispose_app_engine()
    invalidate_cache()


class TestGrantAndRevoke:
    def test_grant_then_holders_includes_the_principal(self, app_db):
        grant("ops-1", OPERATIONS_CAPABILITY, granted_by="bootstrap-admin")
        assert "ops-1" in holders(OPERATIONS_CAPABILITY)

    def test_grant_is_idempotent(self, app_db):
        grant("ops-2", OPERATIONS_CAPABILITY, granted_by="bootstrap-admin")
        grant("ops-2", OPERATIONS_CAPABILITY, granted_by="bootstrap-admin")  # no error
        assert list(holders(OPERATIONS_CAPABILITY)).count("ops-2") == 1

    def test_revoke_removes_a_non_last_holder(self, app_db):
        grant("ops-3", OPERATIONS_CAPABILITY, granted_by="admin")
        grant("ops-4", OPERATIONS_CAPABILITY, granted_by="admin")
        revoke("ops-3", OPERATIONS_CAPABILITY)
        assert holders(OPERATIONS_CAPABILITY) == {"ops-4"}


class TestLastAdminProtection:
    def test_revoking_the_only_database_granted_holder_is_refused(self, app_db):
        grant("only-security-admin", SECURITY_CAPABILITY, granted_by="bootstrap")
        assert holders(SECURITY_CAPABILITY) == {"only-security-admin"}

        with pytest.raises(LastAdminError):
            revoke("only-security-admin", SECURITY_CAPABILITY)

        # And the grant must still be in effect -- refusal is not silent.
        assert holders(SECURITY_CAPABILITY) == {"only-security-admin"}

    def test_revoking_one_of_two_holders_is_allowed(self, app_db):
        grant("security-a", SECURITY_CAPABILITY, granted_by="bootstrap")
        grant("security-b", SECURITY_CAPABILITY, granted_by="bootstrap")
        revoke("security-a", SECURITY_CAPABILITY)  # does not raise
        assert holders(SECURITY_CAPABILITY) == {"security-b"}

    def test_last_admin_protection_holds_independently_for_each_role(self, app_db):
        """Revoking the only operations admin must not be blocked by an
        unrelated security admin existing, and vice versa -- the two
        capabilities are counted independently."""
        grant("only-ops", OPERATIONS_CAPABILITY, granted_by="bootstrap")
        grant("only-security", SECURITY_CAPABILITY, granted_by="bootstrap")

        with pytest.raises(LastAdminError):
            revoke("only-ops", OPERATIONS_CAPABILITY)
        with pytest.raises(LastAdminError):
            revoke("only-security", SECURITY_CAPABILITY)

    def test_env_bootstrapped_holder_counts_toward_the_last_admin_floor(self, app_db):
        """An environment-bootstrapped admin can never be demoted through
        this module at all (it only ever touches the database table) --
        but it must still count as a holder, so revoking the only
        DATABASE-granted holder of the same capability is allowed as long
        as the environment-bootstrapped one remains."""
        env_keys_json = json.dumps([
            {
                "id": "env-security-admin", "name": "Env Security",
                "key_sha256": "a" * 64, "security": True,
            },
        ])
        with cfg.override_settings(api_keys_json=env_keys_json):
            grant("db-security-admin", SECURITY_CAPABILITY, granted_by="env-security-admin")
            assert holders(SECURITY_CAPABILITY) == {"env-security-admin", "db-security-admin"}

            revoke("db-security-admin", SECURITY_CAPABILITY)  # does not raise
            assert holders(SECURITY_CAPABILITY) == {"env-security-admin"}

    def test_env_bootstrapped_last_admin_still_refuses_removing_the_db_side(self, app_db):
        """Even though the environment-bootstrapped principal can never
        actually be demoted via this module, a caller attempting to revoke
        the LAST distinct holder overall (here: itself, granted a second
        time only conceptually -- i.e. the only DB row IS the same id as
        the env entry) must still be refused, since after the revoke the
        capability would have exactly the holders it had before (the env
        one) -- this asserts the boundary condition where env and db
        holder sets overlap on the same id."""
        env_keys_json = json.dumps([
            {
                "id": "dual-listed", "name": "Dual",
                "key_sha256": "b" * 64, "security": True,
            },
        ])
        with cfg.override_settings(api_keys_json=env_keys_json):
            # No separate DB grant for a DIFFERENT id exists, so "dual-listed"
            # is the only holder from either source.
            assert holders(SECURITY_CAPABILITY) == {"dual-listed"}
            with pytest.raises(LastAdminError):
                revoke("dual-listed", SECURITY_CAPABILITY)
