# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""What a raw data value looks like: the shapes the wizard refuses and the preflight counts.

``core.value_shapes`` is the one place that says a string was copied out of a
row. The setup wizard (``--value-synonyms``) and the preflight check
``Aliases hold no raw data values`` both rest on it, so a value one refuses is
a value the other flags. Every value below is invented.
"""

from __future__ import annotations

import pytest

from core.value_shapes import (
    MAX_DISTINCT_VALUES,
    MAX_VALUE_LENGTH,
    SHAPES,
    VALUES_PER_TABLE,
    categorical_values,
    data_shape,
    identifier_words,
    prompt_samples,
    is_categorical_column,
    is_personal_column,
    shape_label,
    unsuitable_value,
    value_shape,
)


class TestValueShape:
    @pytest.mark.parametrize(("value", "shape"), [
        ("1387/03/24", "date"), ("01/01/2010", "date"), ("2024-05-01", "date"), ("2024-05-01 10:00:00", "date"),
        ("2024-05-01T10:00:00Z", "date"), ("20240131", "date"), ("20240131103000", "date"), ("1/2/24", "date"),
        ("12 Jan 2024", "date"), ("Jan 12, 2024 10:30", "date"), ("۱۴۰۲/۰۵/۰۱", "date"),
        ("10:30", "time"), ("10:30:05", "time"), ("9:05 pm", "time"),
        ("09120000001", "phone"), ("+98 912 000 0001", "phone"), ("(021) 555-0100", "phone"), ("+1-555-010-0100", "phone"),
        ("10.0.12.200", "ip"), ("192.168.0.7:8080", "ip"), ("fe80::1", "ip"), ("2001:db8::ff00:42:8329", "ip"),
        ("user@example.test", "email"), ("https://example.test/a?b=1", "url"), ("www.example.test", "url"),
        ("ab xxxxx cd", "masked"), ("****1234", "masked"), ("Sam ***", "masked"), ("xxx", "masked"),
        ("5f4dcc3b5aa765d61d8327deb882cf99", "hash"), ("123e4567-e89b-12d3-a456-426614174000", "hash"),
        ("1234567890", "digits"), ("0012345001", "digits"), ("1234567890123", "digits"),
        ("12 Example Street Building 4 Floor 2", "address"), ("Unit 9 Test Avenue 7", "address"),
        ("خیابان نمونه پلاک ۱۲", "address"),
    ])
    def test_a_value_of_each_shape(self, value, shape):
        assert value_shape(value) == shape

    @pytest.mark.parametrize("value", [
        "", "   ", "customer", "Active", "Open Account", "1402", "q1", "2024", "12:3", "v2.1", "A-12",
        "nvarchar(100)", "فروش", "نرخ ارز", "1234567", "Floor 2",
        "Customer Account Status", "xx", "box", "taxi",
    ])
    def test_a_word_or_a_short_number_has_no_shape(self, value):
        assert value_shape(value) is None

    def test_the_shapes_returned_are_the_documented_ones(self):
        samples = ["1387/03/24", "10:30", "09120000001", "10.0.0.7", "a@b.test", "http://x.test", "xxx", "abcdef0123456789ab9", "1234567890", "1 Main Street 12 Floor"]
        assert {value_shape(v) for v in samples} <= set(SHAPES)

    def test_a_shape_is_shown_in_angle_brackets(self):
        assert shape_label("date") == "<date>"


class TestUnsuitableValue:
    @pytest.mark.parametrize("value", ["Active", "Open Account", "Bank One", "فروش نقدی", "Type 2"])
    def test_a_short_word_or_phrase_is_usable(self, value):
        assert unsuitable_value(value) is None

    @pytest.mark.parametrize(("value", "why"), [
        ("", "short"), ("x", "short"), ("a" * (MAX_VALUE_LENGTH + 1), "long"),
        ("2024-05-01", "date"), ("09120000001", "phone"), ("ab xxxxx cd", "masked"), ("user@example.test", "email"),
        ("7-Q-12", "code"), ("A1-77/9", "code"), ("0042", "code"), ("#12", "code"),
        ("Login failed", "error"), ("Connection timeout", "error"), ("Invalid object name", "error"),
        ("Exception raised", "error"), ("خطا در اتصال", "error"),
    ])
    def test_a_reason_is_given_for_the_rest(self, value, why):
        assert unsuitable_value(value) == why

    def test_the_length_limit_can_be_given(self):
        assert unsuitable_value("a short name", 5) == "long" and unsuitable_value("a short name", 40) is None


class TestColumns:
    @pytest.mark.parametrize(("table", "column"), [
        ("Bank", "Name"), ("Bank", "BankName"), ("Trade", "Status"), ("Trade", "StatusTitle"), ("Trade", "kind"),
        ("Action", "action_type"), ("Order", "OrderCategory"), ("Customer", "Status"), ("Customer", "Type"),
    ])
    def test_a_label_column_may_be_read(self, table, column):
        assert is_categorical_column(table, column)

    @pytest.mark.parametrize(("table", "column"), [
        ("Bank", "Description"), ("Bank", "Notes"), ("Trade", "Amount"), ("Bank", "Code"), ("Bank", "ID"),
        ("Bank", "FirstName"), ("Bank", "LastName"), ("Bank", "FullName"), ("Bank", "Family"), ("Bank", "FamilyName"),
        ("Bank", "Surname"), ("Bank", "UserName"), ("Bank", "MobileName"), ("Bank", "PhoneType"), ("Bank", "AddressType"),
        ("Bank", "EmailKind"), ("Bank", "NationalName"), ("Bank", "BirthTitle"), ("Bank", "PassportType"),
        ("Bank", "TelName"), ("Bank", "IP_Name"), ("Bank", "zip_title"), ("Bank", "CardType"), ("Bank", "NickName"),
        ("Customer", "Name"), ("Customer", "Title"), ("Employee", "Name"), ("UserAccount", "Title"),
        ("Person", "Name"), ("Client", "Name"), ("Members", "Name"),
    ])
    def test_a_personal_or_contact_column_or_a_name_of_people_may_not(self, table, column):
        assert not is_categorical_column(table, column)

    def test_words_of_a_name(self):
        assert identifier_words("BankName") == ["bank", "name"]
        assert identifier_words("action_type_ID") == ["action", "type", "id"]
        assert identifier_words("ISINCode") == ["isin", "code"]
        assert identifier_words("") == []


class TestCategoricalValues:
    def test_unsuitable_values_and_repeats_are_dropped_in_order(self):
        values = ["Open", "open", "Closed", "2024-05-01", "ab xxxxx cd", "Login failed", "", None, 5]
        assert categorical_values(values) == ["Open", "Closed"]

    def test_more_distinct_values_than_the_limit_means_not_categorical(self):
        many = [f"Kind {n}" for n in range(MAX_DISTINCT_VALUES + 1)]
        assert categorical_values(many) == []
        assert len(categorical_values(many[:-1])) == MAX_DISTINCT_VALUES

    def test_the_limits_are_the_documented_ones(self):
        assert (MAX_DISTINCT_VALUES, VALUES_PER_TABLE, MAX_VALUE_LENGTH) == (50, 20, 40)


class TestDataShapeAndPromptSamples:
    @pytest.mark.parametrize(("value", "shape"), [
        ("1387/03/24", "date"), ("ab xxxxx cd", "masked"), ("Login failed for user", "error"),
        ("Connection timeout while reading", "error"), ("failed", None), ("annual", None), ("", None),
    ])
    def test_data_shape_is_the_value_shape_plus_error_phrases(self, value, shape):
        assert data_shape(value) == shape

    def test_personal_columns_are_found_by_name(self):
        assert is_personal_column("Bank", "MobileNo") and is_personal_column("Employee", "Title")
        assert not is_personal_column("Bank", "Name") and not is_personal_column("Bank", "Description")

    def test_prompt_samples_keep_words_and_drop_values_and_personal_columns(self):
        values = ["Open", "09120000001", "Closed", "2024-05-01", "ab xxxxx cd", "Login failed"]
        assert prompt_samples("Trade", "Status", values) == ["Open", "Closed"]
        assert prompt_samples("Trade", "Note", values) == ["Open", "Closed"]
        assert prompt_samples("Trade", "Phone", values) == []
        assert prompt_samples("Customer", "Name", ["Sam"]) == []
