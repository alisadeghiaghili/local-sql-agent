from knowledge.business_rules import BUSINESS_RULES


class RuleRetriever:

    RULE_MAPPING: dict[str, list[str]] = {

        "purchase": ["خرید", "purchase"],
        "trade":    ["معامله", "قرارداد", "trade", "contract"],
        "offer":    ["عرضه", "offer"],
        "order":    ["سفارش", "order"],
        "customer": ["مشتری", "خریدار", "customer"],
        "supplier": ["عرضه کننده", "فروشنده", "supplier"],
        "broker":   ["کارگزار", "broker"],
        "symbol":   ["نماد", "کالا", "محصول", "symbol"],
        "date":     ["تاریخ", "سال", "ماه", "فصل", "date", "year", "month"],
        "ring":     ["تالار", "رینگ", "ring"],
        "topn":     ["بیشترین", "برتر", "بالاترین", "top", "highest"]
    }

    @classmethod
    def retrieve(cls, question: str) -> list[str]:

        question_lower = question.lower()

        results = []

        for rule_key, aliases in cls.RULE_MAPPING.items():

            for alias in aliases:

                if alias.lower() in question_lower:

                    rule = BUSINESS_RULES.get(rule_key)

                    if rule and rule not in results:
                        results.append(rule)

                    break

        return results
