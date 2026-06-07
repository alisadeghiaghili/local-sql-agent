"""Append a QueryLog entry to the JSONL audit log.

Thread-safety
-------------
A module-level ``threading.Lock`` serialises all writes so that
concurrent callers never interleave JSON lines.
"""

from __future__ import annotations

import json
import logging
import os
import threading

import config as cfg
from logs.query_log import QueryLog

_write_lock = threading.Lock()

logger = logging.getLogger(__name__)

# Exposed so tests can patch "logs.logger.settings".
# _log_file() always reads cfg.settings directly for lazy override support.
settings = cfg.settings

# Module-level path variable so tests can patch "logs.logger._LOG_FILE"
_LOG_FILE: str = ""


def _log_file() -> str:
    """Return the effective log file path.

    When ``_LOG_FILE`` is non-empty (patched by tests) that value is used;
    otherwise reads ``cfg.settings.log_dir`` lazily so that
    ``override_settings()`` patches are visible at call-time.
    """
    if _LOG_FILE:
        return _LOG_FILE
    return os.path.join(cfg.settings.log_dir, "query_log.jsonl")


def save_log(log: QueryLog) -> None:
    """Append *log* as a single JSON line to the audit log file."""
    log_path = _log_file()
    line = json.dumps(log.as_dict(), ensure_ascii=False) + "\n"
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with _write_lock:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
    except OSError as exc:
        logger.error("Failed to write query log: %s", exc)
