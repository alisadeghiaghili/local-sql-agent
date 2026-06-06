"""Append a QueryLog entry to logs/query_log.jsonl."""

from __future__ import annotations

import json
import os
from datetime import datetime

from logs.query_log import QueryLog

_LOG_DIR  = "logs"
_LOG_FILE = os.path.join(_LOG_DIR, "query_log.jsonl")


def save_log(log: QueryLog) -> None:
    """Append *log* as a JSON line to ``logs/query_log.jsonl``."""
    os.makedirs(_LOG_DIR, exist_ok=True)
    record = {
        "timestamp":              log.timestamp.isoformat(),
        "question":               log.question,
        "generated_sql":          log.generated_sql,
        "model_name":             log.model_name,
        "status":                 log.status,
        "execution_time_seconds": log.execution_time_seconds,
        "row_count":              log.row_count,
        "excel_file":             log.excel_file,
        "error_message":          log.error_message,
    }
    with open(_LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
