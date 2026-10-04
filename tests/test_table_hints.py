# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for ``database.table_hints.add_nolock_hints``.

Every expected value is the exact output string: the rewriter must insert
``" WITH (NOLOCK)"`` and change nothing else. No database is involved.
"""

from __future__ import annotations

import logging

import pytest
import sqlglot
from sqlglot import exp

from database import table_hints
from database.table_hints import NOLOCK_HINT, add_nolock_hints

H = " WITH (NOLOCK)"


@pytest.fixture(autouse=True)
def _fresh_warning_state():
    table_hints._warned_reasons.clear()
    yield
    table_hints._warned_reasons.clear()


# ---------------------------------------------------------------------------
# Tables that must be hinted
# ---------------------------------------------------------------------------

_HINTED = [
    pytest.param(
        "SELECT * FROM t",
        f"SELECT * FROM t{H}",
        id="bare-name",
    ),
    pytest.param(
        "SELECT o.Id FROM [sales].[Order] o JOIN [ref].[Location] AS l ON l.Id = o.LocationId",
        f"SELECT o.Id FROM [sales].[Order] o{H} JOIN [ref].[Location] AS l{H} ON l.Id = o.LocationId",
        id="hint-goes-after-the-alias-with-and-without-AS",
    ),
    pytest.param(
        "SELECT * FROM a INNER JOIN b ON 1 = 1 LEFT OUTER JOIN c ON 1 = 1 "
        "RIGHT JOIN d ON 1 = 1 FULL OUTER JOIN e ON 1 = 1 CROSS JOIN f JOIN g ON 1 = 1",
        f"SELECT * FROM a{H} INNER JOIN b{H} ON 1 = 1 LEFT OUTER JOIN c{H} ON 1 = 1 "
        f"RIGHT JOIN d{H} ON 1 = 1 FULL OUTER JOIN e{H} ON 1 = 1 CROSS JOIN f{H} JOIN g{H} ON 1 = 1",
        id="every-join-kind",
    ),
    pytest.param(
        "SELECT * FROM a, b x, [c]",
        f"SELECT * FROM a{H}, b x{H}, [c]{H}",
        id="comma-join",
    ),
    pytest.param(
        "SELECT * FROM a CROSS APPLY (SELECT TOP 1 * FROM b WHERE b.i = a.i) q",
        f"SELECT * FROM a{H} CROSS APPLY (SELECT TOP 1 * FROM b{H} WHERE b.i = a.i) q",
        id="table-inside-an-apply-subquery",
    ),
    pytest.param(
        "SELECT * FROM (SELECT x FROM a JOIN b ON a.i = b.i) AS q",
        f"SELECT * FROM (SELECT x FROM a{H} JOIN b{H} ON a.i = b.i) AS q",
        id="derived-table-body",
    ),
    pytest.param(
        "SELECT (SELECT TOP 1 x FROM z) AS s FROM a",
        f"SELECT (SELECT TOP 1 x FROM z{H}) AS s FROM a{H}",
        id="scalar-subquery",
    ),
    pytest.param(
        "SELECT * FROM a WHERE a.k IN (SELECT k FROM b) AND a.j NOT IN (SELECT j FROM c)",
        f"SELECT * FROM a{H} WHERE a.k IN (SELECT k FROM b{H}) AND a.j NOT IN (SELECT j FROM c{H})",
        id="in-subquery",
    ),
    pytest.param(
        "SELECT * FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.i = a.i) "
        "AND NOT EXISTS (SELECT 1 FROM c WHERE c.i = a.i)",
        f"SELECT * FROM a{H} WHERE EXISTS (SELECT 1 FROM b{H} WHERE b.i = a.i) "
        f"AND NOT EXISTS (SELECT 1 FROM c{H} WHERE c.i = a.i)",
        id="exists-and-not-exists",
    ),
    pytest.param(
        "WITH x AS (SELECT Id FROM base1), y AS (SELECT Id FROM base2 b JOIN x ON x.Id = b.Id) "
        "SELECT * FROM y JOIN other ON other.Id = y.Id",
        f"WITH x AS (SELECT Id FROM base1{H}), y AS (SELECT Id FROM base2 b{H} JOIN x ON x.Id = b.Id) "
        f"SELECT * FROM y JOIN other{H} ON other.Id = y.Id",
        id="cte-bodies-hinted-cte-references-not",
    ),
    pytest.param(
        "WITH r AS (SELECT Id, 1 AS n FROM seed UNION ALL SELECT t.Id, r.n + 1 FROM tree t JOIN r ON r.Id = t.ParentId) "
        "SELECT * FROM r",
        f"WITH r AS (SELECT Id, 1 AS n FROM seed{H} UNION ALL SELECT t.Id, r.n + 1 FROM tree t{H} JOIN r ON r.Id = t.ParentId) "
        "SELECT * FROM r",
        id="recursive-cte",
    ),
    pytest.param(
        "SELECT 1 AS v FROM a UNION ALL SELECT 2 FROM b UNION SELECT 3 FROM c "
        "EXCEPT SELECT 4 FROM d INTERSECT SELECT 5 FROM e",
        f"SELECT 1 AS v FROM a{H} UNION ALL SELECT 2 FROM b{H} UNION SELECT 3 FROM c{H} "
        f"EXCEPT SELECT 4 FROM d{H} INTERSECT SELECT 5 FROM e{H}",
        id="every-set-operation-branch",
    ),
    pytest.param(
        "SELECT * FROM [A].[B] JOIN A.B2 ON 1 = 1 JOIN [OtherDb].[dbo].[T] ON 1 = 1",
        f"SELECT * FROM [A].[B]{H} JOIN A.B2{H} ON 1 = 1 JOIN [OtherDb].[dbo].[T]{H} ON 1 = 1",
        id="qualified-bracketed-and-plain-names",
    ),
    pytest.param(
        "SELECT * FROM [Other Db].[dbo].[T x] [a b] JOIN [My Table] AS [m t] ON 1 = 1",
        f"SELECT * FROM [Other Db].[dbo].[T x] [a b]{H} JOIN [My Table] AS [m t]{H} ON 1 = 1",
        id="names-and-aliases-with-spaces",
    ),
    pytest.param(
        "SELECT * FROM [a]]b].[c]]d]",
        f"SELECT * FROM [a]]b].[c]]d]{H}",
        id="escaped-closing-bracket-in-name",
    ),
    pytest.param(
        "SELECT * FROM db1..T x, db1.dbo.U",
        f"SELECT * FROM db1..T x{H}, db1.dbo.U{H}",
        id="default-schema-three-part-name",
    ),
    pytest.param(
        "SELECT * FROM Date d JOIN dbo.Year AS y ON 1 = 1 JOIN [Order] ON 1 = 1",
        f"SELECT * FROM Date d{H} JOIN dbo.Year AS y{H} ON 1 = 1 JOIN [Order]{H} ON 1 = 1",
        id="keyword-like-names",
    ),
    pytest.param(
        "SELECT * FROM t PIVOT (SUM(v) FOR k IN ([a], [b])) AS p",
        f"SELECT * FROM t{H} PIVOT (SUM(v) FOR k IN ([a], [b])) AS p",
        id="hint-goes-before-pivot",
    ),
    pytest.param(
        "SELECT * FROM (a JOIN b ON 1 = 1)",
        f"SELECT * FROM (a{H} JOIN b{H} ON 1 = 1)",
        id="parenthesised-join",
    ),
    pytest.param(
        "SELECT * FROM a; SELECT * FROM b;",
        f"SELECT * FROM a{H}; SELECT * FROM b{H};",
        id="every-statement-of-a-batch",
    ),
]


@pytest.mark.parametrize("sql, expected", _HINTED)
def test_physical_tables_are_hinted(sql, expected):
    assert add_nolock_hints(sql) == expected


# ---------------------------------------------------------------------------
# Tables that must not be hinted
# ---------------------------------------------------------------------------

_UNTOUCHED = [
    pytest.param("SELECT 1", id="no-table"),
    pytest.param("SELECT * FROM #t", id="temp-table"),
    pytest.param("SELECT * FROM ##g JOIN #t2 x ON 1 = 1", id="global-temp-table"),
    pytest.param("SELECT * FROM [#t]", id="bracketed-temp-table"),
    pytest.param("SELECT * FROM @tv", id="table-variable"),
    pytest.param("SELECT * FROM @tv x JOIN @tw ON 1 = 1", id="table-variables-joined"),
    pytest.param("SELECT * FROM (SELECT 1 AS a) x", id="derived-table"),
    pytest.param("SELECT * FROM (VALUES (1), (2)) AS v(a)", id="values-list"),
    pytest.param("SELECT * FROM dbo.fn(1) AS f", id="table-valued-function"),
    pytest.param("SELECT * FROM STRING_SPLIT('a,b', ',') s", id="string-split"),
    pytest.param(
        "SELECT * FROM OPENJSON(@j) WITH (a int '$.a') AS o", id="openjson-with-its-own-with"
    ),
    pytest.param("SELECT * FROM INFORMATION_SCHEMA.TABLES", id="information-schema"),
    pytest.param("SELECT * FROM [INFORMATION_SCHEMA].[COLUMNS] c", id="bracketed-information-schema"),
    pytest.param("SELECT * FROM sys.objects o", id="sys-view"),
    pytest.param("SELECT * FROM [master].[sys].[tables] t", id="three-part-sys-view"),
    pytest.param("SELECT * FROM srv.db.dbo.c", id="four-part-linked-server-name"),
    pytest.param("SELECT * INTO #copy FROM #src", id="select-into-temp"),
    pytest.param("SELECT * FROM t WITH (NOLOCK)", id="already-nolock"),
    pytest.param("SELECT * FROM t x WITH (NOLOCK)", id="already-nolock-after-alias"),
    pytest.param("SELECT * FROM t x with (nolock)", id="already-nolock-lower-case"),
    pytest.param("SELECT * FROM t WITH (READPAST)", id="other-hint-is-kept"),
    pytest.param("SELECT * FROM t x WITH (NOLOCK, READPAST)", id="several-hints-are-kept"),
    pytest.param(
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x", id="only-a-cte-reference"
    ),
]


@pytest.mark.parametrize("sql", _UNTOUCHED)
def test_these_references_are_left_alone(sql):
    assert add_nolock_hints(sql) == sql


def test_cte_names_are_matched_case_insensitively_and_with_an_alias():
    assert add_nolock_hints(
        "WITH Totals AS (SELECT Id FROM t) SELECT * FROM TOTALS x JOIN u ON 1 = 1"
    ) == f"WITH Totals AS (SELECT Id FROM t{H}) SELECT * FROM TOTALS x JOIN u{H} ON 1 = 1"


def test_a_qualified_name_is_a_table_even_when_a_cte_has_that_bare_name():
    assert add_nolock_hints(
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x JOIN dbo.x ON 1 = 1"
    ) == f"WITH x AS (SELECT 1 AS a) SELECT * FROM x JOIN dbo.x{H} ON 1 = 1"


def test_an_unhinted_table_next_to_hinted_and_excluded_ones():
    sql = (
        "SELECT * FROM a WITH (READPAST) JOIN b ON 1 = 1 JOIN #t ON 1 = 1 "
        "JOIN sys.objects o ON 1 = 1 JOIN dbo.f(1) q ON 1 = 1"
    )
    assert add_nolock_hints(sql) == (
        f"SELECT * FROM a WITH (READPAST) JOIN b{H} ON 1 = 1 JOIN #t ON 1 = 1 "
        "JOIN sys.objects o ON 1 = 1 JOIN dbo.f(1) q ON 1 = 1"
    )


def test_the_pre_2005_hint_without_with_is_left_as_written():
    # sqlglot reads `t (NOLOCK)` as a function call, so it is not a table
    # reference this module touches.
    sql = "SELECT * FROM t (NOLOCK)"
    assert add_nolock_hints(sql) == sql


# ---------------------------------------------------------------------------
# Text that must survive unchanged
# ---------------------------------------------------------------------------

_SURVIVES = [
    pytest.param(
        "SELECT * FROM a WHERE x = 'select * from b join c' AND y = 'FROM d'",
        f"SELECT * FROM a{H} WHERE x = 'select * from b join c' AND y = 'FROM d'",
        id="string-literal-with-from-and-table-text",
    ),
    pytest.param(
        "SELECT * FROM a WHERE x = N'it''s from [dbo].[b]' AND y = 'it''s'",
        f"SELECT * FROM a{H} WHERE x = N'it''s from [dbo].[b]' AND y = 'it''s'",
        id="national-literal-with-doubled-quote",
    ),
    pytest.param(
        "SELECT * FROM a WHERE x = 'WITH (NOLOCK)'",
        f"SELECT * FROM a{H} WHERE x = 'WITH (NOLOCK)'",
        id="literal-that-looks-like-a-hint",
    ),
    pytest.param(
        "SELECT * FROM a -- FROM b\nWHERE 1 = 1",
        f"SELECT * FROM a{H} -- FROM b\nWHERE 1 = 1",
        id="line-comment-after-the-table",
    ),
    pytest.param(
        "SELECT * FROM a x -- trailing\nJOIN b ON 1 = 1 -- JOIN c",
        f"SELECT * FROM a x{H} -- trailing\nJOIN b{H} ON 1 = 1 -- JOIN c",
        id="line-comments-around-joins",
    ),
    pytest.param(
        "SELECT /* from z */ * FROM a /* c */ x /* d */ WHERE 1 = 1 /* join q */",
        f"SELECT /* from z */ * FROM a /* c */ x{H} /* d */ WHERE 1 = 1 /* join q */",
        id="block-comments-including-between-name-and-alias",
    ),
    pytest.param(
        "SELECT * FROM a /* multi\nline FROM b\n*/ JOIN c ON 1 = 1",
        f"SELECT * FROM a{H} /* multi\nline FROM b\n*/ JOIN c{H} ON 1 = 1",
        id="multi-line-block-comment",
    ),
    pytest.param(
        "SELECT TOP (?) [Name] FROM [sales].[Customer] WHERE [Name] LIKE ?",
        f"SELECT TOP (?) [Name] FROM [sales].[Customer]{H} WHERE [Name] LIKE ?",
        id="top-with-a-placeholder",
    ),
    pytest.param(
        "SELECT TOP 1000 a.x FROM a ORDER BY a.x",
        f"SELECT TOP 1000 a.x FROM a{H} ORDER BY a.x",
        id="top-with-a-number",
    ),
    pytest.param(
        "SELECT a.x FROM a ORDER BY a.x OFFSET 10 ROWS FETCH NEXT 5 ROWS ONLY",
        f"SELECT a.x FROM a{H} ORDER BY a.x OFFSET 10 ROWS FETCH NEXT 5 ROWS ONLY",
        id="offset-fetch",
    ),
    pytest.param(
        "SELECT a.x FROM a ORDER BY a.x OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
        f"SELECT a.x FROM a{H} ORDER BY a.x OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
        id="offset-fetch-with-placeholders",
    ),
    pytest.param(
        "SELECT TOP 5 a.x, ROW_NUMBER() OVER (PARTITION BY a.y ORDER BY a.z DESC) AS rn, "
        "SUM(a.v) OVER (PARTITION BY a.y) AS s FROM a",
        f"SELECT TOP 5 a.x, ROW_NUMBER() OVER (PARTITION BY a.y ORDER BY a.z DESC) AS rn, "
        f"SUM(a.v) OVER (PARTITION BY a.y) AS s FROM a{H}",
        id="window-functions",
    ),
    pytest.param(
        "SELECT * FROM [مشتری] ص WHERE [نام] = N'شرکت فولاد مبارکه' AND c LIKE N'%تهران%'",
        f"SELECT * FROM [مشتری] ص{H} WHERE [نام] = N'شرکت فولاد مبارکه' AND c LIKE N'%تهران%'",
        id="persian-name-alias-and-literals",
    ),
    pytest.param(
        "SELECT N'مشتری از تهران 🙂' AS lbl, a.x FROM [sales].[Order] a JOIN b ON 1 = 1",
        f"SELECT N'مشتری از تهران 🙂' AS lbl, a.x FROM [sales].[Order] a{H} JOIN b{H} ON 1 = 1",
        id="non-ascii-before-the-tables-keeps-offsets-right",
    ),
    pytest.param(
        "SELECT *\r\nFROM a\r\n  JOIN b ON 1 = 1\r\nWHERE 1 = 1\r\n",
        f"SELECT *\r\nFROM a{H}\r\n  JOIN b{H} ON 1 = 1\r\nWHERE 1 = 1\r\n",
        id="crlf-line-endings",
    ),
    pytest.param(
        "  \n\tSELECT * FROM a   \n",
        f"  \n\tSELECT * FROM a{H}   \n",
        id="surrounding-whitespace",
    ),
    pytest.param(
        "select a.x from a x inner join b y on x.i=y.i where x.v>1",
        f"select a.x from a x{H} inner join b y{H} on x.i=y.i where x.v>1",
        id="lower-case-and-tight-operators",
    ),
]


@pytest.mark.parametrize("sql, expected", _SURVIVES)
def test_everything_else_survives(sql, expected):
    assert add_nolock_hints(sql) == expected


def test_only_the_hint_text_is_added():
    """Removing the inserted hints gives back the input, character for character."""
    for param in _HINTED + _SURVIVES:
        sql, _expected = param.values
        assert add_nolock_hints(sql).replace(NOLOCK_HINT, "") == sql, param.id


def test_rewriting_twice_changes_nothing_more():
    for param in _HINTED + _SURVIVES:
        sql, expected = param.values
        assert add_nolock_hints(expected) == expected, param.id


# ---------------------------------------------------------------------------
# The parameterised templates the retrieval layer sends
# ---------------------------------------------------------------------------

#: One entry per template ``retrieval.value_resolver._build_query`` and
#: ``retrieval.dimension_vocabulary._prefetch_query`` build for the example
#: configuration, verbatim.
_RETRIEVAL_TEMPLATES = [
    "SELECT DISTINCT TOP (?) [Name] FROM [sales].[Customer] WHERE [Name] LIKE ? ESCAPE '\\'",
    "SELECT DISTINCT TOP (?) [PersianName] FROM [ref].[Broker] WHERE [PersianName] LIKE ? ESCAPE '\\'",
    "SELECT DISTINCT TOP (?) [PersianName] FROM [ref].[Currency] WHERE [PersianName] LIKE ? ESCAPE '\\'",
    "SELECT DISTINCT TOP (?) [PersianName] FROM [ref].[Location] WHERE [PersianName] LIKE ? ESCAPE '\\'",
    "SELECT DISTINCT TOP (?) [Name] FROM [ref].[Ring] WHERE [Name] LIKE ? ESCAPE '\\'",
    "SELECT DISTINCT TOP (?) [Commodity_PersianName] FROM [ref].[Symbol] WHERE [Commodity_PersianName] LIKE ? ESCAPE '\\'",
    "SELECT DISTINCT TOP (?) [Commodity_Symbol] FROM [ref].[Symbol] WHERE [Commodity_Symbol] LIKE ? ESCAPE '\\'",
    "SELECT DISTINCT TOP (?) [Customer_Name] FROM [ref].[Supplier] WHERE [Customer_Name] LIKE ? ESCAPE '\\'",
    "SELECT DISTINCT TOP (?) [PersianName] FROM [ref].[Broker]",
    "SELECT DISTINCT TOP (?) [PersianName] FROM [ref].[Currency]",
    "SELECT DISTINCT TOP (?) [PersianName] FROM [ref].[Location]",
    "SELECT DISTINCT TOP (?) [Name] FROM [ref].[Ring]",
    "SELECT DISTINCT TOP (?) [Commodity_PersianName] FROM [ref].[Symbol]",
    "SELECT DISTINCT TOP (?) [Commodity_Symbol] FROM [ref].[Symbol]",
]


def _hinted_template(template: str) -> str:
    """The template with the hint after its (only) table reference."""
    head, sep, tail = template.partition(" WHERE ")
    return f"{head}{H}{sep}{tail}"


@pytest.mark.parametrize("template", _RETRIEVAL_TEMPLATES)
def test_retrieval_templates_keep_their_placeholders_and_escape_clause(template):
    out = add_nolock_hints(template)
    assert out == _hinted_template(template)
    assert out.count("?") == template.count("?")


def test_the_templates_the_code_builds_now_are_rewritten_the_same_way():
    """Not tied to the example schema: build every template with the loaded
    configuration and check the same shape."""
    from retrieval.dimension_vocabulary import PREFETCH_COLUMNS, _prefetch_query
    from retrieval.value_resolver import RESOLVABLE_COLUMNS, _build_query

    built = [
        _build_query(table, column)
        for table, columns in RESOLVABLE_COLUMNS.items()
        for column in columns
    ] + [
        _prefetch_query(table, column)
        for table, columns in PREFETCH_COLUMNS.items()
        for column in columns
    ]
    assert built
    for template in built:
        assert add_nolock_hints(template) == _hinted_template(template)


# ---------------------------------------------------------------------------
# Fallback: the original text, and one warning per reason
# ---------------------------------------------------------------------------

def _warnings(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.name == "database.table_hints" and r.levelno == logging.WARNING
    ]


class TestFallback:
    def test_empty_and_blank_input_is_returned_as_is_without_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="database.table_hints"):
            assert add_nolock_hints("") == ""
            assert add_nolock_hints("  \n") == "  \n"
        assert _warnings(caplog) == []

    def test_a_statement_that_does_not_parse_comes_back_unchanged(self, caplog):
        sql = "SELECT FROM WHERE secret_table_name"
        with caplog.at_level(logging.WARNING, logger="database.table_hints"):
            assert add_nolock_hints(sql) == sql
        (record,) = _warnings(caplog)
        assert "parse_error" in record.getMessage()
        assert "secret_table_name" not in record.getMessage()

    def test_an_unterminated_literal_comes_back_unchanged(self):
        sql = "SELECT * FROM a WHERE x = 'abc"
        assert add_nolock_hints(sql) == sql

    @pytest.mark.parametrize("sql", [
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "INSERT INTO t (a) VALUES (1)",
        "DROP TABLE t",
        "EXEC dbo.proc1",
    ])
    def test_a_statement_that_is_not_a_query_comes_back_unchanged(self, sql, caplog):
        with caplog.at_level(logging.WARNING, logger="database.table_hints"):
            assert add_nolock_hints(sql) == sql
        assert _warnings(caplog)

    def test_one_statement_that_is_not_a_query_leaves_the_whole_batch_alone(self):
        sql = "SELECT * FROM a; DELETE FROM b"
        assert add_nolock_hints(sql) == sql

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM t TABLESAMPLE (10 PERCENT)",
        "SELECT * FROM t FOR SYSTEM_TIME AS OF '2020-01-01' x",
        "SELECT * FROM t AS a(x, y)",
    ])
    def test_a_clause_with_an_uncovered_position_leaves_the_statement_alone(self, sql, caplog):
        # Even the plain tables beside it stay unhinted: the statement is
        # either rewritten and checked as a whole, or not at all.
        joined = f"{sql} JOIN other ON 1 = 1"
        with caplog.at_level(logging.WARNING, logger="database.table_hints"):
            assert add_nolock_hints(joined) == joined
        (record,) = _warnings(caplog)
        assert "unsupported_table_clause" in record.getMessage()

    def test_a_failed_check_of_the_rewrite_returns_the_original(self, monkeypatch, caplog):
        monkeypatch.setattr(table_hints, "NOLOCK_HINT", " WITH (READPAST)")
        sql = "SELECT * FROM a JOIN b ON 1 = 1"
        with caplog.at_level(logging.WARNING, logger="database.table_hints"):
            assert add_nolock_hints(sql) == sql
        (record,) = _warnings(caplog)
        assert "verification_failed" in record.getMessage()

    def test_a_rewrite_that_no_longer_parses_returns_the_original(self, monkeypatch):
        monkeypatch.setattr(table_hints, "NOLOCK_HINT", " WITH ((")
        sql = "SELECT * FROM a"
        assert add_nolock_hints(sql) == sql

    def test_an_unexpected_error_is_swallowed(self, monkeypatch, caplog):
        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(table_hints, "_references", boom)
        sql = "SELECT * FROM a"
        with caplog.at_level(logging.WARNING, logger="database.table_hints"):
            assert add_nolock_hints(sql) == sql
        (record,) = _warnings(caplog)
        assert "unexpected_error" in record.getMessage()

    def test_each_distinct_reason_is_logged_once(self, caplog):
        with caplog.at_level(logging.WARNING, logger="database.table_hints"):
            for _ in range(3):
                add_nolock_hints("SELECT FROM WHERE")      # parse_error
                add_nolock_hints("DELETE FROM t")          # not_a_query
                add_nolock_hints("SELECT * FROM t TABLESAMPLE (5 PERCENT)")
        reasons = sorted(
            next(r for r in ("parse_error", "not_a_query", "unsupported_table_clause")
                 if r in rec.getMessage())
            for rec in _warnings(caplog)
        )
        assert reasons == ["not_a_query", "parse_error", "unsupported_table_clause"]

    def test_a_statement_that_needs_no_hint_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="database.table_hints"):
            add_nolock_hints("SELECT * FROM #t")
            add_nolock_hints("SELECT * FROM t WITH (NOLOCK)")
        assert _warnings(caplog) == []


class TestPositionChecks:
    """An offset sqlglot reports is used only if the text there is the identifier."""

    @staticmethod
    def _identifier(sql: str) -> exp.Identifier:
        return sqlglot.parse_one(sql, read="tsql").find(exp.Table).this

    def test_a_correct_position_is_one_past_the_identifier(self):
        sql = "SELECT * FROM [My Table] x"
        assert table_hints._identifier_end(sql, self._identifier(sql)) == sql.index("]") + 1

    @pytest.mark.parametrize("meta", [
        {},
        {"start": 14},
        {"start": 15, "end": 14},
        {"start": 14, "end": 999},
        {"start": 16, "end": 17},
    ])
    def test_a_missing_or_wrong_position_is_refused(self, meta):
        sql = "SELECT * FROM tbl x"
        ident = self._identifier(sql)
        ident.meta.clear()
        ident.meta.update(meta)
        with pytest.raises(table_hints._Unsupported) as info:
            table_hints._identifier_end(sql, ident)
        assert info.value.reason == "bad_position"

    def test_a_quoted_identifier_must_end_on_its_closing_quote(self):
        sql = "SELECT * FROM [tbl] x"
        ident = self._identifier(sql)
        ident.meta["end"] -= 1
        with pytest.raises(table_hints._Unsupported):
            table_hints._identifier_end(sql, ident)

    def test_a_bad_position_falls_back_for_the_whole_statement(self, monkeypatch, caplog):
        def shifted(sql, ident):
            raise table_hints._Unsupported("bad_position", "shifted")

        monkeypatch.setattr(table_hints, "_identifier_end", shifted)
        sql = "SELECT * FROM a"
        with caplog.at_level(logging.WARNING, logger="database.table_hints"):
            assert add_nolock_hints(sql) == sql
        assert "bad_position" in _warnings(caplog)[0].getMessage()
