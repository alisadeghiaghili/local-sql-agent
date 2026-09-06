# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 2 -- ``appdb.engine``'s same-database refusal (spec §1.2).

This is the single highest-risk check in the phase: if the application
database (which needs writes) and the warehouse connection (which
``docs/db-hardening.md`` specifies as read-only, and which
``database/executor.py`` always rolls back) ever resolved to the same
server and database, this phase's own writes would be the mechanism that
undoes the read-only posture. Every test below uses the real
:func:`appdb.engine.raise_if_same_database` / :func:`_canonical_endpoint`
against real SQLAlchemy URL strings -- no mock at the boundary under test.
"""

from __future__ import annotations

import pytest

from appdb.engine import raise_if_same_database


class TestSameDatabaseRefusal:
    def test_identical_urls_refused(self):
        with pytest.raises(RuntimeError, match="same server and database"):
            raise_if_same_database(
                "mssql+pyodbc://user@server:1433/Auction_DM?driver=x",
                "mssql+pyodbc://user@server:1433/Auction_DM?driver=x",
            )

    def test_localhost_vs_127_0_0_1_same_database_is_refused(self):
        """The exact case the spec calls out by name: 'localhost and
        127.0.0.1 with the same database is the same mistake spelled
        differently'."""
        with pytest.raises(RuntimeError):
            raise_if_same_database(
                "postgresql://appuser:pw@localhost:5432/appdb",
                "postgresql://readonly:pw@127.0.0.1:5432/appdb",
            )

    def test_ipv6_loopback_vs_localhost_same_database_is_refused(self):
        with pytest.raises(RuntimeError):
            raise_if_same_database(
                "postgresql://appuser:pw@[::1]:5432/appdb",
                "postgresql://readonly:pw@localhost:5432/appdb",
            )

    def test_same_server_different_database_is_allowed(self):
        """A normal, deliberate deployment shape -- must NOT be refused."""
        raise_if_same_database(
            "postgresql://appuser:pw@localhost:5432/appdb",
            "postgresql://readonly:pw@localhost:5432/warehouse",
        )

    def test_database_name_case_is_folded(self):
        """`Warehouse` and `warehouse` are the same mistake spelled twice.

        Database-name case sensitivity varies by backend and even by
        platform, so there is no inherited answer. The failure directions
        decide it: a false positive costs an operator a rename and a clear
        start-up error, while a false negative lets the application
        database's writes land on the warehouse and silently undo the
        read-only posture. The check over-matches on purpose.
        """
        with pytest.raises(RuntimeError, match="same server and database"):
            raise_if_same_database(
                "mssql+pyodbc://appuser:pw@DBHOST:1433/Warehouse",
                "mssql+pyodbc://readonly:pw@dbhost:1433/warehouse",
            )

    def test_different_host_same_database_name_is_allowed(self):
        raise_if_same_database(
            "postgresql://appuser:pw@app-db-host:5432/shared_name",
            "postgresql://readonly:pw@warehouse-host:5432/shared_name",
        )

    def test_sqlite_vs_mssql_never_collides(self):
        """The default zero-configuration shape -- SQLite app db, a real
        warehouse on a different backend entirely -- must always pass."""
        raise_if_same_database(
            "sqlite:///logs/app.db",
            "mssql+pyodbc://user@server:1433/Auction_DM?driver=ODBC+Driver+17+for+SQL+Server",
        )

    def test_identical_sqlite_file_path_is_refused(self):
        with pytest.raises(RuntimeError):
            raise_if_same_database("sqlite:///logs/app.db", "sqlite:///logs/app.db")

    def test_sqlite_relative_and_equivalent_path_both_refused(self):
        """Two spellings of the same file (relative vs. containing a
        redundant '..') must resolve to the same canonical path -- the
        SQLite equivalent of the host-normalisation rule."""
        with pytest.raises(RuntimeError):
            raise_if_same_database(
                "sqlite:///logs/app.db",
                "sqlite:///logs/../logs/app.db",
            )

    def test_sqlite_different_files_are_allowed(self):
        raise_if_same_database("sqlite:///logs/app.db", "sqlite:///logs/sessions.db")

    def test_two_in_memory_sqlite_urls_never_collide(self):
        """Regression test for a real bug found while writing this suite:
        the first implementation compared ``id(url_str)`` to give each
        in-memory URL a unique token, which silently broke because CPython
        interns short string literals -- two *separately written*
        ``"sqlite://"`` literals in calling code can share one object id,
        so the very first version of this check flagged two unrelated
        in-memory databases as "the same database". Observed failing
        before the fix (RuntimeError raised here); fixed by using a fresh
        ``uuid.uuid4()`` per call instead of ``id()``.
        """
        raise_if_same_database("sqlite://", "sqlite://")
        # And even the exact same literal object, twice, must not collide --
        # proving the fix does not merely dodge the interning coincidence
        # by accident of two different literals.
        same_literal = "sqlite://"
        raise_if_same_database(same_literal, same_literal)
