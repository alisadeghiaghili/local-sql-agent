# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for logs/logger.py."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from unittest.mock import patch

import pytest

from logs.logger import _rotate, append_jsonl
from logs.query_log import QueryLog

#: Mirrors config.Settings.log_max_bytes / log_backup_count's own
#: default_factory literals (LOG_MAX_BYTES / LOG_BACKUP_COUNT unset) --
#: logs/logger.py no longer keeps its own copy of these (see config.py's
#: "Three layers, not two" section), so this test pins the values here
#: instead. Raised (deployment-readiness pass) from 10 MiB/5 backups to
#: 50 MiB/20 backups so a first production deployment's audit_log.jsonl
#: cannot lose its earliest, unrepeatable week of records to a rotation
#: nobody was watching -- see config.Settings.log_backup_count's docstring
#: for the full reasoning.
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 20


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
        with patch("logs.logger._LOG_FILE", str(log_file)):
            from logs.logger import save_log
            save_log(_make_log())
        assert log_file.exists()

    def test_writes_valid_json_line(self, tmp_path):
        log_file = tmp_path / "query_log.jsonl"
        with patch("logs.logger._LOG_FILE", str(log_file)):
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
        with patch("logs.logger._LOG_FILE", str(log_file)):
            from logs.logger import save_log
            save_log(_make_log(question="q1"))
            save_log(_make_log(question="q2"))
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["question"] == "q1"
        assert json.loads(lines[1])["question"] == "q2"

    def test_unicode_preserved(self, tmp_path):
        log_file = tmp_path / "query_log.jsonl"
        with patch("logs.logger._LOG_FILE", str(log_file)):
            from logs.logger import save_log
            save_log(_make_log(question="بیشترین خرید مشتریان"))
        line = log_file.read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert record["question"] == "بیشترین خرید مشتریان"

    def test_default_path_reads_log_dir_from_settings(self, tmp_path):
        """When _LOG_FILE is not patched, _log_file() falls back to
        cfg.settings.log_dir, read at call time via override_settings()."""
        from config import override_settings
        from logs.logger import save_log

        with patch("logs.logger._LOG_FILE", ""), override_settings(log_dir=str(tmp_path)):
            save_log(_make_log())
        assert (tmp_path / "query_log.jsonl").exists()

    def test_oserror_does_not_raise(self, tmp_path):
        """logger must swallow OSError and not crash the caller."""
        # A path whose parent is itself an existing *file* (not a
        # directory) makes os.makedirs() fail with OSError/NotADirectoryError
        # on every platform, unlike "/dev/null/..." which is Unix-specific
        # and doesn't reliably fail the same way on Windows.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        bad_path = str(blocker / "nested" / "query_log.jsonl")
        with patch("logs.logger._LOG_FILE", bad_path):
            from logs.logger import save_log
            # should not raise
            save_log(_make_log())


class TestRotationSettings:
    def test_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv("LOG_MAX_BYTES", raising=False)
        monkeypatch.delenv("LOG_BACKUP_COUNT", raising=False)
        from logs.logger import _rotation_settings
        assert _rotation_settings() == (_DEFAULT_MAX_BYTES, _DEFAULT_BACKUP_COUNT)

    def test_reads_settings_at_call_time(self):
        """LOG_MAX_BYTES / LOG_BACKUP_COUNT are now Settings fields (see
        config.py), read through cfg.settings at call time -- like every
        other setting -- rather than via a direct os.getenv() call. A live
        patch is therefore config.override_settings(), not monkeypatch.setenv()."""
        from config import override_settings
        from logs.logger import _rotation_settings
        with override_settings(log_max_bytes=500, log_backup_count=3):
            assert _rotation_settings() == (500, 3)


class TestAppendJsonlRotation:
    def test_rotates_at_size_boundary(self, tmp_path):
        """Appending a line that would push the file past max_bytes rotates first."""
        path = str(tmp_path / "q.jsonl")
        # Each written line (JSON + newline) for {"n": N} is a fixed, known
        # size, so the exact byte threshold that triggers rotation is
        # predictable and can be pinned exactly rather than approximated.
        line_size = len(json.dumps({"n": 0}, ensure_ascii=False).encode("utf-8")) + 1

        append_jsonl(path, {"n": 0}, max_bytes=line_size, backup_count=5)
        assert not os.path.exists(path + ".1")
        assert os.path.getsize(path) == line_size

        # The second write would push the file past max_bytes -> rotates.
        append_jsonl(path, {"n": 1}, max_bytes=line_size, backup_count=5)
        assert os.path.exists(path + ".1")
        assert json.loads(open(path + ".1", encoding="utf-8").read().strip())["n"] == 0
        assert json.loads(open(path, encoding="utf-8").read().strip())["n"] == 1

    def test_no_rotation_under_cap(self, tmp_path):
        path = str(tmp_path / "q.jsonl")
        append_jsonl(path, {"n": 0}, max_bytes=1_000_000, backup_count=5)
        append_jsonl(path, {"n": 1}, max_bytes=1_000_000, backup_count=5)
        assert not os.path.exists(path + ".1")
        lines = open(path, encoding="utf-8").read().strip().splitlines()
        assert len(lines) == 2

    def test_zero_max_bytes_disables_rotation(self, tmp_path):
        path = str(tmp_path / "q.jsonl")
        for i in range(20):
            append_jsonl(path, {"n": i}, max_bytes=0, backup_count=5)
        assert not os.path.exists(path + ".1")
        lines = open(path, encoding="utf-8").read().strip().splitlines()
        assert len(lines) == 20

    def test_retention_caps_backup_count(self, tmp_path):
        """Rotating past backup_count drops the oldest generation, never grows unbounded."""
        path = str(tmp_path / "q.jsonl")
        line_size = len(json.dumps({"n": 0}, ensure_ascii=False).encode("utf-8")) + 1
        backup_count = 3

        # Force one rotation per write, several times over backup_count.
        for i in range(10):
            append_jsonl(path, {"n": i}, max_bytes=line_size, backup_count=backup_count)

        assert os.path.exists(path)
        for gen in range(1, backup_count + 1):
            assert os.path.exists(f"{path}.{gen}")
        assert not os.path.exists(f"{path}.{backup_count + 1}")

        # The current (active) file holds the very latest record.
        assert json.loads(open(path, encoding="utf-8").read().strip())["n"] == 9
        # .1 is the next-most-recent, .3 is the oldest retained generation.
        assert json.loads(open(f"{path}.1", encoding="utf-8").read().strip())["n"] == 8
        assert json.loads(open(f"{path}.3", encoding="utf-8").read().strip())["n"] == 6

    def test_backup_count_zero_clears_instead_of_keeping_history(self, tmp_path):
        path = str(tmp_path / "q.jsonl")
        line_size = len(json.dumps({"n": 0}, ensure_ascii=False).encode("utf-8")) + 1
        append_jsonl(path, {"n": 0}, max_bytes=line_size, backup_count=0)
        append_jsonl(path, {"n": 1}, max_bytes=line_size, backup_count=0)
        assert not os.path.exists(path + ".1")
        assert json.loads(open(path, encoding="utf-8").read().strip())["n"] == 1

    def test_oserror_propagates_from_append_jsonl(self, tmp_path):
        """Unlike save_log, the lower-level append_jsonl does not swallow I/O errors."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        bad_path = str(blocker / "nested" / "q.jsonl")
        with pytest.raises(OSError):
            append_jsonl(bad_path, {"n": 0})


class TestSaveLogUsesRotation:
    def test_save_log_rotates_via_settings(self, tmp_path):
        from config import override_settings

        log_file = tmp_path / "query_log.jsonl"
        # "q1" and "q2" are the same length, so both writes serialise to
        # exactly the same number of bytes -- the cap can be pinned exactly.
        line_size = len(
            json.dumps(_make_log(question="q1").as_dict(), ensure_ascii=False).encode("utf-8")
        ) + 1
        with patch("logs.logger._LOG_FILE", str(log_file)), \
                override_settings(log_max_bytes=line_size, log_backup_count=2):
            from logs.logger import save_log
            save_log(_make_log(question="q1"))
            save_log(_make_log(question="q2"))
        assert (tmp_path / "query_log.jsonl.1").exists()
        assert json.loads(log_file.read_text(encoding="utf-8").strip())["question"] == "q2"


class TestAppendJsonlPartialOverrides:
    """append_jsonl(max_bytes=..., backup_count=...) independently defaults
    each argument that is left as None, rather than requiring both or
    neither -- covering both branches of each of the two independent checks.
    """

    def test_only_max_bytes_overridden(self, tmp_path):
        from config import override_settings

        path = str(tmp_path / "q.jsonl")
        line_size = len(json.dumps({"n": 0}, ensure_ascii=False).encode("utf-8")) + 1
        with override_settings(log_backup_count=1):
            append_jsonl(path, {"n": 0}, max_bytes=line_size)  # backup_count from settings
            append_jsonl(path, {"n": 1}, max_bytes=line_size)
        assert os.path.exists(path + ".1")

    def test_only_backup_count_overridden(self, tmp_path):
        from config import override_settings

        path = str(tmp_path / "q.jsonl")
        line_size = len(json.dumps({"n": 0}, ensure_ascii=False).encode("utf-8")) + 1
        with override_settings(log_max_bytes=line_size):
            append_jsonl(path, {"n": 0}, backup_count=1)  # max_bytes from settings
            append_jsonl(path, {"n": 1}, backup_count=1)
        assert os.path.exists(path + ".1")


class TestRotateDirectly:
    """Exercises _rotate()'s edge cases that append_jsonl's own call
    pattern (only ever rotating a file it already confirmed exists)
    cannot reach on its own.
    """

    def test_zero_backup_count_on_missing_file_does_not_raise(self, tmp_path):
        missing = str(tmp_path / "does_not_exist.jsonl")
        _rotate(missing, 0)  # should not raise FileNotFoundError

    def test_rotate_when_active_file_itself_is_missing(self, tmp_path):
        """Only a .1 backup exists (no live file) -- rotate still shifts
        the existing backups without erroring on the missing active file."""
        path = str(tmp_path / "q.jsonl")
        (tmp_path / "q.jsonl.1").write_text('{"n": 0}\n', encoding="utf-8")
        _rotate(path, 3)
        assert not os.path.exists(path)
        assert (tmp_path / "q.jsonl.2").exists()
        assert not (tmp_path / "q.jsonl.1").exists()
