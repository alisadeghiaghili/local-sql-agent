# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for logs/query_log.py."""

from __future__ import annotations

from datetime import datetime

from logs.query_log import QueryLog


class TestQueryLog:
    def _make(self, **kwargs) -> QueryLog:
        defaults = dict(
            timestamp=datetime(2026, 6, 6, 12, 0, 0),
            question="how many contracts?",
            generated_sql="SELECT COUNT(*) FROM [Auction_Fact].[Contract]",
            model_name="test-model",
            status="SUCCESS",
            execution_time_seconds=1.23,
        )
        defaults.update(kwargs)
        return QueryLog(**defaults)

    def test_as_dict_keys(self):
        log = self._make()
        d = log.as_dict()
        expected_keys = {
            "timestamp", "question", "generated_sql", "model_name",
            "status", "execution_time_seconds", "row_count",
            "excel_file", "error_message",
        }
        assert set(d.keys()) == expected_keys

    def test_timestamp_is_iso_string(self):
        log = self._make()
        d = log.as_dict()
        # must be parseable back to datetime
        parsed = datetime.fromisoformat(d["timestamp"])
        assert parsed == log.timestamp

    def test_default_row_count_is_zero(self):
        log = self._make()
        assert log.row_count == 0
        assert log.as_dict()["row_count"] == 0

    def test_default_excel_file_is_none(self):
        log = self._make()
        assert log.excel_file is None
        assert log.as_dict()["excel_file"] is None

    def test_default_error_message_is_none(self):
        log = self._make()
        assert log.error_message is None

    def test_status_success(self):
        log = self._make(status="SUCCESS")
        assert log.as_dict()["status"] == "SUCCESS"

    def test_status_error(self):
        log = self._make(status="ERROR", error_message="db down")
        assert log.as_dict()["status"] == "ERROR"
        assert log.as_dict()["error_message"] == "db down"

    def test_status_out_of_scope(self):
        log = self._make(status="OUT_OF_SCOPE")
        assert log.as_dict()["status"] == "OUT_OF_SCOPE"

    def test_excel_file_and_row_count(self):
        log = self._make(excel_file="/tmp/result.xlsx", row_count=42)
        d = log.as_dict()
        assert d["excel_file"] == "/tmp/result.xlsx"
        assert d["row_count"] == 42

    def test_as_dict_is_json_serialisable(self):
        import json
        log = self._make(excel_file="/tmp/x.xlsx", row_count=5)
        # must not raise
        json.dumps(log.as_dict())
