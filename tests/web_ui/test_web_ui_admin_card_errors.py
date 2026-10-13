# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""An admin card is never left blank when its request fails.

``refreshOne`` in ``web/admin/main.js`` cleared the card body in its catch
block and then, for a 403 that is not a capability gap and for a 401, only
raised the page-level banner or notice -- leaving the card an empty box. The
operator scrolled to a card and saw nothing, with the explanation at the top
of the page. Every branch of the catch now writes its own sentence into the
card (in the ``admin-loading`` style).

Drives the REAL ``web/admin/main.js`` with every ``/admin/*`` call failing
(see ``run_admin_card_errors.mjs`` for the DOM/fetch shim) and asserts on the
HTML each card ends up with.

Requires ``node`` on PATH. Skipped (not failed) when unavailable.
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.web_ui.test_web_ui_admin_auto_refresh import _MAIN_JS, _NODE, _prepare_copy

_HARNESS = Path(__file__).resolve().parent / "run_admin_card_errors.mjs"

#: Cards where a 403 means "your admin key lacks this capability".
_CAPABILITY_CARDS = {"keys": "operations", "accessRequests": "security"}

_CARDS_DECLARATION = re.compile(r"const CARDS = \[(.*?)\];", re.DOTALL)


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


def _card_names(source: str) -> list[str]:
    declared = _CARDS_DECLARATION.search(source)
    assert declared, "no `const CARDS = [...]` declaration found"
    return re.findall(r'"(\w+)"', declared.group(1))


def test_harness_card_list_matches_main_js() -> None:
    in_main = _card_names(_MAIN_JS.read_text(encoding="utf-8"))
    in_harness = _card_names(_HARNESS.read_text(encoding="utf-8"))
    assert in_harness == in_main


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/admin/main.js")
class TestNoCardIsLeftBlank:
    @pytest.mark.parametrize(
        "scenario",
        [
            {"status": 403, "key": True},
            {"status": 401, "key": True},
            {"status": 401, "key": False},
            {"status": 500, "key": True},
            {"network": True, "key": True},
        ],
        ids=["403", "401-key-stored", "401-no-key", "500", "network-failure"],
    )
    def test_every_card_has_text_after_every_kind_of_failure(self, scenario):
        shown = _run(scenario)

        assert shown["cards"], "the harness saw no cards"
        for name, html in shown["bodies"].items():
            assert _text(html), f"card {name!r} was left blank after {scenario}"
            assert "در حال بارگذاری" not in html, f"card {name!r} is still 'loading' after {scenario}"
            assert 'class="admin-loading"' in html, f"card {name!r} is not in the admin-loading style"

    def test_403_card_says_the_key_has_no_access_here_and_points_to_the_banner(self):
        shown = _run({"status": 403, "key": True})

        html = shown["bodies"]["cache"]
        assert "این کلید برای این بخش دسترسی مدیریتی ندارد" in html
        assert "هشدار بالای صفحه" in html
        assert shown["banner"]["hidden"] is False
        assert "دسترسی مدیریتی ندارد" in shown["banner"]["text"]

    def test_403_on_a_capability_card_still_names_the_missing_capability(self):
        shown = _run({"status": 403, "key": True})

        for name, capability in _CAPABILITY_CARDS.items():
            html = shown["bodies"][name]
            assert f"<code>{capability}</code>" in html
            assert f"--{capability}" in html
            assert "هشدار بالای صفحه" not in html

    def test_401_with_a_stored_key_says_the_server_did_not_accept_it(self):
        shown = _run({"status": 401, "key": True})

        assert "سرور کلید را نپذیرفت" in shown["bodies"]["cache"]
        assert any("نپذیرفت" in n for n in shown["notice"])

    def test_401_without_a_key_asks_for_one(self):
        shown = _run({"status": 401, "key": False})

        assert "کلید API وارد نشده است" in shown["bodies"]["cache"]
        assert any("کلید API وارد نشده" in n for n in shown["notice"])

    def test_other_failures_keep_their_own_messages(self):
        shown = _run({"status": 500, "key": True})

        assert "خطا:" in shown["bodies"]["cache"]
        assert "refused by the test server" in shown["bodies"]["cache"]
