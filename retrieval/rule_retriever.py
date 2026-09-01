# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
from knowledge.business_rules import BUSINESS_RULES


class RuleRetriever:

    RULE_MAPPING = {
        "purchase": ["خرید", "purchase"],
        "trade": ["معامله", "معاملات", "trade"],
        "offer": ["عرضه", "offer"],
        "customer": ["مشتری", "خریدار"],
        "supplier": ["عرضه کننده", "فروشنده"],
        "broker": ["کارگزار", "broker"],
        "symbol": ["نماد", "کالا", "symbol"],
        "date": ["تاریخ", "سال", "ماه", "هفته", "فصل", "روز", "year", "month", "week", "season", "day",
                 "شنبه", "جمعه", "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان",
                 "آذر", "دی", "بهمن", "اسفند", "بهار", "تابستان", "پاییز", "زمستان"],
        "ring": ["تالار", "رینگ", "ring"],
        "topn": ["بیشترین", "برتر", "top"],
    }

    @classmethod
    def retrieve(cls, question: str) -> list[str]:
        q = question.lower()
        results = []
        for rule_key, aliases in cls.RULE_MAPPING.items():
            for alias in aliases:
                if alias.lower() in q:
                    rule = BUSINESS_RULES.get(rule_key)
                    if rule:
                        results.append(rule)
                    break
        return results
