# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for eval/recall.py and the ``recall`` subcommand of eval/cli.py.

The recall arithmetic is exercised with an injected ``retrieve`` so it does
not depend on what the retriever happens to return for the loaded schema;
one end-to-end test runs the real ``ContextRetriever`` against whichever
``schema.yaml`` is loaded to prove the wiring (and the determinism of the
output) holds.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_recall.py -q
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from eval.cli import build_parser, main
from eval.models import GoldenCase
from eval.recall import (
    evaluate_recall,
    gold_tables_from_sql,
    render_recall_text,
    report_to_json,
)
from schema_data.columns import TABLE_COLUMNS

_TABLES = list(TABLE_COLUMNS)
_A, _B, _C = _TABLES[0], _TABLES[1], _TABLES[2]


def _case(case_id: str, tables: list[str], **kwargs) -> GoldenCase:
    sql = "SELECT 1 FROM " + " CROSS JOIN ".join(f"[{t}]" for t in tables)
    return GoldenCase(id=case_id, question=f"question {case_id}", expected_sql=sql, **kwargs)


def _fake(returns: dict[str, list[str]]):
    def retrieve(question: str):
        return SimpleNamespace(selected_tables=returns[question])

    return retrieve


# ---------------------------------------------------------------------------
# gold_tables_from_sql
# ---------------------------------------------------------------------------


class TestGoldTables:
    def test_resolves_every_table_of_a_join_to_its_key(self):
        sql = f"SELECT 1 FROM [{_A}] a JOIN [{_B}] b ON a.ID = b.ID"
        assert gold_tables_from_sql(sql).resolved == tuple(sorted([_A, _B]))

    def test_repeated_table_counts_once(self):
        sql = f"SELECT 1 FROM [{_A}] x JOIN [{_A}] y ON x.ID = y.ID"
        assert gold_tables_from_sql(sql).resolved == (_A,)

    def test_cte_reference_is_not_a_table(self):
        sql = f"WITH c AS (SELECT ID FROM [{_A}]) SELECT * FROM c"
        gold = gold_tables_from_sql(sql)
        assert gold.resolved == (_A,)
        assert gold.unresolved == ()

    def test_unknown_table_is_reported_not_dropped(self):
        gold = gold_tables_from_sql(f"SELECT 1 FROM [{_A}] JOIN no_such_table_zz ON 1 = 1")
        assert gold.resolved == (_A,)
        assert gold.unresolved == ("no_such_table_zz",)

    def test_empty_sql_is_refused(self):
        with pytest.raises(ValueError, match="empty"):
            gold_tables_from_sql("   ")

    def test_unparseable_sql_is_refused(self):
        with pytest.raises(ValueError):
            gold_tables_from_sql("SELECT FROM WHERE ((")


# ---------------------------------------------------------------------------
# evaluate_recall
# ---------------------------------------------------------------------------


class TestEvaluateRecall:
    def test_recall_precision_and_means(self):
        cases = [
            _case("full", [_A, _B]),
            _case("half", [_A, _B]),
            _case("none", [_C]),
        ]
        retrieve = _fake({
            "question full": [_A, _B, _C],
            "question half": [_A],
            "question none": [],
        })
        report = evaluate_recall(cases, retrieve=retrieve, select_source=lambda q, c: None, sources=("default",))

        by_id = {c.case_id: c for c in report.cases}
        assert by_id["full"].recall == 1.0
        assert by_id["full"].missed == ()
        assert by_id["full"].precision == pytest.approx(2 / 3)
        assert by_id["half"].recall == 0.5
        assert by_id["half"].missed == (_B,)
        assert by_id["half"].precision == 1.0
        assert by_id["none"].recall == 0.0
        assert by_id["none"].precision == 0.0

        assert report.total_cases == 3
        assert report.scored == 3
        assert report.mean_recall == pytest.approx(0.5)
        assert report.full_recall_pct == pytest.approx(100 / 3)
        assert report.mean_tables == pytest.approx(4 / 3)
        assert report.median_tables == 1.0
        assert report.max_tables == 3
        assert report.mean_precision == pytest.approx((2 / 3 + 1.0 + 0.0) / 3)

    def test_retrieved_tables_are_sorted_and_deduplicated(self):
        report = evaluate_recall(
            [_case("c", [_A])],
            retrieve=_fake({"question c": [_C, _A, _C]}),
            select_source=lambda q, c: None,
            sources=("default",),
        )
        assert report.cases[0].retrieved == tuple(sorted([_A, _C]))

    def test_per_tag_breakdown(self):
        cases = [
            _case("a", [_A], tags=["en", "simple"]),
            _case("b", [_A], tags=["en"]),
            _case("c", [_A], tags=["fa"]),
        ]
        retrieve = _fake({"question a": [_A], "question b": [], "question c": [_A]})
        report = evaluate_recall(cases, retrieve=retrieve, select_source=lambda q, c: None, sources=("default",))
        assert list(report.by_tag) == ["en", "fa", "simple"]
        assert report.by_tag["en"].cases == 2
        assert report.by_tag["en"].mean_recall == 0.5
        assert report.by_tag["fa"].full_recall_pct == 100.0
        assert report.by_tag["simple"].mean_tables == 1.0

    def test_unscorable_cases_are_skipped_with_a_reason_not_counted_as_misses(self):
        cases = [
            _case("ok", [_A]),
            GoldenCase(id="oos", question="weather?", expect="out_of_scope"),
            GoldenCase(id="pending", question="q", expected_sql=f"SELECT 1 FROM [{_A}]",
                       status="pending_review"),
            GoldenCase(id="stale", question="question stale",
                       expected_sql="SELECT 1 FROM table_dropped_long_ago"),
            GoldenCase(id="notable", question="question notable", expected_sql="SELECT 1"),
        ]
        retrieve = _fake({"question ok": [_A]})
        report = evaluate_recall(cases, retrieve=retrieve, select_source=lambda q, c: None, sources=("default",))
        assert report.total_cases == 5
        assert report.scored == 1
        reasons = {s.case_id: s.reason for s in report.skipped}
        assert set(reasons) == {"oos", "pending", "stale", "notable"}
        assert "table_dropped_long_ago" in reasons["stale"]
        assert report.mean_recall == 1.0

    def test_reviewed_case_is_scored(self):
        case = _case("r", [_A], status="reviewed")
        report = evaluate_recall([case], retrieve=_fake({"question r": [_A]}),
                                 select_source=lambda q, c: None, sources=("default",))
        assert report.scored == 1

    def test_no_scored_case_gives_zero_means_not_an_error(self):
        report = evaluate_recall([GoldenCase(id="oos", question="q", expect="out_of_scope")],
                                 retrieve=_fake({}), sources=("default",))
        assert report.scored == 0
        assert report.mean_recall == 0.0
        assert report.full_recall_pct == 0.0

    def test_source_selection_accuracy(self):
        cases = [
            _case("one", [_A], datasource="alpha"),
            _case("two", [_A], datasource="beta"),
            _case("three", [_A], datasource="gamma"),   # not a configured source
            _case("four", [_A]),                          # states no source
        ]
        retrieve = _fake({f"question {n}": [_A] for n in ("one", "two", "three", "four")})
        picks = {"question one": "alpha", "question two": "alpha", "question three": "alpha",
                 "question four": "alpha"}

        def select(question, context):
            return SimpleNamespace(chosen=picks[question])

        report = evaluate_recall(cases, retrieve=retrieve, select_source=select,
                                 sources=("alpha", "beta"))
        sel = report.source_selection
        assert sel is not None
        assert (sel.correct, sel.total, sel.unscorable) == (1, 2, 1)
        assert sel.pct == 50.0
        by_id = {c.case_id: c for c in report.cases}
        assert by_id["one"].source_correct is True
        assert by_id["two"].source_correct is False
        assert by_id["three"].source_correct is None
        assert by_id["four"].source_correct is None

    def test_source_selection_is_not_reported_for_a_single_source(self):
        report = evaluate_recall(
            [_case("one", [_A], datasource="alpha")],
            retrieve=_fake({"question one": [_A]}),
            select_source=lambda q, c: None,   # what select_source_for_question returns
            sources=("alpha",),
        )
        assert report.source_selection is None
        assert report.cases[0].source_correct is None

    def test_background_refresh_is_off_during_the_run_and_restored_after(self):
        from retrieval import dimension_vocabulary

        seen: list[bool] = []

        def retrieve(question: str):
            seen.append(dimension_vocabulary.is_background_refresh_enabled())
            return SimpleNamespace(selected_tables=[_A])

        before = dimension_vocabulary.is_background_refresh_enabled()
        evaluate_recall([_case("c", [_A])], retrieve=retrieve,
                        select_source=lambda q, c: None, sources=("default",))
        assert seen == [False]
        assert dimension_vocabulary.is_background_refresh_enabled() is before


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def _report(self):
        cases = [_case("hit", [_A], tags=["t"]), _case("miss", [_A, _B], tags=["t"])]
        return evaluate_recall(
            cases,
            retrieve=_fake({"question hit": [_A], "question miss": [_A]}),
            select_source=lambda q, c: None,
            sources=("default",),
        )

    def test_text_lists_only_missing_cases_by_default(self):
        text = render_recall_text(self._report())
        assert "mean recall:          0.7500" in text
        assert "miss: recall=0.50" in text
        assert f"missed: {_B}" in text
        assert "hit: recall" not in text

    def test_text_lists_every_case_on_request(self):
        text = render_recall_text(self._report(), all_cases=True)
        assert "hit: recall=1.00" in text
        assert "miss: recall=0.50" in text

    def test_text_states_the_spread_of_tables_retrieved(self):
        assert "mean 1.00, median 1, max 1" in render_recall_text(self._report())

    def test_json_is_valid_and_complete(self):
        data = json.loads(report_to_json(self._report()))
        assert data["scored"] == 2
        assert (data["median_tables"], data["max_tables"]) == (1.0, 1)
        assert data["mean_recall"] == 0.75
        assert data["by_tag"]["t"]["cases"] == 2
        assert [c["case_id"] for c in data["cases"]] == ["hit", "miss"]
        assert data["cases"][1]["missed"] == [_B]
        assert data["source_selection"] is None

    def test_json_is_byte_identical_across_runs(self):
        assert report_to_json(self._report()) == report_to_json(self._report())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_golden(tmp_path, cases):
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in cases) + "\n", encoding="utf-8")
    return path


def _golden_lines():
    return [
        {"id": "g1", "question": "how many of the first table", "tags": ["x"],
         "expected_sql": f"SELECT COUNT(*) FROM [{_A}]"},
        {"id": "g2", "question": "both tables joined", "tags": ["x"],
         "expected_sql": f"SELECT 1 FROM [{_A}] a JOIN [{_B}] b ON a.ID = b.ID"},
    ]


class TestRecallCli:
    def test_parser_requires_golden(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["recall"])

    def test_parser_defaults(self):
        args = build_parser().parse_args(["recall", "--golden", "g.jsonl"])
        assert args.json is False
        assert args.all_cases is False
        assert args.min_recall is None
        assert args.out is None

    def test_text_run_against_the_real_retriever(self, tmp_path, capsys):
        golden = _write_golden(tmp_path, _golden_lines())
        assert main(["recall", "--golden", str(golden)]) == 0
        out = capsys.readouterr().out
        assert out.startswith("Retrieval recall")
        assert "2 scored" in out

    def test_json_run_is_deterministic(self, tmp_path, capsys):
        golden = _write_golden(tmp_path, _golden_lines())
        main(["recall", "--golden", str(golden), "--json"])
        first = capsys.readouterr().out
        main(["recall", "--golden", str(golden), "--json"])
        second = capsys.readouterr().out
        assert first == second
        data = json.loads(first)
        assert data["scored"] == 2
        for case in data["cases"]:
            assert 0.0 <= case["recall"] <= 1.0
            assert case["retrieved"] == sorted(case["retrieved"])

    def test_out_writes_the_json_report(self, tmp_path, capsys):
        golden = _write_golden(tmp_path, _golden_lines())
        out_path = tmp_path / "recall.json"
        assert main(["recall", "--golden", str(golden), "--out", str(out_path)]) == 0
        assert json.loads(out_path.read_text(encoding="utf-8"))["scored"] == 2
        assert "JSON report written" in capsys.readouterr().out

    def test_min_recall_gate_fails_below_the_threshold(self, tmp_path, capsys):
        golden = _write_golden(tmp_path, _golden_lines())
        assert main(["recall", "--golden", str(golden), "--min-recall", "1.01"]) == 1
        assert "below --min-recall" in capsys.readouterr().err

    def test_min_recall_gate_passes_at_zero(self, tmp_path):
        golden = _write_golden(tmp_path, _golden_lines())
        assert main(["recall", "--golden", str(golden), "--min-recall", "0"]) == 0

    def test_nothing_scorable_is_a_failure(self, tmp_path, capsys):
        golden = _write_golden(tmp_path, [{"id": "oos", "question": "weather?", "expect": "out_of_scope"}])
        assert main(["recall", "--golden", str(golden)]) == 1
        assert "No case could be scored" in capsys.readouterr().err
