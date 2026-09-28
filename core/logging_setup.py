# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Ensure the application's own loggers actually reach a handler.

Both documented ways of starting the HTTP API (``uvicorn api.server:app
--host ... --port ... --no-server-header`` and ``python -m api``) leave
the ROOT logger exactly as Python's ``logging`` module starts it: level
``WARNING``, no handlers. uvicorn's own default logging config
(``uvicorn.config.LOGGING_CONFIG``) only attaches handlers to the
``uvicorn`` / ``uvicorn.error`` / ``uvicorn.access`` loggers, each with
``propagate: False`` -- it never touches the root logger, and its
``disable_existing_loggers`` is explicitly ``False``. So every other
logger in the process, including this application's own
(``logging.getLogger(__name__)`` in ``api/server.py`` and elsewhere), is
left exactly where the interpreter started it: no handler anywhere in its
chain, and an effective level of ``WARNING`` inherited from the
unconfigured root. ``logger.info(...)`` calls -- the startup provenance
banner (:func:`core.provenance.log_startup_notice`) and the
CORS-allowlist line (``api/server.py``'s ``lifespan``) among them -- are
filtered out before a single handler is ever asked to write them; they
were never silently dropped BY a handler, they never became ``LogRecord``
objects at all, since the level check happens first.

:func:`configure_stdlib_logging` fixes this without fighting an operator
who configured logging themselves (their own ``logging.basicConfig()``, a
``dictConfig``, a framework's own setup, ...): it only acts when the ROOT
logger has no handlers at all, which is the exact, unambiguous signal
that nothing -- not this codebase, not the operator, not some other
library -- has configured logging yet. When that is true, it attaches one
``StreamHandler`` (stderr, a plain ``[LEVEL] name: message`` format, no
uvicorn-style colour codes -- this runs whether or not uvicorn is even
present) and raises the root logger's level from the default ``WARNING``
to the requested level (:data:`config.Settings.log_level`, default
``INFO``), so every first-party logger that has not set its own level --
which is all of them; nothing in this codebase calls ``setLevel`` on its
own logger -- starts actually emitting at that level.

Idempotent: calling it again once a handler is already present (whether
this function added it or an operator did) is a no-op, so calling it from
both ``api/server.py``'s ``lifespan`` and ``api/__main__.py`` (defence in
depth -- whichever runs first is enough) never attaches a second handler
and never double-prints a line.

Examples
--------
>>> import logging
>>> root = logging.getLogger()
>>> saved_handlers, saved_level = list(root.handlers), root.level
>>> root.handlers = []
>>> root.setLevel(logging.WARNING)
>>> configure_stdlib_logging("INFO")
>>> root.level == logging.INFO
True
>>> len(root.handlers)
1
>>> configure_stdlib_logging("DEBUG")  # already has a handler -- no-op
>>> len(root.handlers)
1
>>> root.level == logging.INFO
True
>>> root.handlers = saved_handlers
>>> root.setLevel(saved_level)
"""

from __future__ import annotations

import logging
import sys


def configure_stdlib_logging(level_name: str) -> None:
    """Attach a stderr handler to the ROOT logger and raise its level to
    *level_name*, but only when the root logger has no handler at all --
    see this module's docstring for why that check is the right one.

    *level_name* is a ``logging`` level name (``"INFO"``, ``"DEBUG"``,
    ...), case-insensitive; an unrecognised name falls back to ``INFO``
    rather than raising -- a typo in ``LOG_LEVEL`` should degrade to a
    sane default, not take startup logging down with it.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    level = getattr(logging, str(level_name).upper(), None)
    if not isinstance(level, int):
        level = logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
