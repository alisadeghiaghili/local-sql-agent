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
    _tables_in_sql,
    analyse,
)


# ---------------------------------------------------------------------------
# _tables_in_sql
# ---------------------------------------------------------------------------

class TestTablesInSql:
    def test_detects_contract(self):
        sql = "SELECT TOP 10 * FROM [Auction_Fact].[Contract]"
        assert "Contract" in _tables_in_sql(sql)

    def test_detects_customer(self):
        sql = "SELECT Name FROM [Auction_Dim].[Customer]"
        assert "Customer" in _tables_in_sql(sql)

    def test_detects_join(self):
        sql = (
            "SELECT c.Name FROM [Auction_Dim].[Customer] c "
            "JOIN [Auction_Fact].[Contract] ct ON c.CustomerID = ct.CustomerID"
        )
        result = _tables_in_sql(sql)
        assert "Customer" in result
        assert "Contract" in result

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
        # 'مشتری' is already in Customer description
        candidates = _candidate_tokens("مشتری")
        assert "مشتری" not in candidates

    def test_filters_existing_synonym_keys(self):
        # 'بهار' is already a synonym key
        candidates = _candidate_tokens("بهار")
        assert "بهار" not in candidates

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
        # Ring is retrieved for 'تالار'
        log = self._write_log(tmp_path, [{
            "status":        "SUCCESS",
            "question":      "تالار",
            "generated_sql": "SELECT TOP 5 * FROM [Auction_Dim].[Ring]",
        }])
        result = analyse(log)
        assert result == []

    def test_detects_miss_when_table_not_retrieved(self, tmp_path):
        # Use a question that is very unlikely to retrieve 'Bank'
        log = self._write_log(tmp_path, [{
            "status":        "SUCCESS",
            "question":      "چیز بسیار نامشناس",
            "generated_sql": "SELECT * FROM [Auction_Dim].[Bank]",
        }])
        result = analyse(log)
        # Bank should be in the missing list
        assert any("Bank" in m.missing for m in result)

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
