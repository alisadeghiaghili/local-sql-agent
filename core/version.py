# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The project's version, in one place.

``api.server``'s ``FastAPI(version=...)`` used to carry its own hardcoded
string. It said ``1.0.0`` while ``CHANGELOG.md`` said ``4.1.0`` -- nobody
noticed, because nothing read it. ``GET /`` now publishes it to any
caller, so a stale number stopped being harmless.

Two numbers that must agree, kept in one place plus a test that they do:
``tests/test_version.py`` reads the newest ``## [x.y.z]`` heading in
``CHANGELOG.md`` and fails if it differs from :data:`__version__`. A
release that forgets one of the two is caught at the point it is made
rather than by whoever reads the wrong one months later.
"""

from __future__ import annotations

__version__ = "4.1.3"
