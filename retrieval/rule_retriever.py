from knowledge.business_rules import BUSINESS_RULES


class RuleRetriever:

    RULE_MAPPING = {

        "purchase": [
            "خرید",
            "purchase"
        ],

        "trade": [
            "معامله",
            "trade"
        ],

        "offer": [
            "عرضه",
            "offer"
        ],

        "customer": [
            "مشتری",
            "خریدار"
        ],

        "supplier": [
            "عرضه کننده",
            "فروشنده"
        ],

        "topn": [
            "بیشترین",
            "برتر",
            "top"
        ]
    }

    @classmethod
    def retrieve(cls, question):

        question = question.lower()

        results = []

        for rule_key, aliases in cls.RULE_MAPPING.items():

            for alias in aliases:

                if alias.lower() in question:

                    rule = BUSINESS_RULES.get(rule_key)

                    if rule:

                        results.append(rule)

                    break

        return results