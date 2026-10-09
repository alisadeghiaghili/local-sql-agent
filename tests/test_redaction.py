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
        redacted = redact_db_url(f"postgresql+psycopg2://u:{password}@h:5432/d")
        assert password not in redacted
        assert MASK in redacted
        assert redacted.endswith("@h:5432/d")

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
