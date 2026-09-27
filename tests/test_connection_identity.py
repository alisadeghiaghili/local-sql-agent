# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``database.connection_identity.with_application_name`` -- Finding 5,
2026 warehouse-load audit: identify this application to the warehouse so
a DBA can attribute its sessions in a trace.

Pure string/URL-parsing tests -- no engine, no network, nothing this
suite's ``_no_real_database`` guard needs to intervene on.
"""

from __future__ import annotations

import urllib.parse

from database.connection_identity import with_application_name


class TestQueryStringStyle:
    def test_adds_app_when_absent(self):
        url = (
            "mssql+pyodbc://user:secret@myhost:1433/Auction_DM"
            "?driver=ODBC+Driver+17+for+SQL+Server"
        )
        result = with_application_name(url, "local-sql-agent")
        assert "APP=local-sql-agent" in result
        # Never dropped anything else already on the URL.
        assert "driver=ODBC" in result
        assert "myhost" in result
        assert "secret" in result  # password preserved, not masked

    def test_does_not_override_an_existing_app(self):
        url = (
            "mssql+pyodbc://user@myhost/Auction_DM"
            "?driver=ODBC+Driver+17+for+SQL+Server&APP=already-set"
        )
        result = with_application_name(url, "local-sql-agent")
        assert result == url

    def test_does_not_override_an_existing_application_name_case_insensitive(self):
        url = (
            "mssql+pyodbc://user@myhost/Auction_DM"
            "?driver=ODBC+Driver+17+for+SQL+Server&Application+Name=custom"
        )
        result = with_application_name(url, "local-sql-agent")
        assert result == url

    def test_result_is_a_valid_url_a_real_engine_could_use(self):
        from sqlalchemy.engine import make_url

        url = "mssql+pyodbc://user@myhost/Auction_DM?driver=ODBC+Driver+17+for+SQL+Server"
        result = with_application_name(url, "local-sql-agent")
        parsed = make_url(result)  # must not raise
        assert parsed.query["APP"] == "local-sql-agent"


class TestOdbcConnectStyle:
    def _odbc_url(self, odbc_connect_str: str) -> str:
        return "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc_connect_str)

    def test_adds_app_when_absent(self):
        raw = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=myhost;DATABASE=Auction_DM;PWD=secret;"
        url = self._odbc_url(raw)
        result = with_application_name(url, "local-sql-agent")

        from sqlalchemy.engine import make_url

        decoded = make_url(result).query["odbc_connect"]
        assert "APP=local-sql-agent" in decoded
        assert "SERVER=myhost" in decoded  # original content preserved
        assert "PWD=secret" in decoded

    def test_does_not_override_an_existing_app_keyword(self):
        raw = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=myhost;APP=already-set;"
        url = self._odbc_url(raw)
        result = with_application_name(url, "local-sql-agent")
        assert result == url

    def test_does_not_override_an_existing_application_name_keyword(self):
        raw = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=myhost;Application Name=custom;"
        url = self._odbc_url(raw)
        result = with_application_name(url, "local-sql-agent")
        assert result == url

    def test_adds_a_separator_when_the_raw_string_has_no_trailing_semicolon(self):
        raw = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=myhost"
        url = self._odbc_url(raw)
        result = with_application_name(url, "local-sql-agent")

        from sqlalchemy.engine import make_url

        decoded = make_url(result).query["odbc_connect"]
        assert ";APP=local-sql-agent;" in decoded
        assert "SERVER=myhostAPP" not in decoded  # never glued onto the previous value


class TestNonMssqlOrEmptyName:
    def test_sqlite_url_is_untouched(self):
        url = "sqlite:///:memory:"
        assert with_application_name(url, "local-sql-agent") == url

    def test_other_mssql_driver_is_untouched(self):
        # mssql+pymssql (a different driver) never gets the pyodbc-only APP keyword.
        url = "mssql+pymssql://user@myhost/Auction_DM"
        assert with_application_name(url, "local-sql-agent") == url

    def test_empty_app_name_is_a_no_op(self):
        url = "mssql+pyodbc://user@myhost/Auction_DM?driver=ODBC+Driver+17+for+SQL+Server"
        assert with_application_name(url, "") == url

    def test_unparsable_url_is_returned_unchanged_not_raised(self):
        garbage = "not a url at all :: %%%"
        assert with_application_name(garbage, "local-sql-agent") == garbage
