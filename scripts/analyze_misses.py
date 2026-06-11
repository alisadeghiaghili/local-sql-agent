#!/usr/bin/env python
"""Synonym-gap analyser for local-sql-agent.

Reads ``logs/query_log.jsonl`` (or a custom path) and detects questions
where the retriever likely returned a **suboptimal table set** — i.e.
the generated SQL references tables that were *not* retrieved, forcing
the LLM to guess from incomplete schema context.

Output
------
- A human-readable console report grouped by missing table.
- An **actionable synonym candidate list** — tokens from the failing
  questions that are not yet in ``schema/synonyms.py``.
- Optionally writes a JSON report to ``logs/synonym_gaps.json``.

Usage
-----
::

    # analyse default log file
    python scripts/analyze_misses.py

    # custom log path + write JSON report
    python scripts/analyze_misses.py --log logs/query_log.jsonl --out logs/gaps.json

    # only show questions where >= 2 tables were missed
    python scripts/analyze_misses.py --min-misses 2

    # dry-run: print candidate synonyms but don't write anything
    python scripts/analyze_misses.py --dry-run

How it works
------------
1. For every SUCCESS entry in the log, extract the table names that
   appear in the generated SQL (``[Schema].[TableName]`` pattern).
   Only names that exist in TABLES are considered valid — unknown
   table names extracted from SQL are silently ignored.
2. Run ``retrieve_tables(question)`` with the *current* retriever to
   see what tables would be selected today.
3. Tables that appear in SQL but were NOT retrieved = **misses**.
4. Tokenise the question; subtract tokens already in
   ``schema/synonyms.py`` keys and in any table description.
   The leftover tokens are **synonym candidates**.
5. Aggregate by (missing table, candidate token) and rank by frequency.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

# Ensure project root is importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema.retriever import retrieve_tables
from schema.synonyms import SYNONYMS
from schema.tables import TABLES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Matches [Schema].[TableName] or bare TableName in FROM / JOIN clauses.
_SQL_TABLE_RE = re.compile(
    r"(?:FROM|JOIN)\s+(?:\[?\w+\]?\.){0,2}\[?(\w+)\]?",
    re.IGNORECASE,
)

# Known logical table names (lowercase → canonical) for fast lookup
_KNOWN_TABLES_LOWER: dict[str, str] = {
    name.lower(): name for name in TABLES
}

# Stop-words: tiny words that are never useful as synonym candidates.
_STOP_FA = (
    "\u062f\u0631 \u0627\u0632 \u0628\u0647 \u0648 \u06cc\u0627 \u0627 \u0628\u0627 \u0628\u0631\u0627\u06cc \u0628\u0631 \u062a\u0627 \u06a9\u0647"
    " \u0627\u06cc\u0646 \u0622\u0646 \u0647\u0627 \u0647\u0627\u06cc \u0645\u06cc \u0646\u0647 \u0647\u0645 \u0647\u0645\u0647 \u0686\u0647 \u0686\u0646\u062f"
    " \u0686\u06cc\u0633\u062a \u06a9\u062f\u0627\u0645"
)
_STOP_EN = (
    "the a an in on at of to is are was were be been"
    " for and or not how many which what when where who whose"
)
_STOP: frozenset[str] = frozenset(_STOP_FA.split() + _STOP_EN.split())

# Build a flat set of all tokens already in descriptions (no need to suggest them).
_DESCRIPTION_TOKENS: frozenset[str] = frozenset(
    token
    for info in TABLES.values()
    for token in info["description"].lower().split()
)

# All current synonym keys (lowercase)
_SYNONYM_KEYS: frozenset[str] = frozenset(SYNONYMS.keys())


class Miss(NamedTuple):
    question:    str
    missing:     list[str]   # table names missed by retriever
    candidates:  list[str]   # token candidates for new synonyms
    sql:         str


def _tables_in_sql(sql: str) -> set[str]:
    """Return logical table names referenced in *sql* (best-effort).

    Only names that exist in the TABLES registry are returned.
    Unknown names (e.g. from a different schema or a hallucinated table)
    are silently ignored so they do not pollute miss analysis.
    """
    found: set[str] = set()
    for m in _SQL_TABLE_RE.finditer(sql):
        raw_name = m.group(1)
        canonical = _KNOWN_TABLES_LOWER.get(raw_name.lower())
        if canonical is not None:
            found.add(canonical)
    return found


def _candidate_tokens(question: str) -> list[str]:
    """Return tokens from *question* that could become new synonym keys."""
    tokens = question.lower().split()
    return [
        t for t in tokens
        if len(t) >= 2
        and t not in _STOP
        and t not in _DESCRIPTION_TOKENS
        and t not in _SYNONYM_KEYS
    ]


def analyse(log_path: Path) -> list[Miss]:
    """Parse *log_path* and return all entries where tables were missed."""
    if not log_path.exists():
        print(f"[warn] Log file not found: {log_path}", file=sys.stderr)
        return []

    misses: list[Miss] = []

    with log_path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[warn] line {lineno}: invalid JSON — {exc}", file=sys.stderr)
                continue

            if entry.get("status") != "SUCCESS":
                continue
            question = entry.get("question", "").strip()
            sql      = entry.get("generated_sql", "").strip()
            if not question or not sql:
                continue

            sql_tables = _tables_in_sql(sql)
            if not sql_tables:
                # SQL references no known tables — nothing to analyse
                continue

            # Use fallback=False so an out-of-domain question returns []
            # which means ALL sql_tables are "missed" (correct behaviour)
            retrieved_tables = set(retrieve_tables(question, fallback=False))
            missed           = sorted(sql_tables - retrieved_tables)

            if missed:
                misses.append(Miss(
                    question   = question,
                    missing    = missed,
                    candidates = _candidate_tokens(question),
                    sql        = sql,
                ))

    return misses


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _build_report(misses: list[Miss]) -> dict:
    """Aggregate misses into a structured report dict."""
    table_miss_count: Counter[str] = Counter()
    table_candidates: dict[str, Counter[str]] = defaultdict(Counter)

    for m in misses:
        for table in m.missing:
            table_miss_count[table] += 1
            for token in m.candidates:
                table_candidates[table][token] += 1

    tables_ranked = [
        {
            "table":          table,
            "miss_count":     count,
            "top_candidates": [
                {"token": tok, "freq": freq}
                for tok, freq in table_candidates[table].most_common(10)
            ],
        }
        for table, count in table_miss_count.most_common()
    ]

    return {
        "total_success_entries_analysed": len(misses),
        "total_miss_events":              len(misses),
        "tables_ranked_by_miss_count":    tables_ranked,
        "all_misses": [
            {
                "question":   m.question,
                "missing":    m.missing,
                "candidates": m.candidates,
            }
            for m in misses
        ],
    }


def _print_report(report: dict, min_misses: int) -> None:
    sep = "=" * 64
    print(sep)
    print(" Synonym Gap Report")
    print(sep)
    print(f" Total miss events : {report['total_miss_events']}")
    print()

    for entry in report["tables_ranked_by_miss_count"]:
        if entry["miss_count"] < min_misses:
            continue
        print(f"  \u2502 Table : {entry['table']}  (missed {entry['miss_count']}x)")
        if entry["top_candidates"]:
            print("  \u2502 Suggested synonym candidates (add to schema/synonyms.py):")
            for c in entry["top_candidates"]:
                print(
                    f"  \u2502   {c['token']!r:30s}"
                    f"  \u2192  [\"{entry['table'].lower()}\"]   # freq={c['freq']}"
                )
        else:
            print("  \u2502 No new candidate tokens found (already in descriptions/synonyms)")
        print("  \u2502")

    if report["total_miss_events"] == 0:
        print("  \u2714 No misses detected \u2014 synonym coverage looks complete!")
    print(sep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Detect synonym gaps from query_log.jsonl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--log",
        default="logs/query_log.jsonl",
        metavar="PATH",
        help="Path to the JSONL query log (default: logs/query_log.jsonl)",
    )
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Write JSON report to this path (optional)",
    )
    p.add_argument(
        "--min-misses",
        type=int,
        default=1,
        metavar="N",
        help="Only show tables missed >= N times (default: 1)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print report but do not write any output file",
    )
    return p.parse_args()


def main() -> None:
    args     = _parse_args()
    log_path = Path(args.log)

    print(f"Analysing: {log_path.resolve()}")
    misses = analyse(log_path)
    report = _build_report(misses)

    _print_report(report, min_misses=args.min_misses)

    if args.out and not args.dry_run:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON report written to: {out_path.resolve()}")


if __name__ == "__main__":
    main()
