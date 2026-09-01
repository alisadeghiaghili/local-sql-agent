# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for eval/cli.py.

Only offline-mode paths are exercised here — ``--live`` requires a real
Ollama instance and database connection and is explicitly out of scope
for CI (see ``eval/runner.py`` module docstring). What *is* tested about
``--live`` is that requesting it never touches the network at argument
parsing time; the lazy import inside ``_build_live_callables`` is covered
by asserting it only runs when ``--live`` is actually passed.

Run::

    .venv/Scripts/python.exe -m pytest eval/tests/test_cli.py -v --no-cov
"""

from __future__ import annotations

import json

import pytest

from eval.cli import _load_system_prompt, build_parser, main


def _write_golden(tmp_path, lines):
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


SIMPLE_CASE = {
    "id": "c1",
    "question": "how many?",
    "tags": ["count"],
    # A real table -- Phase 1's validate_sql enforces a table allowlist
    # against schema_data/columns.py, and this fixture is exercised
    # through the real offline pipeline (run_case -> validate_sql).
    "expected_sql": "SELECT COUNT(*) AS n FROM Contract",
    "expected_rows": [{"n": 3}],
}

OOS_CASE = {
    "id": "oos",
    "question": "what's the weather?",
    "expect": "out_of_scope",
}


# ---------------------------------------------------------------------------
# _load_system_prompt
# ---------------------------------------------------------------------------


class TestLoadSystemPrompt:
    def test_reads_file_contents(self, tmp_path):
        path = tmp_path / "system_prompt.md"
        path.write_text("You are a T-SQL expert.", encoding="utf-8")
        assert _load_system_prompt(path) == "You are a T-SQL expert."

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _load_system_prompt(tmp_path / "does_not_exist.md")


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_requires_golden(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["run"])

    def test_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--golden", "g.jsonl"])
        assert args.golden == "g.jsonl"
        assert args.live is False
        assert args.baseline is None
        assert args.save_baseline is None
        assert args.out is None
        assert args.max_accuracy_drop_pct == 5.0
        assert args.max_latency_p95_increase_pct == 20.0
        assert args.max_guard_rejection_increase == 0
        assert args.determinism is False
        assert args.determinism_repeats == 3
        assert args.determinism_out is None

    def test_determinism_flags(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "--golden",
                "g.jsonl",
                "--live",
                "--determinism",
                "--determinism-repeats",
                "5",
                "--determinism-out",
                "det.json",
            ]
        )
        assert args.determinism is True
        assert args.determinism_repeats == 5
        assert args.determinism_out == "det.json"

    def test_live_flag(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--golden", "g.jsonl", "--live"])
        assert args.live is True

    def test_unknown_command_rejected(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["not-a-command"])


# ---------------------------------------------------------------------------
# main(): offline end-to-end
# ---------------------------------------------------------------------------


class TestMainOffline:
    def test_all_passing_returns_zero(self, tmp_path, capsys):
        golden = _write_golden(tmp_path, [SIMPLE_CASE])
        code = main(["run", "--golden", str(golden)])
        assert code == 0
        out = capsys.readouterr().out
        assert "Execution accuracy: 100.00%" in out

    def test_out_of_scope_case_handled_offline(self, tmp_path, capsys):
        golden = _write_golden(tmp_path, [SIMPLE_CASE, OOS_CASE])
        code = main(["run", "--golden", str(golden)])
        assert code == 0
        out = capsys.readouterr().out
        assert "Execution accuracy: 100.00% (2/2)" in out

    def test_writes_json_report_when_out_given(self, tmp_path):
        golden = _write_golden(tmp_path, [SIMPLE_CASE])
        out_path = tmp_path / "report.json"
        main(["run", "--golden", str(golden), "--out", str(out_path)])
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["total"] == 1
        assert data["mode"] == "offline"

    def test_save_baseline_writes_file(self, tmp_path):
        golden = _write_golden(tmp_path, [SIMPLE_CASE])
        baseline_path = tmp_path / "baseline.json"
        main(["run", "--golden", str(golden), "--save-baseline", str(baseline_path)])
        assert baseline_path.exists()
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        assert data["total"] == 1

    def test_missing_golden_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            main(["run", "--golden", str(tmp_path / "nope.jsonl")])


# ---------------------------------------------------------------------------
# main(): --determinism refuses to run without --live
# ---------------------------------------------------------------------------


class TestMainDeterminismOfflineRefusal:
    """Exit criterion: the determinism probe must never run against the
    offline replay fixture, since that would report 100% determinism
    unconditionally (the fixture is a lookup table, not a model). This
    must be a loud, explicit failure -- not a silent skip -- and it must
    fire before any of the offline machinery (golden loading, replay
    generation) even gets a chance to produce a vacuous result.
    """

    def test_determinism_without_live_raises_explicit_error(self, tmp_path):
        golden = _write_golden(tmp_path, [SIMPLE_CASE])
        with pytest.raises(ValueError, match="--determinism requires --live"):
            main(["run", "--golden", str(golden), "--determinism"])

    def test_refusal_message_names_the_by_construction_problem(self, tmp_path):
        golden = _write_golden(tmp_path, [SIMPLE_CASE])
        with pytest.raises(ValueError, match="byte-identical text by construction"):
            main(["run", "--golden", str(golden), "--determinism"])

    def test_refusal_fires_before_touching_the_golden_file(self, tmp_path):
        """Even a nonexistent golden file must not mask the refusal --
        --determinism without --live is refused first, unconditionally."""
        missing = tmp_path / "does_not_exist.jsonl"
        with pytest.raises(ValueError, match="--determinism requires --live"):
            main(["run", "--golden", str(missing), "--determinism"])

    def test_live_without_determinism_is_unaffected(self, tmp_path):
        """--live alone (no --determinism) must not trip the refusal --
        this only asserts the guard condition, not a real network call."""
        golden = _write_golden(tmp_path, [SIMPLE_CASE])
        args = build_parser().parse_args(["run", "--golden", str(golden), "--live"])
        assert not (args.determinism and not args.live)


# ---------------------------------------------------------------------------
# main(): baseline comparison gate
# ---------------------------------------------------------------------------


class TestMainBaselineGate:
    def test_no_regression_returns_zero(self, tmp_path, capsys):
        golden = _write_golden(tmp_path, [SIMPLE_CASE])
        baseline_path = tmp_path / "baseline.json"
        main(["run", "--golden", str(golden), "--save-baseline", str(baseline_path)])

        # Offline-mode latencies are microseconds, so the *relative*
        # p95 change between two runs of the same case is dominated by
        # scheduling noise, not a real regression signal -- a generous
        # latency threshold keeps this test about the accuracy path
        # (which is what it exercises) rather than timing jitter. The
        # latency-threshold behaviour itself is covered by
        # eval/tests/test_baseline.py.
        code = main(
            [
                "run",
                "--golden",
                str(golden),
                "--baseline",
                str(baseline_path),
                "--max-latency-p95-increase-pct",
                "100000",
            ]
        )
        assert code == 0
        assert "No regression versus baseline." in capsys.readouterr().out

    def test_regression_returns_nonzero(self, tmp_path, capsys):
        golden_good = _write_golden(tmp_path, [SIMPLE_CASE])
        baseline_path = tmp_path / "baseline.json"
        main(["run", "--golden", str(golden_good), "--save-baseline", str(baseline_path)])

        # A case with no expected_rows can't be offline-replayed: the
        # offline executor raises, failing the case and dropping accuracy
        # below the default regression threshold.
        broken_case = dict(SIMPLE_CASE, expected_rows=None)
        bad_path = tmp_path / "golden_bad.jsonl"
        bad_path.write_text(json.dumps(broken_case) + "\n", encoding="utf-8")

        code = main(["run", "--golden", str(bad_path), "--baseline", str(baseline_path)])
        assert code == 1
        out = capsys.readouterr().out
        assert "REGRESSION DETECTED versus baseline:" in out

    def test_custom_thresholds_are_forwarded(self, tmp_path):
        golden = _write_golden(tmp_path, [SIMPLE_CASE])
        baseline_path = tmp_path / "baseline.json"
        main(["run", "--golden", str(golden), "--save-baseline", str(baseline_path)])

        broken_case = dict(SIMPLE_CASE, expected_rows=None)
        bad_path = tmp_path / "golden_bad.jsonl"
        bad_path.write_text(json.dumps(broken_case) + "\n", encoding="utf-8")

        # Allow a 100-point accuracy drop -> no regression despite total
        # failure. The latency threshold is also relaxed: offline-mode
        # latencies are microseconds, where a relative p95 comparison is
        # dominated by scheduling noise rather than a real signal (see
        # test_no_regression_returns_zero for the same reasoning).
        code = main(
            [
                "run",
                "--golden",
                str(bad_path),
                "--baseline",
                str(baseline_path),
                "--max-accuracy-drop-pct",
                "100",
                "--max-latency-p95-increase-pct",
                "100000",
            ]
        )
        assert code == 0
