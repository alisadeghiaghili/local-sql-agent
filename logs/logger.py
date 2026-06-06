"""Append a QueryLog entry to logs/query_log.jsonl.

Thread-safe: each write opens, writes, and closes atomically.
"""

from __future__ import annotations

import json
import logging
import os

from config import settings
from logs.query_log import QueryLog

_LOG_FILE = os.path.join(settings.log_dir, "query_log.jsonl")

logger = logging.getLogger(__name__)


def save_log(log: QueryLog) -> None:
    """Append *log* as a single JSON line to ``logs/query_log.jsonl``."""
    os.makedirs(settings.log_dir, exist_ok=True)
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(log.as_dict(), ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.error("Failed to write query log: %s", exc)
