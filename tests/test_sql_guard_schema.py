"""Schema-allowlist regression tests for security/sql_guard.py.

Phase 1 added a table/column allowlist to ``validate_sql`` (see its module
docstring), checked against ``schema_data.columns.TABLE_COLUMNS``. This
module has three jobs:

1. **Zero false positives on the real schema** — every one of the 12 tables
   and 80 columns actually defined in ``TABLE_COLUMNS`` must validate,
   referenced both unqualified and through a table alias. A false positive
   here means a query a human wrote against the real schema gets rejected
   by the guard for no reason — worse than under-enforcing the allowlist.
2. **The table allowlist actually rejects an unknown table** — a
   hallucinated, out-of-domain, or malicious table name must be refused
   even though the rest of the query looks like an innocuous ``SELECT``,
   and even though the database login is not yet scoped to just these
   tables (that is ``docs/db-hardening.md``, an operator action, not
   something this guard can depend on already being true). A column
   reference this module cannot resolve stays lenient (see
   ``TestUnresolvableColumnReferencesAreAllowed`` below) — it is
   specifically table names that are enforced strictly.
3. **The column allowlist and the denied_columns ACL seam actually deny**
   — a real, resolvable table with a column that is not one of its known
   columns must still be rejected, the optional ``denied_columns`` seam
   must actually deny, and ``*``/``alias.*`` must not be usable to read
   around an active column-denial policy.

Run::

    pytest tests/test_sql_guard_schema.py -v
"""

from __future__ import annotations

import pytest

from schema_data.columns import TABLE_COLUMNS
from security.sql_guard import validate_sql


# ---------------------------------------------------------------------------
# Zero false positives across the whole real schema
# ---------------------------------------------------------------------------

class TestRealSchemaTablesValidate:
    """Every table in TABLE_COLUMNS must validate on its own, unqualified."""

    @pytest.mark.parametrize("table", sorted(TABLE_COLUMNS))
    def test_select_star_from_real_table(self, table: str):
        validate_sql(f"SELECT TOP 10 * FROM [{table}]")  # must not raise


class TestRealSchemaColumnsValidate:
    """Every column in TABLE_COLUMNS must validate when referenced through
    an alias equal to its own table name — the qualified path that
    validate_sql actually resolves and checks against the schema."""

    _CASES = [
        (table, column)
        for table, columns in TABLE_COLUMNS.items()
        for column in columns
    ]

    @pytest.mark.parametrize("table,column", _CASES, ids=[f"{t}.{c}" for t, c in _CASES])
    def test_select_column_qualified_by_table_alias(self, table: str, column: str):
        sql = f"SELECT TOP 10 t.{column} FROM [{table}] AS t"
        validate_sql(sql)  # must not raise

    @pytest.mark.parametrize("table,column", _CASES, ids=[f"{t}.{c}" for t, c in _CASES])
    def test_select_column_qualified_by_table_name(self, table: str, column: str):
        """The same check, qualifying by the table's own name rather than
        an alias -- covers `_collect_table_alias_map`'s no-alias path."""
        sql = f"SELECT TOP 10 [{table}].{column} FROM [{table}]"
        validate_sql(sql)  # must not raise


class TestRealSchemaJoinsValidate:
    """A representative join across fact/dim tables, mirroring the shape of
    real generated SQL (see eval_data.example/golden.jsonl), must validate."""

    def test_contract_joined_to_symbol(self):
        sql = (
            "SELECT TOP 5 s.Commodity_PersianName, SUM(c.TotalPrice) AS TotalValue "
            "FROM [Contract] c JOIN [Symbol] s ON c.Symbol_ID = s.ID "
            "GROUP BY s.Commodity_PersianName ORDER BY TotalValue DESC"
        )
        validate_sql(sql)  # must not raise

    def test_order_reserved_word_table_with_date_join(self):
        """`Order` and `Date` are both T-SQL reserved words -- bracketed
        identifiers must still resolve to their TABLE_COLUMNS entries."""
        sql = (
            "SELECT COUNT(*) AS OrderCount FROM [Order] o "
            "JOIN [Date] d ON o.Date_ID = d.ID WHERE d.PersianYear = 1402"
        )
        validate_sql(sql)  # must not raise

    def test_cte_over_real_table_validates(self):
        sql = (
            "WITH active_customers AS (SELECT ID, Name FROM [Customer] WHERE IsActive = 1) "
            "SELECT TOP 10 Name FROM active_customers"
        )
        validate_sql(sql)  # must not raise


# ---------------------------------------------------------------------------
# The table allowlist rejects what it does not recognise -- strictly
# ---------------------------------------------------------------------------

class TestUnknownTableIsRejected:
    """A table reference that does not resolve to TABLE_COLUMNS (and is not
    a CTE defined earlier in the same query) must be refused, regardless of
    how innocuous the rest of the query looks. This does not depend on the
    database login already being scoped to just the known tables (see
    docs/db-hardening.md) -- the guard must do its job on its own."""

    def test_totally_made_up_table_rejected(self):
        with pytest.raises(ValueError, match="unknown table"):
            validate_sql("SELECT * FROM TotallyMadeUpTable")

    def test_schema_qualified_unknown_table_rejected(self):
        """The schema qualifier does not grant legitimacy -- only the
        table part is resolved against TABLE_COLUMNS, and it still has to
        actually be one of the 12 known tables."""
        with pytest.raises(ValueError, match="unknown table"):
            validate_sql("SELECT * FROM [Evil].[Secrets]")

    def test_out_of_domain_table_rejected(self):
        """The scenario that actually matters: an LLM hallucinating a
        table, or a prompt-injected question, reaching data outside the
        Auction domain -- not just a syntactically-suspicious name."""
        with pytest.raises(ValueError, match="unknown table"):
            validate_sql("SELECT Salary FROM HR_Payroll")

    def test_unknown_table_rejection_is_classified_as_forbidden(self):
        """Unlike an unknown *column* (a semantic/typo error -- see
        TestUnknownColumnOnKnownTableIsRejected below), an unknown *table*
        is treated as a security-relevant rejection: the message contains
        'Forbidden keyword' so api/runner.py routes it to
        ForbiddenSQLError rather than InvalidSQLResponseError."""
        with pytest.raises(ValueError, match="Forbidden keyword") as exc_info:
            validate_sql("SELECT * FROM TotallyMadeUpTable")
        assert "unknown table" in str(exc_info.value)

    def test_unknown_table_in_join_rejected(self):
        """The allowlist is enforced per-table, not just for the first
        table in the query -- a known table joined to an unknown one is
        still refused."""
        with pytest.raises(ValueError, match="unknown table"):
            validate_sql(
                "SELECT c.ID FROM [Contract] c JOIN HR_Payroll h ON c.ID = h.ContractID"
            )

    def test_cte_reference_is_not_treated_as_an_unknown_table(self):
        """A CTE is not a real table and must not be checked against
        TABLE_COLUMNS -- this is the one case where a name unresolvable
        against the schema is still allowed, because it isn't a table
        reference at all."""
        sql = "WITH totally_made_up AS (SELECT 1 AS n) SELECT * FROM totally_made_up"
        validate_sql(sql)  # must not raise


class TestUnknownColumnOnKnownTableIsRejected:
    """A column that is not one of a *resolved* real table's known columns
    must still be rejected -- otherwise the allowlist checks nothing."""

    def test_typo_column_on_real_table_rejected(self):
        with pytest.raises(ValueError, match="Unknown column"):
            validate_sql("SELECT c.NotARealColumn FROM [Contract] c")

    def test_error_message_is_not_classified_as_a_security_block(self):
        """An unknown-column typo is a semantic error, not an attack -- its
        message must not contain 'Forbidden keyword' (api/runner.py uses
        that substring to route security rejections to ForbiddenSQLError
        rather than InvalidSQLResponseError)."""
        with pytest.raises(ValueError) as exc_info:
            validate_sql("SELECT c.NotARealColumn FROM [Contract] c")
        assert "Forbidden keyword" not in str(exc_info.value)


class TestUnresolvableColumnReferencesAreAllowed:
    """Column references this module cannot resolve with confidence are
    allowed, not rejected -- avoiding a false positive is prioritised over
    catching every possible hallucinated reference (see the module
    docstring's discussion of this tradeoff). This leniency is specific to
    *columns*; the *table* each of these queries references is always a
    real, resolvable one (or a CTE) -- see TestUnknownTableIsRejected above
    for why an unresolvable table gets the opposite treatment."""

    def test_derived_table_alias_column_allowed(self):
        sql = "SELECT z.TotalPrice FROM (SELECT TotalPrice FROM [Contract]) z"
        validate_sql(sql)  # must not raise

    def test_cte_alias_qualified_column_allowed(self):
        sql = "WITH cte AS (SELECT ID AS n FROM [Customer]) SELECT cte.n FROM cte"
        validate_sql(sql)  # must not raise

    def test_unqualified_column_on_real_table_allowed_even_if_misspelled(self):
        """A *bare* column is never checked against the schema at all
        (unlike a qualified one -- see TestUnknownColumnOnKnownTableIsRejected),
        regardless of whether it turns out to be real. This is the
        pre-existing column leniency; it is unaffected by the table
        allowlist becoming strict."""
        validate_sql("SELECT ThisColumnDoesNotExist FROM [Contract]")  # must not raise


# ---------------------------------------------------------------------------
# Comments: refused outright, never keyword-scanned
# ---------------------------------------------------------------------------

class TestCommentsAreRefused:
    """A comment is refused because it is present, not because its content
    was scanned for a keyword -- scanning comment text would reproduce the
    exact false-positive class (substring matching) Phase 1 replaced. See
    tests/test_sql_guard_bypass.py::TestTrailingTokenBypass for the
    original bypass case this generalises (a forbidden keyword hidden in a
    trailing comment)."""

    def test_line_comment_refused_even_when_harmless(self):
        """The comment says nothing dangerous -- it is refused anyway,
        because it is a comment at all, not because of what it says."""
        with pytest.raises(ValueError, match="comment"):
            validate_sql("SELECT Price FROM Contract -- just a note to self")

    def test_block_comment_refused(self):
        with pytest.raises(ValueError, match="comment"):
            validate_sql("SELECT Price /* internal */ FROM Contract")

    def test_leading_comment_refused(self):
        with pytest.raises(ValueError, match="comment"):
            validate_sql("-- generated by the model\nSELECT Price FROM Contract")

    def test_comment_rejection_message_does_not_claim_a_forbidden_keyword(self):
        """The message must say a comment was refused, not that a keyword
        was found -- no keyword-scanning happened, so claiming one would
        be misleading (and would misroute through api/runner.py's
        'Forbidden keyword' substring classifier as a keyword match)."""
        with pytest.raises(ValueError) as exc_info:
            validate_sql("SELECT Price FROM Contract -- drop nothing, just a note")
        message = str(exc_info.value)
        assert "comment" in message.lower()
        assert "Forbidden keyword" not in message


# ---------------------------------------------------------------------------
# denied_columns ACL seam
# ---------------------------------------------------------------------------

class TestDeniedColumnsSeam:
    def test_denied_column_rejected(self):
        with pytest.raises(ValueError, match="Forbidden keyword"):
            validate_sql(
                "SELECT NationalID FROM [Customer]",
                denied_columns={"NationalID"},
            )

    def test_denied_column_check_is_case_insensitive(self):
        with pytest.raises(ValueError, match="Forbidden keyword"):
            validate_sql(
                "SELECT nationalcode FROM [Customer]",
                denied_columns={"NATIONALCODE"},
            )

    def test_denied_column_rejected_even_when_qualified(self):
        with pytest.raises(ValueError, match="Forbidden keyword"):
            validate_sql(
                "SELECT c.NationalID FROM [Customer] c",
                denied_columns={"NationalID"},
            )

    def test_column_not_in_denylist_is_allowed(self):
        validate_sql(
            "SELECT Name FROM [Customer]",
            denied_columns={"NationalID"},
        )  # must not raise

    def test_no_denylist_means_no_denial(self):
        validate_sql("SELECT NationalID FROM [Customer]")  # must not raise


class TestStarCannotBypassDeniedColumns:
    """`SELECT *` must not be a loophole around an active column policy --
    it is expanded against whatever it resolves to and checked
    column-by-column, or refused outright when it can't be resolved with
    confidence."""

    def test_bare_star_expanded_and_rejected_when_it_would_expose_denied_column(self):
        with pytest.raises(ValueError, match="Forbidden keyword"):
            validate_sql(
                "SELECT * FROM [Customer]",
                denied_columns={"NationalID"},
            )

    def test_qualified_star_expanded_and_rejected(self):
        with pytest.raises(ValueError, match="Forbidden keyword"):
            validate_sql(
                "SELECT c.* FROM [Customer] c",
                denied_columns={"NationalID"},
            )

    def test_bare_star_allowed_when_expansion_has_no_denied_column(self):
        """Customer has no column named 'DoesNotExist' -- the expansion
        contains no denied column, so this must pass."""
        validate_sql(
            "SELECT * FROM [Customer]",
            denied_columns={"DoesNotExist"},
        )  # must not raise

    def test_star_over_join_expanded_across_every_table(self):
        """A bare '*' with multiple tables in scope expands to the union
        of all of their columns -- a denied column on *either* table must
        still be caught."""
        with pytest.raises(ValueError, match="Forbidden keyword"):
            validate_sql(
                "SELECT * FROM [Contract] c JOIN [Symbol] s ON c.Symbol_ID = s.ID",
                denied_columns={"Commodity_PersianName"},
            )

    def test_star_over_derived_table_refused_when_denylist_active(self):
        """A '*' whose FROM source is a subquery (not a plain table
        reference) cannot be expanded with confidence -- it is refused
        outright rather than silently allowed through."""
        with pytest.raises(ValueError, match="Forbidden keyword"):
            validate_sql(
                "SELECT * FROM (SELECT NationalID FROM [Customer]) z",
                denied_columns={"SomeOtherColumn"},
            )

    def test_qualified_star_on_unresolvable_alias_refused(self):
        """'z.*' where 'z' is a derived-table alias (not a real table)
        cannot be expanded with confidence either -- same refusal as the
        bare-'*'-over-a-subquery case above, just via the qualified form."""
        with pytest.raises(ValueError, match="Forbidden keyword"):
            validate_sql(
                "SELECT z.* FROM (SELECT NationalID FROM [Customer]) z",
                denied_columns={"SomeOtherColumn"},
            )

    def test_star_without_denylist_is_unaffected(self):
        """No denied_columns policy active -- '*' behaves exactly as
        before, no expansion attempted at all."""
        validate_sql("SELECT * FROM [Customer]")  # must not raise
