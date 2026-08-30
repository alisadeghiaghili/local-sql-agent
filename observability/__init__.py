"""Observability package — audit trail and per-stage telemetry.

This package is Phase 0-C of the planned refactor: it builds the audit and
timing *machinery* used to close two gaps found in the production system:

1. ``logs/logger.py::save_log`` is only ever called from the interactive
   REPL (``app.py``); the FastAPI service writes no audit record at all.
2. There is no per-stage timing anywhere, so future latency work has
   nothing to measure against.

Nothing in this package is wired into ``api/**`` yet — that happens in a
follow-up once the API layer stabilises. Everything here is import-safe
(no network calls, no DB connections, no file I/O at import time) and
independently testable.

Modules
-------
:mod:`observability.timing`
    Per-stage timing collection shaped like ``docs/api-contract-v2.md``
    §4's ``Turn.timings``.
:mod:`observability.llm_status`
    Builds the §6 ``Turn.llm`` status block from a raw OpenAI-compatible response.
:mod:`observability.audit`
    The audit record itself and its (never-fails) JSONL writer.
"""

from __future__ import annotations
