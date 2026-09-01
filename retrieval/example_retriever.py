# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
from knowledge.examples import EXAMPLES


class ExampleRetriever:

    QUESTION_TAGS = {
        "customer": ["مشتری", "مشتریان", "خریدار", "خریداران", "customer", "customers", "buyer", "buyers"],
        "supplier": ["عرضه کننده", "عرضه‌کننده", "عرضه کنندگان", "عرضه کنندگانی", "تامین کننده", "تامین‌کننده", "فروشنده", "فروشندگان", "supplier", "suppliers"],
        "broker": ["کارگزار", "کارگزاری", "broker", "brokers"],
        "symbol": ["نماد", "نمادها", "symbol", "symbols"],
        "ring": ["تالار", "رینگ", "ring"],
        "purchase": ["خرید", "خریدها", "purchase", "purchases"],
        "trade": ["معامله", "معاملات", "قرارداد", "قراردادها", "trade", "trades", "contract", "contracts"],
        "offer": ["عرضه", "عرضه ها", "عرضه‌ها", "offer", "offers"],
        "value": ["ارزش", "مبلغ", "ریالی", "value"],
        "volume": ["حجم", "تناژ", "مقدار", "volume", "quantity"],
        "price": ["قیمت", "price"],
        "count": ["تعداد", "چند", "count", "number"],
        "top": ["برتر", "بیشترین", "بالاترین", "top", "highest", "largest"],
        "date": ["تاریخ", "date"],
        "year": ["سال", "year"],
        "month": ["ماه", "month"],
        "day": ["روز", "day"],
        "distinct": ["منحصر", "یکتا", "distinct", "unique"],
        "average": ["میانگین", "متوسط", "average", "avg"],
        "active": ["فعال", "active"],
        "wage": ["کارمزد", "wage", "fee"],
    }

    @classmethod
    def retrieve(cls, question: str, limit: int = 3) -> list[dict]:
        q = question.lower()
        matched_tags = {
            tag
            for tag, aliases in cls.QUESTION_TAGS.items()
            if any(alias.lower() in q for alias in aliases)
        }
        scored = [
            (len(matched_tags & set(ex["tags"])), ex)
            for ex in EXAMPLES
            if len(matched_tags & set(ex["tags"])) > 0
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ex for _, ex in scored[:limit]]
