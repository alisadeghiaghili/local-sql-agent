# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The real-use quality report: ``observability.quality`` and its CLI section.

The computation is checked on hand-built audit records (generic names only),
including records in the format of older versions that lack fields, and on
an empty log. The ratings come from a real application database on
``tmp_path``. The CLI section is run through the real
``scripts.analyze_audit_log.main`` and must stay in that script's privacy
mode: counts and percentiles, never question text.
"""

from __future__ import annotations

import doctest
import json
from datetime import datetime, timedelta, timezone

import pytest

import config as cfg
import observability.quality as quality
from appdb.confirmations import confirm_turn
from appdb.engine import dispose_app_engine
from appdb.feedback import submit_flag
from observability.audit import AuditRecord, save_audit_record
from observability.quality import (
    Ratings,
    build_quality_report,
    load_ratings,
    render_quality_text,
)
from scripts import analyze_audit_log as aal

NOW = datetime(2026, 10, 13, 12, 0, 0)
SECRET = "پرسش-محرمانه-۵۵۲"


def _rec(
    *, ts="2026-10-12T10:00:00", question="q", sql="SELECT 1 AS n", code=None, rows=1,
    ds="sales", total=100, stages=None, llm=None, guard=None, session=None, turn=None,
    columns="auto", **extra,
):
    record = {
        "timestamp": ts,
        "request_id": f"r_{abs(hash((ts, question, turn))) % 10**6}",
        "question": question,
        "generated_sql": sql,
        "guard": guard if guard is not None else {"verdict": "allowed"},
        "row_count": rows,
        "error_code": code,
        "timings": {"total_ms": total, **(stages or {})},
        "llm": llm,
        "datasource": ds,
        "session_id": session,
        "turn_id": turn,
        "columns": (["n"] if code is None and sql else None) if columns == "auto" else columns,
    }
    record.update(extra)
    return record


# ---------------------------------------------------------------------------
# Empty and old-format logs
# ---------------------------------------------------------------------------


class TestEmptyAndOldLogs:
    def test_an_empty_log_yields_zeros_and_none_rates_not_an_error(self):
        report = build_quality_report([], now=NOW)

        for section in (report["recent"], report["all_time"]):
            overall = section["overall"]
            assert overall["questions"] == 0
            assert overall["executed"] == 0
            assert overall["execution_success_rate"] is None
            assert overall["empty_result_rate"] is None
            assert overall["ratings"]["confirmed_share_of_rated"] is None
            assert overall["latency"]["total_ms"]["count"] == 0
            assert overall["latency"]["total_ms"]["p50"] is None
            assert overall["latency"]["llm_split"]["ttft_ms"]["count"] == 0
            assert section["by_datasource"] == {}
        json.dumps(report)  # serialisable, as the admin route returns it

    def test_a_record_with_almost_no_fields_is_counted_and_breaks_nothing(self):
        report = build_quality_report([{"timestamp": "2026-10-12T10:00:00"}, {}], now=NOW)

        assert report["all_time"]["overall"]["questions"] == 2
        assert report["recent"]["overall"]["questions"] == 1, "an undated record is not in the window"
        assert report["all_time"]["overall"]["executed"] == 0
        assert list(report["all_time"]["by_datasource"]) == ["(none)"]

    def test_records_from_older_versions_lack_fields_the_report_needs(self):
        """No timings, llm, guard, datasource, columns, session or turn ids."""
        old = {
            "timestamp": "2026-10-12T10:00:00", "request_id": "r1", "question": "q",
            "generated_sql": "SELECT 1", "row_count": 4, "error_code": None,
        }
        failed = {
            "timestamp": "2026-10-12T11:00:00", "request_id": "r2", "question": "q2",
            "generated_sql": "", "row_count": 0, "error_code": "OUT_OF_SCOPE",
        }

        overall = build_quality_report([old, failed], now=NOW)["recent"]["overall"]

        assert overall["questions"] == 2
        assert overall["executed"] == 1 and overall["execution_success_rate"] == 0.5
        assert overall["out_of_scope"] == 1
        assert overall["guard_rejected"] == 0 and overall["needed_correction"] == 0
        assert overall["latency"]["total_ms"]["count"] == 0
        assert overall["latency"]["llm_split"]["llm_call_count"] == 0

    def test_garbage_in_fields_does_not_raise(self):
        weird = [
            {"timestamp": 12345, "guard": "oops", "llm": "oops", "timings": "oops",
             "error_code": 7, "row_count": "many"},
            {"timestamp": "not a date", "guard": None, "llm": None, "timings": None},
            "not a record",
        ]
        report = build_quality_report(weird, now=NOW)  # type: ignore[arg-type]
        assert report["all_time"]["overall"]["questions"] == 2


# ---------------------------------------------------------------------------
# The figures
# ---------------------------------------------------------------------------


def _fixture_records():
    return [
        _rec(question="ok 1", total=100, stages={"llm_ms": 60}, llm={"corrections": 0}),
        _rec(question="ok 2", total=200, stages={"llm_ms": 120}, llm={"corrections": 1}),
        _rec(question="empty", rows=0, total=300, llm={"corrections": 0}),
        _rec(question="scope", sql="", code="OUT_OF_SCOPE", total=50),
        _rec(question="guard", code="FORBIDDEN_SQL", guard={"verdict": "rejected", "rule": "FORBIDDEN_SQL"}),
        _rec(question="exec", code="QUERY_EXECUTION_ERROR", total=400),
        _rec(question="timeout", code="MODEL_TIMEOUT", sql="", total=900),
        _rec(question="no result", columns=None),
    ]


class TestFigures:
    def test_rates_over_the_window(self):
        overall = build_quality_report(_fixture_records(), now=NOW)["recent"]["overall"]

        assert overall["questions"] == 8
        assert overall["executed"] == 3 and overall["execution_success_rate"] == 0.375
        assert overall["guard_rejected"] == 1 and overall["guard_rejection_rate"] == 0.125
        assert overall["needed_correction"] == 1 and overall["needed_correction_rate"] == 0.125
        assert overall["empty_results"] == 1
        assert overall["empty_result_rate"] == round(1 / 3, 4), "empty is a share of executed answers"
        assert overall["out_of_scope"] == 1 and overall["out_of_scope_rate"] == 0.125
        assert overall["errors"] == 4 and overall["error_rate"] == 0.5
        assert overall["not_executed_without_error"] == 1
        assert overall["executed"] + overall["errors"] + overall["not_executed_without_error"] == 8

    def test_error_rate_by_class_reuses_the_failure_taxonomy(self):
        by_class = build_quality_report(_fixture_records(), now=NOW)["recent"]["overall"]["error_rate_by_class"]

        assert by_class["guard_policy"] == {"count": 1, "rate": 0.125}
        assert by_class["scope_decline"] == {"count": 1, "rate": 0.125}
        assert by_class["transport"] == {"count": 1, "rate": 0.125}
        assert by_class["execution"] == {"count": 1, "rate": 0.125}
        taxonomy = aal.failure_taxonomy(_fixture_records())["by_bucket"]
        assert {k: v["count"] for k, v in by_class.items()} == taxonomy

    def test_latency_overall_and_per_stage_match_the_audit_analysis(self):
        records = _fixture_records()
        latency = build_quality_report(records, now=NOW)["recent"]["overall"]["latency"]
        expected = aal.latency_report(records)

        assert latency["total_ms"] == expected["overall_ms"]
        assert latency["by_stage_ms"] == expected["by_stage_ms"]
        assert latency["total_ms"]["count"] == 8
        assert latency["by_stage_ms"]["llm"]["count"] == 2
        assert latency["by_stage_ms"]["llm"]["p50"] == 90

    def test_the_llm_stage_split_is_there_for_streamed_calls_only(self):
        records = [
            _rec(llm={"ttft_ms": 900, "generation_ms": 2100, "total_ms": 3000}),
            _rec(llm={"ttft_ms": 1100, "generation_ms": 1900, "total_ms": 3000}),
            _rec(llm={"model": "no split recorded"}),
            _rec(llm=None),
        ]
        split = build_quality_report(records, now=NOW)["recent"]["overall"]["latency"]["llm_split"]

        assert split["llm_call_count"] == 3 and split["streamed_call_count"] == 2
        assert split["ttft_ms"]["count"] == 2 and split["ttft_ms"]["p50"] == 1000
        assert split["generation_ms"]["p50"] == 2000
        assert split == {
            **split, "ttft_ms": aal.llm_latency_split(records)["ttft_ms"],
            "generation_ms": aal.llm_latency_split(records)["generation_ms"],
        }

    def test_no_streamed_calls_means_an_empty_split_not_zeros(self):
        split = build_quality_report([_rec(llm={"model": "m"})], now=NOW)["recent"]["overall"]["latency"]["llm_split"]
        assert split["ttft_ms"]["count"] == 0 and split["ttft_ms"]["p50"] is None


class TestPerDatasource:
    def test_every_figure_is_also_computed_per_source_and_the_sources_add_up(self):
        records = [
            _rec(ds="sales", total=100), _rec(ds="sales", total=200),
            _rec(ds="sales", code="OUT_OF_SCOPE", sql="", total=50),
            _rec(ds="stock", total=300), _rec(ds=None, total=10),
            _rec(ds=None, datasource_selection={"chosen": "stock"}, total=20),
        ]
        section = build_quality_report(records, now=NOW)["recent"]

        by_source = section["by_datasource"]
        assert list(by_source) == ["sales", "stock", "(none)"], "largest first"
        assert by_source["sales"]["questions"] == 3 and by_source["sales"]["out_of_scope"] == 1
        assert by_source["stock"]["questions"] == 2, "datasource_selection.chosen is the fallback"
        assert by_source["(none)"]["questions"] == 1
        assert sum(m["questions"] for m in by_source.values()) == section["overall"]["questions"]
        assert by_source["sales"]["latency"]["total_ms"]["p50"] == 100
        # The same shape everywhere.
        assert set(by_source["sales"]) == set(section["overall"])


class TestWindow:
    def test_default_is_seven_days_and_all_time_is_always_there(self):
        records = [
            _rec(ts="2026-10-12T10:00:00"), _rec(ts="2026-10-07T00:00:00"),
            _rec(ts="2026-10-05T00:00:00"), _rec(ts="2026-08-01T00:00:00"),
        ]
        report = build_quality_report(records, now=NOW)

        assert report["window_days"] == 7
        assert report["recent"]["overall"]["questions"] == 2
        assert report["all_time"]["overall"]["questions"] == 4
        assert report["recent"]["since"] == (NOW - timedelta(days=7)).isoformat()
        assert report["all_time"]["since"] is None

    def test_days_widens_the_window(self):
        records = [_rec(ts="2026-10-05T00:00:00"), _rec(ts="2026-08-01T00:00:00")]
        assert build_quality_report(records, days=30, now=NOW)["recent"]["overall"]["questions"] == 1
        assert build_quality_report(records, days=365, now=NOW)["recent"]["overall"]["questions"] == 2

    def test_a_timezone_aware_timestamp_is_compared_in_local_time(self):
        aware = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        report = build_quality_report([_rec(ts=aware)])
        assert report["recent"]["overall"]["questions"] == 1

    def test_days_must_be_positive(self):
        with pytest.raises(ValueError):
            build_quality_report([], days=0)


# ---------------------------------------------------------------------------
# Ratings: confirmed vs flagged, a lower-bound signal
# ---------------------------------------------------------------------------


class TestRatings:
    def _records(self):
        return [
            _rec(session="s", turn="t1"), _rec(session="s", turn="t2"),
            _rec(session="s", turn="t3"), _rec(session="s", turn="t4"),
            _rec(session="s", turn="t_old", ts="2026-08-01T00:00:00"),
        ]

    def test_the_confirmed_share_of_rated_answers(self):
        confirmations = [{"session_id": "s", "turn_id": "t1"}, {"session_id": "s", "turn_id": "t2"},
                         {"session_id": "s", "turn_id": "t2"}]  # duplicates count once
        flags = [{"session_id": "s", "turn_id": "t3"}]

        ratings = build_quality_report(
            self._records(), confirmations=confirmations, flags=flags, now=NOW,
        )["recent"]["overall"]["ratings"]

        assert ratings == {
            "available": True, "confirmed": 2, "flagged": 1, "rated": 3,
            "confirmed_share_of_rated": round(2 / 3, 4),
        }

    def test_an_answer_both_confirmed_and_flagged_is_rated_once(self):
        both = [{"session_id": "s", "turn_id": "t1"}]
        ratings = build_quality_report(
            self._records(), confirmations=both, flags=both, now=NOW,
        )["recent"]["overall"]["ratings"]
        assert (ratings["confirmed"], ratings["flagged"], ratings["rated"]) == (1, 1, 1)

    def test_a_rating_outside_the_window_or_without_an_audit_record_is_not_counted(self):
        confirmations = [
            {"session_id": "s", "turn_id": "t_old"},        # answered before the window
            {"session_id": "s", "turn_id": "t_rotated"},    # audit record gone
        ]
        report = build_quality_report(self._records(), confirmations=confirmations, flags=[], now=NOW)

        assert report["recent"]["overall"]["ratings"]["confirmed"] == 0
        assert report["all_time"]["overall"]["ratings"]["confirmed"] == 1, "t_old is in all time"

    def test_ratings_are_attributed_to_the_data_source_of_the_answer(self):
        records = [_rec(ds="sales", session="s", turn="a"), _rec(ds="stock", session="s", turn="b")]
        report = build_quality_report(
            records, confirmations=[{"session_id": "s", "turn_id": "b"}], flags=[], now=NOW,
        )
        by_source = report["recent"]["by_datasource"]
        assert by_source["stock"]["ratings"]["confirmed"] == 1
        assert by_source["sales"]["ratings"]["confirmed"] == 0

    def test_unreadable_ratings_are_unavailable_not_zero(self):
        ratings = build_quality_report(self._records(), now=NOW)["recent"]["overall"]["ratings"]
        assert ratings["available"] is False
        assert ratings["confirmed"] is None and ratings["confirmed_share_of_rated"] is None

    def test_no_ratings_yet_is_available_with_no_share(self):
        ratings = build_quality_report(
            self._records(), confirmations=[], flags=[], now=NOW,
        )["recent"]["overall"]["ratings"]
        assert ratings["available"] is True and ratings["rated"] == 0
        assert ratings["confirmed_share_of_rated"] is None

    def test_the_report_carries_the_lower_bound_caveat_and_the_stream_timings_note(self):
        caveats = build_quality_report([], now=NOW)["caveats"]
        assert "not accuracy" in caveats["confirmed_share"]
        assert "LLM_STREAM_TIMINGS=true" in caveats["latency_stages"]


class TestLoadRatings:
    @pytest.fixture()
    def env(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        with cfg.override_settings(app_db_url=f"sqlite:///{tmp_path / 'app.db'}", log_dir=str(log_dir)):
            dispose_app_engine()
            yield tmp_path
        dispose_app_engine()

    def _answer(self, turn):
        save_audit_record(AuditRecord(
            timestamp=datetime(2026, 10, 12), request_id=f"r_{turn}", question="q",
            generated_sql="SELECT 1", guard={"verdict": "allowed"}, row_count=1, columns=["n"],
            session_id="s", turn_id=turn,
        ))

    def test_reads_confirmations_and_flags_from_the_application_database(self, env):
        self._answer("t1")
        self._answer("t2")
        confirm_turn(session_id="s", turn_id="t1", principal_id="analyst-1")
        submit_flag(session_id="s", turn_id="t2", reporter_principal_id="analyst-1", category="other")

        ratings = load_ratings()

        assert ratings.error is None
        assert [c["turn_id"] for c in ratings.confirmations] == ["t1"]
        assert [f["turn_id"] for f in ratings.flags] == ["t2"]

    def test_a_missing_sqlite_file_means_nothing_was_rated_and_is_not_created(self, env):
        db_file = env / "app.db"
        assert not db_file.exists()

        ratings = load_ratings()

        assert (ratings.confirmations, ratings.flags, ratings.error) == ([], [], None)
        assert not db_file.exists(), "running a report must not leave a database file behind"

    def test_an_unreachable_database_is_reported_not_raised(self, tmp_path):
        with cfg.override_settings(app_db_url="nonsense-dialect://nowhere/db"):
            dispose_app_engine()
            ratings = load_ratings()
        dispose_app_engine()
        assert ratings.confirmations is None and ratings.flags is None
        assert ratings.error


# ---------------------------------------------------------------------------
# The CLI section: counts and percentiles only
# ---------------------------------------------------------------------------


class TestAnalyzeAuditLogSection:
    @pytest.fixture()
    def log(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        recent = datetime.now() - timedelta(hours=2)
        records = [
            _rec(ts=recent.isoformat(), question=f"{SECRET} {i}", sql=f"SELECT {i} AS leaked_sql",
                 session="s", turn=f"t{i}", llm={"model": "openai:real", "corrections": 0},
                 principal_id="alice-the-analyst", error_message=SECRET)
            for i in range(3)
        ]
        records.append(_rec(ts=recent.isoformat(), question=SECRET, sql="", code="OUT_OF_SCOPE",
                            error_message=SECRET))
        path = log_dir / "audit_log.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                        encoding="utf-8")
        with cfg.override_settings(app_db_url=f"sqlite:///{tmp_path / 'app.db'}", log_dir=str(log_dir)):
            dispose_app_engine()
            confirm_turn_args = dict(session_id="s", turn_id="t0", principal_id="analyst-secret-id")
            yield {"path": path, "confirm": confirm_turn_args}
        dispose_app_engine()

    def test_text_output_has_the_section_and_stays_counts_only(self, log, capsys):
        confirm_turn(**log["confirm"])

        assert aal.main([str(log["path"])]) == 0

        out = capsys.readouterr().out
        assert "Real-use quality" in out
        assert "execution success   : 75.0% (3)" in out
        assert "out of scope        : 25.0% (1)" in out
        assert "confirmed / flagged : 1 / 0" in out
        assert "NOT accuracy" in out
        assert "[source: sales]" in out
        for private in (SECRET, "leaked_sql", "SELECT", "alice-the-analyst", "analyst-secret-id"):
            assert private not in out, f"{private!r} must not appear in the report"

    def test_json_output_carries_the_quality_report(self, log, capsys):
        confirm_turn(**log["confirm"])

        assert aal.main([str(log["path"]), "--json"]) == 0

        report = json.loads(capsys.readouterr().out)
        assert "quality" in report and "latency" in report, "the existing sections are unchanged"
        overall = report["quality"]["recent"]["overall"]
        assert overall["questions"] == 4 and overall["ratings"]["confirmed"] == 1
        assert SECRET not in json.dumps(report["quality"], ensure_ascii=False)

    def test_quality_days_narrows_the_window(self, log, capsys):
        assert aal.main([str(log["path"]), "--json", "--quality-days", "1"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["quality"]["window_days"] == 1

    def test_the_section_still_prints_when_the_application_database_cannot_be_read(
        self, log, capsys, monkeypatch,
    ):
        monkeypatch.setattr(
            quality, "load_ratings",
            lambda: Ratings(confirmations=None, flags=None, error="OperationalError"),
        )

        assert aal.main([str(log["path"])]) == 0

        out = capsys.readouterr().out
        assert "unavailable (application database not read)" in out
        assert "execution success" in out

    def test_a_non_positive_window_is_a_usage_error(self, log):
        with pytest.raises(SystemExit):
            aal.main([str(log["path"]), "--quality-days", "0"])


def test_render_text_for_an_empty_report():
    text = render_quality_text(build_quality_report([], now=NOW))
    assert "n/a" in text and "(no data)" in text


def test_module_doctests_pass():
    failed, attempted = doctest.testmod(quality, verbose=False)
    assert attempted > 0 and failed == 0
