# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Two things the web UI showed wrongly, and the seams that fix them.

The pipeline. ``web/js/render/pipeline.js`` draws five steps and ticks
them from SSE ``stage`` events. The endpoint sent exactly one, naming a
stage ``"plan"`` — an id no step has — so ``setStage`` found no element,
returned silently, and all five sat at "waiting" for the whole turn while
the answer appeared beside them. Nothing in the stack was broken enough to
notice: the events were well-formed, the stream was valid, the turn was
correct.

Progress now comes from :class:`~observability.timing.StageTimer`, which
already knew when each stage started and finished and simply had no way to
say so. The endpoint's own tests
(``tests/test_v2_endpoints.py::TestAskTurnStreaming``) cover the wire
format; this module covers the seam underneath it.

The SQL. ``sql_display`` was the model's own text, so its formatting was
the model's mood: a tidy multi-line statement for one question, a single
300-character line for the next. A reader cannot tell that says nothing
about the query.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observability.timing import StageTimer
from security.sql_guard import pretty_sql


# ---------------------------------------------------------------------------
# StageTimer's observer
# ---------------------------------------------------------------------------

class TestStageTimerReportsProgress:
    def test_a_stage_reports_running_then_done(self):
        seen: list[tuple[str, str]] = []
        timer = StageTimer(on_stage=lambda n, s: seen.append((n, s)))
        with timer.stage("llm"):
            pass
        assert seen == [("llm", "running"), ("llm", "done")]

    def test_a_failing_stage_reports_error_not_done(self):
        """The UI draws the two differently, and a stage that errored is
        where the reader should be looking."""
        seen: list[tuple[str, str]] = []
        timer = StageTimer(on_stage=lambda n, s: seen.append((n, s)))
        with pytest.raises(ValueError):
            with timer.stage("execute"):
                raise ValueError("boom")
        assert seen == [("execute", "running"), ("execute", "error")]

    def test_timings_are_unaffected_by_having_an_observer(self):
        timer = StageTimer(on_stage=lambda n, s: None)
        with timer.stage("guard"):
            pass
        assert timer.snapshot()["guard_ms"] >= 0

    def test_no_observer_is_the_default_and_costs_nothing(self):
        timer = StageTimer()
        with timer.stage("plan"):
            pass
        assert timer.snapshot()["plan_ms"] >= 0

    def test_a_broken_observer_cannot_fail_the_turn(self):
        """Reporting progress is a side concern. A turn that succeeded must
        not be reported as failed because the thing watching it fell over
        — the browser losing its progress bar is not worth losing the
        answer for."""
        def explode(name: str, state: str) -> None:
            raise RuntimeError("observer is broken")

        timer = StageTimer(on_stage=explode)
        with timer.stage("llm"):
            pass  # does not raise
        assert timer.snapshot()["llm_ms"] >= 0

    def test_a_broken_observer_does_not_mask_a_real_failure(self):
        """The swallow must be one-directional: the observer's exception is
        dropped, the stage's own is not."""
        timer = StageTimer(on_stage=lambda n, s: 1 / 0)
        with pytest.raises(ValueError, match="the real problem"):
            with timer.stage("execute"):
                raise ValueError("the real problem")


# ---------------------------------------------------------------------------
# pretty_sql
# ---------------------------------------------------------------------------

class TestPrettySql:
    def test_a_one_line_statement_becomes_readable(self):
        one_line = (
            "SELECT TOP 100 gd.PersianYear, SUM(cc.Quantity) AS TotalQuantity "
            "FROM [Auction_Fact].[CustomerContract] cc "
            "INNER JOIN [General_Dim].[Date] gd ON cc.Date_ID = gd.ID "
            "GROUP BY gd.PersianYear"
        )
        out = pretty_sql(one_line)
        assert out.count("\n") >= 4, "the whole point is that it stops being one line"
        assert "FROM" in out and "GROUP BY" in out

    def test_unparseable_input_comes_back_unchanged(self):
        """Cosmetic work must never cost a successful query its result."""
        garbage = "not sql (((("
        assert pretty_sql(garbage) == garbage

    @pytest.mark.parametrize("value", ["", "   ", "\n\t "])
    def test_empty_input_is_returned_as_given(self, value):
        assert pretty_sql(value) == value

    def test_it_does_not_change_what_the_query_touches(self):
        """Formatting is presentation. If it altered the tables or columns
        involved, the SQL on screen would no longer describe the SQL that
        ran — and this string is what the copy button hands the analyst."""
        from security.sql_guard import extract_touched_tables

        sql = (
            "SELECT TOP 10 c.Name FROM [Auction_Fact].[CustomerContract] cc "
            "INNER JOIN [Auction_Dim].[Customer] c ON cc.BuyerCustomer_ID = c.ID"
        )
        assert sorted(extract_touched_tables(pretty_sql(sql))) == sorted(
            extract_touched_tables(sql)
        )

    def test_the_formatted_text_still_parses(self):
        """It is handed to the analyst to paste into a query tool."""
        import sqlglot

        sql = "SELECT a, b FROM t WHERE a = 1 ORDER BY b"
        sqlglot.parse_one(pretty_sql(sql), read="tsql")  # does not raise
