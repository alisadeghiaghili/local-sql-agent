# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for scripts/analyze_audit_log.py.

Builds small synthetic ``audit_log.jsonl``-shaped fixtures (never reads the
real, git-ignored ``logs/audit_log.jsonl``) so these tests are deterministic
and independent of whatever traffic happens to be on a given machine.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.analyze_audit_log import (
    _classify_error_code,
    _join_bucket,
    _percentile,
    _rate_limit_hit_log_file,
    _stats,
    _time_range,
    build_report,
    cache_behaviour,
    correction_rounds,
    failure_taxonomy,
    finish_reason_distribution,
    iter_records,
    latency_report,
    llm_meta_summary,
    main,
    per_principal_usage,
    record_rate_limit_hit,
    records_by_model,
    resolve_log_paths,
    resolve_rate_limit_hit_paths,
    sql_shape_clusters,
)


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def _rec(**overrides) -> dict:
    """A minimal, well-formed audit record with sensible defaults."""
    base = {
        "timestamp": "2026-09-01T10:00:00",
        "request_id": "r1",
        "question": "چند مشتری فعال داریم؟",
        "generated_sql": "SELECT COUNT(*) FROM [Dim].[Customer]",
        "guard": {"verdict": "allowed", "rule": None, "injected_top": None,
                   "tables_touched": ["Customer"]},
        "row_count": 1,
        "tier": "T2",
        "error_code": None,
        "error_message": None,
        "timings": {"total_ms": 100, "plan_ms": 1, "prompt_ms": 2, "llm_ms": 80,
                     "guard_ms": 3, "execute_ms": 10, "interpret_ms": 4},
        "llm": {
            "backend": "openai", "model": "gpt-oss-20b", "endpoint": None,
            "trusted": False, "endpoint_status": 200, "attempts": 1,
            "finish_reason": "stop", "structured_output": False,
            "prompt_tokens": 100, "completion_tokens": 20, "prefill_ms": None,
            "decode_ms": None, "total_ms": 500, "tokens_per_second": None,
            "prefix_cache_hit": False, "temperature": 0.0, "seed": 7,
            "seed_honored": None, "corrections": 0, "provider": "openai",
            "fallback_used": False, "reasoning_detected": False,
        },
        "columns": ["n"],
        "principal_id": None,
        "resolved_columns": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# resolve_log_paths / iter_records
# ---------------------------------------------------------------------------

class TestResolveLogPaths:
    def test_literal_path(self, tmp_path):
        p = _write_jsonl(tmp_path / "audit_log.jsonl", [_rec()])
        assert resolve_log_paths([str(p)]) == [p]

    def test_glob_matches_rotated_backups(self, tmp_path):
        a = _write_jsonl(tmp_path / "audit_log.jsonl", [_rec()])
        b = _write_jsonl(tmp_path / "audit_log.jsonl.1", [_rec()])
        found = resolve_log_paths([str(tmp_path / "audit_log.jsonl*")])
        assert set(found) == {a, b}

    def test_missing_literal_path_is_skipped(self, tmp_path):
        assert resolve_log_paths([str(tmp_path / "nope.jsonl")]) == []

    def test_multiple_explicit_paths(self, tmp_path):
        a = _write_jsonl(tmp_path / "a.jsonl", [_rec()])
        b = _write_jsonl(tmp_path / "b.jsonl", [_rec()])
        assert set(resolve_log_paths([str(a), str(b)])) == {a, b}


class TestIterRecords:
    def test_reads_valid_lines(self, tmp_path):
        p = _write_jsonl(tmp_path / "audit_log.jsonl", [_rec(request_id="a"), _rec(request_id="b")])
        recs = list(iter_records([p]))
        assert [r["request_id"] for r in recs] == ["a", "b"]

    def test_skips_malformed_lines(self, tmp_path):
        p = tmp_path / "audit_log.jsonl"
        p.write_text('{"request_id": "ok"}\nnot json\n\n', encoding="utf-8")
        recs = list(iter_records([p]))
        assert recs == [{"request_id": "ok"}]

    def test_missing_file_yields_nothing(self, tmp_path):
        assert list(iter_records([tmp_path / "missing.jsonl"])) == []

    def test_reads_across_multiple_files_in_order(self, tmp_path):
        a = _write_jsonl(tmp_path / "a.jsonl", [_rec(request_id="a1")])
        b = _write_jsonl(tmp_path / "b.jsonl", [_rec(request_id="b1")])
        recs = list(iter_records([a, b]))
        assert [r["request_id"] for r in recs] == ["a1", "b1"]


# ---------------------------------------------------------------------------
# percentile / stats
# ---------------------------------------------------------------------------

class TestPercentile:
    def test_empty_is_none(self):
        assert _percentile([], 50) is None

    def test_single_value(self):
        assert _percentile([42], 50) == 42

    def test_median_odd(self):
        assert _percentile([1, 2, 3], 50) == 2

    def test_p99_of_uniform_range(self):
        values = list(range(1, 101))
        assert _percentile(values, 99) == pytest.approx(99.01, abs=0.5)


class TestStats:
    def test_empty(self):
        s = _stats([])
        assert s == {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None}

    def test_nonempty_has_all_keys(self):
        s = _stats([1, 2, 3, 4, 5])
        assert s["count"] == 5
        assert s["mean"] == 3.0
        assert s["p50"] == 3


# ---------------------------------------------------------------------------
# latency_report
# ---------------------------------------------------------------------------

class TestLatencyReport:
    def test_overall_and_stage_counts(self):
        records = [_rec(), _rec()]
        report = latency_report(records)
        assert report["overall_ms"]["count"] == 2
        assert report["by_stage_ms"]["llm"]["count"] == 2
        assert report["by_stage_ms"]["llm"]["p50"] == 80

    def test_missing_timings_contributes_nothing(self):
        records = [_rec(timings={})]
        report = latency_report(records)
        assert report["overall_ms"]["count"] == 0
        assert report["by_stage_ms"]["plan"]["count"] == 0

    def test_stage_names_match_contract(self):
        report = latency_report([_rec()])
        assert set(report["by_stage_ms"]) == {
            "plan", "prompt", "llm", "guard", "execute", "interpret",
        }


# ---------------------------------------------------------------------------
# finish_reason_distribution
# ---------------------------------------------------------------------------

class TestFinishReasonDistribution:
    def test_counts_by_reason(self):
        records = [
            _rec(llm={**_rec()["llm"], "finish_reason": "stop"}),
            _rec(llm={**_rec()["llm"], "finish_reason": "length"}),
            _rec(llm={**_rec()["llm"], "finish_reason": "stop"}),
        ]
        assert finish_reason_distribution(records) == {"stop": 2, "length": 1}

    def test_no_llm_block_is_excluded(self):
        records = [_rec(llm=None)]
        assert finish_reason_distribution(records) == {}


# ---------------------------------------------------------------------------
# error-code classification / failure taxonomy
# ---------------------------------------------------------------------------

class TestClassifyErrorCode:
    @pytest.mark.parametrize("code,bucket", [
        ("FORBIDDEN_SQL", "guard_policy"),
        ("INJECTION_ATTEMPT", "guard_policy"),
        ("INVALID_SQL_RESPONSE", "guard_correctable"),
        ("EMPTY_SQL_RESPONSE", "guard_correctable"),
        ("OUT_OF_SCOPE", "scope_decline"),
        ("MODEL_TIMEOUT", "transport"),
        ("MODEL_UNAVAILABLE", "transport"),
        ("DATABASE_UNAVAILABLE", "transport"),
        ("QUERY_TIMEOUT", "transport"),
        ("QUERY_EXECUTION_ERROR", "execution"),
        ("SOMETHING_NEW", "other"),
    ])
    def test_classification(self, code, bucket):
        assert _classify_error_code(code) == bucket


class TestFailureTaxonomy:
    def test_success_and_failure_counts(self):
        records = [_rec(error_code=None), _rec(error_code="FORBIDDEN_SQL")]
        result = failure_taxonomy(records)
        assert result["success_count"] == 1
        assert result["failure_count"] == 1
        assert result["by_error_code"] == {"FORBIDDEN_SQL": 1}
        assert result["by_bucket"] == {"guard_policy": 1}

    def test_examples_absent_by_default(self):
        records = [_rec(error_code="FORBIDDEN_SQL", question="secret question")]
        result = failure_taxonomy(records)
        assert "examples_by_error_code" not in result

    def test_examples_present_when_opted_in(self):
        records = [_rec(error_code="FORBIDDEN_SQL", question="secret question",
                          error_message="nope")]
        result = failure_taxonomy(records, include_examples=True)
        assert result["examples_by_error_code"]["FORBIDDEN_SQL"] == [
            {"question": "secret question", "error_message": "nope"},
        ]

    def test_examples_capped_at_three(self):
        records = [
            _rec(error_code="FORBIDDEN_SQL", question=f"q{i}") for i in range(10)
        ]
        result = failure_taxonomy(records, include_examples=True)
        assert len(result["examples_by_error_code"]["FORBIDDEN_SQL"]) == 3


# ---------------------------------------------------------------------------
# cache_behaviour
# ---------------------------------------------------------------------------

class TestCacheBehaviour:
    def test_t0_rate(self):
        records = [_rec(tier="T0"), _rec(tier="T2"), _rec(tier="T2"), _rec(tier="T2")]
        result = cache_behaviour(records)
        assert result["t0_count"] == 1
        assert result["t0_rate"] == 0.25

    def test_prefix_cache_hit_rate_among_llm_calls_only(self):
        hit = _rec(llm={**_rec()["llm"], "prefix_cache_hit": True})
        miss = _rec(llm={**_rec()["llm"], "prefix_cache_hit": False})
        no_llm = _rec(llm=None, tier="T0")
        result = cache_behaviour([hit, miss, no_llm])
        assert result["llm_call_count"] == 2
        assert result["prefix_cache_hit_count"] == 1
        assert result["prefix_cache_hit_rate"] == 0.5

    def test_empty_records_do_not_divide_by_zero(self):
        result = cache_behaviour([])
        assert result["t0_rate"] is None
        assert result["prefix_cache_hit_rate"] is None


# ---------------------------------------------------------------------------
# sql_shape_clusters
# ---------------------------------------------------------------------------

class TestJoinBucket:
    @pytest.mark.parametrize("sql,expected", [
        ("SELECT 1", "0"),
        ("SELECT * FROM A JOIN B ON A.id=B.id", "1"),
        ("SELECT * FROM A JOIN B JOIN C", "2+"),
        ("", "0"),
    ])
    def test_bucketing(self, sql, expected):
        assert _join_bucket(sql) == expected


class TestSqlShapeClusters:
    def test_clusters_by_tables_and_join_count(self):
        records = [
            _rec(guard={"verdict": "allowed", "tables_touched": ["Customer"]},
                  generated_sql="SELECT * FROM Customer"),
            _rec(guard={"verdict": "allowed", "tables_touched": ["Customer"]},
                  generated_sql="SELECT * FROM Customer"),
            _rec(guard={"verdict": "allowed", "tables_touched": ["Customer", "Contract"]},
                  generated_sql="SELECT * FROM Customer JOIN Contract ON 1=1"),
        ]
        clusters = sql_shape_clusters(records)
        assert clusters[0]["count"] == 2
        assert clusters[0]["tables_touched"] == ["Customer"]
        assert clusters[0]["join_count"] == "0"
        assert clusters[1]["tables_touched"] == ["Contract", "Customer"]
        assert clusters[1]["join_count"] == "1"

    def test_records_with_no_tables_touched_are_excluded(self):
        records = [_rec(guard={"verdict": "rejected", "tables_touched": None})]
        assert sql_shape_clusters(records) == []

    def test_never_includes_question_text_by_default(self):
        records = [_rec(question="a secret question")]
        clusters = sql_shape_clusters(records)
        assert "example_questions" not in clusters[0]

    def test_examples_only_when_opted_in(self):
        records = [_rec(question="a secret question")]
        clusters = sql_shape_clusters(records, include_examples=True)
        assert clusters[0]["example_questions"] == ["a secret question"]

    def test_top_n_caps_cluster_count(self):
        records = [
            _rec(guard={"verdict": "allowed", "tables_touched": [f"T{i}"]})
            for i in range(5)
        ]
        clusters = sql_shape_clusters(records, top_n=2)
        assert len(clusters) == 2


# ---------------------------------------------------------------------------
# correction_rounds
# ---------------------------------------------------------------------------

class TestCorrectionRounds:
    def test_distribution_and_success_rate(self):
        records = [
            _rec(llm={**_rec()["llm"], "corrections": 0}, error_code=None),
            _rec(llm={**_rec()["llm"], "corrections": 1}, error_code=None),
            _rec(llm={**_rec()["llm"], "corrections": 1}, error_code="FORBIDDEN_SQL"),
        ]
        result = correction_rounds(records)
        assert result["distribution"] == {"0": 1, "1": 2}
        assert result["success_rate_by_rounds"]["1"] == 0.5
        assert result["records_with_any_correction"] == 2
        assert result["records_with_any_correction_that_succeeded"] == 1

    def test_no_llm_block_excluded(self):
        result = correction_rounds([_rec(llm=None)])
        assert result["distribution"] == {}


# ---------------------------------------------------------------------------
# llm_meta_summary
# ---------------------------------------------------------------------------

class TestLlmMetaSummary:
    def test_empty_records(self):
        result = llm_meta_summary([])
        assert result["llm_call_count"] == 0
        assert result["reasoning_detected_rate"] is None

    def test_reasoning_and_fallback_and_provider(self):
        records = [
            _rec(llm={**_rec()["llm"], "reasoning_detected": True, "fallback_used": True,
                       "provider": "openai:gpt-oss"}),
            _rec(llm={**_rec()["llm"], "reasoning_detected": False, "fallback_used": False,
                       "provider": "openai:gpt-oss"}),
        ]
        result = llm_meta_summary(records)
        assert result["reasoning_detected_count"] == 1
        assert result["reasoning_detected_rate"] == 0.5
        assert result["fallback_used_count"] == 1
        assert result["provider_distribution"] == {"openai:gpt-oss": 2}

    def test_seed_honored_only_among_seed_requested(self):
        records = [
            _rec(llm={**_rec()["llm"], "seed": 7, "seed_honored": True}),
            _rec(llm={**_rec()["llm"], "seed": None, "seed_honored": None}),
        ]
        result = llm_meta_summary(records)
        assert result["seed_requested_count"] == 1
        assert result["seed_honored_count"] == 1
        assert result["seed_honored_rate_among_seed_requested"] == 1.0


# ---------------------------------------------------------------------------
# records_by_model / time_range
# ---------------------------------------------------------------------------

class TestRecordsByModel:
    def test_counts_by_model_including_no_llm(self):
        records = [
            _rec(llm={**_rec()["llm"], "model": "gpt-oss-20b"}),
            _rec(llm={**_rec()["llm"], "model": "gpt-oss-20b"}),
            _rec(llm=None),
        ]
        assert records_by_model(records) == {"gpt-oss-20b": 2, "(no llm call)": 1}


class TestTimeRange:
    def test_min_and_max(self):
        records = [_rec(timestamp="2026-09-02T10:00:00"), _rec(timestamp="2026-09-01T10:00:00")]
        result = _time_range(records)
        assert result == {"start": "2026-09-01T10:00:00", "end": "2026-09-02T10:00:00"}

    def test_empty(self):
        assert _time_range([]) == {"start": None, "end": None}


# ---------------------------------------------------------------------------
# build_report — the two-mode contract
# ---------------------------------------------------------------------------

class TestBuildReportModes:
    def test_default_mode_is_safe_and_excludes_examples(self):
        records = [_rec(error_code="FORBIDDEN_SQL")]
        report = build_report(records)
        assert report["mode"] == "aggregate_safe"
        assert "examples_by_error_code" not in report["failure_taxonomy"]
        assert all("example_questions" not in c for c in report["sql_shape_clusters"])

    def test_include_examples_mode_is_labelled(self):
        records = [_rec()]
        report = build_report(records, include_examples=True)
        assert report["mode"] == "aggregate_with_examples"

    def test_report_never_leaks_question_text_in_safe_mode(self):
        """A blunt end-to-end guard: serialise the safe-mode report and
        confirm the verbatim question text used by every fixture record
        does not appear anywhere in it."""
        secret = "این یک سوال کاملا محرمانه است"
        records = [_rec(question=secret, error_code="FORBIDDEN_SQL",
                          error_message=f"rejected: {secret}")]
        report = build_report(records, include_examples=False)
        serialised = json.dumps(report, ensure_ascii=False)
        assert secret not in serialised

    def test_record_count_present(self):
        report = build_report([_rec(), _rec()])
        assert report["record_count"] == 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_no_matching_files_returns_1(self, tmp_path, capsys):
        rc = main([str(tmp_path / "nope*.jsonl")])
        assert rc == 1
        assert "No log files matched" in capsys.readouterr().err

    def test_text_output_default(self, tmp_path, capsys):
        p = _write_jsonl(tmp_path / "audit_log.jsonl", [_rec()])
        rc = main([str(p)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "mode            : aggregate_safe" in out
        assert "record_count    : 1" in out

    def test_json_output(self, tmp_path, capsys):
        p = _write_jsonl(tmp_path / "audit_log.jsonl", [_rec()])
        rc = main([str(p), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "aggregate_safe"
        assert payload["record_count"] == 1

    def test_include_examples_flag_changes_mode_and_warns(self, tmp_path, capsys):
        p = _write_jsonl(tmp_path / "audit_log.jsonl", [_rec()])
        rc = main([str(p), "--include-examples"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "aggregate_with_examples" in out
        assert "VERBATIM EXAMPLE QUESTIONS" in out

    def test_reads_rotated_backups_via_glob(self, tmp_path, capsys):
        _write_jsonl(tmp_path / "audit_log.jsonl", [_rec(request_id="active")])
        _write_jsonl(tmp_path / "audit_log.jsonl.1", [_rec(request_id="rotated")])
        rc = main([str(tmp_path / "audit_log.jsonl*"), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["record_count"] == 2


# ---------------------------------------------------------------------------
# Admin panel phase 6, §4 -- per_principal_usage / record_rate_limit_hit.
# Frozen spec.
# ---------------------------------------------------------------------------

class TestPerPrincipalUsage:
    def test_figures_match_what_build_report_would_say_for_the_same_records(self):
        """Same source, same numbers -- the whole point of deriving this
        from analyze_audit_log rather than a second counter."""
        records = [
            _rec(principal_id="alice", error_code=None, timings={"total_ms": 100}),
            _rec(principal_id="alice", error_code="FORBIDDEN_SQL", timings={"total_ms": 200}),
            _rec(principal_id="bob", error_code=None, timings={"total_ms": 50}),
        ]
        report = per_principal_usage(records)

        alice_records = [r for r in records if r["principal_id"] == "alice"]
        assert report["principals"]["alice"]["queries"] == len(alice_records)
        assert report["principals"]["alice"]["failures"] == sum(
            1 for r in alice_records if r["error_code"]
        )
        expected_latency = _stats([r["timings"]["total_ms"] for r in alice_records])
        assert report["principals"]["alice"]["latency_ms"] == expected_latency

        assert report["principals"]["bob"]["queries"] == 1
        assert report["principals"]["bob"]["failures"] == 0

    def test_never_triggered_says_so_plainly(self):
        records = [_rec(principal_id="alice")]
        report = per_principal_usage(records, rate_limit_hit_records=[])
        assert report["rate_limit_never_triggered"] is True
        assert report["principals"]["alice"]["rate_limit_hits"] == 0

    def test_rate_limit_hits_come_from_the_separate_stream_not_the_records(self):
        records = [_rec(principal_id="alice")]
        hits = [
            {"timestamp": "2026-01-01T00:00:01+00:00", "principal_id": "alice", "path": "/query"},
            {"timestamp": "2026-01-01T00:00:02+00:00", "principal_id": "alice", "path": "/query"},
        ]
        report = per_principal_usage(records, rate_limit_hit_records=hits)
        assert report["principals"]["alice"]["rate_limit_hits"] == 2
        assert report["rate_limit_never_triggered"] is False
        # Not counted as an extra query -- these never reached run_query.
        assert report["principals"]["alice"]["queries"] == 1

    def test_window_filters_both_streams_by_timestamp(self):
        records = [
            _rec(principal_id="alice", timestamp="2020-01-01T00:00:00"),
            _rec(principal_id="alice", timestamp="2099-01-01T00:00:00"),
        ]
        hits = [
            {"timestamp": "2020-01-01T00:00:00", "principal_id": "alice", "path": "/query"},
            {"timestamp": "2099-01-01T00:00:00", "principal_id": "alice", "path": "/query"},
        ]
        report = per_principal_usage(records, hits, since="2050-01-01T00:00:00")
        assert report["principals"]["alice"]["queries"] == 1
        assert report["principals"]["alice"]["rate_limit_hits"] == 1

    def test_no_principal_falls_into_its_own_bucket(self):
        report = per_principal_usage([_rec(principal_id=None)])
        assert report["principals"]["(no principal)"]["queries"] == 1


class TestRecordRateLimitHit:
    def test_record_and_read_back(self, tmp_path):
        import scripts.analyze_audit_log as module

        path = tmp_path / "rate_limit_hits.jsonl"
        module._RATE_LIMIT_HIT_LOG_FILE = str(path)
        try:
            record_rate_limit_hit("alice", "/query")
            assert _rate_limit_hit_log_file() == str(path)
            records = list(iter_records(resolve_rate_limit_hit_paths([str(path)])))
        finally:
            module._RATE_LIMIT_HIT_LOG_FILE = ""

        assert len(records) == 1
        assert records[0]["principal_id"] == "alice"
        assert records[0]["path"] == "/query"
