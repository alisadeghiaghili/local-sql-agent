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
        "sales"
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
        "گزارش عملیات"
    ]
}


class FactRetriever:

    @staticmethod
    def retrieve(question: str):

        question = question.lower()

        matches = []

        for fact, aliases in FACT_PATTERNS.items():

            for alias in aliases:

                if alias.lower() in question:

                    matches.append(fact)

                    break

        return matches