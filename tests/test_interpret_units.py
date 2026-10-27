# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The answer summary writes Rial/ریال on money only.

The reported cases: a total traded volume of ``33,493,503,232`` came back as
"... 33,493,503,232 Rial", and «حجم کلی معاملات چه قدره؟» as "۸۲,۱۹۰,۶۶۳ ریال".
The prompt told the model that every amount is in Rials, so it put the unit on
every number. It now says "Rial only for money" and lists, per numeric result
column, whether the column is money -- from ``column_units:`` of
``schema.yaml`` when the operator declared it, else from the column name, else
"not known" for the model to judge.

What must not move is pinned in ``tests/test_interpret_prompt_pins.py``.
"""

from __future__ import annotations

import pytest

from llm.column_units import (
    MONEY,
    NOT_MONEY,
    UNKNOWN,
    classify_column,
    column_hints,
    declared_units,
    numeric_columns,
)
from llm.interpret import INTERPRET_TEMPLATE, interpret_rows
from llm.providers import MockBackend
from llm.router import LLMRouter
from schema_data.registry import (
    get_column_units,
    schema_in_effect,
    validate_schema_yaml_text,
)


class _Capture(MockBackend):
    def __init__(self) -> None:
        super().__init__(response="SELECT 1")
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "summary"


def _prompt_for(rows: list[dict], question: str = "q") -> str:
    backend = _Capture()
    interpret_rows(LLMRouter(default_chain=[backend]), question, rows)
    return backend.prompts[0]


def _schema(text: str):
    return schema_in_effect(validate_schema_yaml_text(text))


class TestThePromptSaysRialIsForMoneyOnly:
    def test_the_blanket_rule_is_gone(self):
        assert "always express amounts" not in INTERPRET_TEMPLATE
        assert "All monetary values" not in INTERPRET_TEMPLATE

    def test_the_rule_names_what_is_not_money(self):
        for word in ("counts", "quantities", "volumes", "weights", "percentages", "ids", "codes", "dates"):
            assert word in INTERPRET_TEMPLATE
        assert "ONLY" in INTERPRET_TEMPLATE

    def test_the_runner_shares_the_template(self):
        import api.runner as runner

        assert runner._INTERPRET_TEMPLATE is INTERPRET_TEMPLATE


class TestThePromptSaysWhichColumnsAreMoney:
    def test_a_volume_is_marked_not_money(self):
        prompt = _prompt_for([{"TotalVolume": 33493503232}], "What is the total purchase volume?")
        assert "Not money, plain numbers (no Rial/ریال, no other currency): TotalVolume" in prompt
        assert "Money, in Rials" not in prompt

    def test_the_persian_volume_question_is_marked_not_money(self):
        prompt = _prompt_for([{"حجم کلی معاملات": 82190663}], "حجم کلی معاملات چه قدره؟")
        assert "Not money, plain numbers (no Rial/ریال, no other currency): حجم کلی معاملات" in prompt

    def test_a_value_is_marked_money(self):
        prompt = _prompt_for([{"TotalValue": 120_000_000}])
        assert "Money, in Rials (write Rial/ریال): TotalValue" in prompt
        assert "Not money" not in prompt

    def test_each_column_lands_on_its_own_side(self):
        prompt = _prompt_for([{"Name": "A", "TradeCount": 3, "TradeValue": 900, "Total": 4}])
        assert "Money, in Rials (write Rial/ریال): TradeValue" in prompt
        assert "plain numbers (no Rial/ریال, no other currency): TradeCount" in prompt
        assert "Not known (write Rial/ریال only if the numbers are clearly an amount of money): Total" in prompt
        assert "Name" not in prompt.split("Which numeric columns")[1]

    def test_a_result_without_numbers_has_no_hint(self):
        assert "Which numeric columns" not in _prompt_for([{"Name": "A", "Day": "x"}])

    def test_an_empty_result_has_no_hint(self):
        assert "Which numeric columns" not in _prompt_for([])

    def test_the_toman_rule_is_still_in_the_prompt_and_the_answer(self):
        assert "never use toman/تومان" in _prompt_for([{"TotalValue": 1}])
        router = LLMRouter(default_chain=[_AnswersToman()])
        assert interpret_rows(router, "q", [{"TotalValue": 5}]) == "مبلغ ۵ ریال بود"


class _AnswersToman(MockBackend):
    def generate(self, prompt: str) -> str:
        return "مبلغ ۵ تومان بود"


class TestColumnNamesDecideWhenTheSchemaIsSilent:
    @pytest.mark.parametrize("name", [
        "TotalVolume", "Volume", "trade_volume", "OrderCount", "Qty", "Quantity", "Weight",
        "PricePercent", "DiscountPct", "ShareRatio", "ID", "id", "RingID", "ring_id", "ringId",
        "BrokerCode", "National_Code", "Year", "TradeDate", "Rank", "FeeCount",
        "حجم کل معاملات", "تعداد سفارش", "مقدار", "وزن", "درصد تغییر", "کد کارگزار", "شناسه",
        "شماره پرونده", "سال", "تاریخ معامله",
    ])
    def test_not_money(self, name):
        assert classify_column(name) == NOT_MONEY

    @pytest.mark.parametrize("name", [
        "Price", "UnitPrice", "TotalValue", "TradeValue", "Amount", "TotalAmount", "Cost", "Fee",
        "fees", "Commission", "Revenue", "Balance", "VolumeInRial", "AmountRials",
        "قیمت", "قیمت پایانی", "ارزش معاملات", "ارزش‌ها", "مبلغ کل", "کارمزد", "هزینه", "ارزش ریالی",
    ])
    def test_money(self, name):
        assert classify_column(name) == MONEY

    @pytest.mark.parametrize("name", [
        "Total", "Sum", "Avg", "Result", "N", "Paid", "Valid", "Barcode", "Grid", "Android",
        "Discount", "جمع", "میانگین",
    ])
    def test_the_model_decides(self, name):
        assert classify_column(name) == UNKNOWN

    def test_a_count_word_beats_a_money_word(self):
        assert classify_column("FeeCount") == NOT_MONEY
        assert classify_column("PriceChangePercent") == NOT_MONEY

    def test_digits_in_a_name_do_not_hide_the_word(self):
        assert classify_column("Value2") == MONEY
        assert classify_column("Volume_Q1") == NOT_MONEY


class TestColumnUnitsOfSchemaYamlOverrideTheName:
    TEXT = (
        "tables:\n"
        "  Trade:\n"
        "    columns:\n"
        "      ID: id\n"
        "      Volume: units traded\n"
        "      Notional: face amount\n"
        "      Weird: odd\n"
        "    column_units:\n"
        "      Volume: rial\n"
        "      Notional: RIAL\n"
        "      Weird: none\n"
    )

    def test_the_declared_unit_beats_the_name(self):
        with _schema(self.TEXT):
            units = declared_units()
            assert units == {"volume": "rial", "notional": "rial", "weird": "none"}
            assert classify_column("Volume", units) == MONEY
            assert classify_column("Notional", units) == MONEY
            assert classify_column("Weird", units) == NOT_MONEY

    def test_a_declared_none_beats_a_money_name(self):
        text = "tables:\n  T:\n    columns:\n      Price: p\n    column_units:\n      Price: none\n"
        with _schema(text):
            assert classify_column("Price", declared_units()) == NOT_MONEY

    def test_the_lookup_is_case_insensitive(self):
        with _schema(self.TEXT):
            assert classify_column("NOTIONAL", declared_units()) == MONEY

    def test_the_prompt_follows_the_schema(self):
        with _schema(self.TEXT):
            prompt = _prompt_for([{"Volume": 5, "Weird": 7}])
        assert "Money, in Rials (write Rial/ریال): Volume" in prompt
        assert "plain numbers (no Rial/ریال, no other currency): Weird" in prompt

    def test_a_name_with_two_different_units_falls_back_to_the_name(self):
        text = (
            "tables:\n"
            "  A:\n    columns:\n      Value: v\n    column_units:\n      Value: rial\n"
            "  B:\n    columns:\n      Value: v\n    column_units:\n      Value: none\n"
        )
        with _schema(text):
            assert get_column_units() == {}
            assert classify_column("Value", declared_units()) == MONEY  # the name says so

    def test_a_schema_without_units_changes_nothing(self):
        with _schema("tables:\n  T:\n    columns:\n      A: a\n"):
            assert declared_units() == {}


class TestWithoutAReadableSchemaTheNamesStillWork:
    def test_declared_units_never_raises(self, monkeypatch):
        import schema_data.registry as registry

        def _boom() -> dict[str, str]:
            raise registry.ConfigNotFoundError("no schema.yaml")

        monkeypatch.setattr(registry, "get_column_units", _boom)
        assert declared_units() == {}

    def test_a_money_column_still_asks_for_rial_without_a_schema(self, monkeypatch):
        import schema_data.registry as registry

        def _boom() -> dict[str, str]:
            raise ValueError("invalid")

        monkeypatch.setattr(registry, "get_column_units", _boom)
        prompt = _prompt_for([{"TotalValue": 10, "TradeCount": 2}])
        assert "Money, in Rials (write Rial/ریال): TotalValue" in prompt
        assert "TradeCount" in prompt.split("plain numbers")[1]


class TestWhichColumnsAreListed:
    def test_only_columns_with_a_number_are_listed(self):
        rows = [{"Name": "A", "Value": 1, "Flag": True, "Day": None}, {"Name": "B", "Value": 2.5}]
        assert numeric_columns(rows) == ["Value"]

    def test_a_decimal_and_a_numpy_number_count(self):
        from decimal import Decimal

        import numpy as np

        assert numeric_columns([{"a": Decimal("1.5"), "b": np.int64(3), "c": np.float64(2.0)}]) == ["a", "b", "c"]

    def test_the_hint_block_is_empty_without_numbers(self):
        assert column_hints([{"Name": "A"}]) == ""
        assert column_hints([]) == ""
