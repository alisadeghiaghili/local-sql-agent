from knowledge.examples import EXAMPLES


class ExampleRetriever:

    QUESTION_TAGS: dict[str, list[str]] = {

        "customer":  ["مشتری", "مشتریان", "خریدار", "خریداران", "customer", "customers", "buyer", "buyers"],
        "supplier":  ["عرضه کننده", "عرضه‌کننده", "تامین کننده", "تامین‌کننده", "supplier", "suppliers"],
        "broker":    ["کارگزار", "کارگزاری", "broker", "brokers"],
        "symbol":    ["نماد", "نمادها", "کالا", "symbol", "symbols"],
        "ring":      ["تالار", "رینگ", "ring"],
        "purchase":  ["خرید", "خریدها", "purchase", "purchases"],
        "trade":     ["معامله", "معاملات", "قرارداد", "قراردادها", "trade", "trades", "contract", "contracts"],
        "offer":     ["عرضه", "عرضه ها", "عرضه‌ها", "offer", "offers"],
        "value":     ["ارزش", "مبلغ", "ریالی", "value"],
        "volume":    ["حجم", "تناژ", "مقدار", "volume", "quantity"],
        "price":     ["قیمت", "price"],
        "count":     ["تعداد", "چند", "count", "number"],
        "top":       ["برتر", "بیشترین", "بالاترین", "top", "highest", "largest"],
        "date":      ["تاریخ", "date"],
        "year":      ["سال", "year"],
        "month":     ["ماه", "month"],
        "day":       ["روز", "day"],
        "distinct":  ["منحصر", "یکتا", "distinct", "unique"],
        "average":   ["میانگین", "متوسط", "average", "avg"],
        "active":    ["فعال", "active"],
        "wage":      ["کارمزد", "wage", "fee"],
        "sum":       ["جمع", "مجموع", "total", "sum"]
    }

    @classmethod
    def retrieve(cls, question: str, limit: int = 3) -> list[dict]:

        question_lower = question.lower()

        matched_tags: set[str] = set()

        for tag, aliases in cls.QUESTION_TAGS.items():
            if any(alias.lower() in question_lower for alias in aliases):
                matched_tags.add(tag)

        if not matched_tags:
            return EXAMPLES[:limit]

        scored: list[tuple[int, int, dict]] = []

        for example in EXAMPLES:
            example_tags = set(example["tags"])
            score = len(matched_tags.intersection(example_tags))
            if score > 0:
                scored.append((score, len(example_tags), example))

        scored.sort(key=lambda x: (x[0], -x[1]), reverse=True)

        return [item[2] for item in scored[:limit]]
