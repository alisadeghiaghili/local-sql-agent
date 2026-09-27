# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``web/js/render/turn.js``'s ``DB_GENERIC_REJECTION`` constant must stay
byte-for-byte identical to ``database/errors.py``'s
``_GENERIC_STATEMENT_MESSAGE``.

The ``QUERY_EXECUTION_ERROR`` failure banner (``renderFailureState`` in
``turn.js``) compares ``turn.error.message`` against ``DB_GENERIC_REJECTION``
to decide whether the database gave a specific, worth-showing reason or just
the backend's generic fallback. If the two constants ever drift apart, the
generic fallback would either leak through as if it were specific detail, or
a genuinely specific database message would get silently swallowed as if it
were the fallback -- either way, silently, with no visible symptom short of
staring at the banner. This test imports the RUNTIME value from
``database.errors`` (never regex-parsed out of the source) and regex-extracts
the JS constant out of ``turn.js``, then asserts they are equal.
"""

from __future__ import annotations

import re
from pathlib import Path

import database.errors

_REPO = Path(__file__).resolve().parent.parent
_TURN_JS = _REPO / "web" / "js" / "render" / "turn.js"


def _extract_db_generic_rejection() -> str:
    assert _TURN_JS.exists(), f"cannot find {_TURN_JS}"
    content = _TURN_JS.read_text(encoding="utf-8")
    match = re.search(r'const DB_GENERIC_REJECTION\s*=\s*"((?:[^"\\]|\\.)*)"', content)
    assert match, "cannot find `const DB_GENERIC_REJECTION = \"...\";` in turn.js"
    return match.group(1)


def test_db_generic_rejection_matches_backend_runtime_value():
    js_message = _extract_db_generic_rejection()
    backend_message = database.errors._GENERIC_STATEMENT_MESSAGE

    assert js_message == backend_message, (
        f"turn.js's DB_GENERIC_REJECTION ({js_message!r}) no longer matches "
        f"database.errors._GENERIC_STATEMENT_MESSAGE ({backend_message!r}) -- "
        "the QUERY_EXECUTION_ERROR banner's suppression check in turn.js will "
        "misfire until these are brought back in sync."
    )


if __name__ == "__main__":  # pragma: no cover
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
