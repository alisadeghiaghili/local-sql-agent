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


def _log_file() -> str:
    """Return the log file path, resolved at call-time from ``cfg.settings``.

    Evaluated lazily so that ``override_settings(log_dir=...)`` in tests
    takes effect without reloading the module.
    """
    return os.path.join(cfg.settings.log_dir, "query_log.jsonl")


def save_log(log: QueryLog) -> None:
    """Append *log* as a single JSON line to the audit log file."""
    log_file = _log_file()
    os.makedirs(cfg.settings.log_dir, exist_ok=True)
    line = json.dumps(log.as_dict(), ensure_ascii=False) + "\n"
    try:
        with _write_lock:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
    except OSError as exc:
        logger.error("Failed to write query log: %s", exc)
