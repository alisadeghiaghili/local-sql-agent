# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Multi-dialect coverage for security/sql_guard.py and security/dialects.py.

Two things this file is NOT
----------------------------
It does not re-verify anything for ``tsql`` -- ``tests/test_sql_guard.py``
and ``tests/test_sql_guard_bypass.py`` already do that, unchanged, and
this phase's report records that they still pass byte-for-byte identically
(see that report's "tsql guard allowlist unchanged" section). Duplicating
those assertions here with ``dialect="tsql"`` would just be the same test
twice.

It does not prove postgres or mysql work end-to-end -- no server for
either was available while this phase was written (see the phase report).
What it DOES prove for those two: the guard's rejection mechanisms
(stacked statements, forbidden statement types, comments, system
catalogues, denied columns, the N'...' / '+' -concatenation gaps this
phase found) hold when :func:`~security.sql_guard.validate_sql` is pinned
to that dialect, and that :func:`~security.sql_guard.transpile_and_revalidate`
does not silently pass through something it shouldn't. SQLite gets the
same coverage here PLUS real execution in
``tests/integration/test_sqlite_multi_dialect_live.py`` -- this file's
SQLite cases are validation-only, that one is "run it for real".

Bypass suite, parametrised over every supported dialect
-------------------------------------------------------------
``tests/test_sql_guard_bypass.py``'s own docstring explains why this
matters: "The 13 bypasses this project closed were found by execution,
not by reading. ... Other dialects have their own quirks. A guard proven
for tsql and assumed for postgres has unknown holes." The cases below
are the dialect-agnostic *mechanisms* from that file (stacked statements,
un-space-separated keywords, comments, string-literal/substring false
positives, forbidden statement types, denied columns) re-run with
``validate_sql(..., dialect=d)`` for every ``d`` in ``postgres``,
``mysql``, ``sqlite`` (tsql is covered by the original file). A handful of
tsql-specific attack shapes (``WAITFOR DELAY``, ``OPENROWSET``) are not
reproduced per-dialect here because the mechanism that closes them
(stacked-statement rejection, or the dialect-agnostic
``_DANGEROUS_FUNCTION_NAMES``/``xp_*``/``sp_*`` check) is already
exercised by the cases that ARE parametrised -- see each class's
docstring for which original bypass family it covers.
"""

from __future__ import annotations

import pytest

from schema_data.columns import TABLE_COLUMNS
from security.dialects import DIALECT_PROFILES, get_dialect_profile
from security.sql_guard import (
    CorrectableRejection,
    PolicyRejection,
    _is_string_literal_operand,
    ensure_top,
    extract_touched_tables,
    transpile_and_revalidate,
    transpile_sql,
    validate_sql,
)

#: A table name drawn from whichever schema is loaded (real project_config/
#: or the committed project_config.example/) -- mirrors
#: tests/test_sql_guard_bypass.py's own ``_ANY_KNOWN_TABLE``, for exactly
#: the same reason: these cases are about the SQL shape, not about
#: depending on a specific warehouse's table names existing.
_ANY_KNOWN_TABLE = next(iter(TABLE_COLUMNS))

#: The three dialects this phase newly adds, i.e. everything besides tsql
#: (already covered elsewhere -- see module docstring).
NEW_DIALECTS = ("postgres", "mysql", "sqlite")

ALL_DIALECTS = ("tsql", "postgres", "mysql", "sqlite")


# ---------------------------------------------------------------------------
# security.dialects itself
# ---------------------------------------------------------------------------

class TestDialectProfiles:
    def test_all_four_dialects_are_registered(self):
        assert set(DIALECT_PROFILES) == {"tsql", "postgres", "mysql", "sqlite"}

    @pytest.mark.parametrize("dialect", ALL_DIALECTS)
    def test_every_profile_has_a_non_empty_system_schema_blocklist(self, dialect):
        """A dialect with no catalogue list must be refused at start-up
        (see require_dialect_supported) -- this asserts the precondition
        for that check holds for every dialect actually shipped."""
        profile = get_dialect_profile(dialect)
        assert profile.system_schemas, (
            f"{dialect}: empty system_schemas is indistinguishable from "
            "'nothing to block' -- see DialectProfile's docstring"
        )

    @pytest.mark.parametrize("dialect", ALL_DIALECTS)
    def test_supports_national_literal_is_explicitly_set(self, dialect):
        """Every profile must have an opinion -- see the field's own
        docstring for why there is deliberately no default."""
        profile = get_dialect_profile(dialect)
        assert isinstance(profile.supports_national_literal, bool)

    def test_sqlite_has_no_session_timeout_mechanism(self):
        """A real, accepted limitation (see require_dialect_supported's
        docstring), not a bug -- asserted explicitly so a future edit that
        accidentally gives sqlite a fake statement is caught."""
        assert DIALECT_PROFILES["sqlite"].session_timeout_statement is None

    def test_sqlite_has_no_schema_qualification(self):
        assert DIALECT_PROFILES["sqlite"].schema_qualification == "none"

    def test_mysql_schema_qualification_is_database_not_schema(self):
        assert DIALECT_PROFILES["mysql"].schema_qualification == "database"

    def test_require_dialect_supported_rejects_unknown_dialect(self):
        from security.dialects import UnsupportedDialectError, require_dialect_supported

        with pytest.raises(UnsupportedDialectError):
            require_dialect_supported("oracle")

    def test_require_dialect_supported_rejects_empty_catalogue_list(self):
        """A dialect registered with an empty system_schemas must be
        refused at start-up -- the failure direction that loses is
        silently treating 'forgot to configure' as 'nothing to block'."""
        from dataclasses import replace

        from security.dialects import UnsupportedDialectError, require_dialect_supported

        broken = replace(DIALECT_PROFILES["postgres"], system_schemas=frozenset())
        with pytest.raises(UnsupportedDialectError):
            with pytest.MonkeyPatch.context() as mp:
                mp.setitem(DIALECT_PROFILES, "postgres", broken)
                require_dialect_supported("postgres")


# ---------------------------------------------------------------------------
# Attack family 1 & 3: separator/trailing-token bypass of a keyword scan.
# Generalizes trivially: DROP/DELETE parse identically in every dialect's
# grammar, so the guard's node-type check (not a substring scan) rejects
# them the same way regardless of dialect.
# ---------------------------------------------------------------------------

class TestNonSpaceSeparatorBypassPerDialect:
    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_drop_followed_by_newline_is_not_blocked(self, dialect):
        sql = "SELECT 1;DROP\nTABLE t"
        with pytest.raises(ValueError):
            validate_sql(sql, dialect=dialect)

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_delete_followed_by_tab_is_not_blocked(self, dialect):
        sql = "SELECT 1; DELETE\tFROM t"
        with pytest.raises(ValueError):
            validate_sql(sql, dialect=dialect)

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_forbidden_keyword_as_final_token_is_not_blocked(self, dialect):
        sql = f"SELECT a FROM {_ANY_KNOWN_TABLE} WHERE x=1 -- DROP"
        with pytest.raises(ValueError):
            validate_sql(sql, dialect=dialect)


# ---------------------------------------------------------------------------
# Attack family 2: write/DDL operations, and remote/file access.
# ---------------------------------------------------------------------------

class TestUnlistedWriteOperationsPerDialect:
    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_select_into_creates_a_table(self, dialect):
        """SELECT ... INTO parses to the same exp.Into node in every
        dialect sqlglot supports (verified directly), so this bypass
        closes identically regardless of target dialect."""
        sql = f"SELECT * INTO newtbl FROM {_ANY_KNOWN_TABLE}"
        with pytest.raises(PolicyRejection):
            validate_sql(sql, dialect=dialect)

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_create_table_not_blocked(self, dialect):
        sql = "SELECT 1; CREATE TABLE x (a int)"
        with pytest.raises(ValueError):
            validate_sql(sql, dialect=dialect)

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_grant_not_blocked(self, dialect):
        sql = "SELECT 1; GRANT CONTROL TO public"
        with pytest.raises(ValueError):
            validate_sql(sql, dialect=dialect)


class TestPerDialectRemoteAccessFunctions:
    """DialectProfile.extra_dangerous_functions -- config-keyed per
    dialect, not a branch -- closes each dialect's own remote/file-access
    vector the same way the universal OPENROWSET/OPENQUERY/OPENDATASOURCE
    list closes tsql's."""

    def test_postgres_dblink_is_forbidden(self):
        sql = "SELECT * FROM dblink('conn', 'select 1') AS t(a int)"
        with pytest.raises(PolicyRejection):
            validate_sql(sql, dialect="postgres")

    def test_mysql_load_file_is_forbidden(self):
        sql = f"SELECT LOAD_FILE('/etc/passwd') FROM {_ANY_KNOWN_TABLE}"
        with pytest.raises(PolicyRejection):
            validate_sql(sql, dialect="mysql")


# ---------------------------------------------------------------------------
# Attack family 4: stacked statements are not rejected as a class.
# ---------------------------------------------------------------------------

class TestStackedStatementsPerDialect:
    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_stacked_benign_looking_statement_is_not_rejected_outright(self, dialect):
        sql = "SELECT 1; SELECT 2"
        with pytest.raises(PolicyRejection):
            validate_sql(sql, dialect=dialect)


# ---------------------------------------------------------------------------
# Attack family 5: false positives -- legitimate SQL must NOT be rejected.
# ---------------------------------------------------------------------------

class TestSubstringFalsePositivesPerDialect:
    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_exp_date_column_is_not_rejected(self, dialect):
        validate_sql(f"SELECT EXP_DATE FROM {_ANY_KNOWN_TABLE}", dialect=dialect)

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_resp_code_column_is_not_rejected(self, dialect):
        validate_sql(f"SELECT RESP_CODE FROM {_ANY_KNOWN_TABLE}", dialect=dialect)


class TestKeywordInsideStringLiteralPerDialect:
    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_drop_inside_string_literal_is_not_a_drop_statement(self, dialect):
        sql = f"SELECT * FROM {_ANY_KNOWN_TABLE} WHERE Note = 'please DROP the box'"
        validate_sql(sql, dialect=dialect)


# ---------------------------------------------------------------------------
# Attack family 6: denied-column policy must hold per dialect too.
# ---------------------------------------------------------------------------

#: A column that actually belongs to _ANY_KNOWN_TABLE -- the star-expansion
#: case below must resolve the table's REAL columns to prove anything, so
#: (unlike the by-name check, which fires regardless of whether the named
#: column really exists on the table) this cannot be a hardcoded guess.
_A_REAL_COLUMN_OF_ANY_KNOWN_TABLE = next(iter(TABLE_COLUMNS[_ANY_KNOWN_TABLE]))


class TestDeniedColumnsPerDialect:
    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_denied_column_forbidden(self, dialect):
        with pytest.raises(PolicyRejection):
            validate_sql(
                f"SELECT {_A_REAL_COLUMN_OF_ANY_KNOWN_TABLE} FROM {_ANY_KNOWN_TABLE}",
                dialect=dialect, denied_columns={_A_REAL_COLUMN_OF_ANY_KNOWN_TABLE},
            )

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_star_cannot_read_around_denied_column(self, dialect):
        with pytest.raises(PolicyRejection):
            validate_sql(
                f"SELECT * FROM {_ANY_KNOWN_TABLE}",
                dialect=dialect, denied_columns={_A_REAL_COLUMN_OF_ANY_KNOWN_TABLE},
            )


# ---------------------------------------------------------------------------
# Attack family 7: system catalogues, per dialect's own names.
# ---------------------------------------------------------------------------

class TestSystemCataloguePerDialect:
    def test_postgres_pg_catalog_is_forbidden(self):
        with pytest.raises(PolicyRejection):
            validate_sql("SELECT * FROM pg_catalog.pg_tables", dialect="postgres")

    def test_postgres_information_schema_is_forbidden(self):
        with pytest.raises(PolicyRejection):
            validate_sql("SELECT * FROM information_schema.tables", dialect="postgres")

    def test_postgres_bare_pg_prefixed_name_is_forbidden(self):
        """A pg_* table with no schema qualifier at all -- the prefix
        check (DialectProfile.system_name_prefixes), not just the
        exact-match schema check, must catch this."""
        with pytest.raises(PolicyRejection):
            validate_sql("SELECT * FROM pg_tables", dialect="postgres")

    def test_mysql_information_schema_is_forbidden(self):
        with pytest.raises(PolicyRejection):
            validate_sql("SELECT * FROM information_schema.tables", dialect="mysql")

    def test_mysql_performance_schema_is_forbidden(self):
        with pytest.raises(PolicyRejection):
            validate_sql("SELECT * FROM performance_schema.threads", dialect="mysql")

    def test_sqlite_master_is_forbidden(self):
        with pytest.raises(PolicyRejection):
            validate_sql("SELECT * FROM sqlite_master", dialect="sqlite")

    def test_sqlite_prefixed_name_is_forbidden(self):
        with pytest.raises(PolicyRejection):
            validate_sql("SELECT * FROM sqlite_sequence", dialect="sqlite")


# ---------------------------------------------------------------------------
# LIMIT: valid syntax for every dialect except tsql -- the guard's one
# narrowly-scoped `if dialect == "tsql":` (see validate_sql's docstring).
# ---------------------------------------------------------------------------

class TestLimitIsValidExceptTsql:
    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_limit_is_accepted_for_every_non_tsql_dialect(self, dialect):
        validate_sql(f"SELECT * FROM {_ANY_KNOWN_TABLE} LIMIT 10", dialect=dialect)

    def test_limit_is_still_rejected_for_tsql(self):
        with pytest.raises(CorrectableRejection):
            validate_sql(f"SELECT * FROM {_ANY_KNOWN_TABLE} LIMIT 10", dialect="tsql")


# ---------------------------------------------------------------------------
# This phase's own two findings: N'...' and `+` concatenation.
# ---------------------------------------------------------------------------

class TestNationalLiteralHandling:
    def test_tsql_accepts_national_literal(self):
        """Native, correct T-SQL -- must never be refused for tsql itself."""
        validate_sql(f"SELECT * FROM {_ANY_KNOWN_TABLE} WHERE x = N'abc'", dialect="tsql")

    def test_mysql_accepts_national_literal(self):
        """MySQL genuinely supports N'...' as a synonym for _utf8'...'."""
        validate_sql(f"SELECT * FROM {_ANY_KNOWN_TABLE} WHERE x = N'abc'", dialect="mysql")

    @pytest.mark.parametrize("dialect", ("postgres", "sqlite"))
    def test_postgres_and_sqlite_reject_national_literal_explicitly(self, dialect):
        """The guard's own explicit backstop (validate_sql's National
        check) -- this is what fires if a National literal somehow
        reaches validate_sql for a dialect that cannot parse it for real,
        rather than silently letting it through to fail (or worse,
        silently misbehave) at the database."""
        with pytest.raises(CorrectableRejection):
            validate_sql(f"SELECT * FROM {_ANY_KNOWN_TABLE} WHERE x = N'abc'", dialect=dialect)

    @pytest.mark.parametrize("dialect", ("postgres", "sqlite"))
    def test_transpile_sql_strips_national_prefix_for_unsupported_dialect(self, dialect):
        """The actual fix: transpile_sql rewrites N'...' to a plain
        string literal for a dialect that cannot parse the former --
        proven by checking the exact rendered text, not just that it
        doesn't raise."""
        out = transpile_sql(
            f"SELECT * FROM {_ANY_KNOWN_TABLE} WHERE x = N'abc'",
            target_dialect=dialect,
        )
        assert "N'" not in out
        assert "'abc'" in out

    def test_transpile_sql_leaves_national_prefix_for_mysql(self):
        out = transpile_sql(
            f"SELECT * FROM {_ANY_KNOWN_TABLE} WHERE x = N'abc'",
            target_dialect="mysql",
        )
        assert "N'abc'" in out

    @pytest.mark.parametrize("dialect", ("postgres", "sqlite"))
    def test_transpile_and_revalidate_succeeds_with_national_literal(self, dialect):
        """End-to-end (minus real execution -- see the live SQLite
        integration test for that): a tsql query with a national literal
        must survive transpile_and_revalidate for postgres/sqlite, not be
        refused, since the rewrite makes it valid on both."""
        sql = f"SELECT TOP 10 * FROM {_ANY_KNOWN_TABLE} WHERE x = N'فولاد'"
        result = transpile_and_revalidate(sql, target_dialect=dialect)
        assert "N'" not in result


class TestPlusConcatenationGap:
    """T-SQL `+` string concatenation transpiles UNCHANGED to every other
    dialect, where `+` is exclusively numeric addition -- found by direct
    execution against SQLite (`'foo' + 'bar'` evaluates to `0`, not an
    error). See tests/integration/test_sqlite_multi_dialect_live.py for
    the live-execution proof; this module-level test covers the guard
    rule itself, parametrised over every affected dialect."""

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_string_literal_plus_is_refused(self, dialect):
        sql = f"SELECT Name + ' Co' AS Label FROM {_ANY_KNOWN_TABLE}"
        with pytest.raises(CorrectableRejection):
            validate_sql(sql, dialect=dialect)

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_national_literal_plus_is_also_refused(self, dialect):
        """The N'...' operand must be recognised as a string too (it
        parses as exp.National, a distinct node type from exp.Literal --
        see _is_string_literal_operand's docstring)."""
        sql = f"SELECT Name + N' Co' AS Label FROM {_ANY_KNOWN_TABLE}"
        with pytest.raises(CorrectableRejection):
            validate_sql(sql, dialect=dialect)

    def test_plus_is_never_refused_for_tsql(self):
        """This is correct, native T-SQL string concatenation -- must
        never be refused when validating tsql itself."""
        validate_sql(f"SELECT Name + ' Co' AS Label FROM {_ANY_KNOWN_TABLE}", dialect="tsql")

    def test_numeric_addition_between_columns_is_not_refused(self):
        """The guard must not become so aggressive it breaks ordinary
        numeric addition -- only a string-literal operand triggers the
        rule (see validate_sql's own comment for why column+column can't
        be safely disambiguated without column-type metadata)."""
        # A column-only addition (no literal operand) must be allowed
        # unconditionally, tsql or not -- this is the deliberately
        # un-caught residual gap the phase report documents.
        for dialect in ("tsql", *NEW_DIALECTS):
            validate_sql(f"SELECT a + b AS Total FROM {_ANY_KNOWN_TABLE}", dialect=dialect)

    def test_is_string_literal_operand_recognises_both_node_types(self):
        import sqlglot
        from sqlglot import exp

        lit = sqlglot.parse_one("'x'", read="tsql")
        national = sqlglot.parse_one("N'x'", read="tsql")
        assert _is_string_literal_operand(lit)
        assert _is_string_literal_operand(national)
        assert not _is_string_literal_operand(exp.column("a"))


# ---------------------------------------------------------------------------
# ensure_top: AST path for non-tsql dialects (tsql path is covered,
# unchanged, by tests/test_sql_guard.py::TestEnsureTop).
# ---------------------------------------------------------------------------

class TestEnsureTopNonTsqlDialects:
    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_injects_limit_when_missing(self, dialect):
        result = ensure_top("SELECT a FROM t", n=25, dialect=dialect)
        assert "25" in result
        assert "a" in result

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_leaves_existing_limit_untouched(self, dialect):
        sql = "SELECT a FROM t LIMIT 5"
        result = ensure_top(sql, n=50, dialect=dialect)
        assert result == sql  # byte-identical, not just semantically equal

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_caps_outer_query_of_a_union(self, dialect):
        result = ensure_top("SELECT a FROM t1 UNION SELECT b FROM t2", n=5, dialect=dialect)
        assert "_ensure_top_capped" in result
        assert result.count("UNION") == 1

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_raises_for_union_with_top_level_order_by(self, dialect):
        sql = "SELECT a FROM t1 UNION SELECT b FROM t2 ORDER BY a"
        with pytest.raises(CorrectableRejection):
            ensure_top(sql, n=5, dialect=dialect)

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_raises_for_unparsable_input(self, dialect):
        with pytest.raises(CorrectableRejection):
            ensure_top("not sql at all", n=5, dialect=dialect)


# ---------------------------------------------------------------------------
# transpile_and_revalidate: the pipeline's own no-op-for-tsql contract,
# and its refusal behaviour for the other dialects.
# ---------------------------------------------------------------------------

class TestTranspileAndRevalidate:
    def test_tsql_target_is_a_true_passthrough(self):
        """When target_dialect == source_dialect ("tsql", this
        deployment's default), the function must return the EXACT same
        string object's value unchanged -- no parse, no render -- this is
        the guarantee that makes a tsql-only deployment see zero
        behavioural change from this function existing at all."""
        sql = f"SELECT TOP 10 * FROM {_ANY_KNOWN_TABLE} WHERE x = N'test value'"
        assert transpile_and_revalidate(sql, target_dialect="tsql") == sql

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_forbidden_construct_is_still_refused_after_transpiling(self, dialect):
        """A PolicyRejection in the source tsql must still surface (via
        clean SQL that transpiles fine but references a denied column)."""
        with pytest.raises(PolicyRejection):
            transpile_and_revalidate(
                f"SELECT Name FROM {_ANY_KNOWN_TABLE}",
                target_dialect=dialect, denied_columns={"Name"},
            )

    @pytest.mark.parametrize("dialect", NEW_DIALECTS)
    def test_touched_tables_are_identical_across_transpilation(self, dialect):
        sql = f"SELECT TOP 5 * FROM {_ANY_KNOWN_TABLE}"
        result = transpile_and_revalidate(sql, target_dialect=dialect)
        before = set(extract_touched_tables(sql, dialect="tsql"))
        after = set(extract_touched_tables(result, dialect=dialect))
        assert before == after
        assert before  # sanity: the known table actually resolved

    def test_unparsable_source_raises_correctable(self):
        with pytest.raises(CorrectableRejection):
            transpile_and_revalidate("not sql at all", target_dialect="postgres")
