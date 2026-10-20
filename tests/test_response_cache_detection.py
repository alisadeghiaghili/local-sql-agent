# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A response cache in front of the model: the preflight check, the wizard
guard and the runtime early warning.

A gateway between this application and the model server can keep a semantic
or prefix *response cache* and answer a new prompt with the reply stored for a
similar one. Two incidents of that shape went unnoticed because nothing in the
reply says it came from a cache: one alias list for 170 of 190 tables, and the
risk that "count of active customers" is answered with the SQL cached for
"count of inactive customers".

Everything here is offline: the endpoint is a fake ``requests.post`` that
behaves like an honest model server or like a gateway caching by prompt
prefix, and the clock of the runtime watch is a fake.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

import setup_project as sp
from config import override_settings
from observability.response_cache_watch import ResponseCacheWatch, mark_suspected
from scripts.verify_deployment import (
    _ECHO_CHECK_NAME,
    _ECHO_MAX_TOKENS,
    build_checks,
    check_model_answers_each_prompt,
)
from tests._wizard_fakes import AliasesViaGenerate

LOCAL = "http://127.0.0.1:8000/v1"
REMOTE = "https://models.example.test/v1"


# ---------------------------------------------------------------------------
# Fake model servers
# ---------------------------------------------------------------------------

class _Reply:
    status_code = 200

    def __init__(self, text: str) -> None:
        self._text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [{"message": {"content": self._text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5},
        }


class FakeServer:
    """An honest model server by default; with ``cache_prefix`` a gateway that
    answers any prompt starting with the same characters with the first reply.
    """

    def __init__(self, *, cache_prefix: int | None = None, wrap: str = "{}") -> None:
        self.cache_prefix = cache_prefix
        self.wrap = wrap
        self.stored: dict[str, str] = {}
        self.payloads: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):  # noqa: A002
        self.payloads.append(json)
        prompt = json["messages"][0]["content"]
        nonce = prompt.rsplit(": ", 1)[-1]
        if self.cache_prefix is None:
            return _Reply(self.wrap.format(nonce))
        key = prompt[: self.cache_prefix]
        return _Reply(self.stored.setdefault(key, self.wrap.format(nonce)))


def _check(server=None, *, post=None, **settings):
    base = {"openai_base_url": LOCAL, "openai_model": "m", "openai_api_key": "k",
            "llm_provider": "openai"}
    base.update(settings)
    patched = post or server.post
    with override_settings(**base), patch("llm.providers.requests.post", side_effect=patched) as mock:
        result = check_model_answers_each_prompt()
    return result, mock


# ---------------------------------------------------------------------------
# Preflight: Model endpoint answers each prompt itself
# ---------------------------------------------------------------------------

class TestPreflightCheck:
    def test_an_honest_endpoint_passes(self):
        server = FakeServer()
        result, mock = _check(server)
        assert (result.name, result.status) == ("Model endpoint answers each prompt itself", "PASS")
        assert mock.call_count == 2
        assert "each test request was answered with its own token" in result.detail

    def test_a_model_that_adds_words_around_the_token_still_passes(self):
        result, _ = _check(FakeServer(wrap="Sure! The token is {} -- anything else?"))
        assert result.status == "PASS"

    def test_a_gateway_caching_by_prompt_prefix_fails(self):
        result, _ = _check(FakeServer(cache_prefix=40))
        assert result.status == "FAIL"
        detail = " ".join(result.detail.split())
        assert "response cache in front of the model returned another prompt's answer" in detail
        assert "wrong SQL" in detail
        assert "disable response caching for this client at the gateway" in detail
        assert "OPENAI_BASE_URL at the model server directly" in detail

    def test_the_prompts_differ_only_in_the_random_token(self):
        server = FakeServer()
        _check(server)
        first, second = (p["messages"][0]["content"] for p in server.payloads)
        head1, nonce1 = first.rsplit(": ", 1)
        head2, nonce2 = second.rsplit(": ", 1)
        assert head1 == head2 and nonce1 != nonce2
        assert len(nonce1) == len(nonce2) == 32 and int(nonce1, 16) >= 0

    def test_the_token_is_fresh_for_every_run(self):
        a, b = FakeServer(), FakeServer()
        _check(a)
        _check(b)
        assert {p["messages"][0]["content"] for p in a.payloads}.isdisjoint(
            {p["messages"][0]["content"] for p in b.payloads}
        )

    def test_the_request_is_the_servers_own_with_a_small_budget(self):
        server = FakeServer()
        _check(
            server, llm_seed=11, llm_temperature=0.0, llm_num_predict=4096,
            llm_extra_body_json='{"chat_template_kwargs": {"enable_thinking": false}}',
        )
        for payload in server.payloads:
            assert payload["seed"] == 11 and payload["temperature"] == 0.0
            assert payload["chat_template_kwargs"] == {"enable_thinking": False}
            assert payload["max_tokens"] == _ECHO_MAX_TOKENS <= 128
            assert payload["model"] == "m"

    def test_a_smaller_configured_budget_is_kept(self):
        server = FakeServer()
        _check(server, llm_num_predict=64)
        assert {p["max_tokens"] for p in server.payloads} == {64}

    def test_the_timeout_is_short(self):
        seen = []

        def post(url, headers=None, json=None, timeout=None, **kwargs):  # noqa: A002
            seen.append(timeout)
            return FakeServer().post(url, json=json)

        _check(post=post)
        assert seen and all(t <= 30 for t in seen)

    def test_the_detail_reports_latency_and_none_of_the_reply(self):
        server = FakeServer(wrap="SECRET-WORDS {}")
        result, _ = _check(server)
        assert "ms" in result.detail and "replies in" in result.detail
        assert "SECRET-WORDS" not in result.detail
        for payload in server.payloads:
            assert payload["messages"][0]["content"].rsplit(": ", 1)[-1] not in result.detail

    def test_a_reply_without_its_own_token_fails_too(self):
        result, _ = _check(post=lambda *a, **k: _Reply("I cannot help with that."))
        assert result.status == "FAIL"
        assert "did not contain its own random token" in result.detail
        assert "disable response caching" in result.detail

    def test_mock_mode_skips_without_a_request(self):
        result, mock = _check(FakeServer(), llm_provider="mock")
        assert result.status == "SKIP" and "mock" in result.detail
        assert mock.call_count == 0

    def test_a_remote_endpoint_without_allow_remote_skips_without_a_request(self):
        result, mock = _check(FakeServer(), openai_base_url=REMOTE, llm_allow_remote=False)
        assert result.status == "SKIP"
        assert "LLM_ALLOW_REMOTE" in result.detail
        assert mock.call_count == 0

    def test_a_remote_endpoint_with_allow_remote_is_tested(self):
        result, mock = _check(FakeServer(), openai_base_url=REMOTE, llm_allow_remote=True)
        assert result.status == "PASS" and mock.call_count == 2

    def test_an_explicit_llm_trusted_decides_like_the_router_does(self):
        result, mock = _check(FakeServer(), openai_base_url=REMOTE, llm_allow_remote=False,
                              llm_trusted=True)
        assert result.status == "PASS" and mock.call_count == 2

    def test_an_unreachable_endpoint_skips(self):
        def down(*a, **k):
            raise requests.ConnectionError("refused")

        result, _ = _check(post=down)
        assert result.status == "SKIP"
        assert "OpenAI-compatible model exists" in result.detail

    def test_a_timeout_skips(self):
        def slow(*a, **k):
            raise requests.Timeout("slow")

        result, _ = _check(post=slow)
        assert result.status == "SKIP"

    def test_a_reasoning_model_cut_off_before_answering_is_inconclusive(self):
        class Cut(_Reply):
            def json(self):
                return {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}

        result, _ = _check(post=lambda *a, **k: Cut(""))
        assert result.status == "SKIP" and "LLM_EXTRA_BODY" in result.detail

    def test_it_sits_next_to_the_model_exists_check(self):
        names = [c.__name__ for c in build_checks()]
        at = names.index("check_openai_model_exists")
        assert names[at + 1] == "check_model_answers_each_prompt"
        assert names.count("check_model_answers_each_prompt") == 1

    def test_the_admin_panel_runs_it_only_as_a_deep_check(self):
        from api.admin_routes import _DEEP_CHECK_NAMES

        assert "check_model_answers_each_prompt" in _DEEP_CHECK_NAMES

    def test_the_name_is_the_documented_one(self):
        assert _ECHO_CHECK_NAME == "Model endpoint answers each prompt itself"


# ---------------------------------------------------------------------------
# Wizard guard
# ---------------------------------------------------------------------------

def _col(name: str, type_: str = "int") -> SimpleNamespace:
    return SimpleNamespace(name=name, type=type_, sample_values=[])


def _table(name: str, *, fact: bool = False, dims: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, schema="", full_name=name, classification="fact" if fact else "dim",
        columns=[_col("id"), _col("amount")],
        foreign_keys=[SimpleNamespace(referred_table=d) for d in dims],
    )


def _snapshot(names: list[str], *, fact: bool = False) -> SimpleNamespace:
    tables = [_table(n, fact=fact) for n in names]
    return SimpleNamespace(
        tables=tables, fact_tables=tables if fact else [],
        dim_tables=[] if fact else tables, relationships=[],
    )


class ScriptedLLM(AliasesViaGenerate):
    """Answers by prompt; ``answer(table_name) -> JSON`` decides what each table gets."""

    provider = "fake"
    model = "fake"

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls = 0

    def test_connection(self) -> bool:
        return True

    def generate(self, prompt: str, expect_json: bool = True):
        self.calls += 1
        if "NLQ-to-SQL example pairs" in prompt:
            return [{"tags": ["count"], "question": "How many?", "sql": "SELECT 1"}]
        table = prompt.split("Table name: ")[-1].splitlines()[0] if "Table name: " in prompt else (
            prompt.split("Fact table: ")[-1].splitlines()[0]
        )
        return self.answer(table)


def _aliases_same(_table_name: str):
    return {"aliases": ["thing", "item"], "description": "a thing"}


def _aliases_own(table_name: str):
    return {"aliases": [f"{table_name} alias"], "description": "d"}


def _rules_same(table_name: str):
    return {"rules": {"value_col": "amount", "volume_col": "qty",
                      "rule_text": f"Use {table_name} for totals, join the dimension tables."}}


_TOPIC = {"sales_a": "revenue", "sales_b": "returns", "sales_c": "shipping", "sales_d": "discounts"}


def _rules_own(table_name: str):
    return {"rules": {"value_col": _TOPIC[table_name], "volume_col": "qty",
                      "rule_text": f"Use {table_name} for {_TOPIC[table_name]} questions."}}


def _args(*extra: str) -> argparse.Namespace:
    return sp._build_parser().parse_args(["--non-interactive", "--language", "en", *extra])


@pytest.fixture(autouse=True)
def _unwrapped_console(monkeypatch):
    # rich wraps at the terminal width, which differs between runners (the
    # Windows legs wrap where the Linux ones do not) and would split a phrase.
    if hasattr(sp.console, "_width"):
        monkeypatch.setattr(sp.console, "_width", 10_000)


def _flat(text: str) -> str:
    return " ".join(text.split())


class TestWizardStopsOnRepeatedAnswers:
    def test_three_identical_alias_lists_in_a_row_stop_step_3(self, capsys):
        log: dict = {}
        with pytest.raises(sp.CachedAnswersError) as caught:
            sp.step3_aliases(_args(), _snapshot(["t1", "t2", "t3", "t4"]),
                             ScriptedLLM(_aliases_same), log, "en")
        message = _flat(str(caught.value))
        assert "Step 3 stopped" in message
        assert "identical alias list for 3 different tables" in message
        assert "response cache" in message
        assert "verify_deployment.py" in message and "Model endpoint answers each prompt itself" in message
        assert "disable response caching for this client at the gateway" in message
        assert "OPENAI_BASE_URL" in message
        assert "--allow-repeated-answers" in message and "--resume" in message
        assert "step3_aliases" not in log  # not recorded as done

    def test_the_stop_comes_before_the_third_table_is_asked_about_any_more(self):
        llm = ScriptedLLM(_aliases_same)
        with pytest.raises(sp.CachedAnswersError):
            sp.step3_aliases(_args(), _snapshot([f"t{i}" for i in range(10)]), llm, {}, "en")
        assert llm.calls == 3

    def test_two_identical_then_a_different_answer_is_fine(self, capsys):
        answers = iter([["a"], ["a"], ["b"], ["b"], ["a"]])
        llm = ScriptedLLM(lambda _t: {"aliases": next(answers), "description": ""})
        result = sp.step3_aliases(_args(), _snapshot([f"t{i}" for i in range(5)]), llm, {}, "en")
        assert len(result["entities"]) == 5
        assert "2 distinct alias lists for 5 tables" in _flat(capsys.readouterr().out)

    def test_empty_answers_do_not_count_as_a_repeat(self):
        llm = ScriptedLLM(lambda _t: {"aliases": [], "description": ""})
        result = sp.step3_aliases(_args(), _snapshot([f"t{i}" for i in range(6)]), llm, {}, "en")
        assert result["ring_aliases"] == {}

    def test_varied_answers_pass_and_the_summary_counts_distinct_answers(self, capsys):
        names = [f"t{i}" for i in range(7)]
        sp.step3_aliases(_args(), _snapshot(names), ScriptedLLM(_aliases_own), {}, "en")
        assert "7 distinct alias lists for 7 tables" in _flat(capsys.readouterr().out)

    def test_allow_repeated_answers_continues_with_a_warning_and_a_summary(self, capsys):
        names = [f"t{i}" for i in range(5)]
        log: dict = {}
        result = sp.step3_aliases(
            _args("--allow-repeated-answers"), _snapshot(names), ScriptedLLM(_aliases_same), log, "en",
        )
        out = _flat(capsys.readouterr().out)
        assert len(result["entities"]) == 5 and "step3_aliases" in log
        assert out.count("continuing because of --allow-repeated-answers") == 1
        assert "1 distinct alias list for 5 tables" in out

    def test_three_identical_rule_texts_stop_step_4_with_the_table_names_taken_out(self):
        snap = _snapshot(["sales_a", "sales_b", "sales_c", "sales_d"], fact=True)
        with pytest.raises(sp.CachedAnswersError) as caught:
            sp.step4_business_rules(_args(), snap, ScriptedLLM(_rules_same), {})
        assert "Step 4 stopped" in str(caught.value) and "rule text" in str(caught.value)

    def test_rule_texts_that_really_differ_pass_step_4(self, capsys):
        snap = _snapshot(["sales_a", "sales_b", "sales_c", "sales_d"], fact=True)
        rules = sp.step4_business_rules(_args(), snap, ScriptedLLM(_rules_own), {})
        assert len(rules) == 4
        assert "4 distinct rule texts for 4 tables" in _flat(capsys.readouterr().out)

    def test_step_4_with_the_override_continues(self):
        snap = _snapshot(["sales_a", "sales_b", "sales_c", "sales_d"], fact=True)
        rules = sp.step4_business_rules(
            _args("--allow-repeated-answers"), snap, ScriptedLLM(_rules_same), {},
        )
        assert len(rules) == 4

    def test_the_fallback_text_the_wizard_writes_itself_is_not_an_answer(self):
        # The model gives nothing for any table; the wizard's own per-table
        # fallback text must not look like a repeated model answer.
        snap = _snapshot(["a", "b", "c", "d"], fact=True)
        llm = ScriptedLLM(lambda _t: {"rules": {}})
        assert len(sp.step4_business_rules(_args(), snap, llm, {})) == 4

    def test_the_flag_is_documented_in_the_help(self):
        assert "--allow-repeated-answers" in sp._build_parser().format_help()


class TestWizardRunIsResumableAndWritesNothing:
    @pytest.fixture()
    def db(self, tmp_path):
        path = tmp_path / "wizard.db"
        conn = sqlite3.connect(path)
        for name in ("alpha", "beta", "gamma", "delta"):
            conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, label TEXT)")
        conn.commit()
        conn.close()
        return path

    def _argv(self, db, out, *extra):
        return ["--db-url", f"sqlite:///{db}", "--language", "en", "--non-interactive",
                "--output", str(out), *extra]

    def test_the_stopped_run_exits_3_writes_no_file_and_resumes(self, tmp_path, db, capsys):
        out = tmp_path / "out"
        with patch.object(sp, "setup_wizard_llm", return_value=ScriptedLLM(_aliases_same)):
            assert sp.main(self._argv(db, out)) == 3
        text = _flat(capsys.readouterr().out)
        assert "Step 3 stopped" in text and "response cache" in text
        assert sorted(p.name for p in out.iterdir()) == [".setup_log.json"]
        log = json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))
        assert sp._step_done(log, "step2_schema") and not sp._step_done(log, "step3_aliases")

        honest = ScriptedLLM(_aliases_own)
        with patch.object(sp, "setup_wizard_llm", return_value=honest):
            assert sp.main(self._argv(db, out, "--resume")) == 0
        assert (out / "entities.yaml").is_file() and honest.calls > 0

    def test_the_override_lets_the_same_run_finish(self, tmp_path, db):
        out = tmp_path / "out"
        with patch.object(sp, "setup_wizard_llm", return_value=ScriptedLLM(_aliases_same)):
            assert sp.main(self._argv(db, out, "--allow-repeated-answers")) == 0
        assert (out / "aliases.yaml").is_file()

    def test_varied_answers_finish_without_the_flag(self, tmp_path, db):
        out = tmp_path / "out"
        with patch.object(sp, "setup_wizard_llm", return_value=ScriptedLLM(_aliases_own)):
            assert sp.main(self._argv(db, out)) == 0

    def test_a_dry_run_stops_too_and_writes_nothing(self, tmp_path, db):
        out = tmp_path / "out"
        with patch.object(sp, "setup_wizard_llm", return_value=ScriptedLLM(_aliases_same)):
            assert sp.main(self._argv(db, out, "--dry-run")) == 3
        assert not out.exists()


# ---------------------------------------------------------------------------
# Runtime early warning
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name == "observability.response_cache_watch" and r.levelno == logging.WARNING]


class TestResponseCacheWatch:
    def _watch(self, **kw):
        clock = Clock()
        return ResponseCacheWatch(threshold_ms=50, clock=clock, **kw), clock

    def test_identical_sql_for_a_different_question_under_the_threshold_warns_once(self, caplog):
        watch, clock = self._watch()
        caplog.set_level(logging.WARNING)
        assert watch.observe(None, "count of active customers", "SELECT 1", 800) is False
        clock.now += 3
        assert watch.observe(None, "count of inactive customers", "SELECT 1", 9) is True
        clock.now += 3
        assert watch.observe(None, "count of other customers", "SELECT 1", 7) is True
        assert len(_warnings(caplog)) == 1
        text = _warnings(caplog)[0].getMessage()
        assert "response cache" in text and "verify_deployment" in text

    def test_slow_identical_sql_does_not_warn(self, caplog):
        watch, clock = self._watch()
        caplog.set_level(logging.WARNING)
        watch.observe(None, "question one", "SELECT 1", 900)
        clock.now += 2
        assert watch.observe(None, "question two", "SELECT 1", 400) is False
        assert _warnings(caplog) == []

    def test_the_same_question_again_is_not_suspected(self):
        watch, clock = self._watch()
        watch.observe(None, "question one", "SELECT 1", 900)
        clock.now += 1
        assert watch.observe(None, "  Question   ONE ", "SELECT 1", 3) is False

    def test_different_sql_is_not_suspected(self):
        watch, clock = self._watch()
        watch.observe(None, "question one", "SELECT 1", 900)
        clock.now += 1
        assert watch.observe(None, "question two", "SELECT 2", 3) is False

    def test_the_comparison_is_per_data_source(self):
        watch, clock = self._watch()
        watch.observe("sales", "question one", "SELECT 1", 900)
        clock.now += 1
        assert watch.observe("stock", "question two", "SELECT 1", 3) is False
        assert watch.observe("sales", "question three", "SELECT 1", 3) is True

    def test_an_old_entry_outside_the_window_does_not_count(self):
        watch, clock = self._watch(window_seconds=30)
        watch.observe(None, "question one", "SELECT 1", 900)
        clock.now += 31
        assert watch.observe(None, "question two", "SELECT 1", 3) is False

    def test_a_new_streak_after_a_quiet_window_warns_again(self, caplog):
        watch, clock = self._watch(window_seconds=30)
        caplog.set_level(logging.WARNING)
        watch.observe(None, "q1", "SELECT 1", 900)
        clock.now += 1
        watch.observe(None, "q2", "SELECT 1", 3)
        clock.now += 5
        watch.observe(None, "q3", "SELECT 1", 3)
        assert len(_warnings(caplog)) == 1
        clock.now += 100
        watch.observe(None, "q4", "SELECT 1", 3)
        clock.now += 1
        watch.observe(None, "q5", "SELECT 1", 3)
        assert len(_warnings(caplog)) == 2

    def test_the_memory_is_bounded_and_the_oldest_entry_goes_first(self):
        watch, clock = self._watch(capacity=3)
        for i in range(5):
            watch.observe(None, f"question {i}", f"SELECT {i}", 900)
            clock.now += 1
        assert len(watch._seen) == 3
        assert watch.observe(None, "another", "SELECT 0", 3) is False  # evicted
        assert watch.observe(None, "another again", "SELECT 4", 3) is True  # kept

    def test_an_unknown_reply_time_is_never_suspected(self):
        watch, clock = self._watch()
        watch.observe(None, "q1", "SELECT 1", 900)
        clock.now += 1
        assert watch.observe(None, "q2", "SELECT 1", None) is False

    def test_a_zero_threshold_switches_it_off(self):
        watch = ResponseCacheWatch(threshold_ms=0, clock=Clock())
        watch.observe(None, "q1", "SELECT 1", 1)
        assert watch.observe(None, "q2", "SELECT 1", 1) is False
        assert len(watch._seen) == 0

    def test_mark_suspected_adds_the_flag_only_when_flagged(self):
        watch, clock = self._watch()
        first = mark_suspected({"total_ms": 800}, source=None, question="q1", sql="SELECT 1", watch=watch)
        assert "suspected_cached_response" not in first
        clock.now += 1
        second = mark_suspected({"total_ms": 8}, source=None, question="q2", sql="SELECT 1", watch=watch)
        assert second["suspected_cached_response"] is True

    def test_mark_suspected_never_raises(self):
        class Broken:
            def observe(self, *a, **k):
                raise RuntimeError("boom")

        assert mark_suspected({"total_ms": 1}, source=None, question="q", sql="s", watch=Broken()) == {
            "total_ms": 1,
        }
        assert mark_suspected(None, source=None, question="q", sql="s") is None


class TestRunnerAuditCarriesTheFlag:
    """The flag reaches the audit record's ``llm`` block and the response, and
    the request itself is untouched."""

    SQL = "SELECT TOP 5 * FROM [sales].[Customer]"

    def _run(self, tmp_path, question, total_ms, watch):
        from api.runner import run_query
        from llm.sql_agent import SQLAgent
        from tests.test_runner_audit import FakeBackendWithFinishReason, _execute_ok

        meta = {"raw": {"usage": {"prompt_tokens": 100, "completion_tokens": 20}},
                "endpoint_status": 200, "attempts": 1, "finish_reason": "stop",
                "total_ms": total_ms}
        agent = SQLAgent(backend=FakeBackendWithFinishReason(self.SQL, meta),
                         execute_fn=_execute_ok, max_corrections=0)
        audit = tmp_path / "audit_log.jsonl"
        with patch("api.runner.agent", agent), patch("observability.audit._AUDIT_LOG_FILE", str(audit)), \
                patch("observability.response_cache_watch._default", watch):
            response = run_query(question, system_prompt="sp", mode="sql")
        return response, json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])

    def test_a_fast_identical_sql_for_another_question_is_flagged_in_the_audit_llm_block(
        self, tmp_path, caplog,
    ):
        from api.query_cache import query_cache

        query_cache.clear()
        caplog.set_level(logging.WARNING)
        watch = ResponseCacheWatch(threshold_ms=50, clock=Clock())
        first_response, first = self._run(tmp_path, "count of active customers", 700, watch)
        second_response, second = self._run(tmp_path, "count of inactive customers", 6, watch)
        assert "suspected_cached_response" not in first["llm"]
        assert second["llm"]["suspected_cached_response"] is True
        assert second_response.llm["suspected_cached_response"] is True
        assert second_response.sql == self.SQL and second["error_code"] is None  # not blocked
        assert len(_warnings(caplog)) == 1

    def test_a_slow_identical_sql_is_not_flagged(self, tmp_path):
        watch = ResponseCacheWatch(threshold_ms=50, clock=Clock())
        self._run(tmp_path, "count of active customers", 700, watch)
        _, second = self._run(tmp_path, "count of inactive customers", 640, watch)
        assert "suspected_cached_response" not in second["llm"]


class TestSetting:
    _VALID_URL = dict(
        db_connection_url="mssql+pyodbc://prod-db-host:1433/RealDB?driver=ODBC+Driver+17+for+SQL+Server",
    )

    def test_the_default_is_50_ms(self, monkeypatch):
        from config import Settings

        monkeypatch.delenv("LLM_CACHE_SUSPECT_MS", raising=False)
        assert Settings().llm_cache_suspect_ms == 50
        monkeypatch.setenv("LLM_CACHE_SUSPECT_MS", "120")
        assert Settings().llm_cache_suspect_ms == 120

    @pytest.mark.parametrize("ok", [0, 1, 50, 5000])
    def test_validate_accepts_zero_and_positive_values(self, ok):
        import config as cfg

        with override_settings(llm_cache_suspect_ms=ok, **self._VALID_URL):
            cfg.settings.validate()

    def test_a_negative_value_is_refused(self):
        import config as cfg

        with override_settings(llm_cache_suspect_ms=-1, **self._VALID_URL):
            with pytest.raises(ValueError, match="LLM_CACHE_SUSPECT_MS"):
                cfg.settings.validate()
