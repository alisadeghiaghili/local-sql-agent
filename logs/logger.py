"""Append a QueryLog entry to the JSONL audit log.

Thread-safety
-------------
A module-level ``threading.Lock`` serialises all writes so that
concurrent callers (e.g. a future FastAPI app) never interleave JSON
lines.  The lock is held only for the duration of the ``write`` +
``flush`` syscalls, so contention is negligible in practice.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from config import settings
from logs.query_log import QueryLog

_LOG_FILE = os.path.join(settings.log_dir, "query_log.jsonl")
_write_lock = threading.Lock()

logger = logging.getLogger(__name__)


def save_log(log: QueryLog) -> None:
    """Append *log* as a single JSON line to the audit log file.

    Guarantees
    ----------
    - Each record is written atomically: no two records can interleave.
    - An ``OSError`` (e.g. disk full, permission denied) is caught and
      logged; it never propagates to the caller.
    """
    os.makedirs(settings.log_dir, exist_ok=True)
    line = json.dumps(log.as_dict(), ensure_ascii=False) + "\n"
    try:
        with _write_lock:
            with open(_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
    except OSError as exc:
        logger.error("Failed to write query log: %s", exc)
