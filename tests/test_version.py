# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The declared version and the changelog's newest entry must agree.

``api.server``'s FastAPI ``version`` sat at ``1.0.0`` while the project
was at ``4.1.0``. That was harmless only for as long as nothing read it;
``GET /`` publishes it now, so it is a claim the project makes to every
caller and it has to be true.

Reading the changelog rather than trusting a second constant is the point:
the failure mode being prevented is exactly "two places, one updated".
"""

from __future__ import annotations

import re
from pathlib import Path

from core.version import __version__

_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
_HEADING = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def _newest_changelog_version() -> str:
    match = _HEADING.search(_CHANGELOG.read_text(encoding="utf-8"))
    assert match is not None, "CHANGELOG.md has no '## [x.y.z]' heading to read"
    return match.group(1)


class TestVersionMatchesChangelog:
    def test_declared_version_is_the_newest_changelog_entry(self):
        assert __version__ == _newest_changelog_version(), (
            f"core.version.__version__ is {__version__!r} but CHANGELOG.md's "
            f"newest entry is {_newest_changelog_version()!r} -- a release "
            "updated one and not the other, and GET / publishes the former"
        )

    def test_the_served_app_reports_that_same_version(self):
        """The number a caller actually receives, not just the constant."""
        import api.server as server_module

        assert server_module.app.version == __version__
