# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Importing this module stops the process when ``project_config/`` is unusable.

``api.server`` imports it ahead of every module that reads a
``project_config/`` file at import time (``security.sql_guard`` binds the
schema's table allowlist, ``retrieval`` the entities, and so on). A missing
or invalid file then ends ``uvicorn api.server:app`` and ``python -m api``
with three short lines on stderr and a non-zero status, not a traceback.
It stays fail-fast: there is no degraded mode.

A valid ``project_config/`` makes the import a no-op. The checking itself
lives in :mod:`core.config_preflight`, which tests call directly.
"""

from __future__ import annotations

from core.config_preflight import SERVER_IMPORT_FILES, exit_if_project_config_unusable

exit_if_project_config_unusable(
    SERVER_IMPORT_FILES,
    hints=(
        "run python scripts/verify_deployment.py to check every project_config file",
        "nothing was started",
    ),
)
