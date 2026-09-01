# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for observability/audit.py."""

from __future__ import annotations

import json
import os
from datetime import datetime
from unittest.mock import patch

import pytest

from observability.audit import AuditRecord, save_audit_record


def _record(**overrides) -> AuditRecord:
    defaults = dict(
        timestamp=datetime(2026, 8, 26, 12, 0, 0),
        request_id="r_abc123",
        question="چند مشتری فعال داریم؟",
        generated_sql="SELECT COUNT(*) FROM [Dim].[Customer]",
        guard={"verdict": "allowed", "rule": None, "injected_top": None,
               "tables_touched": ["Customer"]},
        row_count=1,
        tier="T2",
        timings={"total_ms": 100, "plan_ms": 1, "prompt_ms": 2, "llm_ms": 90,
                  "guard_ms": 1, "execute_ms": 6, "interpret_ms": 0},
        llm={"backend": "ollama", "model": "m", "endpoint_status": 200,
             "attempts": 1, "finish_reason": "stop", "structured_output": False,
             "prompt_tokens": 100, "completion_tokens": 10, "prefill_ms": 5,
             "decode_ms": 50, "total_ms": 55, "tokens_per_second": 20.0,
             "prefix_cache_hit": False, "temperature": 0.0, "seed": None,
             "corrections": 0},
        columns=["CustomerCount"],
    )
    defaults.update(overrides)
    return AuditRecord(**defaults)


class TestAuditRecordShape:
    def test_as_dict_round_trips_core_fields(self):
        record = _record()
        d = record.as_dict()
        assert d["request_id"] == "r_abc123"
        assert d["question"] == "چند مشتری فعال داریم؟"
        assert d["generated_sql"] == "SELECT COUNT(*) FROM [Dim].[Customer]"
        assert d["row_count"] == 1
        assert d["tier"] == "T2"
        assert d["guard"]["verdict"] == "allowed"
        assert d["columns"] == ["CustomerCount"]

    def test_timestamp_serialised_as_iso_string(self):
        d = _record().as_dict()
        assert d["timestamp"] == "2026-08-26T12:00:00"

    def test_timings_embedded_verbatim(self):
        d = _record().as_dict()
        assert d["timings"]["llm_ms"] == 90

    def test_llm_embedded_verbatim(self):
        d = _record().as_dict()
        assert d["llm"]["prefix_cache_hit"] is False

    def test_defaults_for_optional_fields(self):
        record = AuditRecord(
            timestamp=datetime(2026, 8, 26, 12, 0, 0),
            request_id="r_1", question="q", generated_sql="SELECT 1",
            guard={"verdict": "allowed"},
        )
        d = record.as_dict()
        assert d["row_count"] == 0
        assert d["tier"] is None
        assert d["error_code"] is None
        assert d["error_message"] is None
        assert d["timings"] == {}
        assert d["llm"] is None
        assert d["columns"] is None

    def test_error_fields_populated_on_failure(self):
        record = _record(
            tier=None, error_code="FORBIDDEN_SQL",
            error_message="DELETE is not allowed", row_count=0, llm=None,
        )
        d = record.as_dict()
        assert d["error_code"] == "FORBIDDEN_SQL"
        assert d["error_message"] == "DELETE is not allowed"
        assert d["llm"] is None

    def test_is_json_serialisable(self):
        d = _record().as_dict()
        line = json.dumps(d, ensure_ascii=False)
        assert json.loads(line) == d


class TestNeverWritesRowData:
    def test_columns_of_plain_strings_accepted(self):
        record = _record(columns=["CustomerName", "TotalVolume"])
        assert record.columns == ["CustomerName", "TotalVolume"]

    def test_columns_none_is_fine(self):
        record = _record(columns=None)
        assert record.columns is None

    def test_columns_containing_int_rejected(self):
        with pytest.raises(TypeError, match="row values"):
            _record(columns=["CustomerCount", 42])

    def test_columns_containing_dict_rejected(self):
        """A caller accidentally passing a row (e.g. {'CustomerName': 'Acme'})
        instead of a column name must be rejected, not silently logged."""
        with pytest.raises(TypeError, match="row values"):
            _record(columns=[{"CustomerName": "Acme"}])

    def test_columns_containing_tuple_rejected(self):
        with pytest.raises(TypeError, match="row values"):
            _record(columns=[("Acme", 123)])

    def test_error_message_names_offending_index(self):
        with pytest.raises(TypeError, match="index 2"):
            _record(columns=["A", "B", 3])

    def test_no_field_exists_that_could_hold_row_values(self):
        """Structural guarantee: the dataclass has no field named anything
        like 'rows'/'result'/'data' that could carry row payloads."""
        field_names = set(AuditRecord.__dataclass_fields__.keys())
        forbidden = {"rows", "row_data", "result", "results", "data"}
        assert field_names.isdisjoint(forbidden)


class TestSaveAuditRecord:
    def test_creates_audit_log_file(self, tmp_path):
        log_file = tmp_path / "audit_log.jsonl"
        with patch("observability.audit._AUDIT_LOG_FILE", str(log_file)):
            save_audit_record(_record())
        assert log_file.exists()

    def test_writes_valid_json_line(self, tmp_path):
        log_file = tmp_path / "audit_log.jsonl"
        with patch("observability.audit._AUDIT_LOG_FILE", str(log_file)):
            save_audit_record(_record(request_id="r_42"))
        line = log_file.read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert record["request_id"] == "r_42"

    def test_appends_multiple_records(self, tmp_path):
        log_file = tmp_path / "audit_log.jsonl"
        with patch("observability.audit._AUDIT_LOG_FILE", str(log_file)):
            save_audit_record(_record(request_id="r_1"))
            save_audit_record(_record(request_id="r_2"))
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["request_id"] == "r_1"
        assert json.loads(lines[1])["request_id"] == "r_2"

    def test_unicode_preserved(self, tmp_path):
        log_file = tmp_path / "audit_log.jsonl"
        with patch("observability.audit._AUDIT_LOG_FILE", str(log_file)):
            save_audit_record(_record(question="بیشترین خرید مشتریان"))
        record = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert record["question"] == "بیشترین خرید مشتریان"

    def test_uses_log_dir_setting_when_path_not_overridden(self, tmp_path):
        from config import override_settings

        with patch("observability.audit._AUDIT_LOG_FILE", ""), \
             override_settings(log_dir=str(tmp_path)):
            save_audit_record(_record())
        assert (tmp_path / "audit_log.jsonl").exists()

    def test_oserror_does_not_raise(self, tmp_path):
        """The hard rule: writing an audit record must never fail the user's query."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        bad_path = str(blocker / "nested" / "audit_log.jsonl")
        with patch("observability.audit._AUDIT_LOG_FILE", bad_path):
            # should not raise
            save_audit_record(_record())

    def test_oserror_is_logged(self, tmp_path, caplog):
        import logging

        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        bad_path = str(blocker / "nested" / "audit_log.jsonl")
        with patch("observability.audit._AUDIT_LOG_FILE", bad_path):
            with caplog.at_level(logging.ERROR, logger="observability.audit"):
                save_audit_record(_record())
        assert any("audit" in rec.message.lower() for rec in caplog.records)

    def test_shares_rotation_with_logs_logger(self, tmp_path):
        """save_audit_record delegates to logs.logger.append_jsonl, so the
        audit log rotates under the same LOG_MAX_BYTES/LOG_BACKUP_COUNT
        knobs as the REPL's query log -- one rotation implementation, not two.

        Both knobs are Settings fields (see config.py), so the live patch
        is config.override_settings(), not monkeypatch.setenv()."""
        from config import override_settings

        log_file = tmp_path / "audit_log.jsonl"
        line_size = len(
            json.dumps(_record(request_id="r_1").as_dict(), ensure_ascii=False).encode("utf-8")
        ) + 1
        with patch("observability.audit._AUDIT_LOG_FILE", str(log_file)), \
                override_settings(log_max_bytes=line_size, log_backup_count=2):
            save_audit_record(_record(request_id="r_1"))
            save_audit_record(_record(request_id="r_1"))  # same length request_id
        assert (tmp_path / "audit_log.jsonl.1").exists()
