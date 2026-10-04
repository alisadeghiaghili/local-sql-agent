# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The admin panel's schema-drift card shows ``misplaced_tables``.

``schema_data.drift`` reports a table found in a different data source than
``schema.yaml`` assigns it to (``misplaced_tables``, each with the
``datasource:`` value to write). Drives the REAL ``web/admin/main.js`` --
same module-graph copy as ``test_web_ui_admin_auto_refresh.py`` -- with
``/admin/schema-drift`` answering a chosen payload (see
``run_admin_schema_drift.mjs``) and asserts on the HTML the card receives.

Requires ``node`` on PATH. Skipped (not failed) when unavailable.
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.web_ui.test_web_ui_admin_auto_refresh import _NODE, _prepare_copy

_HARNESS = Path(__file__).resolve().parent / "run_admin_schema_drift.mjs"

_BASE = {
    "checked_at": "t", "schemas_scanned": [], "warehouse_only": [], "schema_only": [],
    "type_changed": [], "unverifiable_tables": [], "baseline_available": True,
    "cache": {"cached": False, "age_seconds": 0, "ttl_seconds": 300},
}


def _card_html(payload: dict) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        main_mjs = _prepare_copy(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(main_mjs), json.dumps({**_BASE, **payload})],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])["html"]


def _misplaced(table="Future_Dim.Broker", suggested="Future_DM", found=("Future_DM",)):
    return {
        "table": table, "assigned": ["Auction_DM"], "missing_from": ["Auction_DM"],
        "found_in": list(found), "suggested_datasource": suggested,
        "hint": f"{table}: not in Auction_DM, found in {', '.join(found)}",
    }


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/admin/main.js")
class TestSchemaDriftCardPlacement:
    def test_a_misplaced_table_is_listed_with_the_line_to_write(self):
        html = _card_html({"misplaced_tables": [_misplaced()]})
        assert "جدول در منبع دادهٔ دیگری است" in html
        assert 'dir="ltr">Future_Dim.Broker</td>' in html
        assert 'dir="ltr">Auction_DM</td>' in html
        assert 'dir="ltr">Future_DM</td>' in html
        assert 'dir="ltr">datasource: Future_DM</td>' in html
        assert "انحرافی یافت نشد" not in html

    def test_several_suggested_sources_are_written_as_a_list(self):
        html = _card_html({"misplaced_tables": [
            _misplaced("Dim.Date", ["Future_DM", "Cold_DM"], ("Future_DM", "Cold_DM")),
        ]})
        assert "datasource: [Future_DM, Cold_DM]" in html

    def test_table_names_are_escaped(self):
        html = _card_html({"misplaced_tables": [_misplaced(table="<img src=x onerror=1>")]})
        assert "<img" not in html
        assert "&lt;img" in html

    def test_without_the_field_the_card_renders_as_before(self):
        html = _card_html({})
        assert "انحرافی یافت نشد" in html
        assert "جدول در منبع دادهٔ دیگری است" not in html

    def test_an_empty_list_is_no_drift(self):
        assert "انحرافی یافت نشد" in _card_html({"misplaced_tables": []})
