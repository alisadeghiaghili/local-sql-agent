# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The analyst status view says why the three lights are red.

6.14.0: all three lights red, the server log holding only ``OPTIONS /health``,
``OPTIONS /v2/sessions`` and ``OPTIONS /v2/access-requests`` -- each ``400``.
The explanation existed (``describeTransportFailure``) but only as the lights'
tooltip, which nobody reads. ``refreshHealth`` in ``web/js/main.js`` now also
asks ``diagnosis.js`` and shows one page-level banner.

``main.js`` cannot be executed whole (its top level is the DOM-heavy boot), so
this extracts the REAL ``refreshHealth`` and the REAL ``diagnoser`` declaration
from it, verbatim, and runs them under Node against a fake ``Api.health()`` and
``fetch`` with the real ``diagnosis.js`` (``run_analyst_status_diagnosis.mjs``).

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
from tests.web_ui.test_web_ui_admin_auto_refresh import _NODE
from tests.web_ui.test_web_ui_onclarify_live_mode import _extract_function

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MAIN_JS = _REPO_ROOT / "web" / "js" / "main.js"
_DIAGNOSIS_JS = _REPO_ROOT / "web" / "js" / "diagnosis.js"
_INDEX_HTML = _REPO_ROOT / "web" / "index.html"
_HARNESS = Path(__file__).resolve().parent / "run_analyst_status_diagnosis.mjs"

PAGE = "http://localhost:8077"
BASE = "http://localhost:8000"

_MODULE = """\
// Written at test time by tests/web_ui/test_web_ui_analyst_status_diagnosis.py.
// The real refreshHealth and the real `diagnoser` declaration from
// web/js/main.js, verbatim, wired to spies standing in for the DOM.
import {{
  CODE_UNEXPECTED_PAYLOAD, createDiagnoser, hideDiagnosisBanner, isApiHealthShape, showDiagnosisBanner,
}} from "./diagnosis.mjs";

export class ApiError extends Error {{
  constructor(message, status, code = null) {{ super(message); this.status = status; this.code = code; }}
}}

export const state = {{ baseUrl: "" }};
let healthImpl = async () => {{ throw new Error("no health impl"); }};
export const setHealthImpl = (fn) => {{ healthImpl = fn; }};
const api = {{ health: () => healthImpl() }};

const calls = [];
function setHealth(a, l, d, label) {{ calls.push({{ lights: [a, l, d], tooltip: label }}); }}
export const setHealthCalls = () => calls.length;
export const lastLights = () => (calls.at(-1) || {{}}).lights;
export const lastTooltip = () => (calls.at(-1) || {{}}).tooltip;

const banner = {{
  hidden: true, writes: 0, _text: "",
  get textContent() {{ return this._text; }},
  set textContent(v) {{ this.writes += 1; this._text = v; }},
}};
const $ = (id) => (id === "api-diagnosis" ? banner : null);
export const bannerState = () => ({{ hidden: banner.hidden, text: banner._text, writes: banner.writes }});
export function reset() {{
  calls.length = 0; banner.hidden = true; banner.writes = 0; banner._text = "";
}}

// ---- extracted verbatim from web/js/main.js ----------------------------
{diagnoser_src}

{refresh_health_src}
// ---- end extracted source -----------------------------------------------

export {{ refreshHealth }};
"""

pytestmark = pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js code")


def _diagnoser_declaration(src: str) -> str:
    found = re.search(r"const diagnoser = createDiagnoser\(\{.*?\}\);\nlet healthCheckSeq = 0;", src, re.DOTALL)
    assert found, "web/js/main.js no longer declares `const diagnoser = createDiagnoser({...});` and `healthCheckSeq`"
    return found.group(0)


@pytest.fixture(scope="module")
def ran() -> dict:
    src = _MAIN_JS.read_text(encoding="utf-8")
    module = _MODULE.format(
        diagnoser_src=_diagnoser_declaration(src),
        refresh_health_src=_extract_function(src, "async function refreshHealth() {", "refreshHealth"),
    )
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "diagnosis.mjs").write_text(_DIAGNOSIS_JS.read_text(encoding="utf-8"), encoding="utf-8")
        built = Path(tmp) / "status.mjs"
        built.write_text(module, encoding="utf-8")
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(built)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestHealthyApi:
    def test_no_banner_and_the_lights_follow_the_response(self, ran):
        shown = ran["real_api"]

        assert shown["banner"]["hidden"] is True
        assert shown["banner"]["writes"] == 0
        assert shown["lights"] == [True, True, True]


class TestRefusedOrUnreachable:
    def test_the_banner_names_both_causes_and_the_pages_origin(self, ran):
        shown = ran["cors_refused"]

        assert shown["lights"] == [False, False, False]
        assert shown["banner"]["hidden"] is False
        text = shown["banner"]["text"]
        assert f"\nCORS_ALLOWED_ORIGINS={PAGE}\n" in text
        assert f"API روی {BASE} اجرا شده است" in text
        assert shown["probed"] == []

    def test_three_failures_in_a_row_leave_one_banner_text(self, ran):
        shown = ran["three_failures_in_a_row"]

        assert shown["setHealthCalls"] == 3
        assert shown["banner"]["writes"] == 1
        assert shown["banner"]["hidden"] is False


@pytest.mark.parametrize("scenario", ["static_server_404", "html_200", "other_service_json"])
class TestAddressThatIsNotTheApi:
    def test_the_banner_says_the_address_is_not_the_api(self, ran, scenario):
        shown = ran[scenario]

        assert shown["lights"] == [False, False, False]
        assert shown["banner"]["hidden"] is False
        text = shown["banner"]["text"]
        assert BASE in text and PAGE in text and "API نیست" in text
        assert "web/js/config.js" in text and "http://<host>:8000" in text
        assert shown["probed"] == [f"{BASE}/health"]


class TestOtherFailures:
    def test_a_server_error_shows_no_banner_and_costs_no_probe(self, ran):
        shown = ran["server_error"]

        assert shown["lights"] == [False, False, False]
        assert shown["banner"]["hidden"] is True
        assert shown["probed"] == []


class TestTheBannerFollowsTheLatestCheck:
    def test_it_goes_away_once_the_api_answers(self, ran):
        assert ran["fixed_afterwards"]["whileWrong"]["hidden"] is False
        assert ran["fixed_afterwards"]["afterFix"]["hidden"] is True

    def test_a_slow_older_check_does_not_overwrite_a_newer_answer(self, ran):
        assert ran["overlapping_checks"]["hidden"] is True
        assert ran["overlapping_checks"]["writes"] == 0


class TestWiring:
    """What the extracted code cannot prove about the page around it."""

    def test_main_js_wires_the_diagnoser_to_the_current_base_and_the_pages_origin(self):
        src = _MAIN_JS.read_text(encoding="utf-8")
        declaration = _diagnoser_declaration(src)

        assert "getBaseUrl: () => state.baseUrl" in declaration
        assert "getPageOrigin: () => location.origin" in declaration

    def test_the_diagnoser_is_declared_before_the_boot_calls_that_use_it(self):
        src = _MAIN_JS.read_text(encoding="utf-8")

        assert src.index("const diagnoser = createDiagnoser(") < src.index("\nsetMode(state.mode);")

    def test_simulated_mode_hides_the_banner(self):
        src = _MAIN_JS.read_text(encoding="utf-8")
        simulated = src[src.index('if (mode === "simulated") {'):src.index("refreshHealth();\n  }\n\n  refreshSessionsForMode")]

        assert 'hideDiagnosisBanner($("api-diagnosis"))' in simulated

    def test_the_page_has_the_banner_element_hidden_and_announced(self):
        html = _INDEX_HTML.read_text(encoding="utf-8")
        element = re.search(r'<div id="api-diagnosis"[^>]*>', html)

        assert element, "web/index.html has no #api-diagnosis element"
        assert 'role="alert"' in element.group(0) and " hidden" in element.group(0)
        assert "</div>" in html[element.end():element.end() + 12]  # empty: filled by textContent only
