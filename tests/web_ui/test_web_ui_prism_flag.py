# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The T-SQL Prism patch must not poison the grammar it marks.

`web/js/prism-tsql-patch.js` records "already patched" by writing
`__tsqlPatched` onto the SQL grammar object. Prism tokenises by iterating
that object's keys and using each value as a token, so an *enumerable*
marker lands in the iteration, Prism calls `.exec` on the boolean `true`,
throws, and `highlightSql` falls back to plain text. The effect in the
browser: generated SQL stopped being highlighted at all -- silently, because
the failure is swallowed by design (highlighting is presentation-only).

`run_sql_highlight.mjs` already exercises this patch against a stub grammar,
but it only asserts the marker is *idempotent*, never that it is hidden from
enumeration -- so it stayed green while the real page showed no colours. This
module adds the missing invariant, driven through the REAL patch file under
Node with a `window` shim: after patching, the marker is readable (idempotency
holds) but absent from `Object.keys` (Prism's iteration never sees it).

Kept as a separate module rather than extending `run_sql_highlight.mjs`, so
the existing harness is not edited.

Policy: `docs/design/DESIGN.md` §16 (prettify + highlight pipeline).
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PATCH = _REPO_ROOT / "web" / "js" / "prism-tsql-patch.js"
_HARNESS = Path(__file__).resolve().parent / "run_prism_flag.mjs"
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_the_patch_marker_is_not_enumerable_on_the_grammar() -> None:
    assert _PATCH.exists(), f"expected {_PATCH} to exist"
    assert _HARNESS.exists(), f"expected {_HARNESS} to exist"

    result = subprocess.run(
        [_NODE, str(_HARNESS), str(_PATCH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=NODE_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        "prism-tsql-patch flag check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        "harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
