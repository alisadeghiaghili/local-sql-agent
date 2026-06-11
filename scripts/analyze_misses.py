"""Offline analysis script: find questions where the retriever misses expected tables.

Usage::

    python scripts/analyze_misses.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema_data.retriever import retrieve_tables
from knowledge.aliases import SYNONYMS
from schema_data.tables import TABLE_DESCRIPTIONS as TABLES

TEST_CASES: list[tuple[str, list[str]]] = [
    ("قرارداد مشتری در تالار پتروشیمی", ["CustomerContract", "Ring"]),
    ("حجم معاملات امسال", ["Contract", "Date"]),
    ("بیشترین خریداران در فصل بهار", ["CustomerContract", "Date"]),
    ("کارگزاران فعال در رینگ فلزات", ["Broker", "Ring"]),
    ("ارزش عرضه کالا به تفکیک نماد", ["Offer", "Symbol"]),
]


def main() -> None:
    misses = 0
    for question, expected in TEST_CASES:
        result = retrieve_tables(question)
        missing = [t for t in expected if t not in result]
        status = "✅" if not missing else "❌"
        print(f"{status}  Q: {question}")
        if missing:
            print(f"     Retrieved : {result}")
            print(f"     Missing   : {missing}")
            misses += 1
    print(f"\n{len(TEST_CASES) - misses}/{len(TEST_CASES)} passed")


if __name__ == "__main__":
    main()
