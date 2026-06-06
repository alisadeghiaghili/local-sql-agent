"""Unit tests for logs/logger.py."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from unittest.mock import patch

import pytest

from logs.query_log import QueryLog


def _make_log(**kwargs) -> QueryLog:
    defaults = dict(
        timestamp=datetime(2026, 6, 6, 10, 0, 0),
        question="test question",
        generated_sql="SELECT 1",
        model_name="test-model",
        status="SUCCESS",
        execution_time_seconds=0.5,
    )
    defaults.update(kwargs)
    return QueryLog(**defaults)


class TestSaveLog:
    def test_creates_log_file(self, tmp_path):
        log_file = tmp_path / "query_log.jsonl"
        with patch("logs.logger._LOG_FILE", str(log_file)), \
             patch("logs.logger.settings") as mock_settings:
            mock_settings.log_dir = str(tmp_path)
            from logs.logger import save_log
            save_log(_make_log())
        assert log_file.exists()

    def test_writes_valid_json_line(self, tmp_path):
        log_file = tmp_path / "query_log.jsonl"
        with patch("logs.logger._LOG_FILE", str(log_file)), \
             patch("logs.logger.settings") as mock_settings:
            mock_settings.log_dir = str(tmp_path)
            from logs.logger import save_log
            log = _make_log(row_count=10, excel_file="/tmp/x.xlsx")
            save_log(log)
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["status"] == "SUCCESS"
        assert record["row_count"] == 10
        assert record["question"] == "test question"

    def test_appends_multiple_records(self, tmp_path):
        log_file = tmp_path / "query_log.jsonl"
        with patch("logs.logger._LOG_FILE", str(log_file)), \
             patch("logs.logger.settings") as mock_settings:
            mock_settings.log_dir = str(tmp_path)
            from logs.logger import save_log
            save_log(_make_log(question="q1"))
            save_log(_make_log(question="q2"))
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["question"] == "q1"
        assert json.loads(lines[1])["question"] == "q2"

    def test_unicode_preserved(self, tmp_path):
        log_file = tmp_path / "query_log.jsonl"
        with patch("logs.logger._LOG_FILE", str(log_file)), \
             patch("logs.logger.settings") as mock_settings:
            mock_settings.log_dir = str(tmp_path)
            from logs.logger import save_log
            save_log(_make_log(question="بیشترین خرید مشتریان"))
        line = log_file.read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert record["question"] == "بیشترین خرید مشتریان"

    def test_oserror_does_not_raise(self, tmp_path):
        """logger must swallow OSError and not crash the caller."""
        with patch("logs.logger._LOG_FILE", "/dev/null/invalid/path"), \
             patch("logs.logger.settings") as mock_settings:
            mock_settings.log_dir = "/dev/null/invalid"
            from logs.logger import save_log
            # should not raise
            save_log(_make_log())
