# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Visibility over failed authentication attempts (admin panel phase 6).

``docs/admin-panel-architecture.md`` §9 asked whether bucketing failed
admin auth on IP alone is enough. The answer (recorded in that document
now): guessing a real key is arithmetically impossible
(:data:`security.auth.MIN_KEY_LENGTH` gives every key 256 bits of
entropy), so the threat this module answers is a *leaked* key being
tried, not a *guessed* one -- and the signal for that is a sudden run of
failures, not a tighter rate limit. This module is that signal.

:func:`record_auth_failure` is called by ``api.auth.AuthMiddleware`` for
every request that presented an ``Authorization`` header which failed to
resolve to a principal -- never for a request with no header at all
(ordinary unauthenticated traffic, which is not a failure of anything).
:func:`summarize_auth_failures` is what ``api/admin_ops_routes.py``
surfaces: a count and a per-source-address breakdown over a window, so an
operator sees a spike the moment it starts rather than discovering a
leaked key only after it was used for something worse.

Append-only JSONL, the same ``logs.logger.append_jsonl`` writer
``observability.audit`` and ``appdb.admin_audit`` already use -- this is
not a new kind of mechanism, only a new named stream, kept separate from
both of those for the same reason they are separate from each other:
mixing concerns pollutes the analysis each stream exists for.

Not a security control on its own
------------------------------------
Recording a failure is visibility, not enforcement -- the actual
enforcement (a small, separate rate-limit bucket for auth failures,
protecting the shared unauthenticated budget from a looping bad client)
lives in :mod:`api.middleware`. Binding admin keys to a source-address
allowlist would be a stronger control still, but it is not implemented
here -- see ``docs/admin-panel-architecture.md`` §9 for why that stays
opt-in rather than a default: a legitimate admin travelling, or working
through a rotating egress IP, must not be locked out by a control this
codebase turned on unconditionally.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import config as cfg
from logs.logger import append_jsonl

logger = logging.getLogger(__name__)

# Module-level path variable so tests can patch
# "security.auth_failures._AUTH_FAILURE_LOG_FILE", mirroring
# appdb.admin_audit._ADMIN_ACTION_LOG_FILE's own test seam.
_AUTH_FAILURE_LOG_FILE: str = ""


def _auth_failure_log_file() -> str:
    if _AUTH_FAILURE_LOG_FILE:
        return _AUTH_FAILURE_LOG_FILE
    return os.path.join(cfg.settings.log_dir, "auth_failure_log.jsonl")


def record_auth_failure(source_ip: str, path: str) -> None:
    """Append one auth-failure record. Never raises -- mirrors
    ``appdb.admin_audit.record_admin_action``'s "must never fail the
    caller's request" contract, since this is called from
    ``AuthMiddleware.dispatch``, on the hot path of every single request.

    *source_ip* is the raw TCP peer address (``request.client.host``),
    deliberately NOT the trusted-proxy-aware resolution
    ``api.middleware.RateLimitMiddleware._client_ip`` performs -- this is
    an operational visibility signal, not an enforcement boundary, and the
    simpler reading is the one an operator actually wants when scanning
    for "which address is this coming from".
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_ip": source_ip,
        "path": path,
    }
    try:
        append_jsonl(_auth_failure_log_file(), record)
    except OSError as exc:  # pragma: no cover - defensive, mirrors admin_audit
        logger.error("Failed to write auth-failure record: %s", exc)


def iter_auth_failures() -> list[dict[str, Any]]:
    """Every recorded auth failure, oldest first. Empty list if nothing
    has ever been recorded."""
    path = _auth_failure_log_file()
    if not os.path.exists(path):
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def summarize_auth_failures(
    records: list[dict[str, Any]], *, window_seconds: float | None = None, top_n: int = 10,
) -> dict[str, Any]:
    """Count and per-source-address breakdown of *records* within the last
    *window_seconds* (``None`` means "every record ever recorded").

    Returns
    -------
    dict
        ``{"total", "window_seconds", "by_source_ip", "admin_path_total"}``
        -- ``by_source_ip`` is ``{ip: count}``, the *top_n* most frequent
        addresses, descending. ``admin_path_total`` counts only the subset
        whose ``path`` starts with ``/admin`` -- the figure most directly
        answering "is someone trying admin keys", surfaced alongside the
        broader total rather than replacing it (a failure against any
        route is still evidence a stored key is wrong or leaking).
    """
    if window_seconds is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        selected = [
            r for r in records
            if _parse_ts(r.get("timestamp")) is not None
            and _parse_ts(r.get("timestamp")) >= cutoff
        ]
    else:
        selected = list(records)

    counts: Counter[str] = Counter(r.get("source_ip", "unknown") for r in selected)
    admin_total = sum(1 for r in selected if str(r.get("path", "")).startswith("/admin"))

    return {
        "total": len(selected),
        "window_seconds": window_seconds,
        "by_source_ip": dict(counts.most_common(top_n)),
        "admin_path_total": admin_total,
    }


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
