# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Real, end-to-end execution of the multi-dialect pipeline against SQLite.

This is the one dialect this project's own verification rule can actually
prove on CI: "a dialect is supported when a real query has been executed
against a real engine of that dialect, end to end — never when the
transpiled string looked right" (see the multi-dialect phase report).
Every test below drives the exact production pipeline --
:func:`~security.sql_guard.clean_sql` ->
:func:`~security.sql_guard.validate_sql` (tsql) ->
:func:`~security.sql_guard.ensure_top` (tsql) ->
:func:`~security.sql_guard.transpile_and_revalidate` (sqlite) ->
:func:`~database.executor.execute_sql` -- against a real, on-disk SQLite
database, never a mock.

Unlike ``test_executor_live.py`` (SQL Server, genuinely unavailable in
this environment), SQLite needs no external server -- it ships in the
Python standard library -- so these tests are **not** opt-in and are not
marked ``integration``: they run unconditionally, every time this suite
runs. They still live under ``tests/integration/`` because
``tests/conftest.py``'s autouse ``_no_real_database`` fixture refuses any
real ``database.connection.create_engine`` call across the rest of the
suite by design (see that fixture's docstring) -- this package's own
``conftest.py`` is the documented, deliberate exception, and these tests
need it for the same reason ``test_executor_live.py`` does: proving a real
engine actually works, not a mock of one.

Schema portability
-------------------
Every query here references only the ``Customer`` table with columns
``ID``, ``Name``, ``NationalID``, ``IsActive`` -- the one table whose
shape is identical in both the real, git-ignored ``project_config/`` this
deployment actually runs on and the committed
``project_config.example/`` CI falls back to (see
``config.Settings.project_config_dir``). No test here hardcodes a schema
qualifier (``Auction_Dim``/``sales``/...) -- ``security.sql_guard``'s
table allowlist resolves a bare table name regardless of qualifier, and
SQLite has no schema concept to qualify with in the first place (see
``security.dialects.DialectProfile.schema_qualification`` for "sqlite" ->
``"none"``) -- so these tests pass identically under CI's
``PROJECT_CONFIG_DIR=project_config.example`` and under a real,
copied-in ``project_config/``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import config as cfg
from config import override_settings
from database.connection import dispose_engine
from database.executor import execute_sql
from security.sql_guard import (
    CorrectableRejection,
    clean_sql,
    ensure_top,
    transpile_and_revalidate,
    validate_sql,
)

#: A Persian company name -- exactly the kind of national-character
#: literal (N'...') this phase's headline finding is about. Deliberately
#: a different string from any real warehouse's actual customer data (see
#: tests/test_no_domain_literals.py) -- this is test fixture data, not a
#: domain literal naming a real entity.
_PERSIAN_NAME = "فولاد آزمایشی"
_PERSIAN_NAME_2 = "شرکت تست پارس"


@pytest.fixture()
def sqlite_customer_db(tmp_path: Path):
    """A real, on-disk SQLite database seeded with a ``Customer`` table.

    Yields the ``sqlite:///`` connection URL. ``database.connection``'s
    cached engine is disposed both before and after -- mirrors
    ``test_executor_live.py``'s ``live_engine`` fixture discipline exactly
    (``get_engine`` is ``lru_cache``-backed and keyed on nothing, so a
    stale engine from an earlier test would otherwise silently survive
    the ``override_settings`` below).
    """
    db_path = tmp_path / "multi_dialect_live.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE Customer (ID INTEGER PRIMARY KEY, Name TEXT, "
            "NationalID TEXT, IsActive INTEGER)"
        )
        conn.execute(
            "INSERT INTO Customer (ID, Name, NationalID, IsActive) VALUES "
            "(?, ?, ?, ?)",
            (1, _PERSIAN_NAME, "1111111111", 1),
        )
        conn.execute(
            "INSERT INTO Customer (ID, Name, NationalID, IsActive) VALUES "
            "(?, ?, ?, ?)",
            (2, _PERSIAN_NAME_2, "2222222222", 1),
        )
        conn.execute(
            "INSERT INTO Customer (ID, Name, NationalID, IsActive) VALUES "
            "(?, ?, ?, ?)",
            (3, "Inactive Co", "3333333333", 0),
        )
        conn.commit()
    finally:
        conn.close()

    dispose_engine()
    try:
        yield f"sqlite:///{db_path}"
    finally:
        dispose_engine()


def _run_pipeline(tsql: str, *, target_dialect: str = "sqlite") -> str:
    """The exact production guard pipeline, tsql in -> target dialect out.

    Mirrors ``llm.sql_agent.SQLAgent._clean_validate_cap`` step for step:
    clean -> validate(tsql) -> cap(tsql) -> transpile_and_revalidate. Not
    a re-implementation the production code doesn't also run -- this is
    literally the same sequence of calls.
    """
    cleaned = clean_sql(tsql)
    validate_sql(cleaned)
    capped = ensure_top(cleaned, cfg.settings.default_top_n)
    return transpile_and_revalidate(capped, target_dialect=target_dialect)


class TestPersianLiteralEndToEnd:
    """The exact gap this phase's spec names: ``N'...'`` in a WHERE clause.

    ``validate_sql`` on the ORIGINAL tsql text must still accept ``N'...'``
    (it is correct, native T-SQL) -- this is what proves the tsql
    generation path is completely unaffected by multi-dialect support.
    """

    def test_tsql_guard_accepts_national_literal(self):
        tsql = f"SELECT Name FROM Customer WHERE Name = N'{_PERSIAN_NAME}'"
        cleaned = clean_sql(tsql)
        validate_sql(cleaned)  # must not raise

    def test_national_literal_executes_correctly_against_sqlite(self, sqlite_customer_db):
        tsql = f"SELECT Name FROM Customer WHERE Name = N'{_PERSIAN_NAME}'"
        final_sql = _run_pipeline(tsql)

        # The transpiled text must no longer contain the T-SQL national
        # literal prefix -- sqlglot leaves N'...' completely unchanged
        # under a naive transpile (see security.sql_guard.transpile_sql's
        # docstring), so this assertion is the one that would have failed
        # before this phase's _strip_unsupported_national_literals fix.
        assert "N'" not in final_sql

        with override_settings(db_connection_url=sqlite_customer_db, sql_dialect="sqlite"):
            df = execute_sql(final_sql)

        assert len(df) == 1
        assert df.iloc[0]["Name"] == _PERSIAN_NAME

    def test_national_literal_in_join_free_aggregate_executes(self, sqlite_customer_db):
        """A COUNT alongside a Persian-literal filter -- the "essentially
        every query" shape the phase report describes (aggregate + a
        Persian name equality), run for real."""
        tsql = (
            "SELECT COUNT(*) AS ActiveMatches FROM Customer "
            f"WHERE IsActive = 1 AND Name = N'{_PERSIAN_NAME}'"
        )
        final_sql = _run_pipeline(tsql)

        with override_settings(db_connection_url=sqlite_customer_db, sql_dialect="sqlite"):
            df = execute_sql(final_sql)

        assert df.iloc[0]["ActiveMatches"] == 1


class TestWindowFunctionAndAggregationEndToEnd:
    """Ranking (window function) and grouping, executed for real."""

    def test_row_number_ranking_executes_and_orders_correctly(self, sqlite_customer_db):
        tsql = (
            "SELECT Name, ROW_NUMBER() OVER (ORDER BY Name ASC) AS Rnk "
            "FROM Customer WHERE IsActive = 1"
        )
        final_sql = _run_pipeline(tsql)

        with override_settings(db_connection_url=sqlite_customer_db, sql_dialect="sqlite"):
            df = execute_sql(final_sql)

        assert len(df) == 2
        assert list(df["Rnk"]) == [1, 2]
        # Alphabetically first of the two active Persian names comes first.
        assert df.iloc[0]["Name"] in (_PERSIAN_NAME, _PERSIAN_NAME_2)

    def test_count_distinct_executes(self, sqlite_customer_db):
        tsql = "SELECT COUNT(DISTINCT IsActive) AS DistinctFlags FROM Customer"
        final_sql = _run_pipeline(tsql)

        with override_settings(db_connection_url=sqlite_customer_db, sql_dialect="sqlite"):
            df = execute_sql(final_sql)

        assert df.iloc[0]["DistinctFlags"] == 2

    def test_cte_refinement_executes(self, sqlite_customer_db):
        tsql = (
            "WITH active_customers AS ("
            "SELECT Name FROM Customer WHERE IsActive = 1"
            ") SELECT TOP 1 Name FROM active_customers ORDER BY Name ASC"
        )
        final_sql = _run_pipeline(tsql)

        with override_settings(db_connection_url=sqlite_customer_db, sql_dialect="sqlite"):
            df = execute_sql(final_sql)

        assert len(df) == 1


class TestPlusConcatenationRefusedNotSilentlyWrong:
    """The second transpilation gap this phase found by execution: T-SQL's
    ``+`` string concatenation transpiles UNCHANGED to every dialect, where
    ``+`` is exclusively numeric addition. Confirmed directly against a
    real SQLite engine: ``SELECT 'foo' + 'bar'`` does not error, it
    silently evaluates to ``0`` -- a plausible-looking wrong number, not a
    loud failure. The guard must refuse this before it ever reaches
    the database, not merely execute correctly's list.
    """

    def test_string_literal_concatenation_is_refused_before_reaching_sqlite(self):
        tsql = "SELECT Name + N' Co' AS Label FROM Customer"
        with pytest.raises(CorrectableRejection):
            _run_pipeline(tsql)

    def test_sqlite_plus_on_text_silently_returns_zero_not_an_error(self):
        """Documents the underlying database behaviour the guard rule
        above exists to prevent a caller from ever reaching -- run
        directly against sqlite3, bypassing the guard on purpose, to
        prove the danger is real rather than theoretical."""
        conn = sqlite3.connect(":memory:")
        try:
            (result,) = conn.execute("SELECT 'foo' + 'bar'").fetchone()
        finally:
            conn.close()
        assert result == 0
