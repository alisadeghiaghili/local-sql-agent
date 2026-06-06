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

# Expose settings at module level so tests can patch "logs.logger.settings"
settings = cfg.settings

# Module-level path variable so tests can patch "logs.logger._LOG_FILE"
# Initialised to empty string; _resolve_log_file() always wins at runtime.
_LOG_FILE: str = ""


def _resolve_log_file() -> str:
    """Return the effective log file path.

    Prefers the module-level ``_LOG_FILE`` override (used by tests) when it
    is non-empty; otherwise falls back to ``settings.log_dir``.
    """
    if _LOG_FILE:
        return _LOG_FILE
    return os.path.join(settings.log_dir, "query_log.jsonl")


def save_log(log: QueryLog) -> None:
    """Append *log* as a single JSON line to the audit log file."""
    log_file = _resolve_log_file()
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    line = json.dumps(log.as_dict(), ensure_ascii=False) + "\n"
    try:
        with _write_lock:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
    except OSError as exc:
        logger.error("Failed to write query log: %s", exc)
