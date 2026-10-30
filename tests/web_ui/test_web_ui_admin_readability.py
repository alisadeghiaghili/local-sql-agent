# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel readability: dates, the checks card, the rail and the top bar.

* ``web/admin/time.js`` turns the server's ISO-8601 strings into a Persian
  calendar date and time and keeps the original for the title; anything
  that is not a date is shown unchanged (``run_admin_time.mjs``).
* The deployment-checks card shows FAIL and SKIP as cards and folds the
  passed checks into one line that opens to the same cards.
* The summary rail writes "N از M", not "N / M": laid out right to left,
  "2 / 21" read as "21 / 2".
* The top bar's backend-address field has its own rules again; they left
  ``style.css`` with the analyst page's copy of the field.

Requires ``node`` on PATH for the behavioural test. Skipped (not failed)
when unavailable.
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parent.parent.parent / "web"
_ADMIN_JS = _WEB / "admin" / "main.js"
_ADMIN_CSS = _WEB / "admin" / "admin.css"
_ACCESS_JS = _WEB / "admin" / "access-requests.js"
_TIME_JS = _WEB / "admin" / "time.js"
_HARNESS = Path(__file__).resolve().parent / "run_admin_time.mjs"
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/admin/*.js under test")
def test_server_dates_become_persian_dates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        time_mjs = Path(tmp) / "time.mjs"
        time_mjs.write_text(_TIME_JS.read_text(encoding="utf-8"), encoding="utf-8")
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(time_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ALL_SCENARIOS_PASSED" in result.stdout


def test_dates_on_the_cards_go_through_the_formatter() -> None:
    src = _ADMIN_JS.read_text(encoding="utf-8")
    assert 'import { formatWhen } from "./time.js";' in src
    assert "${escapeHtml(range.start" not in src, "the audit range is printed raw again"
    assert "whenHtml(range.start)" in src and "whenHtml(range.end)" in src
    assert "whenHtml(c.fetched_at)" in src
    assert "formatWhen(stats.baseline.generated_at" in src
    helper = src[src.index("function whenHtml("):]
    helper = helper[: helper.index("\n}\n")]
    assert "escapeHtml(title)" in helper and "escapeHtml(text" in helper, "both parts are escaped"
    access = _ACCESS_JS.read_text(encoding="utf-8")
    assert "formatWhen(row.created_at)" in access
    assert "createdLine.title = created.title" in access


def test_passed_checks_fold_into_one_line() -> None:
    src = _ADMIN_JS.read_text(encoding="utf-8")
    body = src[src.index("function renderHealth("):src.index("let _lastCacheStats")]
    assert 'sorted.filter((c) => c.status !== "PASS")' in body
    assert 'sorted.filter((c) => c.status === "PASS")' in body
    assert '<details class="admin-checks-passed">' in body
    assert "attention.join(\"\") + passedBlock" in body
    css = _ADMIN_CSS.read_text(encoding="utf-8")
    assert ".admin-checks-passed { grid-column: 1 / -1; }" in css


def test_the_rail_says_n_of_m() -> None:
    src = _ADMIN_JS.read_text(encoding="utf-8")
    rail = src[src.index("function setRailItem("):src.index("function markLastUpdated(")]
    assert "${failed} / ${checks.length}" not in rail
    assert "${fmtNum(failed)} از ${fmtNum(checks.length)}" in rail


def test_the_backend_address_field_is_styled() -> None:
    css = _ADMIN_CSS.read_text(encoding="utf-8")
    for selector in (".live-base-row", ".live-base-input", ".live-base-connect"):
        assert re.search(re.escape(selector) + r"\s*\{", css), f"{selector} has no rule"
    assert ".live-base-input:focus-visible" in css
