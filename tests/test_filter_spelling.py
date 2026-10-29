# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The filter value spelling fix (``retrieval.filter_spelling``) and the letter-form key behind it.

The warehouse stores a value with the Arabic letters ``ي``/``ك``; the model writes the
Persian ``ی``/``ک``. These tests cover the pure pieces (the key, the choice of the stored
value, the ``LIKE`` pattern rule), the rewrite of a statement (what changes and, as
important, what does not), and the audit block. The two generation paths are in
``tests/test_filter_spelling_paths.py``; what happens without the fix is pinned in
``tests/test_filter_spelling_pins.py``.

Every letter that matters is written as an escape: ``ی`` and ``ي`` look alike.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from config import override_settings
from core.persian import LETTER_FORM_GROUPS, letter_form_key, normalize_for_matching
from observability.audit import AuditRecord
from retrieval import dimension_vocabulary as dv
from retrieval.filter_spelling import (
    SpellingFix,
    audit_block,
    fix_filter_spelling,
    respell_filters,
    respell_like_pattern,
    respell_literal,
)

PERSIAN_YEH, ARABIC_YEH, MAKSURA = "ی", "ي", "ى"
PERSIAN_KAF, ARABIC_KAF = "ک", "ك"
ZWNJ = "‌"


def arabic(text: str) -> str:
    """*text* with its Persian ``ی``/``ک`` written as the Arabic ``ي``/``ك``."""
    return text.replace(PERSIAN_YEH, ARABIC_YEH).replace(PERSIAN_KAF, ARABIC_KAF)


#: A hall as the model writes it, as the warehouse stores it, and one more hall.
HALL = "تالار صادراتی کوچک"
STORED = arabic(HALL)
OTHER = "تالار صنعتی"
assert HALL != STORED

HEAD = "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE "


def seed(values: list[str] | None, column: str = "Name", table: str = "Ring") -> None:
    if values is not None:
        dv.refresh_vocabulary(table, column, execute_fn=lambda sql, params: pd.DataFrame({column: values}))


@pytest.fixture(autouse=True)
def _cache():
    dv.clear_vocabulary_cache()
    yield
    dv.clear_vocabulary_cache()


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


class TestLetterFormKey:
    @pytest.mark.parametrize(
        ("one", "other"),
        [
            (f"عل{PERSIAN_YEH}", f"عل{ARABIC_YEH}"),
            (f"عل{PERSIAN_YEH}", f"عل{MAKSURA}"),
            (f"{PERSIAN_KAF}تاب", f"{ARABIC_KAF}تاب"),
            ("می" + ZWNJ + "خواهم", "میخواهم"),
            ("ﻱ", PERSIAN_YEH),
            ("ﻛ", PERSIAN_KAF),
            ("آ", "آ"),
            (HALL, STORED),
        ],
    )
    def test_the_variants_of_one_letter_have_one_key(self, one, other):
        assert letter_form_key(one) == letter_form_key(other)

    @pytest.mark.parametrize(
        ("one", "other"),
        [
            ("Ali", "ali"),
            ("۱۲", "12"),
            ("١٢", "12"),
            ("تالار ها", "تالارها"),
            ("تالار" + ZWNJ + "ها", "تالار ها"),
            ("ة", "ه"),
            ("ۀ", "هٔ"),
            ("أ", "ا"),
            ("a b", "a  b"),
        ],
    )
    def test_everything_else_keeps_its_own_key(self, one, other):
        assert letter_form_key(one) != letter_form_key(other)

    def test_it_is_narrower_than_the_matching_fold(self):
        assert (normalize_for_matching("Ali"), letter_form_key("Ali")) == ("ali", "Ali")
        assert (normalize_for_matching("\u06f1\u06f2"), letter_form_key("\u06f1\u06f2")) == ("12", "\u06f1\u06f2")
        assert (normalize_for_matching("a  b"), letter_form_key("a  b")) == ("a b", "a  b")

    def test_the_groups_have_the_persian_letter_first(self):
        assert LETTER_FORM_GROUPS == (
            (PERSIAN_YEH, ARABIC_YEH, MAKSURA),
            (PERSIAN_KAF, ARABIC_KAF),
        )

    def test_it_is_idempotent(self):
        for text in (HALL, STORED, "ﻱﻛ", "a" + ZWNJ + "b"):
            assert letter_form_key(letter_form_key(text)) == letter_form_key(text)

    def test_the_matching_fold_is_not_changed_by_any_of_this(self):
        assert normalize_for_matching(STORED) == HALL
        assert normalize_for_matching(MAKSURA) == MAKSURA


# ---------------------------------------------------------------------------
# Which stored value a literal meant
# ---------------------------------------------------------------------------


class TestRespellLiteral:
    def test_the_one_stored_value_with_the_same_letters_is_returned(self):
        assert respell_literal(HALL, [STORED, OTHER]) == STORED

    def test_a_stored_value_is_left_alone_even_when_another_has_the_same_key(self):
        assert respell_literal(STORED, [STORED, HALL]) is None
        assert respell_literal(HALL, [STORED, HALL]) is None

    def test_two_different_stored_values_with_the_same_key_give_nothing(self):
        half = HALL.replace(PERSIAN_KAF, ARABIC_KAF)
        assert letter_form_key(half) == letter_form_key(STORED)
        assert respell_literal(HALL, [STORED, half, OTHER]) is None

    def test_the_same_value_listed_twice_is_one_candidate(self):
        assert respell_literal(HALL, [STORED, OTHER, STORED]) == STORED

    def test_a_zwnj_difference_is_a_difference_of_spelling(self):
        stored = "می" + ZWNJ + "خواهم"
        assert respell_literal("میخواهم", [stored]) == stored
        assert respell_literal(stored, ["میخواهم"]) == "میخواهم"

    @pytest.mark.parametrize(
        ("literal", "stored"),
        [
            ("alpha", ["Alpha"]),
            ("1402", ["۱۴۰۲"]),
            ("a  b", ["a b"]),
            ("", ["", "x"]),
            ("x", []),
        ],
    )
    def test_case_digits_and_spaces_are_not_spelling(self, literal, stored):
        assert respell_literal(literal, stored) is None


class TestRespellLikePattern:
    def test_a_column_that_stores_one_form_gives_the_pattern_that_form(self):
        pattern = f"%{PERSIAN_KAF}وچ%{PERSIAN_YEH}%"
        assert respell_like_pattern(pattern, [STORED, OTHER.replace(PERSIAN_YEH, ARABIC_YEH)]) == (
            f"%{ARABIC_KAF}وچ%{ARABIC_YEH}%"
        )

    def test_each_group_is_decided_on_its_own(self):
        # the column stores Arabic YEH and Persian KAF: only the YEH of the pattern changes
        stored = [f"{PERSIAN_KAF}{ARABIC_YEH}ش"]
        assert respell_like_pattern(f"%{PERSIAN_KAF}{PERSIAN_YEH}%", stored) == f"%{PERSIAN_KAF}{ARABIC_YEH}%"

    def test_the_other_variants_of_the_group_are_written_as_the_stored_form_too(self):
        assert respell_like_pattern(f"%{MAKSURA}%", [f"x{ARABIC_YEH}"]) == f"%{ARABIC_YEH}%"
        assert respell_like_pattern(f"%{ARABIC_YEH}%", [f"x{PERSIAN_YEH}"]) == f"%{PERSIAN_YEH}%"

    def test_a_column_that_stores_both_forms_of_a_group_leaves_it_alone(self):
        stored = [f"a{ARABIC_YEH}", f"b{PERSIAN_YEH}"]
        assert respell_like_pattern(f"%{PERSIAN_YEH}%", stored) is None

    def test_one_value_that_mixes_the_forms_is_a_column_that_mixes_them(self):
        assert respell_like_pattern(f"%{PERSIAN_YEH}%", [f"a{ARABIC_YEH}{PERSIAN_YEH}"]) is None

    def test_a_group_the_column_never_uses_is_left_alone(self):
        assert respell_like_pattern(f"%{PERSIAN_KAF}%", [f"a{ARABIC_YEH}"]) is None

    def test_an_arabic_presentation_form_in_the_column_leaves_everything_alone(self):
        assert respell_like_pattern(f"%{PERSIAN_YEH}%", [f"a{ARABIC_YEH}", "ﻱ"]) is None

    def test_a_pattern_already_in_the_stored_form_or_without_letters_is_unchanged(self):
        assert respell_like_pattern(f"%{ARABIC_YEH}%", [f"a{ARABIC_YEH}"]) is None
        assert respell_like_pattern("%abc_%", [f"a{ARABIC_YEH}"]) is None
        assert respell_like_pattern("%", []) is None

    def test_wildcards_and_everything_else_are_kept(self):
        assert respell_like_pattern(f"[a-c]%_{PERSIAN_YEH}x", [f"a{ARABIC_YEH}"]) == f"[a-c]%_{ARABIC_YEH}x"


# ---------------------------------------------------------------------------
# The statement
# ---------------------------------------------------------------------------


class TestEquality:
    def test_the_issue_pair_a_join_over_a_dimension_with_the_arabic_spelling_stored(self):
        seed([STORED, OTHER])
        head = (
            "SELECT TOP 5 o.[TotalAmount] FROM [sales].[Order] AS o "
            "JOIN [ref].[Ring] AS r ON o.RingID = r.ID WHERE "
        )
        fix = fix_filter_spelling(f"{head}r.[Name] = N'{HALL}'")

        assert fix.sql == f"{head}r.[Name] = N'{STORED}'"
        assert fix.fixed == 1
        assert fix.columns == ("Ring.Name",)

    def test_a_literal_that_is_a_stored_value_is_untouched(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{STORED}'"
        assert fix_filter_spelling(sql) == SpellingFix(sql)

    def test_two_stored_values_it_could_mean_leave_it_untouched(self):
        seed([STORED, HALL.replace(PERSIAN_KAF, ARABIC_KAF), OTHER])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        assert fix_filter_spelling(sql) == SpellingFix(sql)

    def test_a_literal_that_means_nothing_stored_is_untouched(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'Gamma'"
        assert fix_filter_spelling(sql) == SpellingFix(sql)

    def test_no_cached_vocabulary_leaves_it_untouched(self):
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        assert fix_filter_spelling(sql) == SpellingFix(sql)

    def test_a_cold_column_starts_no_background_refresh(self):
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        with patch("retrieval.dimension_vocabulary._trigger_background_refresh") as trigger:
            assert fix_filter_spelling(sql) == SpellingFix(sql)
        trigger.assert_not_called()

    def test_a_stale_vocabulary_is_used_and_starts_no_background_refresh(self):
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        with patch.object(dv._cache, "get_with_state", return_value=([STORED, OTHER], False)), patch(
            "retrieval.dimension_vocabulary._trigger_background_refresh",
        ) as trigger:
            assert fix_filter_spelling(sql).sql == f"{HEAD}r.[Name] = N'{STORED}'"
        trigger.assert_not_called()

    def test_a_vocabulary_cached_for_another_column_is_not_this_columns(self):
        seed([STORED, OTHER], column="Code")
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        assert fix_filter_spelling(sql) == SpellingFix(sql)

    def test_a_vocabulary_that_may_be_cut_short_leaves_it_untouched(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        with override_settings(default_top_n=2):
            assert fix_filter_spelling(sql) == SpellingFix(sql)
        with override_settings(default_top_n=3):
            assert fix_filter_spelling(sql).fixed == 1

    def test_an_empty_vocabulary_leaves_it_untouched(self):
        seed([])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        assert fix_filter_spelling(sql) == SpellingFix(sql)

    def test_a_denied_column_is_never_read_for_its_values(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        assert fix_filter_spelling(sql, denied_columns=["Name"]) == SpellingFix(sql)
        assert fix_filter_spelling(sql, denied_columns=["Ring.Name"]) == SpellingFix(sql)

    def test_a_join_only_column_is_never_read_for_its_values(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        assert fix_filter_spelling(sql, denied_columns=["ref.Ring.Name"]) == SpellingFix(sql)

    def test_a_denied_column_of_another_table_does_not_matter(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        assert fix_filter_spelling(sql, denied_columns=["Customer.Name"]).fixed == 1

    @pytest.mark.parametrize("operator", ["=", "<>", "!="])
    def test_equal_and_not_equal(self, operator):
        seed([STORED, OTHER])
        fix = fix_filter_spelling(f"{HEAD}r.[Name] {operator} N'{HALL}'")
        assert fix.fixed == 1
        assert fix.sql == f"{HEAD}r.[Name] {operator} N'{STORED}'"

    def test_the_literal_may_be_on_the_left(self):
        seed([STORED, OTHER])
        assert fix_filter_spelling(f"{HEAD}N'{HALL}' = r.[Name]").sql == f"{HEAD}N'{STORED}' = r.[Name]"

    def test_in_and_not_in_rewrite_only_the_members_that_need_it(self):
        seed([STORED, OTHER])
        fix = fix_filter_spelling(f"{HEAD}r.[Name] IN (N'{HALL}', N'{OTHER}', N'Gamma')")
        assert fix.sql == f"{HEAD}r.[Name] IN (N'{STORED}', N'{OTHER}', N'Gamma')"
        assert fix.fixed == 1
        fix = fix_filter_spelling(f"{HEAD}r.[Name] NOT IN (N'{HALL}')")
        assert fix.sql == f"{HEAD}r.[Name] NOT IN (N'{STORED}')"

    def test_every_literal_that_needs_it_is_rewritten_and_counted(self):
        seed([STORED, OTHER.replace(PERSIAN_YEH, ARABIC_YEH)])
        other_stored = OTHER.replace(PERSIAN_YEH, ARABIC_YEH)
        fix = fix_filter_spelling(f"{HEAD}r.[Name] IN (N'{HALL}', N'{OTHER}') OR r.[Name] <> N'{HALL}'")
        assert fix.sql == f"{HEAD}r.[Name] IN (N'{STORED}', N'{other_stored}') OR r.[Name] <> N'{STORED}'"
        assert fix.fixed == 3
        assert fix.columns == ("Ring.Name",)

    def test_the_prefix_and_the_quoting_are_kept(self):
        seed([STORED, OTHER])
        assert fix_filter_spelling(f"{HEAD}r.[Name] = '{HALL}'").sql == f"{HEAD}r.[Name] = '{STORED}'"
        assert fix_filter_spelling(f"{HEAD}r.[Name] = n'{HALL}'").sql == f"{HEAD}r.[Name] = n'{STORED}'"
        assert fix_filter_spelling(f"{HEAD}r.[Name] = (N'{HALL}')").sql == f"{HEAD}r.[Name] = (N'{STORED}')"

    def test_a_quote_inside_the_stored_value_is_doubled(self):
        stored = STORED + "'s"
        seed([stored, OTHER])
        fix = fix_filter_spelling(f"{HEAD}r.[Name] = N'{HALL}''s'")
        assert fix.sql == f"{HEAD}r.[Name] = N'{STORED}''s'"

    def test_the_rest_of_the_statement_is_kept_to_the_character(self):
        seed([STORED, OTHER])
        sql = (
            "SELECT   TOP 5 r.[Name]   -- the name\n"
            "FROM [ref].[Ring] AS r WITH (NOLOCK)\n"
            f"WHERE\tr.[Name]=N'{HALL}'  /* tail */\n"
            "ORDER BY 1"
        )
        assert fix_filter_spelling(sql).sql == sql.replace(f"N'{HALL}'", f"N'{STORED}'")

    def test_a_column_found_through_a_cte_or_a_derived_table(self):
        seed([STORED, OTHER])
        cte = f"WITH c AS (SELECT * FROM [ref].[Ring]) SELECT TOP 5 c.[Name] FROM c WHERE c.[Name] = N'{HALL}'"
        assert fix_filter_spelling(cte).sql == cte.replace(HALL, STORED)
        derived = f"SELECT TOP 5 d.[Name] FROM (SELECT [Name] FROM [ref].[Ring]) AS d WHERE d.[Name] = N'{HALL}'"
        assert fix_filter_spelling(derived).sql == derived.replace(HALL, STORED)

    def test_a_filter_in_a_subquery_and_in_a_join_condition(self):
        seed([STORED, OTHER])
        sql = (
            "SELECT TOP 5 o.[TotalAmount] FROM [sales].[Order] AS o WHERE o.RingID IN "
            f"(SELECT r.ID FROM [ref].[Ring] AS r WHERE r.[Name] = N'{HALL}')"
        )
        assert fix_filter_spelling(sql).sql == sql.replace(HALL, STORED)

    def test_a_column_inside_an_expression_is_not_a_plain_read_of_a_stored_column(self):
        seed([STORED, OTHER])
        for condition in (f"LOWER(r.[Name]) = N'{HALL}'", f"r.[Name] + N'x' = N'{HALL}x'", f"N'{HALL}' = N'{HALL}'"):
            sql = f"{HEAD}{condition}"
            assert fix_filter_spelling(sql) == SpellingFix(sql)

    def test_an_unqualified_name_two_tables_share_needs_both_vocabularies(self):
        join = (
            "SELECT TOP 5 b.[PersianName] FROM [ref].[Broker] AS b JOIN [ref].[Currency] AS c ON c.ID = b.ID "
            "WHERE [PersianName] = N'{}'"
        )
        seed([STORED, OTHER], column="PersianName", table="Broker")
        assert fix_filter_spelling(join.format(HALL)) == SpellingFix(join.format(HALL))

        seed(["Dollar"], column="PersianName", table="Currency")
        fix = fix_filter_spelling(join.format(HALL))
        assert fix.sql == join.format(STORED)
        assert fix.columns == ("Broker.PersianName", "Currency.PersianName")

    def test_a_value_either_of_the_shared_columns_stores_is_known(self):
        join = (
            "SELECT TOP 5 b.[PersianName] FROM [ref].[Broker] AS b JOIN [ref].[Currency] AS c ON c.ID = b.ID "
            "WHERE [PersianName] = N'{}'"
        )
        seed([OTHER], column="PersianName", table="Broker")
        seed([STORED], column="PersianName", table="Currency")
        assert fix_filter_spelling(join.format(STORED)).fixed == 0
        assert fix_filter_spelling(join.format(HALL)).sql == join.format(STORED)

    def test_a_statement_that_does_not_parse_is_returned_as_given(self):
        assert fix_filter_spelling("not sql at all (") == SpellingFix("not sql at all (")
        assert fix_filter_spelling("") == SpellingFix("")

    def test_the_fix_is_idempotent(self):
        seed([STORED, OTHER])
        once = fix_filter_spelling(f"{HEAD}r.[Name] = N'{HALL}'")
        assert fix_filter_spelling(once.sql) == SpellingFix(once.sql)

    def test_a_zwnj_in_the_stored_value_is_written_back(self):
        stored = "تالار" + ZWNJ + "ها"
        seed([stored, OTHER])
        fix = fix_filter_spelling(f"{HEAD}r.[Name] = N'تالارها'")
        assert fix.sql == f"{HEAD}r.[Name] = N'{stored}'"

    def test_a_presentation_form_in_the_stored_value_is_matched_and_written_back(self):
        stored = "ﻱﻛ"
        seed([stored, OTHER])
        fix = fix_filter_spelling(f"{HEAD}r.[Name] = N'{PERSIAN_YEH}{PERSIAN_KAF}'")
        assert fix.sql == f"{HEAD}r.[Name] = N'{stored}'"

    @pytest.mark.parametrize(
        ("dialect", "template"),
        [
            ("tsql", "SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'{}'"),
            ("postgres", 'SELECT r."Name" FROM "ref"."Ring" AS r WHERE r."Name" = \'{}\' LIMIT 5'),
            ("mysql", "SELECT r.`Name` FROM `ref`.`Ring` AS r WHERE r.`Name` = '{}' LIMIT 5"),
            ("sqlite", 'SELECT r."Name" FROM "Ring" AS r WHERE r."Name" = \'{}\' LIMIT 5'),
        ],
    )
    def test_the_dialect_of_the_statement_is_the_one_it_is_read_in(self, dialect, template):
        seed([STORED, OTHER])
        fix = fix_filter_spelling(template.format(HALL), dialect=dialect)
        assert fix.sql == template.format(STORED)
        assert fix.fixed == 1


class TestLike:
    def test_a_pattern_over_a_column_that_stores_arabic_letters_gets_them(self):
        seed([STORED, OTHER.replace(PERSIAN_YEH, ARABIC_YEH)])
        fix = fix_filter_spelling(f"{HEAD}r.[Name] LIKE N'%{PERSIAN_KAF}وچ%'")
        assert fix.sql == f"{HEAD}r.[Name] LIKE N'%{ARABIC_KAF}وچ%'"
        assert fix.fixed == 1
        assert fix.columns == ("Ring.Name",)

    def test_not_like_and_escape(self):
        seed([STORED, OTHER.replace(PERSIAN_YEH, ARABIC_YEH)])
        fix = fix_filter_spelling(f"{HEAD}r.[Name] NOT LIKE N'%{PERSIAN_KAF}\\%%' ESCAPE '\\'")
        assert fix.sql == f"{HEAD}r.[Name] NOT LIKE N'%{ARABIC_KAF}\\%%' ESCAPE '\\'"

    def test_a_column_that_stores_both_forms_is_untouched(self):
        seed([STORED, HALL, OTHER])
        sql = f"{HEAD}r.[Name] LIKE N'%{PERSIAN_KAF}وچ%'"
        assert fix_filter_spelling(sql) == SpellingFix(sql)

    def test_a_denied_column_and_a_missing_vocabulary_are_untouched(self):
        sql = f"{HEAD}r.[Name] LIKE N'%{PERSIAN_KAF}وچ%'"
        assert fix_filter_spelling(sql) == SpellingFix(sql)
        seed([STORED, OTHER])
        assert fix_filter_spelling(sql, denied_columns=["Name"]) == SpellingFix(sql)

    def test_a_pattern_that_is_not_a_literal_or_a_column_that_is_not_a_column(self):
        seed([STORED, OTHER])
        for condition in (
            f"r.[Name] LIKE N'%' + N'{PERSIAN_KAF}' + N'%'",
            f"LOWER(r.[Name]) LIKE N'%{PERSIAN_KAF}%'",
            f"N'{PERSIAN_KAF}' LIKE r.[Name]",
        ):
            sql = f"{HEAD}{condition}"
            assert fix_filter_spelling(sql) == SpellingFix(sql)

    def test_equality_and_like_in_one_statement(self):
        seed([STORED, OTHER.replace(PERSIAN_YEH, ARABIC_YEH)])
        fix = fix_filter_spelling(f"{HEAD}r.[Name] = N'{HALL}' OR r.[Name] LIKE N'{PERSIAN_KAF}%'")
        assert fix.sql == f"{HEAD}r.[Name] = N'{STORED}' OR r.[Name] LIKE N'{ARABIC_KAF}%'"
        assert fix.fixed == 2


# ---------------------------------------------------------------------------
# The guard, the audit block
# ---------------------------------------------------------------------------


class TestRespellFilters:
    def test_it_returns_the_rewritten_statement_and_the_block(self):
        seed([STORED, OTHER])
        sql, block = respell_filters(f"{HEAD}r.[Name] = N'{HALL}'")
        assert sql == f"{HEAD}r.[Name] = N'{STORED}'"
        assert block == {"fixed": 1, "columns": ["Ring.Name"]}

    def test_nothing_to_do_returns_the_statement_and_no_block(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{STORED}'"
        assert respell_filters(sql) == (sql, None)

    def test_a_statement_the_guard_refuses_after_the_rewrite_runs_as_it_was(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        with patch("retrieval.filter_spelling.validate_sql", side_effect=ValueError("refused")):
            assert respell_filters(sql) == (sql, None)

    def test_the_guard_is_asked_about_the_rewritten_statement_with_the_callers_policy(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        with patch("retrieval.filter_spelling.validate_sql") as guard:
            respell_filters(sql, denied_columns=["Customer.Name"], dialect="tsql")
        guard.assert_called_once_with(f"{HEAD}r.[Name] = N'{STORED}'", denied_columns=["Customer.Name"], dialect="tsql")

    def test_it_never_raises(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        with patch("retrieval.filter_spelling.usable_vocabularies", side_effect=RuntimeError("boom")):
            assert respell_filters(sql) == (sql, None)

    def test_a_rewrite_that_does_not_read_back_as_intended_is_dropped(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        with patch("retrieval.filter_spelling._set_text"):
            assert fix_filter_spelling(sql) == SpellingFix(sql)

    def test_a_literal_whose_position_is_unknown_is_not_edited(self):
        seed([STORED, OTHER])
        sql = f"{HEAD}r.[Name] = N'{HALL}'"
        with patch("retrieval.filter_spelling._source_span", return_value=None):
            assert fix_filter_spelling(sql) == SpellingFix(sql)


class TestAuditBlock:
    def test_the_block_holds_a_count_and_column_names_only(self):
        block = audit_block(SpellingFix("SELECT 1", 2, ("Ring.Name", "Symbol.Name")))
        assert block == {"fixed": 2, "columns": ["Ring.Name", "Symbol.Name"]}

    def test_no_block_without_a_fix(self):
        assert audit_block(SpellingFix("SELECT 1")) is None

    def test_the_block_of_a_real_fix_has_no_value(self):
        seed([STORED, OTHER])
        _sql, block = respell_filters(f"{HEAD}r.[Name] = N'{HALL}'")
        text = str(block)
        assert HALL not in text and STORED not in text

    def test_the_audit_record_accepts_the_block_and_writes_it_only_when_set(self):
        from datetime import datetime

        base = dict(timestamp=datetime(2026, 1, 1), request_id="r", question="q", generated_sql="SELECT 1",
                    guard={"verdict": "allowed"})
        with_block = AuditRecord(**base, value_spelling={"fixed": 1, "columns": ["Ring.Name"]}).as_dict()
        without = AuditRecord(**base).as_dict()
        assert with_block["value_spelling"] == {"fixed": 1, "columns": ["Ring.Name"]}
        assert "value_spelling" not in without
        assert list(with_block)[:-1] == list(without)

    def test_the_audit_record_refuses_anything_that_could_hold_a_value(self):
        from datetime import datetime

        base = dict(timestamp=datetime(2026, 1, 1), request_id="r", question="q", generated_sql="SELECT 1",
                    guard={"verdict": "allowed"})
        for block in (
            {"fixed": 1, "columns": ["Ring.Name"], "value": STORED},
            {"fixed": 1},
            {"fixed": "1", "columns": []},
            {"fixed": True, "columns": []},
            {"fixed": 1, "columns": [STORED, 3]},
            ["fixed"],
        ):
            with pytest.raises(TypeError):
                AuditRecord(**base, value_spelling=block)

    def test_a_line_written_before_the_field_existed_still_loads(self):
        import json

        line = json.dumps({"timestamp": "2026-01-01T00:00:00", "request_id": "r", "question": "q"})
        assert "value_spelling" not in json.loads(line)


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


class TestSetting:
    def test_it_is_on_by_default(self, monkeypatch):
        from config import Settings

        monkeypatch.delenv("FILTER_VALUE_SPELLING", raising=False)
        assert Settings().filter_value_spelling is True

    @pytest.mark.parametrize(("raw", "expected"), [("false", False), ("0", False), ("no", False), ("true", True), ("1", True), ("YES", True)])
    def test_the_environment_switches_it(self, monkeypatch, raw, expected):
        from config import Settings

        monkeypatch.setenv("FILTER_VALUE_SPELLING", raw)
        assert Settings().filter_value_spelling is expected

    def test_the_example_environment_file_documents_it_on(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
        assert "FILTER_VALUE_SPELLING=true" in text.splitlines()
