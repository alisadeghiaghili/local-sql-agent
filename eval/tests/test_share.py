# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``eval.cli run --share``: the share-safe bundle, end to end, on SQLite.

The "warehouse" is an in-memory SQLite database (T-SQL is transpiled with
sqlglot, so ``TOP`` and bracketed names work) and the "model" a dictionary
from question to SQL, so a whole ``--live --reference live`` run happens in
CI. Distinctive sentinel values sit in the table; the tests assert they
never reach the serialised bundle, in any mode.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_share.py -q
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pandas as pd
import pytest
import sqlglot

from eval import cli
from eval.models import MISMATCH_CLASSES
from eval.report import build_report
from eval.runner import SourceTrace, run_golden_set
from eval.share import (
    FORMAT_VERSION,
    build_share_bundle,
    check_share_path,
    privacy_block,
    render_share_json,
    write_share_bundle,
)
from eval.store import load_golden_cases
from schema_data.columns import TABLE_COLUMNS

REPO_ROOT = Path(__file__).resolve().parents[2]
_TABLE = next(iter(TABLE_COLUMNS))
T = f"[{_TABLE}]"

#: Values that exist only as cell data. If one is in the bundle, a value leaked.
TEXT_SENTINELS = ["SENTINEL_LABEL_ALPHA", "SENTINEL_LABEL_BRAVO", "SENTINEL_LABEL_CHARLIE",
                  "نشانه_محرمانه_یک", "نشانه_محرمانه_دو", "نشانه_محرمانه_سه"]
NUMBER_SENTINELS = [8675309.25, 7654321.5, 6543210.75, 5432109.125, 4321098.0625, 3210987.0]

#: A string used only inside SQL text and error messages, never as a cell.
SQL_LITERAL = "LiteralOnlyInSql"
ERROR_VALUE = "ErrorValueSentinel"


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(f"CREATE TABLE {T} (ID INTEGER, Label TEXT, Amount REAL)")
    conn.executemany(
        f"INSERT INTO {T} VALUES (?, ?, ?)",
        [(i + 1, TEXT_SENTINELS[i], NUMBER_SENTINELS[i]) for i in range(6)],
    )
    return conn


def _sqlite_executor():
    conn = _database()
    calls: list[str] = []

    def execute(sql: str) -> pd.DataFrame:
        calls.append(sql)
        if ERROR_VALUE in sql:
            raise RuntimeError(
                f"Conversion failed when converting the varchar value '{ERROR_VALUE}' "
                "to data type int (row 987654321)."
            )
        native = sqlglot.transpile(sql, read="tsql", write="sqlite")[0]
        return pd.read_sql_query(native, conn)

    execute.calls = calls  # type: ignore[attr-defined]
    return execute


def _case(case_id, question, expected, tags=("count",), **extra):
    return {"id": case_id, "question": question, "tags": list(tags), "expected_sql": expected,
            "datasource": "sales", **extra}


#: id -> (reference SQL, generated SQL). The generated SQL is what the fake model answers.
SCENARIOS = {
    "match": (f"SELECT COUNT(*) AS n FROM {T}", f"SELECT COUNT(*) AS total FROM {T}"),
    "subset": (f"SELECT ID FROM {T}", f"SELECT ID FROM {T} WHERE ID <= 2"),
    "superset": (f"SELECT ID FROM {T} WHERE ID <= 2", f"SELECT ID FROM {T}"),
    "row_count": (f"SELECT ID FROM {T} WHERE ID <= 2", f"SELECT ID FROM {T} WHERE ID >= 4"),
    "columns": (f"SELECT ID FROM {T}", f"SELECT ID, Label FROM {T}"),
    "col_order": (f"SELECT ID, Label FROM {T}", f"SELECT Label, ID FROM {T}"),
    "order": (f"SELECT TOP 6 ID FROM {T} ORDER BY ID", f"SELECT TOP 6 ID FROM {T} ORDER BY ID DESC"),
    "values": (f"SELECT Label FROM {T} WHERE ID = 1", f"SELECT Label FROM {T} WHERE ID = 2"),
    "empty": (f"SELECT ID FROM {T}", f"SELECT ID FROM {T} WHERE ID > 100"),
    "expected_empty": (f"SELECT ID FROM {T} WHERE ID > 100", f"SELECT ID FROM {T}"),
    "off_by_one": (f"SELECT COUNT(*) AS n FROM {T}", f"SELECT COUNT(*) AS n FROM {T} WHERE ID <= 5"),
    "off_a_little": (f"SELECT SUM(Amount) AS s FROM {T}", f"SELECT SUM(Amount) * 0.995 AS s FROM {T}"),
    "literal": (f"SELECT ID FROM {T} WHERE Label = '{SQL_LITERAL}' AND ID > 3",
                f"SELECT ID FROM {T} WHERE Label = '{SQL_LITERAL}' AND ID > 3"),
    "exec_error": (f"SELECT ID FROM {T}", f"SELECT ID FROM {T} WHERE Label = '{ERROR_VALUE}'"),
    "guard": (f"SELECT ID FROM {T}", f"DELETE FROM {T}"),
    "boom": (f"SELECT ID FROM {T}", None),
}


def _golden(tmp_path):
    lines = []
    for index, (key, (reference, _)) in enumerate(SCENARIOS.items()):
        lines.append(_case(f"case_{key}", f"question {key}?", reference,
                           tags=("count" if index % 2 else "rank", f"source:{index % 2}")))
    lines.append({"id": "case_oos", "question": "question oos?", "expect": "out_of_scope",
                  "tags": ["oos"]})
    lines.append({"id": "case_pending", "question": "question pending?", "status": "pending_review",
                  "expected_sql": f"SELECT ID FROM {T}", "tags": ["rank"]})
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
                    encoding="utf-8")
    return path


def _generate(trace):
    answers = {f"question {key}?": gen for key, (_, gen) in SCENARIOS.items()}

    def generate(question):
        if trace is not None:
            trace.record("sales")
        if question == "question oos?":
            raise ValueError("OUT_OF_SCOPE")
        sql = answers[question]
        if sql is None:
            raise RuntimeError(f"model endpoint unreachable ('{ERROR_VALUE}')")
        return sql

    return generate


RUN_INFO = {
    "app_version": "9.9.9", "prefix_version": "abc123def456", "model": "test-model",
    "prompt_retrieval_token_budget": 6000, "sources": {"sales": "static", "stock": "retrieval"},
    "structured": False, "float_tolerance": 1e-6,
}


@pytest.fixture
def fake_live(monkeypatch):
    execute = _sqlite_executor()

    def build(structured=False, trace=None):
        return _generate(trace), execute

    monkeypatch.setattr(cli, "_build_live_callables", build)
    monkeypatch.setattr(cli, "_print_prefix_cache_probe", lambda question: None)
    monkeypatch.setattr(cli, "_collect_run_info", lambda args: dict(RUN_INFO))
    return execute


def _run(tmp_path, *extra):
    golden = _golden(tmp_path)
    share = tmp_path / "out" / "round-1.json"
    code = cli.main(["run", "--golden", str(golden), "--live", "--reference", "live",
                     "--share", str(share), *extra])
    return code, share, golden


def _bundle(share: Path) -> dict:
    return json.loads(share.read_text(encoding="utf-8"))


def _by_id(bundle):
    return {c["case_id"]: c for c in bundle["cases"]}


class TestBundleContent:
    def test_every_mismatch_class_is_diagnosed_end_to_end(self, tmp_path, fake_live):
        code, share, _ = _run(tmp_path)
        assert code == 0
        cases = _by_id(_bundle(share))
        got = {key: cases[f"case_{key}"]["diagnosis"]["mismatch_class"] for key in SCENARIOS}
        assert got == {
            "match": "match",
            "subset": "actual_is_subset",
            "superset": "actual_is_superset",
            "row_count": "row_count_differs",
            "columns": "column_count_differs",
            "col_order": "column_names_differ",
            "order": "same_rows_different_order",
            "values": "values_differ_same_shape",
            "empty": "actual_empty",
            "expected_empty": "expected_empty",
            "off_by_one": "values_differ_same_shape",
            "off_a_little": "values_differ_same_shape",
            "literal": "match",
            "exec_error": "not_executed",
            "guard": "not_executed",
            "boom": "not_executed",
        }
        assert cases["case_oos"]["diagnosis"]["mismatch_class"] == "match"  # correctly declined
        assert set(got.values()) | {"match"} <= set(MISMATCH_CLASSES)

    def test_counts_names_and_buckets(self, tmp_path, fake_live):
        _, share, _ = _run(tmp_path)
        cases = _by_id(_bundle(share))
        subset = cases["case_subset"]["diagnosis"]
        assert (subset["expected_rows"], subset["actual_rows"]) == (6, 2)
        assert subset["expected_columns"] == ["ID"] == subset["actual_columns"]
        assert cases["case_columns"]["diagnosis"]["actual_columns"] == ["ID", "Label"]
        assert cases["case_off_by_one"]["diagnosis"]["relative_error_bucket"] == ">=10%"
        assert cases["case_off_a_little"]["diagnosis"]["relative_error_bucket"] == "<1%"
        assert cases["case_values"]["diagnosis"]["relative_error_bucket"] is None
        assert cases["case_match"]["diagnosis"]["relative_error_bucket"] is None

    def test_per_case_fields(self, tmp_path, fake_live):
        _, share, _ = _run(tmp_path)
        case = _by_id(_bundle(share))["case_subset"]
        assert set(case) == {
            "case_id", "tags", "question", "expected_sql", "generated_sql", "status", "passed",
            "expected_datasource", "selected_datasource", "corrections", "guard_rejections",
            "error", "latency_seconds", "diagnosis",
        }
        assert case["question"] == "question subset?"
        assert case["expected_sql"] == SCENARIOS["subset"][0]
        assert case["generated_sql"] == SCENARIOS["subset"][1]
        assert (case["status"], case["passed"]) == ("fingerprint_mismatch", False)
        assert (case["expected_datasource"], case["selected_datasource"]) == ("sales", "sales")
        assert case["corrections"] == 0
        assert "case_pending" not in _by_id(_bundle(share))  # not active: not run

    def test_guard_rejection_reason_is_class_plus_redacted_message(self, tmp_path, fake_live):
        _, share, _ = _run(tmp_path)
        case = _by_id(_bundle(share))["case_guard"]
        assert case["status"] == "guard_rejected"
        assert len(case["guard_rejections"]) == 1
        rejection = case["guard_rejections"][0]
        assert rejection["class"].endswith("Rejection")
        assert rejection["message"]
        assert case["error"]

    def test_corrections_reported_by_the_generator_are_carried(self):
        from eval.models import GoldenCase

        trace = SourceTrace()

        def generate(question):
            trace.record_corrections(2)
            return f"SELECT COUNT(*) AS n FROM {T}"

        frame = pd.DataFrame({"n": [6]})
        case = GoldenCase(id="c", question="q", expected_sql=f"SELECT COUNT(*) AS n FROM {T}")
        results = run_golden_set([case], generate, lambda sql: frame, reference="live",
                                 source_trace=trace)
        assert results[0].corrections == 2
        assert run_golden_set([case], lambda q: case.expected_sql, lambda sql: frame,
                              reference="live")[0].corrections == 0

    def test_header_and_privacy_block(self, tmp_path, fake_live):
        _, share, golden = _run(tmp_path)
        bundle = _bundle(share)
        assert bundle["format_version"] == FORMAT_VERSION == 1
        assert bundle["app_version"] == "9.9.9"
        assert bundle["prefix_version"] == "abc123def456"
        assert bundle["model"] == "test-model"
        assert bundle["prompt_retrieval_token_budget"] == 6000
        assert bundle["sources"] == {"sales": {"path": "static"}, "stock": {"path": "retrieval"}}
        assert bundle["golden"]["file_name"] == "golden.jsonl"
        assert str(tmp_path) not in json.dumps(bundle)  # a name, never a directory
        assert len(bundle["golden"]["sha256"]) == 64
        assert bundle["run"]["started_at"] <= bundle["run"]["finished_at"]
        assert bundle["run"]["reference"] == "live"
        privacy = bundle["privacy"]
        assert privacy["sql_literals"] == "verbatim"
        assert any("result rows" in line for line in privacy["does_not_contain"])
        assert any("question" in line for line in privacy["contains"])

    def test_golden_hash_is_newline_agnostic(self, tmp_path, fake_live):
        _, share, golden = _run(tmp_path)
        before = _bundle(share)["golden"]["sha256"]
        golden.write_bytes(golden.read_bytes().replace(b"\n", b"\r\n"))
        share2 = tmp_path / "out" / "round-2.json"
        cli.main(["run", "--golden", str(golden), "--live", "--reference", "live",
                  "--share", str(share2)])
        assert _bundle(share2)["golden"]["sha256"] == before

    def test_summary(self, tmp_path, fake_live):
        _, share, _ = _run(tmp_path)
        bundle = _bundle(share)
        summary = bundle["summary"]
        cases = bundle["cases"]
        passed = sum(1 for c in cases if c["passed"])
        assert summary["total"] == len(cases) == 17
        assert summary["passed"] == passed
        assert summary["execution_accuracy_pct"] == round(100 * passed / len(cases), 2)
        assert set(summary["mismatch_classes"]) == set(MISMATCH_CLASSES)
        assert sum(summary["mismatch_classes"].values()) == len(cases)
        assert summary["mismatch_classes"]["not_executed"] == 3
        assert summary["per_tag"]["oos"] == {"passed": 1, "total": 1, "accuracy_pct": 100.0}
        assert summary["per_datasource"]["sales"]["total"] == 16
        assert summary["correction_rounds"] == {"total": 0, "cases_with_corrections": 0}
        assert summary["guard_rejections"] == 1
        assert 0 <= summary["latency_seconds"]["p50"] <= summary["latency_seconds"]["p95"]
        assert summary["source_selection"]["total"] == 16


class TestNoValueLeaks:
    @pytest.mark.parametrize("flags", [(), ("--share-redact-literals",)])
    def test_no_cell_value_is_in_the_serialised_file(self, tmp_path, fake_live, flags):
        code, share, _ = _run(tmp_path, *flags)
        assert code == 0
        text = share.read_text(encoding="utf-8")
        for sentinel in TEXT_SENTINELS:
            assert sentinel not in text
        for number in NUMBER_SENTINELS:
            for form in (str(number), f"{number:g}", str(int(number)), f"{number:.2f}"):
                assert form not in text
        # a Persian-digit rendering of the integer parts too
        for number in NUMBER_SENTINELS:
            assert str(int(number)).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")) not in text

    def test_no_cell_value_reaches_the_normal_report_either(self, tmp_path, fake_live):
        out = tmp_path / "report.json"
        _run(tmp_path, "--out", str(out))
        text = out.read_text(encoding="utf-8")
        for sentinel in TEXT_SENTINELS:
            assert sentinel not in text

    def test_no_credential_endpoint_or_path_in_the_header(self, tmp_path, fake_live, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-SHOULD-NOT-APPEAR-0001")
        monkeypatch.setenv("OPENAI_BASE_URL", "http://secret-host.internal:8000/v1")
        _, share, _ = _run(tmp_path)
        text = share.read_text(encoding="utf-8")
        for forbidden in ("sk-SHOULD", "secret-host", "OPENAI_", "internal:8000", "password="):
            assert forbidden not in text
        assert "principal_id" not in text  # no such key; the privacy block only says it is absent

    def test_a_model_name_that_is_a_url_is_cut_to_its_last_segment(self, tmp_path):
        golden = _golden(tmp_path)
        cases = load_golden_cases(golden)
        report = build_report([], mode="live", reference="live")
        info = dict(RUN_INFO, model="http://user:pw@host:8000/models/test-model")
        bundle = build_share_bundle(report, cases, golden_path=golden, run_info=info,
                                    started_at="a", finished_at="b")
        assert bundle["model"] == "test-model"


class TestErrorRedaction:
    def test_error_text_has_no_quoted_literal_and_no_long_digit_run(self, tmp_path, fake_live):
        _, share, _ = _run(tmp_path)
        cases = _by_id(_bundle(share))
        error = cases["case_exec_error"]["error"]
        assert error.startswith("RuntimeError: Conversion failed")
        assert "'…'" in error
        assert ERROR_VALUE not in error
        assert "987654321" not in error and "#" in error
        boom = cases["case_boom"]["error"]
        assert ERROR_VALUE not in boom

    def test_the_unredacted_report_still_has_the_text_the_operator_needs(self, tmp_path, fake_live):
        out = tmp_path / "report.json"
        _run(tmp_path, "--out", str(out))
        results = {r["case_id"]: r for r in json.loads(out.read_text(encoding="utf-8"))["results"]}
        assert ERROR_VALUE in results["case_exec_error"]["error"]  # --out is not the share file


class TestRedactLiterals:
    def test_default_keeps_the_sql_verbatim(self, tmp_path, fake_live):
        _, share, _ = _run(tmp_path)
        bundle = _bundle(share)
        case = _by_id(bundle)["case_literal"]
        assert SQL_LITERAL in case["expected_sql"] and SQL_LITERAL in case["generated_sql"]
        assert bundle["privacy"]["sql_literals"] == "verbatim"

    def test_flag_replaces_string_and_numeric_literals_in_both_texts(self, tmp_path, fake_live):
        _, share, _ = _run(tmp_path, "--share-redact-literals")
        bundle = _bundle(share)
        case = _by_id(bundle)["case_literal"]
        for key in ("expected_sql", "generated_sql"):
            assert SQL_LITERAL not in case[key]
            assert ":str" in case[key] and ":num" in case[key]
            assert "> 3" not in case[key]
            assert "Label" in case[key]  # the structure is still readable
        assert bundle["privacy"]["sql_literals"] == "redacted"
        assert "replaced" in bundle["privacy"]["sql_literals_note"]
        # what was diagnosed does not change
        assert case["diagnosis"]["mismatch_class"] == "match"

    def test_unparseable_sql_is_withheld_not_guessed_at(self, tmp_path):
        golden = _golden(tmp_path)
        cases = load_golden_cases(golden)
        from eval.models import CaseResult

        bad = CaseResult("case_match", "q", [], "guard_rejected", f"SELECT ( FROM '{SQL_LITERAL}'",
                         None, "x", 0.1)
        report = build_report([bad], mode="live", reference="live")
        bundle = build_share_bundle(report, cases, golden_path=golden, run_info=RUN_INFO,
                                    started_at="a", finished_at="b", redact_literals=True)
        assert SQL_LITERAL not in render_share_json(bundle)
        assert "withheld" in bundle["cases"][0]["generated_sql"]


    def test_a_missed_out_of_scope_error_does_not_repeat_the_sql(self, tmp_path):
        golden = tmp_path / "g.jsonl"
        golden.write_text(json.dumps({"id": "oos", "question": "weather?", "expect": "out_of_scope"})
                          + "\n", encoding="utf-8")
        cases = load_golden_cases(golden)
        sql = f"SELECT ID FROM {T} WHERE ID > 12 AND Label = '{SQL_LITERAL}'"
        results = run_golden_set(cases, lambda q: sql, _sqlite_executor(), reference="live")
        assert results[0].status == "missed_out_of_scope"
        report = build_report(results, mode="live", reference="live")
        kwargs = dict(golden_path=golden, run_info=RUN_INFO, started_at="a", finished_at="b")
        redacted = build_share_bundle(report, cases, redact_literals=True, **kwargs)
        assert redacted["cases"][0]["error"] == "expected OUT_OF_SCOPE but generator returned SQL"
        verbatim = build_share_bundle(report, cases, **kwargs)
        assert SQL_LITERAL not in verbatim["cases"][0]["error"]  # quoted text is masked anyway


class TestHeaderCollector:
    def test_the_real_collector_reads_the_live_configuration_and_nothing_secret(self):
        from core.version import __version__

        args = cli.build_parser().parse_args(
            ["run", "--golden", "g.jsonl", "--live", "--reference", "live", "--share", "x.json"]
        )
        info = cli._collect_run_info(args)
        assert info["app_version"] == __version__
        assert len(info["prefix_version"]) == 12
        assert isinstance(info["model"], str) and "://" not in info["model"]
        assert info["sources"] and set(info["sources"].values()) <= {"static", "retrieval"}
        assert isinstance(info["prompt_retrieval_token_budget"], int)
        assert set(info) == {"app_version", "prefix_version", "model", "sources", "structured",
                             "prompt_retrieval_token_budget", "float_tolerance"}

    def test_it_runs_before_the_model_is_called(self, tmp_path, monkeypatch):
        order = []
        monkeypatch.setattr(cli, "_collect_run_info", lambda args: order.append("info") or dict(RUN_INFO))
        monkeypatch.setattr(cli, "_print_prefix_cache_probe", lambda question: None)

        def build(structured=False, trace=None):
            order.append("build")
            return _generate(trace), _sqlite_executor()

        monkeypatch.setattr(cli, "_build_live_callables", build)
        _run(tmp_path)
        assert order[0] == "info"


class TestRefusals:
    def test_share_without_live_reference_live_is_refused_with_a_clear_message(
        self, tmp_path, capsys
    ):
        golden = _golden(tmp_path)
        share = tmp_path / "s.json"
        for argv in (
            ["run", "--golden", str(golden), "--share", str(share)],
            ["run", "--golden", str(golden), "--live", "--share", str(share)],
            ["run", "--golden", str(golden), "--reference", "live", "--share", str(share)],
        ):
            assert cli.main(argv) == 2
            err = capsys.readouterr().err
            assert "--share requires --live --reference live" in err
            assert not share.exists()
            assert "Traceback" not in err

    def test_nothing_runs_before_the_refusal(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(cli, "_build_live_callables", lambda **kw: called.append(1))
        golden = _golden(tmp_path)
        assert cli.main(["run", "--golden", str(golden), "--live", "--share",
                         str(tmp_path / "s.json")]) == 2
        assert called == []

    def test_redact_literals_without_share_is_refused(self, tmp_path, capsys):
        golden = _golden(tmp_path)
        assert cli.main(["run", "--golden", str(golden), "--share-redact-literals"]) == 2
        assert "--share-redact-literals only applies together with --share" in capsys.readouterr().err

    def test_share_and_out_may_not_be_the_same_file(self, tmp_path, fake_live, capsys):
        golden = _golden(tmp_path)
        same = tmp_path / "x.json"
        code = cli.main(["run", "--golden", str(golden), "--live", "--reference", "live",
                         "--share", str(same), "--out", str(same)])
        assert code == 2 and "different files" in capsys.readouterr().err

    def test_a_bundle_cannot_be_built_from_an_offline_report(self, tmp_path):
        golden = _golden(tmp_path)
        report = build_report([], mode="offline", reference="stored")
        with pytest.raises(ValueError, match="--live"):
            build_share_bundle(report, load_golden_cases(golden), golden_path=golden,
                               run_info=RUN_INFO, started_at="a", finished_at="b")


class TestPathGuard:
    def test_paths_inside_the_tracked_tree_are_refused(self, tmp_path, fake_live, capsys):
        golden = _golden(tmp_path)
        for relative in ("docs/round-1.json", "round-1.json", "eval_data.example/round-1.json",
                         "eval/round-1.json", ".git/round-1.json"):
            target = REPO_ROOT / relative
            assert "tracked tree" in (check_share_path(target) or "")
            code = cli.main(["run", "--golden", str(golden), "--live", "--reference", "live",
                             "--share", str(target)])
            assert code == 2
            assert not target.exists()
        assert "refusing to write the share file" in capsys.readouterr().err

    def test_the_ignored_data_directory_and_outside_paths_are_allowed(self, tmp_path):
        assert check_share_path(REPO_ROOT / "eval_data" / "share" / "round-1.json") is None
        assert check_share_path(tmp_path / "round-1.json") is None
        assert check_share_path("/srv/app/eval_data/x.json", repo_root="/srv/app") is None
        assert "tracked tree" in check_share_path("/srv/app/x.json", repo_root="/srv/app")

    def test_a_directory_is_refused(self, tmp_path):
        assert "directory" in check_share_path(tmp_path)

    def test_the_guard_runs_before_the_run_and_again_before_writing(self, tmp_path):
        with pytest.raises(ValueError, match="tracked tree"):
            write_share_bundle({"a": 1}, REPO_ROOT / "docs" / "x.json")

    def test_dotdot_cannot_smuggle_a_path_into_the_tree(self):
        assert "tracked tree" in check_share_path(REPO_ROOT / "eval_data" / ".." / "docs" / "x.json")
        assert check_share_path(REPO_ROOT / "docs" / ".." / "eval_data" / "x.json") is None


class TestFile:
    def test_utf8_stable_keys_and_unix_newlines(self, tmp_path, fake_live):
        _, share, _ = _run(tmp_path)
        raw = share.read_bytes()
        assert b"\r\n" not in raw and raw.endswith(b"\n")
        text = raw.decode("utf-8")
        assert "question subset?" in text
        bundle = json.loads(text)
        assert text == render_share_json(bundle)
        assert list(bundle) == sorted(bundle)
        assert [c["case_id"] for c in bundle["cases"]] == sorted(c["case_id"] for c in bundle["cases"])

    def test_persian_text_is_kept_readable(self, tmp_path):
        golden = tmp_path / "g.jsonl"
        golden.write_text(json.dumps({"id": "fa", "question": "تعداد مشتریان چند است؟",
                                       "expected_sql": f"SELECT COUNT(*) AS n FROM {T}"},
                                      ensure_ascii=False) + "\n", encoding="utf-8")
        execute = _sqlite_executor()
        results = run_golden_set(load_golden_cases(golden),
                                 lambda q: f"SELECT COUNT(*) AS n FROM {T}", execute,
                                 reference="live")
        bundle = build_share_bundle(build_report(results, mode="live", reference="live"),
                                    load_golden_cases(golden), golden_path=golden,
                                    run_info=RUN_INFO, started_at="a", finished_at="b")
        assert "تعداد مشتریان چند است؟" in render_share_json(bundle)

    def test_two_runs_diff_cleanly(self, tmp_path, fake_live):
        _, first, golden = _run(tmp_path)
        second = tmp_path / "out" / "round-2.json"
        cli.main(["run", "--golden", str(golden), "--live", "--reference", "live",
                  "--share", str(second)])
        a, b = _bundle(first), _bundle(second)
        for bundle in (a, b):
            bundle["run"].pop("started_at"), bundle["run"].pop("finished_at")
            for case in bundle["cases"]:
                case.pop("latency_seconds")
            bundle["summary"].pop("latency_seconds")
        assert a == b

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_the_file_is_owner_only(self, tmp_path, fake_live):
        _, share, _ = _run(tmp_path)
        assert stat.S_IMODE(share.stat().st_mode) == 0o600

    def test_the_normal_report_is_still_written(self, tmp_path, fake_live):
        out = tmp_path / "report.json"
        _run(tmp_path, "--out", str(out))
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["reference"] == "live" and len(report["results"]) == 17
        assert report["results"][0]["diagnosis"] is not None


def test_privacy_block_states_the_literal_mode():
    assert privacy_block(redact_literals=True)["sql_literals"] == "redacted"
    assert "verbatim" in privacy_block(redact_literals=False)["sql_literals_note"]
