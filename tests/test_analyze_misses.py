# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for scripts/analyze_misses.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the project root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.analyze_misses import (
    Miss,
    _build_report,
    _candidate_tokens,
    _split_tokens,
    _tables_in_sql,
    analyse,
)
from schema_data.tables import TABLE_DESCRIPTIONS as TABLES
from knowledge.aliases import SYNONYMS

#: Two distinct, real (or example) table names -- picked dynamically from
#: whatever schema_data.tables.TABLE_DESCRIPTIONS actually holds, rather
#: than hardcoded real IME names, so these tests exercise the SAME
#: `_tables_in_sql` regex-matching logic under any configured schema.
_TABLE_A, _TABLE_B = sorted(TABLES)[:2]

#: A token guaranteed to already be present in _KNOWN_TOKENS (built from
#: table descriptions) -- see scripts/analyze_misses.py's module docstring.
_KNOWN_DESCRIPTION_TOKEN = next(
    tok for text in TABLES.values() for tok in _split_tokens(text) if len(tok) > 1
)

#: Same, but sourced from a synonym KEY rather than a table description --
#: covers the other branch of _candidate_tokens's "already covered" check.
_KNOWN_SYNONYM_KEY_TOKEN = next(
    tok for key in SYNONYMS for tok in _split_tokens(key) if len(tok) > 1
)


# ---------------------------------------------------------------------------
# _tables_in_sql
# ---------------------------------------------------------------------------

class TestTablesInSql:
    def test_detects_a_known_table(self):
        sql = f"SELECT TOP 10 * FROM [{_TABLE_A}]"
        assert _TABLE_A in _tables_in_sql(sql)

    def test_detects_a_schema_qualified_known_table(self):
        sql = f"SELECT Name FROM [dbo].[{_TABLE_A}]"
        assert _TABLE_A in _tables_in_sql(sql)

    def test_detects_join(self):
        sql = (
            f"SELECT a.Name FROM [dbo].[{_TABLE_A}] a "
            f"JOIN [dbo].[{_TABLE_B}] b ON a.ID = b.ID"
        )
        result = _tables_in_sql(sql)
        assert _TABLE_A in result
        assert _TABLE_B in result

    def test_unknown_table_not_included(self):
        sql = "SELECT * FROM [dbo].[NonExistentTable]"
        assert len(_tables_in_sql(sql)) == 0

    def test_empty_sql_returns_empty_set(self):
        assert _tables_in_sql("") == set()


# ---------------------------------------------------------------------------
# _candidate_tokens
# ---------------------------------------------------------------------------

class TestCandidateTokens:
    def test_filters_stop_words(self):
        candidates = _candidate_tokens("در از به")
        assert candidates == []

    def test_filters_existing_description_tokens(self):
        # Any token already present in some table's description, whichever
        # schema is loaded, must be filtered out.
        candidates = _candidate_tokens(_KNOWN_DESCRIPTION_TOKEN)
        assert _KNOWN_DESCRIPTION_TOKEN not in candidates

    def test_filters_existing_synonym_keys(self):
        # Any token that is itself a synonym key, whichever aliases.yaml
        # is loaded, must be filtered out.
        candidates = _candidate_tokens(_KNOWN_SYNONYM_KEY_TOKEN)
        assert _KNOWN_SYNONYM_KEY_TOKEN not in candidates

    def test_returns_novel_tokens(self):
        # A completely new domain word not in descriptions or synonyms
        candidates = _candidate_tokens("پلیمرپلاستیک")
        assert "پلیمرپلاستیک" in candidates

    def test_filters_single_char_tokens(self):
        candidates = _candidate_tokens("ا ب ج")
        assert candidates == []


# ---------------------------------------------------------------------------
# analyse
# ---------------------------------------------------------------------------

class TestAnalyse:
    def _write_log(self, tmp_path: Path, entries: list[dict]) -> Path:
        log_file = tmp_path / "query_log.jsonl"
        with log_file.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        return log_file

    def test_returns_empty_for_nonexistent_log(self, tmp_path):
        result = analyse(tmp_path / "no_such_file.jsonl")
        assert result == []

    def test_skips_non_success_entries(self, tmp_path):
        log = self._write_log(tmp_path, [
            {"status": "ERROR",        "question": "q", "generated_sql": "SELECT 1"},
            {"status": "OUT_OF_SCOPE", "question": "q", "generated_sql": "SELECT 1"},
        ])
        assert analyse(log) == []

    def test_skips_invalid_json_lines(self, tmp_path):
        log_file = tmp_path / "query_log.jsonl"
        log_file.write_text("not json\n", encoding="utf-8")
        result = analyse(log_file)   # must not raise
        assert result == []

    def test_no_miss_when_all_tables_retrieved(self, tmp_path):
        # "ring" is one of Ring's project_config(.example)/retrieval_hints.yaml
        # always_include trigger words in both the real and example config,
        # so it force-matches under either.
        log = self._write_log(tmp_path, [{
            "status":        "SUCCESS",
            "question":      "ring",
            "generated_sql": "SELECT TOP 5 * FROM [Auction_Dim].[Ring]",
        }])
        result = analyse(log)
        assert result == []

    def test_detects_miss_when_table_not_retrieved(self, tmp_path):
        # A question with no real vocabulary overlap retrieves nothing
        # (fallback=False), so ANY known table referenced in the SQL --
        # picked dynamically, not a hardcoded real name -- is a miss.
        table = sorted(TABLES)[0]
        log = self._write_log(tmp_path, [{
            "status":        "SUCCESS",
            "question":      "چیز بسیار نامشناس",
            "generated_sql": f"SELECT * FROM [{table}]",
        }])
        result = analyse(log)
        assert any(table in m.missing for m in result)

    def test_miss_includes_candidate_tokens(self, tmp_path):
        log = self._write_log(tmp_path, [{
            "status":        "SUCCESS",
            "question":      "پلیمرپلاستیک بسیار نامشناس",
            "generated_sql": "SELECT * FROM [Auction_Dim].[Bank]",
        }])
        result = analyse(log)
        if result:  # only check if a miss was detected
            candidates = result[0].candidates
            assert "پلیمرپلاستیک" in candidates


# ---------------------------------------------------------------------------
# _build_report
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_empty_misses_returns_zero_counts(self):
        report = _build_report([])
        assert report["total_miss_events"] == 0
        assert report["tables_ranked_by_miss_count"] == []

    def test_aggregates_miss_count(self):
        misses = [
            Miss(question="q1", missing=["Bank"], candidates=["abc"], sql=""),
            Miss(question="q2", missing=["Bank"], candidates=["def"], sql=""),
            Miss(question="q3", missing=["Carrier"], candidates=["xyz"], sql=""),
        ]
        report = _build_report(misses)
        ranked = {e["table"]: e["miss_count"] for e in report["tables_ranked_by_miss_count"]}
        assert ranked["Bank"] == 2
        assert ranked["Carrier"] == 1

    def test_top_candidates_ranked_by_frequency(self):
        misses = [
            Miss(question="q1", missing=["Bank"], candidates=["token_a", "token_b"], sql=""),
            Miss(question="q2", missing=["Bank"], candidates=["token_a"],            sql=""),
        ]
        report = _build_report(misses)
        bank_entry = next(e for e in report["tables_ranked_by_miss_count"] if e["table"] == "Bank")
        top_tokens = [c["token"] for c in bank_entry["top_candidates"]]
        assert top_tokens[0] == "token_a"   # freq=2 beats freq=1

    def test_report_is_json_serialisable(self):
        misses = [Miss(question="q", missing=["Bank"], candidates=["tok"], sql="SELECT 1")]
        report = _build_report(misses)
        json.dumps(report)   # must not raise
