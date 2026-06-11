FACT_PATTERNS = {

    "CustomerContract": [
        "خرید",
        "purchase",
        "customer purchase",
        "خریدار"
    ],

    "Contract": [
        "معامله",
        "قرارداد",
        "trade",
        "sales",
        "contract"
    ],

    "Offer": [
        "عرضه",
        "offer",
        "supply"
    ],

    "Order": [
        "سفارش",
        "order"
    ],

    "TalarLog": [
        "لاگ",
        "گزارش عملیات",
        "audit"
    ]
}


class FactRetriever:

    @staticmethod
    def retrieve(question: str) -> list[str]:

        question_lower = question.lower()

        matches = []

        for fact, aliases in FACT_PATTERNS.items():

            for alias in aliases:

                if alias.lower() in question_lower:

                    matches.append(fact)

                    break

        return matches
