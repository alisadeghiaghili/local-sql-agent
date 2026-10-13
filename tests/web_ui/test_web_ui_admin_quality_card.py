# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The admin card "کیفیت در استفاده‌ی واقعی" -- real-use quality and latency.

Drives the REAL ``web/admin/main.js`` (see ``run_admin_quality_card.mjs`` for
the DOM/fetch shim) with ``GET /admin/quality`` answering with a recorded
report shape, and asserts on the HTML the card ends up with:

* the figures are there -- overall, per data source and per stage;
* the two notes the card must carry: the confirmed share is a lower bound,
  not accuracy, and the ``llm`` stage split appears only with
  ``LLM_STREAM_TIMINGS=true``;
* the card is never blank and never prints ``undefined`` / ``NaN`` / ``null``,
  whether the report is full, has no ratings, has no streamed calls, is empty,
  or the request fails (``test_web_ui_admin_card_errors.py`` also runs this
  card through every failure kind);
* the card stays in the 30-second auto-refresh (the route is cached on the
  server), while its own button forces a recompute (``refresh=1``) and the
  window selector sends ``days``.

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

from tests.web_ui.test_web_ui_admin_auto_refresh import _ADMIN_DIR, _MAIN_JS, _NODE, _prepare_copy

_HARNESS = Path(__file__).resolve().parent / "run_admin_quality_card.mjs"
_INDEX_HTML = _ADMIN_DIR / "index.html"
_CARDS_DECLARATION = re.compile(r"const CARDS = \[(.*?)\];", re.DOTALL)
_EXPENSIVE_DECLARATION = re.compile(r"const EXPENSIVE_CARDS = new Set\(\[(.*?)\]\);", re.DOTALL)


def _stats(count=10, p50=120, p95=450):
    return {"count": count, "mean": p50, "p50": p50, "p95": p95, "p99": p95}


def _metrics(questions=40, *, ratings=True, streamed=6, source_note=""):
    return {
        "questions": questions,
        "executed": 30,
        "not_executed_without_error": 0,
        "execution_success_rate": 0.75,
        "guard_rejected": 4,
        "guard_rejection_rate": 0.1,
        "needed_correction": 6,
        "needed_correction_rate": 0.15,
        "empty_results": 3,
        "empty_result_rate": 0.1,
        "out_of_scope": 2,
        "out_of_scope_rate": 0.05,
        "errors": 10,
        "error_rate": 0.25,
        "error_rate_by_class": {
            "guard_policy": {"count": 4, "rate": 0.1},
            "scope_decline": {"count": 2, "rate": 0.05},
        },
        "ratings": (
            {"available": True, "confirmed": 12, "flagged": 3, "rated": 15, "confirmed_share_of_rated": 0.8}
            if ratings else
            {"available": False, "confirmed": None, "flagged": None, "rated": None,
             "confirmed_share_of_rated": None}
        ),
        "latency": {
            "total_ms": _stats(40, 1500, 9000),
            "by_stage_ms": {
                "plan": _stats(40, 5, 9), "prompt": _stats(40, 10, 20), "llm": _stats(30, 1200, 8000),
                "guard": _stats(30, 2, 4), "execute": _stats(30, 200, 900), "interpret": _stats(0, None, None),
            },
            "llm_split": {
                "llm_call_count": 30, "streamed_call_count": streamed,
                "ttft_ms": _stats(streamed, 700, 2500) if streamed else _stats(0, None, None),
                "generation_ms": _stats(streamed, 900, 5000) if streamed else _stats(0, None, None),
            },
        },
    }


def _section(**kw):
    return {
        "since": "2026-10-06T12:00:00", "until": "2026-10-13T12:00:00",
        "overall": _metrics(**kw),
        "by_datasource": {
            "sales": _metrics(25, **kw),
            "stock": _metrics(15, **kw),
        },
    }


def _report(**kw):
    return {
        "generated_at": "2026-10-13T12:00:00", "window_days": 7,
        "recent": _section(**kw), "all_time": _section(**kw),
        "caveats": {"confirmed_share": "...", "latency_stages": "..."},
        "cache": {"cached": False, "age_seconds": 0, "ttl_seconds": 300},
    }


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
    return re.sub(r"<[^>]+>", " ", html)


def _card_names(source: str, pattern: re.Pattern[str]) -> list[str]:
    declared = pattern.search(source)
    assert declared, "declaration not found"
    return re.findall(r'"(\w+)"', declared.group(1))


# ---------------------------------------------------------------------------
# Wiring that does not need node
# ---------------------------------------------------------------------------


def test_the_card_exists_in_the_page_and_the_jump_nav() -> None:
    html = _INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="sec-quality"' in html and 'href="#sec-quality"' in html
    assert 'id="quality-body"' in html
    assert 'data-refresh="quality"' in html
    assert "کیفیت در استفاده‌ی واقعی" in html
    assert 'id="quality-days"' in html


def test_quality_is_a_card_and_stays_in_the_auto_refresh() -> None:
    source = _MAIN_JS.read_text(encoding="utf-8")
    assert "quality" in _card_names(source, _CARDS_DECLARATION)
    assert _card_names(source, _EXPENSIVE_DECLARATION) == ["health", "schemaDrift"], (
        "quality is cached on the server instead of being excluded from the 30s refresh"
    )


def test_harness_card_list_matches_main_js() -> None:
    assert _card_names(_HARNESS.read_text(encoding="utf-8"), _CARDS_DECLARATION) == _card_names(
        _MAIN_JS.read_text(encoding="utf-8"), _CARDS_DECLARATION,
    )


# ---------------------------------------------------------------------------
# The rendered card
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/admin/main.js")
class TestQualityCard:
    def test_shows_the_figures_overall_per_source_and_per_stage(self):
        shown = _run({"payload": _report()})
        html = shown["body"]
        text = _text(html)

        assert "۷ روز گذشته" in text and "کل دوره" in text
        assert "75.0%" in text, "execution success rate"
        assert "10.0%" in text and "15.0%" in text and "5.0%" in text
        assert "80.0%" in text, "confirmed share of rated"
        assert "guard_policy" in text and "scope_decline" in text, "error rate by class"
        assert "sales" in text and "stock" in text, "per data source"
        assert "1500" in text and "9000" in text, "end-to-end p50 / p95"
        for stage in ("plan", "prompt", "llm", "guard", "execute", "interpret"):
            assert f">{stage}<" in html, f"stage {stage} row"
        assert "llm · ttft" in text and "llm · generation" in text
        assert "700" in text and "2500" in text, "ttft p50 / p95"
        assert "همین الان اجرا شد" in text, "the cache age line"

    def test_says_the_confirmed_share_is_not_accuracy_and_when_the_llm_split_appears(self):
        text = _text(_run({"payload": _report()})["body"])

        assert "حد پایین" in text and "نه دقت" in text
        assert "LLM_STREAM_TIMINGS=true" in text

    def test_both_notes_are_there_even_when_there_is_no_data(self):
        text = _text(_run({"payload": {}})["body"])

        assert "داده‌ای در این بازه یافت نشد" in text
        assert "نه دقت" in text and "LLM_STREAM_TIMINGS=true" in text

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"ratings": False}, {"streamed": 0}, {"ratings": False, "streamed": 0}],
        ids=["full", "no-ratings", "no-streamed-calls", "neither"],
    )
    def test_never_prints_undefined_nan_or_null(self, kwargs):
        html = _run({"payload": _report(**kwargs)})["body"]

        assert _text(html).strip()
        for bad in ("undefined", "NaN", "null", "[object"):
            assert bad not in html, f"{bad!r} leaked into the card with {kwargs}"

    def test_unavailable_ratings_show_a_dash_not_zero(self):
        text = _text(_run({"payload": _report(ratings=False)})["body"])
        assert "—" in text
        assert "80.0%" not in text

    def test_a_partial_payload_from_an_older_server_still_renders(self):
        partial = {"window_days": 7, "recent": {"overall": {"questions": 3}}}
        html = _run({"payload": partial})["body"]

        assert "undefined" not in html and "NaN" not in html
        assert "پرسش‌ها" in _text(html)

    @pytest.mark.parametrize("status", [500, 403, 401])
    def test_a_failed_request_leaves_a_sentence_in_the_card(self, status):
        html = _run({"status": status})["body"]

        assert _text(html).strip(), f"the card was blank after HTTP {status}"
        assert "در حال بارگذاری" not in html
        assert 'class="admin-loading"' in html

    def test_a_server_error_names_the_server_message_in_the_card(self):
        html = _run({"status": 500})["body"]
        assert "refused by the test server" in html


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/admin/main.js")
class TestRefreshBehaviour:
    def test_boot_and_the_auto_refresh_tick_fetch_the_report_without_forcing(self):
        shown = _run({"payload": _report(), "tick": True})

        assert len(shown["bootUrls"]) == 1
        assert shown["bootUrls"][0].endswith("/admin/quality?days=7")
        assert len(shown["tickUrls"]) == 1, "quality is part of the 30s auto-refresh"
        assert all("refresh=1" not in u for u in shown["bootUrls"] + shown["tickUrls"]), (
            "the timer must be served from the server's cache"
        )

    def test_the_cards_own_button_forces_a_recompute(self):
        shown = _run({"payload": _report(), "click": True})

        assert len(shown["clickUrls"]) == 1
        assert "refresh=1" in shown["clickUrls"][0] and "days=7" in shown["clickUrls"][0]

    def test_changing_the_window_sends_days_and_reuses_the_cache(self):
        shown = _run({"payload": _report(), "change": True})

        assert len(shown["changeUrls"]) == 1
        assert "days=30" in shown["changeUrls"][0]
        assert "refresh=1" not in shown["changeUrls"][0]
