"""Append JSONL records to size-rotated audit log files.

Thread-safety
-------------
A module-level ``threading.Lock`` serialises all writes (and any rotation
that a write triggers) so that concurrent callers never interleave JSON
lines or race on the rename chain that rotation performs.

Rotation
--------
Every log file written through :func:`append_jsonl` (which includes
:func:`save_log`) is size-capped: once appending the next line would push
the file past ``LOG_MAX_BYTES`` bytes, the current file is rotated —
``query_log.jsonl`` becomes ``query_log.jsonl.1``, the old ``.1`` becomes
``.2``, and so on — before the new line is written to a fresh file. At
most ``LOG_BACKUP_COUNT`` rotated files are kept; the oldest is deleted
once that count would be exceeded. This is what makes the README's
"rotating JSONL logger" claim true — previously ``save_log`` appended
forever.

Both knobs (``LOG_MAX_BYTES``, ``LOG_BACKUP_COUNT`` — see ``.env.example``)
are read through ``cfg.settings.log_max_bytes`` / ``cfg.settings.
log_backup_count`` at call time, the same lazy-read discipline every other
setting in this project follows. They previously bypassed ``config.py``
and read directly via :func:`os.getenv`, because ``config.py`` was owned
by concurrent work outside this module's file boundary at the time this
rotation logic was written; that lock has since cleared, so both fields
now live on ``Settings`` like everything else (see ``config.py``'s
``log_max_bytes``/``log_backup_count`` fields for the historical note).
``LOG_DIR`` was already a ``Settings`` field throughout, so the log
*directory* itself has always been resolved through ``cfg.settings.log_dir``
at call time.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

import config as cfg
from logs.query_log import QueryLog

_write_lock = threading.Lock()

logger = logging.getLogger(__name__)

# Module-level path variable so tests can patch "logs.logger._LOG_FILE".
_LOG_FILE: str = ""

#: Default size cap (bytes) applied when LOG_MAX_BYTES is unset: 10 MiB.
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024

#: Default number of rotated backups kept when LOG_BACKUP_COUNT is unset.
_DEFAULT_BACKUP_COUNT = 5


def _log_file() -> str:
    """Return the effective log file path.

    When ``_LOG_FILE`` is non-empty (patched by tests) that value is used;
    otherwise reads ``cfg.settings.log_dir`` lazily so that
    ``override_settings()`` patches are visible at call-time.
    """
    if _LOG_FILE:
        return _LOG_FILE
    return os.path.join(cfg.settings.log_dir, "query_log.jsonl")


def _rotation_settings() -> tuple[int, int]:
    """Read ``(max_bytes, backup_count)`` from ``cfg.settings`` at call time.

    Reading lazily (rather than capturing the values at import time) means
    :func:`config.override_settings` patches in tests are visible
    immediately, matching every other ``cfg.settings`` access in this
    project.

    Returns
    -------
    tuple[int, int]
        ``(max_bytes, backup_count)``. ``max_bytes <= 0`` disables
        rotation entirely (the file grows without bound). ``backup_count
        <= 0`` means "rotate but keep no history" — the file is cleared
        rather than shifted into a ``.1`` backup.

    Examples
    --------
    >>> _rotation_settings()[0] == _DEFAULT_MAX_BYTES
    True
    >>> from config import override_settings
    >>> with override_settings(log_max_bytes=999, log_backup_count=1):
    ...     _rotation_settings()
    (999, 1)
    """
    return cfg.settings.log_max_bytes, cfg.settings.log_backup_count


def _rotate(path: str, backup_count: int) -> None:
    """Shift *path* through the ``.1 .. .N`` backup chain, dropping the oldest.

    ``path`` -> ``path.1``, the previous ``path.1`` -> ``path.2``, etc.,
    up to ``path.<backup_count>``; anything that would land beyond that is
    removed. Must be called while holding ``_write_lock``.

    Parameters
    ----------
    path:
        The active log file to rotate out of the way.
    backup_count:
        How many rotated generations to retain. ``<= 0`` means keep none
        — *path* is simply removed so the next write starts a fresh file.
    """
    if backup_count <= 0:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return

    oldest = f"{path}.{backup_count}"
    if os.path.exists(oldest):
        os.remove(oldest)
    for generation in range(backup_count - 1, 0, -1):
        src = f"{path}.{generation}"
        dst = f"{path}.{generation + 1}"
        if os.path.exists(src):
            os.replace(src, dst)
    if os.path.exists(path):
        os.replace(path, f"{path}.1")


def append_jsonl(
    path: str,
    payload: dict[str, Any],
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> None:
    """Append *payload* as one JSON line to *path*, rotating first if needed.

    Shared by :func:`save_log` and, via :mod:`observability.audit`, by the
    audit trail writer — both are size-rotated JSONL logs and should not
    duplicate the rotation logic.

    Parameters
    ----------
    path:
        Target log file. Parent directories are created if missing.
    payload:
        A JSON-serialisable dict. Written with ``ensure_ascii=False`` so
        non-Latin text (e.g. Persian questions) is stored as literal
        UTF-8, not ``\\uXXXX`` escapes.
    max_bytes:
        Size cap in bytes. ``None`` (the default) reads ``LOG_MAX_BYTES``
        from the environment via :func:`_rotation_settings`. ``<= 0``
        disables rotation.
    backup_count:
        Rotated generations to retain. ``None`` (the default) reads
        ``LOG_BACKUP_COUNT`` from the environment via
        :func:`_rotation_settings`.

    Raises
    ------
    OSError
        Propagated to the caller on any I/O failure (directory creation,
        rotation, or the write itself). Callers that must never fail a
        user-facing operation because of a logging problem (see
        :func:`save_log`) are responsible for catching this.

    Examples
    --------
    >>> import tempfile, os, json
    >>> d = tempfile.mkdtemp()
    >>> p = os.path.join(d, "x.jsonl")
    >>> append_jsonl(p, {"a": 1}, max_bytes=10_000, backup_count=2)
    >>> append_jsonl(p, {"a": 2}, max_bytes=10_000, backup_count=2)
    >>> lines = open(p, encoding="utf-8").read().strip().splitlines()
    >>> [json.loads(l)["a"] for l in lines]
    [1, 2]
    """
    if max_bytes is None or backup_count is None:
        default_max, default_backup = _rotation_settings()
        if max_bytes is None:
            max_bytes = default_max
        if backup_count is None:
            backup_count = default_backup

    line = json.dumps(payload, ensure_ascii=False) + "\n"
    line_size = len(line.encode("utf-8"))

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    with _write_lock:
        if max_bytes > 0 and os.path.exists(path):
            current_size = os.path.getsize(path)
            if current_size + line_size > max_bytes:
                _rotate(path, backup_count)
        # newline="" disables Python's universal-newline translation, so a
        # "\n" in *line* is written as a single 0x0A byte on every platform
        # (not "\r\n" on Windows). This keeps JSONL files byte-consistent
        # across the OSes this project runs on, and is what makes the
        # size-boundary math above exact rather than platform-dependent.
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write(line)
            fh.flush()


def save_log(log: QueryLog) -> None:
    """Append *log* as a single, size-rotated JSON line to the audit log file.

    Signature is unchanged from before rotation was added — ``app.py``
    calls this directly and is out of scope for this change.

    I/O failures (including any failure during rotation) are caught,
    logged, and swallowed: a broken log file must never fail the user's
    query.
    """
    log_path = _log_file()
    try:
        append_jsonl(log_path, log.as_dict())
    except OSError as exc:
        logger.error("Failed to write query log: %s", exc)
