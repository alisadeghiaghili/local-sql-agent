# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The admin panel says once that the address is wrong, not thirteen times 404.

6.14.0: every admin section showed 404 and the API log had no ``/admin/...``
request at all, because the address saved in the browser pointed at the static
file server. Each card said ``خطا: HTTP 404``; nothing said "this address is not
the API". The same panel, opened from a page the server had not allowed, showed
the same card errors for a refused cross-origin call.

Drives the REAL ``web/admin/main.js`` (see ``run_admin_diagnosis.mjs`` for the
DOM/fetch shim) with the configured address answering the way a static file
server, a wrong service, a refusing or absent API and the real API do, and
asserts on what the page ends up showing: one page-level banner, thirteen short
card lines pointing to it, one probe of ``/health``, and every pre-existing
message (401, 403, capability gaps, 500) unchanged.

Requires ``node`` on PATH. Skipped (not failed) when unavailable.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.web_ui import NODE_TIMEOUT_SECONDS
from tests.web_ui.test_web_ui_admin_auto_refresh import _NODE, _prepare_copy

_HARNESS = Path(__file__).resolve().parent / "run_admin_diagnosis.mjs"

PAGE = "http://localhost:8077"
BASE = "http://localhost:8000"
CARD_LINE = "به API نرسید؛ پیام بالای صفحه را ببینید."

pytestmark = pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/admin/main.js")


def _run(scenario: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        main_mjs = _prepare_copy(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(main_mjs), json.dumps(scenario)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def _health_probes(shown: dict) -> list[str]:
    return [u for u in shown["fetched"] if u.endswith("/health")]


@pytest.mark.parametrize("mode", ["static_404", "html_200", "wrong_health"])
class TestAddressThatIsNotTheApi:
    def test_one_banner_names_the_base_and_the_page(self, mode):
        shown = _run({"mode": mode})

        assert shown["diagnosis"]["hidden"] is False
        text = shown["diagnosis"]["text"]
        assert BASE in text and PAGE in text
        assert "به API نمی‌رسد" in text and "API نیست" in text
        assert "http://<host>:8000" in text
        assert "CORS_ALLOWED_ORIGINS" not in text  # a different problem, not offered here

    def test_the_banner_is_written_once_for_thirteen_failing_cards(self, mode):
        shown = _run({"mode": mode})

        assert len(shown["cards"]) == 13
        assert shown["diagnosis"]["writes"] == 1

    def test_the_address_is_asked_about_once(self, mode):
        shown = _run({"mode": mode})

        assert _health_probes(shown) == [f"{BASE}/health"]

    def test_every_card_is_short_and_points_to_the_banner(self, mode):
        shown = _run({"mode": mode})

        for name, html in shown["bodies"].items():
            assert _text(html) == CARD_LINE, name
            assert 'class="admin-loading"' in html, name
            assert "404" not in html and "http://" not in html, name  # not repeating the long message


class TestRefusedOrUnreachable:
    def test_the_banner_names_both_causes_and_the_pages_own_origin(self):
        shown = _run({"mode": "network"})

        text = shown["diagnosis"]["text"]
        assert shown["diagnosis"]["hidden"] is False
        assert f"\nCORS_ALLOWED_ORIGINS={PAGE}\n" in text
        assert f"API روی {BASE} اجرا شده است" in text

    def test_the_origin_is_the_pages_even_when_the_page_is_on_another_address(self):
        shown = _run({"mode": "network", "page": "http://172.16.101.42:8077"})

        assert "CORS_ALLOWED_ORIGINS=http://172.16.101.42:8077\n" in shown["diagnosis"]["text"]

    def test_once_and_without_probing(self):
        shown = _run({"mode": "network"})

        assert shown["diagnosis"]["writes"] == 1
        assert _health_probes(shown) == []
        for name, html in shown["bodies"].items():
            assert _text(html) == CARD_LINE, name

    def test_a_page_on_the_apis_own_origin_is_told_it_is_not_the_api(self):
        """The saved base is the page itself: the 6.14.0 admin symptom, in its simplest form."""
        shown = _run({"mode": "static_404", "page": BASE})

        assert "همان سرورِ خودِ این صفحه است" in shown["diagnosis"]["text"]
        assert shown["diagnosis"]["text"].count(BASE) == 2


class TestTheApiAnswering:
    def test_a_500_keeps_its_own_card_message_and_raises_no_banner(self):
        shown = _run({"mode": "api_500"})

        assert shown["diagnosis"]["hidden"] is True
        assert shown["diagnosis"]["writes"] == 0
        assert "خطا:" in shown["bodies"]["cache"]
        assert "refused by the test server" in shown["bodies"]["cache"]
        assert CARD_LINE not in shown["bodies"]["cache"]

    def test_a_403_keeps_the_existing_messages_exactly(self):
        shown = _run({"mode": "api_403"})

        assert shown["diagnosis"]["hidden"] is True
        assert shown["forbidden"]["hidden"] is False
        assert "این کلید برای این بخش دسترسی مدیریتی ندارد" in shown["bodies"]["cache"]
        assert "<code>operations</code>" in shown["bodies"]["keys"]
        assert "<code>security</code>" in shown["bodies"]["accessRequests"]
        assert _health_probes(shown) == []  # a 403 is the API answering: no probe

    def test_a_404_from_the_real_api_is_not_called_a_wrong_address(self):
        """/health is the API's own, so a 404 on an /admin route is that route's business."""
        shown = _run({"mode": "api_404"})

        assert shown["diagnosis"]["hidden"] is True
        assert shown["diagnosis"]["writes"] == 0
        assert _health_probes(shown) == [f"{BASE}/health"]  # asked once, and the answer was the API
        assert "no such admin route" in shown["bodies"]["cache"]
        assert CARD_LINE not in shown["bodies"]["cache"]
