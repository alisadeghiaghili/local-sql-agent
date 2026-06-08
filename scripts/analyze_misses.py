#!/usr/bin/env python
"""Synonym-gap analyser for local-sql-agent."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema.retriever import retrieve_tables
from schema.synonyms import SYNONYMS
from schema.tables import TABLES

_SQL_TABLE_RE = re.compile(
    r"(?:FROM|JOIN)\s+(?:\[?\w+\]?\.)*\[?(\w+)\]?",
    re.IGNORECASE,
)

_STOP_FA = (
    "در از به و یا ا با برای بر تا که"
    " این آن ها های می نه هم همه چه چند"
    " چیست کدام"
)
_STOP_EN = (
    "the a an in on at of to is are was were be been"
    " for and or not how many which what when where who whose"
)
_STOP: frozenset[str] = frozenset(_STOP_FA.split() + _STOP_EN.split())

_DESCRIPTION_TOKENS: frozenset[str] = frozenset(
    token
    for info in TABLES.values()
    for token in info["description"].lower().split()
)

_SYNONYM_KEYS: frozenset[str] = frozenset(SYNONYMS.keys())


class Miss(NamedTuple):
    question:   str
    missing:    list[str]
    candidates: list[str]
    sql:        str


def _tables_in_sql(sql: str) -> set[str]:
    """Return table names referenced in *sql* via FROM/JOIN clauses.

    Unlike the previous version, this no longer filters by TABLES — it returns
    every name matched by the regex so that the analyser can detect tables that
    the retriever missed *even when those tables exist in the schema registry*.
    The caller (analyse) is responsible for deciding what counts as a miss.
    """
    found: set[str] = set()
    for m in _SQL_TABLE_RE.finditer(sql):
        name = m.group(1)
        if name:  # skip empty captures
            found.add(name)
    return found


def _candidate_tokens(question: str) -> list[str]:
    tokens = question.lower().split()
    return [
        t for t in tokens
        if len(t) >= 2
        and t not in _STOP
        and t not in _DESCRIPTION_TOKENS
        and t not in _SYNONYM_KEYS
    ]


def analyse(log_path: Path) -> list[Miss]:
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

            sql_tables       = _tables_in_sql(sql)
            retrieved_tables = set(retrieve_tables(question))
            missed           = sorted(sql_tables - retrieved_tables)

            if missed:
                misses.append(Miss(
                    question   = question,
                    missing    = missed,
                    candidates = _candidate_tokens(question),
                    sql        = sql,
                ))

    return misses


def _build_report(misses: list[Miss]) -> dict:
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
            {"question": m.question, "missing": m.missing, "candidates": m.candidates}
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
        print(f"  │ Table : {entry['table']}  (missed {entry['miss_count']}x)")
        if entry["top_candidates"]:
            print("  │ Suggested synonym candidates:")
            for c in entry["top_candidates"]:
                print(f"  │   {c['token']!r:30s}  →  freq={c['freq']}")
        else:
            print("  │ No new candidate tokens found")
        print("  │")
    if report["total_miss_events"] == 0:
        print("  ✔ No misses detected — synonym coverage looks complete!")
    print(sep)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Detect synonym gaps from query_log.jsonl")
    p.add_argument("--log", default="logs/query_log.jsonl", metavar="PATH")
    p.add_argument("--out", default=None, metavar="PATH")
    p.add_argument("--min-misses", type=int, default=1, metavar="N")
    p.add_argument("--dry-run", action="store_true")
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
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON report written to: {out_path.resolve()}")


if __name__ == "__main__":
    main()
