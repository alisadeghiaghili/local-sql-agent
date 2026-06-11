import re
from knowledge.aliases import RING_ALIASES


class ValueRetriever:

    @staticmethod
    def extract_ring(question: str):
        for canonical_name, aliases in RING_ALIASES.items():
            for alias in aliases:
                if alias in question:
                    return canonical_name
        return None

    @staticmethod
    def extract_year(question: str):
        match = re.search(r"\b(13\d{2}|14\d{2})\b", question)
        return int(match.group(1)) if match else None

    @classmethod
    def retrieve(cls, question: str) -> dict:
        filters = {}
        ring = cls.extract_ring(question)
        if ring:
            filters["Ring"] = ring
        year = cls.extract_year(question)
        if year:
            filters["PersianYear"] = year
        return filters
