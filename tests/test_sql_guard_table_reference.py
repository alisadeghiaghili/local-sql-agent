# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression tests for the ADR-001 projection-allowlist rule added to
``validate_sql`` (see its module docstring's ADR-001 paragraph and rules
11-13): a table-less query (R1), a server/session-state read anywhere in
the tree (R2), and an unrecognised ``exp.Anonymous`` function anywhere in
the tree (R3).

This module has four jobs:

1. **R1 actually fires** — a query with no non-CTE table reference at all
   is rejected as ``CorrectableRejection``/``no_table_reference``, with
   ``is_refusal`` set.
2. **R2/R3 actually fire, with a real table present** — so these tests
   prove the new rules, not the pre-existing table allowlist.
3. **Hidden positions are caught** — not just the projection: ``WHERE``,
   ``ORDER BY``, a CTE body, and a scalar subquery.
4. **Zero false positives** — every legitimate, ordinary data-function
   negative control from the design's validated catalogue must still pass
   ``validate_sql`` without raising. This is the load-bearing half of this
   module: R2/R3 are a broad catch-all, and the whole point of ADR-001 is
   that it does not reject ordinary analytic SQL.

Run::

    pytest tests/test_sql_guard_table_reference.py -v
"""

from __future__ import annotations

import pytest

import security.sql_guard as sql_guard
from schema_data.columns import TABLE_COLUMNS
from security.sql_guard import CorrectableRejection, PolicyRejection, validate_sql

#: A table (and one of its columns) picked dynamically from whatever
#: schema is loaded (real or project_config.example/), rather than a
#: hardcoded real name -- see tests/test_sql_guard_schema.py, the
#: established precedent for this idiom.
_ANY_TABLE = next(iter(TABLE_COLUMNS))
_ANY_COLUMN = next(iter(TABLE_COLUMNS[_ANY_TABLE]))


# ---------------------------------------------------------------------------
# R1 -- table reference required
# ---------------------------------------------------------------------------


class TestR1TableLessQueryIsRejected:
    @pytest.mark.parametrize("sql", [
        "SELECT 1",
        "SELECT * FROM (SELECT 1 AS a) z",
    ])
    def test_no_table_reference_is_correctable_and_a_refusal(self, sql):
        with pytest.raises(CorrectableRejection) as exc_info:
            validate_sql(sql)
        assert exc_info.value.reason == "no_table_reference"
        assert exc_info.value.is_refusal is True


# ---------------------------------------------------------------------------
# R2 -- forbidden state-reading nodes, with a real table present
# ---------------------------------------------------------------------------


class TestR2StateReadingNodesAreRejected:
    @pytest.mark.parametrize("expr", [
        "@@version",
        "@@spid",
        "CURRENT_USER",
        "SYSTEM_USER",
        "SESSION_USER",
        "SUSER_NAME()",
        "SCHEMA_NAME(1)",
        "OBJECT_ID('x')",
    ])
    def test_state_node_in_projection_is_forbidden_statement(self, expr):
        sql = f"SELECT {expr} FROM [{_ANY_TABLE}]"
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql(sql)
        assert exc_info.value.reason == "forbidden_statement"


# ---------------------------------------------------------------------------
# R3 -- Anonymous metadata-function allowlist, with a real table present
# ---------------------------------------------------------------------------


class TestR3MetadataAnonymousFunctionsAreRejected:
    @pytest.mark.parametrize("expr", [
        "SERVERPROPERTY('x')",
        "IS_SRVROLEMEMBER('sysadmin')",
        f"OBJECT_NAME(a.{_ANY_COLUMN})",
        f"COL_NAME(a.{_ANY_COLUMN},1)",
        "DB_NAME()",
        "HOST_NAME()",
        "DB_ID()",
        "APP_NAME()",
        "PWDENCRYPT('x')",
        "LOGINPROPERTY('sa','x')",
        "HAS_PERMS_BY_NAME(NULL,NULL,'CONTROL SERVER')",
        "FILE_NAME(1)",
        "CONNECTIONPROPERTY('x')",
        "SESSION_CONTEXT(N'x')",
    ])
    def test_metadata_function_in_projection_is_forbidden_statement(self, expr):
        sql = f"SELECT {expr} FROM [{_ANY_TABLE}] a"
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql(sql)
        assert exc_info.value.reason == "forbidden_statement"


# ---------------------------------------------------------------------------
# Hidden positions -- not just the projection
# ---------------------------------------------------------------------------


class TestHiddenPositionsAreStillCaught:
    def test_state_node_in_where(self):
        sql = f"SELECT {_ANY_COLUMN} FROM [{_ANY_TABLE}] WHERE {_ANY_COLUMN} = @@spid"
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql(sql)
        assert exc_info.value.reason == "forbidden_statement"

    def test_metadata_function_in_order_by(self):
        sql = (
            f"SELECT {_ANY_COLUMN} FROM [{_ANY_TABLE}] "
            "ORDER BY SERVERPROPERTY('x')"
        )
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql(sql)
        assert exc_info.value.reason == "forbidden_statement"

    def test_state_node_inside_a_cte_body(self):
        sql = "WITH c AS (SELECT @@version AS v) SELECT * FROM c"
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql(sql)
        assert exc_info.value.reason == "forbidden_statement"

    def test_metadata_function_inside_a_scalar_subquery(self):
        sql = f"SELECT (SELECT SERVERPROPERTY('x')) AS s FROM [{_ANY_TABLE}]"
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql(sql)
        assert exc_info.value.reason == "forbidden_statement"


# ---------------------------------------------------------------------------
# Negative controls -- ordinary analytic SQL must still pass
# ---------------------------------------------------------------------------


class TestNegativeControlsStillPass:
    @pytest.mark.parametrize("sql", [
        f"SELECT COUNT(*) FROM [{_ANY_TABLE}]",
        f"SELECT SUM(1) FROM [{_ANY_TABLE}]",
        f"SELECT AVG(1) FROM [{_ANY_TABLE}]",
        f"SELECT VAR(1) FROM [{_ANY_TABLE}]",
        f"SELECT STDEV(1) FROM [{_ANY_TABLE}]",
        f"SELECT CAST({_ANY_COLUMN} AS INT) FROM [{_ANY_TABLE}]",
        f"SELECT ISNULL({_ANY_COLUMN}, 0) FROM [{_ANY_TABLE}]",
        f"SELECT ROUND(1.5, 0) FROM [{_ANY_TABLE}]",
        f"SELECT YEAR(GETDATE()) FROM [{_ANY_TABLE}]",
        f"SELECT DATEADD(day, 1, GETDATE()) FROM [{_ANY_TABLE}]",
        f"SELECT GETDATE() FROM [{_ANY_TABLE}]",
        f"SELECT ROW_NUMBER() OVER (ORDER BY {_ANY_COLUMN}) FROM [{_ANY_TABLE}]",
        f"SELECT STRING_AGG({_ANY_COLUMN}, ',') FROM [{_ANY_TABLE}]",
        f"SELECT CASE WHEN {_ANY_COLUMN} IS NULL THEN 0 ELSE 1 END FROM [{_ANY_TABLE}]",
        f"WITH c AS (SELECT {_ANY_COLUMN} FROM [{_ANY_TABLE}]) SELECT * FROM c",
    ])
    def test_ordinary_analytic_sql_does_not_raise(self, sql):
        validate_sql(sql)  # must not raise


# ---------------------------------------------------------------------------
# Reason registration
# ---------------------------------------------------------------------------


class TestNoTableReferenceReasonIsRegistered:
    def test_registered_in_sql_guard_reasons(self):
        assert "no_table_reference" in sql_guard._REASONS

    def test_registered_in_guard_verdict_reason_literal(self):
        from session.models import GuardVerdict

        # A valid reason must construct cleanly; the closed-set validation
        # itself is already covered by tests/test_guard_error_contract.py
        # (TestGuardVerdictReasonSubject.test_reason_is_restricted_to_the_closed_set).
        v = GuardVerdict(verdict="rejected", reason="no_table_reference")
        assert v.reason == "no_table_reference"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
