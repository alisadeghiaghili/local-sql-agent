"""Offline analysis script: find questions where the retriever misses expected tables.

Usage::

    python scripts/analyze_misses.py [LOG_PATH]

If LOG_PATH is omitted, defaults to ``logs/query_log.jsonl``.

The script reads a JSONL query-log file produced by ``logs/logger.py``,
identifies SUCCESS entries where the generated SQL references a table that
the retriever did *not* surface, and reports which novel question tokens could
be added to ``knowledge/aliases.py`` to close the gap.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema_data.retriever import retrieve_tables
from knowledge.aliases import SYNONYMS
from schema_data.tables import TABLE_DESCRIPTIONS as TABLES

# ---------------------------------------------------------------------------
# Persian stop-words (closed-class tokens to skip during candidate analysis)
# ---------------------------------------------------------------------------
_STOP: frozenset[str] = frozenset(
    "در از به با که این آن را برای تا هم هر چه یا اما ولی چون"
    " اگر پس بر روی زیر بین بالا پایین همه هیچ خود مثل مانند"
    " باید شاید شد شده است بود بودن کردن کرد دارد داشت داشتن"
    " می‌شود می‌کند می‌دهد می‌شد می‌کرد بر اساس نسبت به طور".split()
)

# Pre-compute the union of all tokens already covered by descriptions + synonyms.
# We extract tokens from BOTH the Persian and English parts of each description
# so that a Persian word like مشتری (which appears in the Customer description
# as "بایر / customer master data") is recognised as already-known.
_KNOWN_TOKENS: frozenset[str] = frozenset(
    token
    for text in (*TABLES.values(), *SYNONYMS.keys())
    for token in re.split(r"[\s\u060c,;/\-\u2014\u2013—]+", text)
    if len(token) > 1
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class Miss:
    """One retrieval miss detected from a query-log entry."""
    question:   str
    missing:    list[str]
    candidates: list[str]
    sql:        str


def _tables_in_sql(sql: str) -> set[str]:
    """Return the set of *known* table names referenced in *sql*.

    Matches bracketed SQL Server identifiers: ``[Schema].[TableName]``
    and plain unbracketed references, then filters against the known
    ``TABLES`` catalogue.
    """
    if not sql:
        return set()
    # Match [Schema].[Table] or bare [Table] or bare TableName
    pattern = re.compile(
        r"\[\w+\]\.\[(\w+)\]"  # [Schema].[Table]  — capture Table
        r"|\[(\w+)\]"           # [Table]
        r"|(\b[A-Z][A-Za-z]+\b)"  # BareTableName
    )
    found: set[str] = set()
    for m in pattern.finditer(sql):
        name = m.group(1) or m.group(2) or m.group(3) or ""
        if name and name in TABLES:
            found.add(name)
    return found


def _candidate_tokens(question: str) -> list[str]:
    """Return tokens from *question* that are not yet covered by the knowledge base.

    Filters out:
    - Single-character tokens
    - Persian stop-words
    - Tokens already present in ``TABLES`` descriptions or ``SYNONYMS`` keys
    """
    tokens = re.split(r"[\s\u060c,;/\-]+", question.strip())
    result: list[str] = []
    for tok in tokens:
        tok = tok.strip()
        if len(tok) <= 1:
            continue
        if tok in _STOP:
            continue
        if tok in _KNOWN_TOKENS:
            continue
        result.append(tok)
    return result


def analyse(log_path: Path) -> list[Miss]:
    """Read a JSONL query-log file and return a list of :class:`Miss` objects.

    Only ``SUCCESS`` entries are inspected. An entry is a miss if the SQL it
    produced references at least one table that ``retrieve_tables`` would *not*
    have surfaced for the same question.

    Uses ``fallback=False`` so that questions with no vocabulary match do **not**
    silently return every table — those are the most important misses to surface.
    """
    if not log_path.exists():
        return []

    misses: list[Miss] = []
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("status") != "SUCCESS":
                continue

            question = entry.get("question", "")
            sql      = entry.get("generated_sql", "")

            tables_in_sql = _tables_in_sql(sql)
            if not tables_in_sql:
                continue

            # fallback=False: an unrecognised question returns [] instead of
            # every table, so genuine vocabulary gaps are detected as misses.
            retrieved = set(retrieve_tables(question, fallback=False))
            missing   = sorted(tables_in_sql - retrieved)
            if not missing:
                continue

            misses.append(Miss(
                question   = question,
                missing    = missing,
                candidates = _candidate_tokens(question),
                sql        = sql,
            ))

    return misses


def _build_report(misses: list[Miss]) -> dict[str, Any]:
    """Aggregate *misses* into a structured report dict.

    Returns a JSON-serialisable mapping with:
    - ``total_miss_events``  — number of Miss objects
    - ``tables_ranked_by_miss_count`` — list of
      ``{table, miss_count, top_candidates}`` sorted descending by miss_count
    """
    table_counter: Counter[str] = Counter()
    # table -> Counter of candidate tokens
    candidate_by_table: dict[str, Counter[str]] = {}

    for miss in misses:
        for table in miss.missing:
            table_counter[table] += 1
            if table not in candidate_by_table:
                candidate_by_table[table] = Counter()
            candidate_by_table[table].update(miss.candidates)

    ranked = []
    for table, count in table_counter.most_common():
        top_cands = [
            {"token": tok, "frequency": freq}
            for tok, freq in candidate_by_table[table].most_common(10)
        ]
        ranked.append({
            "table":          table,
            "miss_count":     count,
            "top_candidates": top_cands,
        })

    return {
        "total_miss_events":         len(misses),
        "tables_ranked_by_miss_count": ranked,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("logs/query_log.jsonl")
    misses   = analyse(log_path)

    if not misses:
        print("✅  No retrieval misses found.")
        return

    report = _build_report(misses)
    print(f"\n🔍  {report['total_miss_events']} miss event(s) detected\n")
    print("-" * 60)
    for entry in report["tables_ranked_by_miss_count"]:
        print(f"  Table : {entry['table']}  (missed {entry['miss_count']}x)")
        for cand in entry["top_candidates"]:
            print(f"    candidate token: {cand['token']!r}  (freq={cand['frequency']})")
    print("-" * 60)


if __name__ == "__main__":
    main()
