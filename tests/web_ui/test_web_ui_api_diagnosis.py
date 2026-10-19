# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A failed call to the configured API address is classified, once, in Persian.

6.14.0, two symptoms with one cause class -- the page does not say that the
address it calls is wrong:

* every status light red while the server log held only ``OPTIONS ... 400``:
  the page was opened as ``http://localhost:8077`` and the server allowed
  ``http://172.16.101.42:8077``; a browser shows that to the page as the same
  ``TypeError`` an unreachable host gives;
* every admin card ``404``: the address saved in the browser pointed at the
  static file server.

``web/js/diagnosis.js`` classifies one failed call as ``wrong_target``,
``cors_or_unreachable`` or ``other``. This drives the REAL file under Node
(``run_api_diagnosis.mjs``) with a fake ``fetch`` and asserts on every branch,
on the Persian texts, and on the two properties that stop thirteen cards from
saying the same long thing: one probe per round, one banner write per text.

Requires ``node`` on PATH. Skipped (not failed) when unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.web_ui import NODE_TIMEOUT_SECONDS

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DIAGNOSIS_JS = _REPO_ROOT / "web" / "js" / "diagnosis.js"
_HARNESS = Path(__file__).resolve().parent / "run_api_diagnosis.mjs"
_NODE = shutil.which("node")

PAGE = "http://localhost:8077"
BASE = "http://localhost:8000"

pytestmark = pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/diagnosis.js")


@pytest.fixture(scope="module")
def ran() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "diagnosis.mjs"
        copy.write_text(_DIAGNOSIS_JS.read_text(encoding="utf-8"), encoding="utf-8")
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(copy)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _kind(ran: dict, case: str) -> str:
    return ran["cases"][case]["diagnosis"]["kind"]


def _why(ran: dict, case: str) -> str | None:
    return ran["cases"][case]["diagnosis"]["why"]


class TestWrongTarget:
    def test_the_base_is_the_pages_own_server(self, ran):
        assert _kind(ran, "page_origin_equal_static_server") == "wrong_target"
        assert _why(ran, "page_origin_equal_static_server") == "same_origin"

    def test_the_pages_own_server_that_does_not_answer_at_all_is_still_wrong_target(self, ran):
        assert _kind(ran, "page_origin_equal_no_answer") == "wrong_target"

    def test_a_same_origin_api_is_not_called_wrong(self, ran):
        """The API behind one proxy with the page: /health is the API's own."""
        assert _kind(ran, "page_origin_equal_but_it_is_the_api") == "other"

    def test_health_answers_404(self, ran):
        assert (_kind(ran, "health_404"), _why(ran, "health_404")) == ("wrong_target", "404")

    def test_health_answers_html(self, ran):
        assert (_kind(ran, "health_html"), _why(ran, "health_html")) == ("wrong_target", "html")

    @pytest.mark.parametrize(
        "case", ["health_json_wrong_shape", "health_json_null", "health_text_plain_200"],
    )
    def test_health_answers_something_that_is_not_the_apis_response(self, ran, case):
        assert (_kind(ran, case), _why(ran, case)) == ("wrong_target", "shape")

    def test_a_non_json_body_from_a_static_server_is_wrong_target(self, ran):
        assert _kind(ran, "payload_failure_on_a_static_server") == "wrong_target"

    def test_the_probe_is_a_get_of_health_on_the_configured_base(self, ran):
        assert ran["cases"]["health_404"]["probed"] == [f"{BASE}/health"]


class TestCorsOrUnreachable:
    def test_a_network_type_error_is_cors_or_unreachable_without_probing(self, ran):
        """Probing would fail the same way; the browser cannot say which cause it is."""
        assert _kind(ran, "network_type_error") == "cors_or_unreachable"
        assert ran["cases"]["network_type_error"]["probed"] == []

    def test_an_error_that_carries_the_transport_code_is_a_network_failure(self, ran):
        assert ran["mapping"]["transport_code"] == {"type": "network"}
        assert ran["mapping"]["type_error"] == {"type": "network"}


class TestOther:
    def test_the_real_api_answering_health_means_the_failure_is_something_else(self, ran):
        assert _kind(ran, "real_api_answers_health") == "other"

    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
    def test_a_status_only_an_api_can_answer_is_never_probed(self, ran, status):
        case = ran["cases"][f"http_{status}"]
        assert case["diagnosis"]["kind"] == "other"
        assert case["probed"] == []

    def test_405_is_probed_like_404(self, ran):
        assert _kind(ran, "http_405") == "wrong_target"

    def test_nothing_to_diagnose_is_other(self, ran):
        assert _kind(ran, "no_failure") == "other"

    def test_a_probe_that_cannot_connect_is_not_a_verdict(self, ran):
        assert _kind(ran, "probe_cannot_reach_either") == "other"

    def test_errors_map_to_failures(self, ran):
        assert ran["mapping"]["syntax_error"] == {"type": "payload"}
        assert ran["mapping"]["payload_code"] == {"type": "payload"}
        assert ran["mapping"]["http"] == {"type": "http", "status": 404}
        assert ran["mapping"]["unknown"] is None
        assert ran["mapping"]["nothing"] is None


class TestHealthShape:
    def test_the_real_health_response_has_the_shape(self, ran):
        assert ran["shape"]["real"] is True
        assert ran["shape"]["degraded_without_model"] is True  # `model` is omitted for anonymous callers

    @pytest.mark.parametrize("name", ["empty_object", "wrong_status", "string_flags", "array", "null"])
    def test_anything_else_does_not(self, ran, name):
        assert ran["shape"][name] is False

    def test_it_matches_the_servers_health_model(self):
        """Pin the three field names against api/models.py so the two cannot drift."""
        from typing import get_args

        from api.models import HealthResponse

        fields = HealthResponse.model_fields
        assert {"status", "openai", "database"} <= set(fields)
        assert get_args(fields["status"].annotation) == ("ok", "degraded", "down")
        assert fields["openai"].annotation is bool and fields["database"].annotation is bool


class TestMessages:
    def test_wrong_target_names_the_current_base_the_page_origin_and_what_to_enter(self, ran):
        text = ran["messages"]["wrong_target_404_admin"]

        assert BASE in text and PAGE in text
        assert "http://<host>:8000" in text
        assert "نوار بالای صفحه" in text
        assert "API نیست" in text

    def test_wrong_target_on_the_pages_own_server_says_so(self, ran):
        text = ran["messages"]["wrong_target_same_origin_admin"]

        assert text.count(PAGE) == 2  # the base is the page's own origin
        assert "همان سرورِ خودِ این صفحه است" in text
        assert "نه API" in text
        assert "http://<host>:8000" in text

    def test_each_reason_for_not_being_the_api_is_named(self, ran):
        assert "404" in ran["messages"]["wrong_target_404_admin"]
        assert "HTML" in ran["messages"]["wrong_target_html_admin"]
        assert "شکل پاسخ API را ندارد" in ran["messages"]["wrong_target_shape_admin"]

    def test_the_analyst_page_has_no_address_bar_so_it_says_where_the_address_lives(self, ran):
        text = ran["messages"]["wrong_target_same_origin_analyst"]

        assert "web/js/config.js" in text and "DEFAULT_BASE_URL" in text
        assert "نوار بالای صفحه" not in text

    @pytest.mark.parametrize("surface", ["admin", "analyst"])
    def test_cors_or_unreachable_names_both_causes_with_their_remedies(self, ran, surface):
        text = ran["messages"][f"cors_or_unreachable_{surface}"]

        # the page's origin, verbatim, exactly as the value to add
        assert f"CORS_ALLOWED_ORIGINS={PAGE}" in text
        assert f"\nCORS_ALLOWED_ORIGINS={PAGE}\n" in text  # on a line of its own, to copy whole
        assert text.count(PAGE) == 2
        # cause 1: not allowed; remedy: add it, or open the page with an allowed address
        assert "مجاز" in text and ".env" in text and "ری‌استارت" in text
        assert "پروتکل، نام سرور و پورت" in text
        # cause 2: the API is not running at the base
        assert f"API روی {BASE} اجرا شده است" in text

    def test_other_has_no_text(self, ran):
        assert ran["messages"]["other"] is None

    def test_the_card_text_is_short_and_points_to_the_banner(self, ran):
        assert ran["messages"]["card"] == "به API نرسید؛ پیام بالای صفحه را ببینید."

    def test_no_english_prose_beyond_literal_values(self, ran):
        """What remains outside Persian is a literal: addresses, the setting name, a file name."""
        import re

        for name, text in ran["messages"].items():
            if text is None:
                continue
            leftover = re.sub(
                r"https?://\S+|CORS_ALLOWED_ORIGINS|DEFAULT_BASE_URL|web/js/config\.js|\.env|/health|API|HTML|<host>|\d+",
                "", text,
            )
            assert not re.search(r"[A-Za-z]{2,}", leftover), (name, leftover)


class TestOneAnswerPerRound:
    def test_thirteen_failing_cards_cost_one_probe_and_share_one_answer(self, ran):
        assert ran["diagnoser"]["thirteen_cards_kinds"] == ["wrong_target"]
        assert ran["diagnoser"]["thirteen_cards_probes"] == 1
        assert ran["diagnoser"]["same_round_again_probes"] == 1

    def test_a_new_round_asks_again(self, ran):
        assert ran["diagnoser"]["after_reset_probes"] == 2

    def test_a_403_is_not_diagnosed_and_costs_no_probe(self, ran):
        assert ran["diagnoser"]["forbidden_kind"] == "other"
        assert ran["diagnoser"]["forbidden_probes"] == 2

    def test_a_different_address_is_a_different_question(self, ran):
        assert ran["diagnoser"]["a_new_address_is_a_new_question"] == [
            f"{BASE}/health", "http://localhost:9000/health",
        ]

    def test_the_banner_is_written_once_however_many_calls_ask_for_it(self, ran):
        shown = ran["banner"]["after_thirteen_calls"]
        assert shown["writes"] == 1
        assert shown["hidden"] is False
        assert BASE in shown["text"]

    def test_the_banner_hides_for_other_and_on_request(self, ran):
        assert ran["banner"]["after_other"]["hidden"] is True
        assert ran["banner"]["after_hide"]["hidden"] is True
        assert ran["banner"]["no_element_is_fine"] is False
