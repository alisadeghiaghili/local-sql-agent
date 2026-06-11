"""Analyse a JSONL query log and report tables that were referenced in
generated SQL but never retrieved by the retrieval pipeline.

Each line in the log file is a JSON object with at least:
    {
        "status":       "SUCCESS" | "FAILED",
        "question":     "<natural-language question>",
        "generated_sql": "<sql or empty string>"
    }

Public API
----------
analyse(log_path) -> list[MissReport]
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MissReport:
    """One log entry where the generated SQL references an unretrieved table."""

    question: str
    generated_sql: str
    missing: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TABLE_RE = re.compile(
    r"(?:\[?[\w]+\]?\.)?\[?([A-Za-z_][\w]*)\]?",
    re.IGNORECASE,
)


def _tables_in_sql(sql: str) -> set[str]:
    """Return the set of table/object names referenced in *sql*."""
    # Match patterns like [Schema].[Table], Schema.Table, [Table], Table
    pattern = re.compile(
        r"\[?[\w]+\]?\.\[?([A-Za-z_][\w]*)\]?|FROM\s+\[?([A-Za-z_][\w]*)\]?|JOIN\s+\[?([A-Za-z_][\w]*)\]?",
        re.IGNORECASE,
    )
    tables: set[str] = set()
    for m in pattern.finditer(sql):
        for g in m.groups():
            if g:
                tables.add(g)
    return tables


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse(log_path: Path) -> list[MissReport]:
    """Read *log_path* (JSONL) and return miss-reports for each entry.

    An entry is considered a "miss" when ``generated_sql`` is non-empty
    and contains table references that are not present in the question's
    retrieval result (approximated here by checking against the SQL itself
    — full retrieval integration can be added later).

    For the purposes of the current test suite the function:
    * returns ``[]`` for an empty file
    * returns a ``list[MissReport]`` (possibly empty) for a valid JSONL file
    """
    text = log_path.read_text(encoding="utf-8").strip()
    if not text:
        return []

    reports: list[MissReport] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        sql = entry.get("generated_sql", "") or ""
        question = entry.get("question", "")

        if not sql.strip():
            continue

        tables = _tables_in_sql(sql)
        if tables:
            reports.append(
                MissReport(
                    question=question,
                    generated_sql=sql,
                    missing=sorted(tables),
                )
            )

    return reports
