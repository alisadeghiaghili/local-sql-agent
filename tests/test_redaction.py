# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for core/redaction.py, the one connection-string redactor.

The schema inspector used to mask passwords with ``re.sub(r":[^:@/]+@", ...)``,
which leaves part of a password containing ``@`` or ``:`` visible and never
looks at ``PWD=`` inside an ``odbc_connect`` query value. Every place that
shows or writes a connection string now goes through
:func:`core.redaction.redact_db_url`.
"""

from __future__ import annotations

import pytest

from appdb.engine import redact_url
from core.redaction import MASK, UNPARSEABLE_URL, redact_db_url, scrub_secrets, url_secrets
from database.datasources import DataSource
from database.schema_inspector import SchemaInspector

SECRET = "Zx9-hunter2"


class TestRedactDbUrl:
    def test_masks_a_plain_password_and_keeps_the_target(self):
        redacted = redact_db_url(f"mssql+pyodbc://nlq:{SECRET}@db1.example.test/Sales")
        assert redacted == f"mssql+pyodbc://nlq:{MASK}@db1.example.test/Sales"

    @pytest.mark.parametrize(
        "password",
        ["p@ss", "p:ss", "p/ss", "p%40ss", "a@b@c", "pa:ss@wo/rd", "a b"],
    )
    def test_masks_passwords_containing_url_delimiters(self, password):
        # Exact output: no fragment of the password may survive, whichever
        # way a SQLAlchemy release splits an unescaped "@" or "/".
        redacted = redact_db_url(f"postgresql+psycopg2://u:{password}@h:5432/d")
        assert redacted == f"postgresql+psycopg2://u:{MASK}@h:5432/d"

    def test_masks_pwd_inside_odbc_connect(self):
        url = (
            "mssql+pyodbc:///?odbc_connect="
            f"DRIVER={{ODBC Driver 18}};SERVER=db1;UID=nlq;PWD={SECRET};Encrypt=yes"
        )
        redacted = redact_db_url(url)
        assert SECRET not in redacted
        # SQLAlchemy re-encodes the odbc_connect value when it renders the URL.
        assert "SERVER%3Ddb1" in redacted  # the target survives
        assert "PWD%3D%2A%2A%2A" in redacted  # PWD=***

    def test_masks_password_in_a_url_encoded_odbc_connect(self):
        url = f"mssql+pyodbc:///?odbc_connect=UID%3Dnlq%3BPWD%3D{SECRET}%3BEncrypt%3Dyes"
        assert SECRET not in redact_db_url(url)

    @pytest.mark.parametrize("key", ["PWD", "password", "Passwd", "token", "api_key"])
    def test_masks_secret_query_parameters_case_insensitively(self, key):
        redacted = redact_db_url(f"mssql+pyodbc://u@h/d?driver=X&{key}={SECRET}")
        assert SECRET not in redacted
        assert "driver=X" in redacted

    def test_a_url_without_credentials_is_unchanged(self):
        assert redact_db_url("sqlite:///data/x.db") == "sqlite:///data/x.db"

    @pytest.mark.parametrize("text", ["", "   ", "not a url", "hunter2"])
    def test_a_string_that_is_not_a_url_becomes_a_placeholder(self, text):
        assert redact_db_url(text) == UNPARSEABLE_URL

    def test_an_unparseable_url_shaped_string_is_masked_by_pattern(self):
        redacted = redact_db_url(f"weird://u:{SECRET} x@h")
        assert SECRET not in redacted

    def test_unparseable_input_with_pwd_is_masked(self):
        redacted = redact_db_url(f"weird://[bad host];PWD={SECRET};x=1")
        assert SECRET not in redacted

    @pytest.mark.parametrize("value", [None, 5, b"mssql://u:p@h/d", ["x"]])
    def test_non_string_input_never_raises_and_is_never_echoed(self, value):
        assert redact_db_url(value) == UNPARSEABLE_URL  # type: ignore[arg-type]

    def test_is_idempotent(self):
        once = redact_db_url(f"mssql+pyodbc://nlq:{SECRET}@db1/Sales")
        assert redact_db_url(once) == once


class TestUrlSecrets:
    def test_lists_the_password_and_its_encoded_spellings_longest_first(self):
        found = url_secrets("postgresql://u:p%40ss@h/d")
        assert "p@ss" in found
        assert "p%40ss" in found
        assert found == sorted(found, key=len, reverse=True)

    def test_includes_odbc_pwd(self):
        assert SECRET in url_secrets(f"mssql+pyodbc:///?odbc_connect=UID=a;PWD={SECRET}")

    def test_no_credentials_means_no_secrets(self):
        assert url_secrets("sqlite:///x.db") == []


class TestScrubSecrets:
    def test_removes_the_url_and_any_echoed_credential(self):
        url = f"mssql+pyodbc://nlq:{SECRET}@db1/Sales"
        scrubbed = scrub_secrets(f"cannot open {url}; login failed (pw {SECRET})", url)
        assert SECRET not in scrubbed
        assert "db1/Sales" in scrubbed

    def test_with_no_url_the_text_is_unchanged(self):
        assert scrub_secrets("nothing to hide", "") == "nothing to hide"


class TestEveryCallerUsesTheSharedRedactor:
    """The weak copies are gone: each surface masks what the helper masks."""

    ODBC_URL = f"mssql+pyodbc:///?odbc_connect=SERVER=db1;UID=nlq;PWD={SECRET}"
    AT_URL = f"mssql+pyodbc://nlq:p@{SECRET}@db1/Sales"

    @pytest.mark.parametrize("url", [ODBC_URL, AT_URL])
    def test_schema_inspector(self, url):
        masked = SchemaInspector._redact_url(url)
        assert SECRET not in masked
        assert masked == redact_db_url(url)

    @pytest.mark.parametrize("url", [ODBC_URL, AT_URL])
    def test_application_database_messages(self, url):
        assert SECRET not in redact_url(url)

    @pytest.mark.parametrize("url", [ODBC_URL, AT_URL])
    def test_data_source_redacted_url(self, url):
        assert SECRET not in DataSource("m", url, "DB_URL_MAIN", "tsql", "app").redacted_url

    def test_the_setup_wizard_keeps_its_historical_names(self):
        import setup_project as sp

        assert sp._redact_db_url is redact_db_url
        assert sp._scrub_secrets is scrub_secrets

    def test_schema_inspector_snapshot_records_no_password(self):
        # The snapshot's source_url is what gets written to disk / shown.
        assert SECRET not in SchemaInspector._redact_url(f"mssql+pyodbc://u:{SECRET}@h/d")


class TestRedactErrorText:
    """What may leave the server in a share bundle: no quoted value, no long digit run."""

    def test_quoted_string_literals_become_an_ellipsis(self):
        from core.redaction import redact_error_text

        text = "Conversion failed when converting the varchar value 'Some Name' to data type int."
        assert redact_error_text(text) == (
            "Conversion failed when converting the varchar value '…' to data type int."
        )

    def test_escaped_quotes_stay_inside_one_literal(self):
        from core.redaction import redact_error_text

        assert redact_error_text("bad value 'it''s here' and 'x'") == "bad value '…' and '…'"

    def test_an_unterminated_literal_is_masked_to_the_end(self):
        from core.redaction import redact_error_text

        assert redact_error_text("cut off at 'Some Nam") == "cut off at '…"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("id 1234 and 123 and 12", "id # and 123 and 12"),
            ("amount 1234567.89", "amount #.89"),
            ("code ۱۲۳۴۵۶ done", "code # done"),
            ("code ١٢٣٤٥٦ done", "code # done"),
            ("version 6.11.0", "version 6.11.0"),
        ],
    )
    def test_digit_runs_of_four_or_more_become_a_hash(self, text, expected):
        from core.redaction import redact_error_text

        assert redact_error_text(text) == expected

    def test_persian_text_inside_quotes_is_masked(self):
        from core.redaction import redact_error_text

        assert redact_error_text("مقدار 'علی رضایی' نامعتبر") == "مقدار '…' نامعتبر"

    def test_connection_credentials_are_masked_first(self):
        from core.redaction import redact_error_text

        out = redact_error_text(f"Login failed: UID=nlq;PWD={SECRET};Server=db1 via mssql://nlq:{SECRET}@db1/x")
        assert SECRET not in out
        assert MASK in out

    def test_none_and_empty_are_empty_and_long_text_is_cut(self):
        from core.redaction import redact_error_text

        assert redact_error_text(None) == "" == redact_error_text("")
        assert len(redact_error_text("x" * 5000)) <= 601

    def test_sqlalchemy_style_statement_echo_loses_its_parameters(self):
        from core.redaction import redact_error_text

        out = redact_error_text("(pyodbc.Error) ('42000', '[42000] bad') [SQL: SELECT 1 WHERE n = 'Acme']")
        assert "Acme" not in out and "42000" not in out


class TestRedactSqlLiterals:
    """Literals are found by parsing, never by pattern."""

    def test_string_and_numeric_literals_become_placeholders(self):
        from core.redaction import redact_sql_literals

        out = redact_sql_literals(
            "SELECT TOP 5 Name FROM T WHERE City = 'Oslo' AND Year = 2024 AND Rate > 0.5"
        )
        assert out == (
            "SELECT TOP (:num) Name FROM T WHERE City = :str AND Year = :num AND Rate > :num"
        )

    def test_a_quote_inside_a_comment_or_bracketed_name_is_not_a_literal(self):
        from core.redaction import redact_sql_literals

        out = redact_sql_literals("SELECT [it's] FROM T -- 'not a string'\nWHERE a = 'x'")
        assert "it's" in out and "not a string" not in out and ":str" in out

    def test_national_hex_and_in_lists(self):
        from core.redaction import redact_sql_literals

        out = redact_sql_literals("SELECT N'علی', 0x1F FROM T WHERE c IN (1, 2, 'k')")
        assert "علی" not in out and "0x1F" not in out and "'k'" not in out
        assert out.count(":num") == 2

    def test_order_by_and_group_by_positions_are_kept(self):
        from core.redaction import redact_sql_literals

        out = redact_sql_literals("SELECT a, COUNT(*) FROM T GROUP BY 1 ORDER BY 2 DESC")
        assert "GROUP BY 1" in out and "ORDER BY 2" in out

    def test_several_statements_and_unparseable_text(self):
        from core.redaction import SQL_WITHHELD, redact_sql_literals

        assert redact_sql_literals("SELECT 1; SELECT 'a'") == "SELECT :num; SELECT :str"
        assert redact_sql_literals("SELECT ( FROM 'secret'") == SQL_WITHHELD
        assert redact_sql_literals(None) == "" == redact_sql_literals("  ")
