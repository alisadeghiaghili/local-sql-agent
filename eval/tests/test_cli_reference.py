# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``eval.cli run --reference live`` end to end, with a fake warehouse and model.

``--live`` normally needs a reachable endpoint and database; here
``eval.cli._build_live_callables`` is replaced with fakes so the whole
command -- argument parsing, the same-run reference comparison, the
per-source report, baseline saving and the baseline reference guard -- can
run in CI.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_cli_reference.py -q
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from eval import cli
from eval.fingerprint import fingerprint_dataframe
from schema_data.columns import TABLE_COLUMNS

_TABLE = next(iter(TABLE_COLUMNS))
SQL_COUNT = f"SELECT COUNT(*) AS n FROM {_TABLE}"
SQL_COUNT_ALIASED = f"SELECT COUNT(*) AS total FROM {_TABLE}"
SQL_WRONG = f"SELECT COUNT(*) AS n FROM {_TABLE} WHERE 1 = 0"

#: What "the warehouse" returns today. The golden file's fingerprints were
#: recorded when the count was 3, so they are stale.
TODAY = {
    SQL_COUNT: pd.DataFrame({"n": [4]}),
    SQL_COUNT_ALIASED: pd.DataFrame({"total": [4]}),
    SQL_WRONG: pd.DataFrame({"n": [0]}),
}
STALE = fingerprint_dataframe(pd.DataFrame({"n": [3]}))


def _golden(tmp_path, *lines):
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
                    encoding="utf-8")
    return path


CASES = [
    {"id": "ok_aliased", "question": "how many (sales)?", "expected_sql": SQL_COUNT,
     "expected_fingerprint": STALE, "datasource": "sales"},
    {"id": "wrong", "question": "how many (inventory)?", "expected_sql": SQL_COUNT,
     "expected_fingerprint": STALE, "datasource": "inventory"},
    {"id": "pending", "question": "how many (pending)?", "status": "pending_review",
     "expected_sql": SQL_COUNT},
]


@pytest.fixture
def fake_live(monkeypatch):
    """Replace the live wiring: the 'model' answers per question, the 'warehouse' per SQL."""
    answers = {
        "how many (sales)?": ("sales", SQL_COUNT_ALIASED),
        "how many (inventory)?": ("sales", SQL_WRONG),  # wrong source AND wrong SQL
    }
    executed: list[str] = []

    def build(structured=False, trace=None):
        def generate(question):
            source, sql = answers[question]
            if trace is not None:
                trace.record(source)
            return sql

        def execute(sql):
            executed.append(sql)
            return TODAY[sql]

        return generate, execute

    monkeypatch.setattr(cli, "_build_live_callables", build)
    monkeypatch.setattr(cli, "_print_prefix_cache_probe", lambda question: None)
    return executed


class TestParser:
    def test_reference_defaults_to_stored(self):
        args = cli.build_parser().parse_args(["run", "--golden", "g.jsonl"])
        assert args.reference == "stored"
        assert args.float_tolerance == pytest.approx(1e-6)

    def test_reference_live_and_tolerance_parse(self):
        args = cli.build_parser().parse_args(
            ["run", "--golden", "g.jsonl", "--live", "--reference", "live", "--float-tolerance", "0.01"]
        )
        assert (args.reference, args.float_tolerance) == ("live", 0.01)

    def test_unknown_reference_is_a_usage_error(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["run", "--golden", "g.jsonl", "--reference", "never"])

    def test_verify_parses(self):
        args = cli.build_parser().parse_args(["verify", "--golden", "g.jsonl", "--accept"])
        assert args.accept and not args.refresh and not args.dry_run


class TestOfflineRefusal:
    def test_reference_live_without_live_is_refused(self, tmp_path):
        path = _golden(tmp_path, CASES[0])
        with pytest.raises(ValueError, match="--reference live requires --live"):
            cli.main(["run", "--golden", str(path), "--reference", "live"])


class TestRunReferenceLive:
    def test_stale_fingerprints_do_not_matter_and_wrong_answers_are_caught(
        self, tmp_path, fake_live, capsys
    ):
        path = _golden(tmp_path, *CASES)
        out = tmp_path / "report.json"
        code = cli.main(["run", "--golden", str(path), "--live", "--reference", "live",
                         "--out", str(out)])
        assert code == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["reference"] == "live" and data["mode"] == "live"
        statuses = {r["case_id"]: r["status"] for r in data["results"]}
        assert statuses == {"ok_aliased": "pass", "wrong": "fingerprint_mismatch"}  # pending skipped
        assert data["source_accuracy"] == {
            "inventory": {"passed": 0, "total": 1}, "sales": {"passed": 1, "total": 1},
        }
        assert data["source_selection"] == {"correct": 1, "total": 2}
        text = capsys.readouterr().out
        assert "Reference: live" in text
        assert "Source-selection accuracy: 50.00% (1/2)" in text
        # generated then reference, for each of the two runnable cases
        assert fake_live == [
            SQL_COUNT_ALIASED, SQL_COUNT, SQL_WRONG, SQL_COUNT,
        ]

    def test_the_same_data_in_stored_mode_reports_false_failures(self, tmp_path, fake_live):
        path = _golden(tmp_path, CASES[0])
        out = tmp_path / "stored.json"
        cli.main(["run", "--golden", str(path), "--live", "--out", str(out)])
        result = json.loads(out.read_text(encoding="utf-8"))["results"][0]
        assert result["status"] == "fingerprint_mismatch"  # the stale fingerprint

    def test_baselines_are_tied_to_their_reference(self, tmp_path, fake_live):
        path = _golden(tmp_path, CASES[0])
        stored_baseline = tmp_path / "stored_baseline.json"
        cli.main(["run", "--golden", str(path), "--live", "--save-baseline", str(stored_baseline)])
        with pytest.raises(ValueError, match="--reference live"):
            cli.main(["run", "--golden", str(path), "--live", "--reference", "live",
                      "--baseline", str(stored_baseline)])

        live_baseline = tmp_path / "live_baseline.json"
        assert cli.main(["run", "--golden", str(path), "--live", "--reference", "live",
                         "--save-baseline", str(live_baseline)]) == 0
        # The fake runs take microseconds, so the relative p95 change between
        # two of them is scheduling noise; a generous latency threshold keeps
        # this test about the reference pairing (the same reasoning as
        # eval/tests/test_cli.py's baseline tests).
        assert cli.main(["run", "--golden", str(path), "--live", "--reference", "live",
                         "--baseline", str(live_baseline),
                         "--max-latency-p95-increase-pct", "100000"]) == 0

    def test_a_regression_against_a_live_baseline_exits_one(self, tmp_path, fake_live):
        good = _golden(tmp_path, CASES[0])
        baseline = tmp_path / "b.json"
        cli.main(["run", "--golden", str(good), "--live", "--reference", "live",
                  "--save-baseline", str(baseline)])
        both = tmp_path / "both.jsonl"
        both.write_text(
            "\n".join(json.dumps(c) for c in (CASES[0], CASES[1])) + "\n", encoding="utf-8"
        )
        # the baseline covered one passing case; now half of the set fails
        code = cli.main(["run", "--golden", str(both), "--live", "--reference", "live",
                         "--baseline", str(baseline), "--max-accuracy-drop-pct", "10"])
        assert code == 1

    def test_float_tolerance_flag_reaches_the_comparison(self, tmp_path, monkeypatch):
        golden = _golden(tmp_path, {"id": "c", "question": "q?", "expected_sql": SQL_COUNT})
        warehouse = {SQL_COUNT: pd.DataFrame({"n": [100.0]}),
                     SQL_COUNT_ALIASED: pd.DataFrame({"n": [100.4]})}

        def build(structured=False, trace=None):
            return (lambda q: SQL_COUNT_ALIASED), (lambda sql: warehouse[sql])

        monkeypatch.setattr(cli, "_build_live_callables", build)
        monkeypatch.setattr(cli, "_print_prefix_cache_probe", lambda question: None)

        def status(*extra):
            out = tmp_path / "r.json"
            cli.main(["run", "--golden", str(golden), "--live", "--reference", "live",
                      "--out", str(out), *extra])
            return json.loads(out.read_text(encoding="utf-8"))["results"][0]["status"]

        assert status() == "fingerprint_mismatch"
        assert status("--float-tolerance", "0.01") == "pass"


class TestOfflineRunIgnoresNonActiveCases:
    def test_pending_review_duplicates_do_not_break_the_offline_replay(self, tmp_path, capsys):
        active = {"id": "a", "question": "same question", "expected_sql": SQL_COUNT,
                  "expected_rows": [{"n": 3}]}
        candidate = {"id": "b", "question": "same question", "status": "pending_review",
                     "expected_sql": SQL_COUNT}
        path = _golden(tmp_path, active, candidate)
        assert cli.main(["run", "--golden", str(path)]) == 0
        assert "(1/1)" in capsys.readouterr().out
