# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Prefix-cache warm-up (``llm/warmup.py``) -- what is sent, when, and what it may never do.

The warm-up exists so the first question after a restart does not pay the
full prefill of the static prompt prefix. Everything here is about the
four promises that make it safe to leave on by default:

* it sends **exactly** the bytes real requests start with (not a copy of
  the builder, the builder's own output);
* it **never blocks** startup, and a real request arriving mid-warm-up is
  unaffected;
* every **failure** is a log line and nothing else -- and that line holds
  no prompt text and no key;
* the **setting** turns it off.

A fake backend stands in for the model server throughout; no test opens a
network connection (``requests.post`` is patched where the real transport
is under test).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

import config as cfg
from api import runner
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache
from config import Settings, override_settings
from core.models import RetrievalContext
from llm import warmup
from llm.base import LLMBackend
from llm.providers import MockBackend, OpenAIBackend
from llm.router import LLMRouter, build_prompt_segments
from llm.sql_agent import SQLAgent

SYSTEM_PROMPT = "You are a T-SQL expert."
#: Stands in for anything that must never reach a log line: prompt text,
#: response text, a key.
SECRET = "sk-test-SECRET-MARKER-0123456789"


class RecordingBackend(LLMBackend):
    """A model server's stand-in: records what ``warm_prefix`` is sent.

    Parameters
    ----------
    block:
        If given, ``warm_prefix`` waits on it (up to 10 s) before
        answering, to hold a warm-up open.
    fail:
        If given, raised from ``warm_prefix``.
    trusted:
        Reported as the backend's trust.
    """

    def __init__(self, *, block: threading.Event | None = None,
                 fail: BaseException | None = None, trusted: bool = True) -> None:
        self.calls: list[tuple[str, float | None]] = []
        self.entered = threading.Event()
        self._block = block
        self._fail = fail
        self._trusted = trusted

    @property
    def trusted(self) -> bool:
        return self._trusted

    @property
    def name(self) -> str:
        return "recording:test"

    def generate(self, prompt: str) -> str:  # pragma: no cover - never called
        raise AssertionError("a warm-up must not generate")

    def warm_prefix(self, prefix: str, *, timeout: float | None = None) -> dict:
        self.calls.append((prefix, timeout))
        self.entered.set()
        if self._block is not None:
            self._block.wait(10)
        if self._fail is not None:
            raise self._fail
        return {"prompt_tokens": 4321, "cached_tokens": None}


def _router(backend: LLMBackend) -> LLMRouter:
    return LLMRouter(default_chain=[backend])


def _two_sources():
    """Make the deployment look like it has two data sources, ``alpha`` and ``beta``."""
    return patch("database.datasources.datasource_names", return_value=("alpha", "beta"))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_VALID_URL = dict(
    db_connection_url="mssql+pyodbc://prod-db-host:1433/RealDB?driver=ODBC+Driver+17+for+SQL+Server",
)


class TestSettings:
    def test_on_by_default(self, monkeypatch):
        monkeypatch.delenv("LLM_PREFIX_WARMUP_ON_STARTUP", raising=False)
        assert Settings().llm_prefix_warmup_on_startup is True

    def test_env_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("LLM_PREFIX_WARMUP_ON_STARTUP", "false")
        assert Settings().llm_prefix_warmup_on_startup is False

    def test_budget_default_and_env(self, monkeypatch):
        monkeypatch.delenv("LLM_PREFIX_WARMUP_TIMEOUT_SECONDS", raising=False)
        assert Settings().llm_prefix_warmup_timeout_seconds == 180.0
        monkeypatch.setenv("LLM_PREFIX_WARMUP_TIMEOUT_SECONDS", "45.5")
        assert Settings().llm_prefix_warmup_timeout_seconds == 45.5

    @pytest.mark.parametrize("bad", [0, -1])
    def test_validate_refuses_a_budget_that_is_not_positive(self, bad):
        with override_settings(llm_prefix_warmup_timeout_seconds=bad, **_VALID_URL):
            with pytest.raises(ValueError, match="LLM_PREFIX_WARMUP_TIMEOUT_SECONDS"):
                cfg.settings.validate()


# ---------------------------------------------------------------------------
# The transport: OpenAIBackend.warm_prefix
# ---------------------------------------------------------------------------

def _reply(body: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    return resp


class TestOpenAIWarmPrefix:
    def test_request_is_the_prefix_alone_one_token_temperature_zero(self):
        backend = OpenAIBackend(model="m", api_key=SECRET, base_url="http://localhost:8000/v1")
        with override_settings(llm_temperature=0.7), patch(
            "llm.providers.requests.post",
            return_value=_reply({"usage": {"prompt_tokens": 4600}}),
        ) as post:
            info = backend.warm_prefix("THE PREFIX", timeout=12.5)

        (url,), kwargs = post.call_args
        payload = kwargs["json"]
        assert url == "http://localhost:8000/v1/chat/completions"
        assert payload["messages"] == [{"role": "user", "content": "THE PREFIX"}]
        assert payload["max_tokens"] == 1
        assert payload["temperature"] == 0
        assert payload["model"] == "m"
        assert "stream" not in payload
        assert kwargs["timeout"] == 12.5
        assert info == {"prompt_tokens": 4600, "cached_tokens": None, "endpoint_status": 200}

    def test_keeps_the_sampling_fields_and_extra_body_real_requests_send(self):
        backend = OpenAIBackend(model="m", api_key="", base_url="http://localhost:8000/v1")
        extra = json.dumps({"chat_template_kwargs": {"enable_thinking": False}})
        with override_settings(llm_extra_body_json=extra, llm_seed=11), patch(
            "llm.providers.requests.post", return_value=_reply({}),
        ) as post:
            backend.warm_prefix("p")
        payload = post.call_args.kwargs["json"]
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert payload["seed"] == 11

    def test_reports_cached_tokens_when_the_server_does(self):
        backend = OpenAIBackend(model="m", api_key="k", base_url="http://localhost:8000/v1")
        body = {"usage": {"prompt_tokens": 4600, "prompt_tokens_details": {"cached_tokens": 4592}}}
        with patch("llm.providers.requests.post", return_value=_reply(body)):
            assert backend.warm_prefix("p")["cached_tokens"] == 4592

    def test_http_error_propagates_for_the_caller_to_log(self):
        backend = OpenAIBackend(model="m", api_key="k", base_url="http://localhost:8000/v1")
        bad = _reply({}, status=400)
        bad.raise_for_status.side_effect = requests.HTTPError(response=bad)
        with patch("llm.providers.requests.post", return_value=bad) as post:
            with pytest.raises(requests.HTTPError):
                backend.warm_prefix("p")
        assert post.call_count == 1, "a warm-up is never retried"

    def test_mock_backend_has_nothing_to_warm(self):
        with pytest.raises(NotImplementedError):
            MockBackend().warm_prefix("p")


# ---------------------------------------------------------------------------
# What is sent: byte-identical to the real requests' prefix
# ---------------------------------------------------------------------------

class TestExactPrefix:
    def test_warm_up_text_is_what_a_real_request_starts_with(self):
        backend = RecordingBackend()
        results = warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))

        assert [r.status for r in results] == ["warmed"]
        assert len(backend.calls) == 1
        sent = backend.calls[0][0]

        real = build_prompt_segments(
            "how many rows were sold last month", SYSTEM_PROMPT, RetrievalContext(),
        )
        assert real.static_prefix == sent
        # What the transport puts in the request: the flattened real prompt
        # begins with the warm-up's message, character for character.
        assert real.flatten().startswith(sent)
        assert len(sent) > len(SYSTEM_PROMPT)

    def test_it_is_the_builders_cached_string_not_a_copy(self):
        from prompt_engine.static_prefix import build_static_prefix

        backend = RecordingBackend()
        warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))
        assert backend.calls[0][0] == build_static_prefix(SYSTEM_PROMPT)

    def test_nothing_variable_is_in_it(self):
        backend = RecordingBackend()
        warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))
        sent = backend.calls[0][0]
        for per_request in ("USER QUESTION", "DETECTED FILTERS", "SESSION CONTEXT"):
            assert per_request not in sent

    def test_one_request_per_data_source_each_with_its_own_prefix(self):
        from tests._source_fixtures import SOURCES, configured_sources

        backend = RecordingBackend()
        with configured_sources():
            results = warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))
            expected = {
                name: build_prompt_segments(
                    "q", SYSTEM_PROMPT, RetrievalContext(), source=name,
                ).static_prefix
                for name in SOURCES
            }
        assert [(r.source, r.status) for r in results] == [(n, "warmed") for n in SOURCES]
        assert [c[0] for c in backend.calls] == [expected[n] for n in SOURCES]
        assert len(set(expected.values())) == len(SOURCES), "each source has its own prefix"
        assert f"Data source: {SOURCES[1]}" in expected[SOURCES[1]]

    def test_a_source_on_the_retrieval_path_is_skipped_not_warmed(self):
        backend = RecordingBackend()
        with override_settings(prompt_retrieval_token_budget=1):
            results = warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))
        assert [r.status for r in results] == ["skipped"]
        assert "retrieval" in results[0].detail
        assert backend.calls == []

    def test_request_timeout_is_what_is_left_of_the_budget(self):
        backend = RecordingBackend()
        warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend), timeout_seconds=90)
        timeout = backend.calls[0][1]
        assert 0 < timeout <= 90

    def test_default_budget_comes_from_the_setting(self):
        backend = RecordingBackend()
        with override_settings(llm_prefix_warmup_timeout_seconds=33):
            warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))
        assert 0 < backend.calls[0][1] <= 33

    def test_a_spent_budget_skips_the_remaining_sources(self):
        backend = RecordingBackend()
        with _two_sources():
            results = warmup.warm_prefix_cache(
                SYSTEM_PROMPT, lambda: _router(backend), timeout_seconds=0,
            )
        assert [r.status for r in results] == ["skipped", "skipped"]
        assert backend.calls == []


# ---------------------------------------------------------------------------
# Failure is a log line
# ---------------------------------------------------------------------------

class TestFailuresAreSwallowedAndSafe:
    def test_a_failing_endpoint_is_reported_not_raised(self, caplog):
        backend = RecordingBackend(fail=RuntimeError(f"boom {SECRET} {SYSTEM_PROMPT}"))
        with caplog.at_level(logging.INFO, logger="llm.warmup"):
            results = warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))
        assert [r.status for r in results] == ["failed"]
        assert results[0].detail == "RuntimeError"
        assert "failed after" in caplog.text

    def test_log_lines_hold_no_prompt_text_response_text_or_key(self, caplog):
        prefix_marker = "PRIVATE-SCHEMA-MARKER"
        backend = RecordingBackend(fail=requests.ConnectionError(f"{SECRET} {prefix_marker}"))
        with caplog.at_level(logging.DEBUG):
            warmup.warm_prefix_cache(prefix_marker + SYSTEM_PROMPT, lambda: _router(backend))
            warmup.warm_prefix_cache(prefix_marker + SYSTEM_PROMPT, lambda: _router(RecordingBackend()))
        assert SECRET not in caplog.text
        assert prefix_marker not in caplog.text
        assert "BUSINESS RULES" not in caplog.text

    def test_http_status_is_named_but_the_message_is_not(self, caplog):
        response = requests.Response()
        response.status_code = 400
        backend = RecordingBackend(fail=requests.HTTPError(f"400 {SECRET}", response=response))
        with caplog.at_level(logging.INFO, logger="llm.warmup"):
            results = warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))
        assert results[0].detail == "HTTPError (HTTP 400)"
        assert SECRET not in caplog.text

    def test_one_line_per_source_with_duration_and_prompt_tokens(self, caplog):
        backend = RecordingBackend()
        with caplog.at_level(logging.INFO, logger="llm.warmup"), _two_sources(), patch(
            "database.datasources.table_datasource_sets", return_value={},
        ):
            warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))
        lines = [r.getMessage() for r in caplog.records if r.name == "llm.warmup"]
        assert len(lines) == 2
        assert "source=alpha warmed in" in lines[0] and "ms, prompt_tokens=4321" in lines[0]
        assert "source=beta warmed in" in lines[1]

    def test_a_failure_does_not_stop_the_next_source(self):
        class Flaky(RecordingBackend):
            def warm_prefix(self, prefix, *, timeout=None):
                first = not self.calls
                super().warm_prefix(prefix, timeout=timeout)
                if first:
                    raise TimeoutError
                return {"prompt_tokens": 1}

        backend = Flaky()
        with _two_sources(), patch("database.datasources.table_datasource_sets", return_value={}):
            results = warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))
        assert [r.status for r in results] == ["failed", "warmed"]

    def test_a_router_that_cannot_be_built_is_a_skip(self):
        def broken():
            raise ValueError(f"LLM_ROUTES is malformed {SECRET}")

        results = warmup.warm_prefix_cache(SYSTEM_PROMPT, broken)
        assert [r.status for r in results] == ["skipped"]
        assert SECRET not in results[0].detail

    def test_a_backend_with_no_cache_is_a_skip(self):
        results = warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(MockBackend()))
        assert results[0].status == "skipped"
        assert "no prefix cache" in results[0].detail

    def test_an_untrusted_endpoint_is_never_sent_the_schema(self):
        backend = RecordingBackend(trusted=False)
        with override_settings(llm_allow_remote=False):
            results = warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(backend))
        assert results[0].status == "skipped"
        assert backend.calls == []

    def test_a_second_warm_up_while_one_runs_sends_nothing(self):
        release = threading.Event()
        slow = RecordingBackend(block=release)
        other = RecordingBackend()
        thread = warmup.start_background_warmup(SYSTEM_PROMPT, lambda: _router(slow))
        try:
            assert slow.entered.wait(5)
            assert warmup.warm_prefix_cache(SYSTEM_PROMPT, lambda: _router(other)) is None
            assert other.calls == []
        finally:
            release.set()
            thread.join(5)


# ---------------------------------------------------------------------------
# Startup: not blocked, failures not fatal, the setting is honoured
# ---------------------------------------------------------------------------

_LIFESPAN_SETTINGS = dict(
    # lifespan's own checks, unrelated to this module (see
    # tests/test_startup_vocabulary_warmup.py).
    db_connection_url=(
        "mssql+pyodbc://prod-db-host:1433/RealDB?driver=ODBC+Driver+17+for+SQL+Server"
    ),
    auth_required=False,
)


@pytest.fixture()
def fake_agent():
    """Install an agent whose router is a ``RecordingBackend``; undo afterwards."""

    def install(backend: LLMBackend) -> None:
        runner._reset_agent_for_testing(SQLAgent(router=_router(backend)))

    yield install
    runner._reset_agent_for_testing(None)


def _start(**overrides):
    import api.server as server_module

    return (
        override_settings(**{**_LIFESPAN_SETTINGS, **overrides}),
        patch("retrieval.dimension_vocabulary.warm_all", return_value={}),
        server_module.app,
    )


class TestStartup:
    def test_startup_sends_the_prefix_without_waiting_for_the_answer(self, fake_agent):
        release = threading.Event()
        backend = RecordingBackend(block=release)
        fake_agent(backend)
        settings_cm, vocab_cm, app = _start(llm_prefix_warmup_on_startup=True)
        try:
            with settings_cm, vocab_cm:
                with TestClient(app) as client:
                    # Entered the lifespan and got here while the warm-up
                    # request is still held open by the "model server".
                    assert backend.entered.wait(5), "the warm-up was never sent"
                    assert not release.is_set()
                    assert client.get("/health").status_code == 200
                    sent = backend.calls[0][0]
                    assert sent.startswith("\n") and "DATABASE SCHEMA" in sent
        finally:
            release.set()

    def test_a_real_request_mid_warm_up_is_served_and_not_queued_behind_it(self):
        """The warm-up holds no lock a question needs: while its request is
        still open, ``generate_for_task`` answers at once."""
        release = threading.Event()

        class Both(RecordingBackend):
            def generate_with_meta(self, prompt):
                return "SELECT 1", {"raw": {}, "endpoint_status": 200, "attempts": 1}

        backend = Both(block=release)
        router = _router(backend)
        thread = warmup.start_background_warmup(SYSTEM_PROMPT, lambda: router)
        try:
            assert backend.entered.wait(5)
            segments = build_prompt_segments("q", SYSTEM_PROMPT, RetrievalContext())
            result = router.generate_for_task(
                __import__("llm.router", fromlist=["TaskType"]).TaskType.SQL_GENERATION, segments,
            )
            assert result.text == "SELECT 1"
            assert thread.is_alive(), "the warm-up was still open when the question was answered"
        finally:
            release.set()
            thread.join(5)

    def test_a_failing_warm_up_leaves_the_server_serving(self, fake_agent, caplog):
        backend = RecordingBackend(fail=RuntimeError(f"endpoint down {SECRET}"))
        fake_agent(backend)
        settings_cm, vocab_cm, app = _start(llm_prefix_warmup_on_startup=True)
        with caplog.at_level(logging.INFO, logger="llm.warmup"), settings_cm, vocab_cm:
            with TestClient(app) as client:
                assert backend.entered.wait(5)
                assert client.get("/health").status_code == 200
                # Let the daemon thread finish logging before the context closes.
                for thread in threading.enumerate():
                    if thread.name == "llm-prefix-warmup":
                        thread.join(5)
        assert "failed after" in caplog.text
        assert SECRET not in caplog.text

    def test_an_unbuildable_router_leaves_the_server_serving(self):
        runner._reset_agent_for_testing(None)
        settings_cm, vocab_cm, app = _start(
            llm_prefix_warmup_on_startup=True, llm_routes_json="not json",
        )
        with settings_cm, vocab_cm:
            with TestClient(app) as client:
                for thread in threading.enumerate():
                    if thread.name == "llm-prefix-warmup":
                        thread.join(5)
                assert client.get("/health").status_code == 200
        runner._reset_agent_for_testing(None)

    def test_the_setting_off_means_nothing_is_started(self):
        settings_cm, vocab_cm, app = _start(llm_prefix_warmup_on_startup=False)
        with settings_cm, vocab_cm, patch(
            "llm.warmup.start_background_warmup",
        ) as start:
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200
        start.assert_not_called()

    def test_the_setting_on_starts_it_once_with_the_loaded_prompt(self):
        settings_cm, vocab_cm, app = _start(llm_prefix_warmup_on_startup=True)
        with settings_cm, vocab_cm, patch(
            "llm.warmup.start_background_warmup",
        ) as start:
            with TestClient(app):
                pass
        assert start.call_count == 1
        prompt, factory = start.call_args.args
        assert prompt and factory is runner.get_llm_router

    def test_a_start_that_raises_does_not_stop_startup(self):
        settings_cm, vocab_cm, app = _start(llm_prefix_warmup_on_startup=True)
        with settings_cm, vocab_cm, patch(
            "llm.warmup.start_background_warmup", side_effect=RuntimeError("no threads"),
        ):
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200

    def test_background_start_returns_while_the_request_is_still_open(self):
        release = threading.Event()
        backend = RecordingBackend(block=release)
        thread = warmup.start_background_warmup(SYSTEM_PROMPT, lambda: _router(backend))
        try:
            assert backend.entered.wait(5)
            assert thread.is_alive() and thread.daemon
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# POST /admin/llm/warmup
# ---------------------------------------------------------------------------

def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_OPS_KEY = "a" * 40
_SECURITY_KEY = "b" * 40
_ANALYST_KEY = "c" * 40
_KEYS_JSON = json.dumps([
    {"id": "ops-admin", "name": "Ops", "key_sha256": _sha256(_OPS_KEY), "operations": True},
    {"id": "sec-admin", "name": "Sec", "key_sha256": _sha256(_SECURITY_KEY), "security": True},
    {"id": "analyst-1", "name": "Analyst", "key_sha256": _sha256(_ANALYST_KEY)},
])


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture()
def admin_client(tmp_path, fake_agent):
    import api.server as server_module
    import api.v2_routes as v2_routes

    with override_settings(
        app_db_url=f"sqlite:///{tmp_path / 'appdb.db'}", api_keys_json=_KEYS_JSON,
        auth_required=True,
    ):
        dispose_app_engine()
        invalidate_cache()
        server_module._system_prompt = SYSTEM_PROMPT
        import api.admin_ops_routes as ops_routes

        ops_routes._system_prompt = SYSTEM_PROMPT
        v2_routes._reset_for_testing()
        yield TestClient(server_module.app, raise_server_exceptions=False)
        ops_routes._system_prompt = ""
        v2_routes._reset_for_testing()
    dispose_app_engine()
    invalidate_cache()


class TestAdminRoute:
    def test_operations_can_trigger_it_and_gets_one_entry_per_source(self, admin_client, fake_agent):
        backend = RecordingBackend()
        fake_agent(backend)
        resp = admin_client.post("/admin/llm/warmup", headers=_auth(_OPS_KEY))
        assert resp.status_code == 200
        (entry,) = resp.json()["results"]
        assert entry["status"] == "warmed"
        assert entry["prompt_tokens"] == 4321
        assert isinstance(entry["duration_ms"], int)
        assert backend.calls and backend.calls[0][0].startswith("\n")
        # Never the prompt, in the response either.
        assert "DATABASE SCHEMA" not in resp.text

    def test_it_runs_even_when_the_startup_setting_is_off(self, admin_client, fake_agent):
        backend = RecordingBackend()
        fake_agent(backend)
        with override_settings(llm_prefix_warmup_on_startup=False):
            resp = admin_client.post("/admin/llm/warmup", headers=_auth(_OPS_KEY))
        assert resp.status_code == 200 and len(backend.calls) == 1

    def test_a_failed_source_is_reported_in_the_body_not_as_an_http_error(self, admin_client, fake_agent):
        fake_agent(RecordingBackend(fail=TimeoutError(SECRET)))
        resp = admin_client.post("/admin/llm/warmup", headers=_auth(_OPS_KEY))
        assert resp.status_code == 200
        assert resp.json()["results"][0]["status"] == "failed"
        assert SECRET not in resp.text

    def test_it_is_recorded_in_the_admin_action_log(self, admin_client, fake_agent):
        fake_agent(RecordingBackend())
        admin_client.post("/admin/llm/warmup", headers=_auth(_OPS_KEY))
        actions = admin_client.get("/admin/actions", headers=_auth(_OPS_KEY)).json()["actions"]
        assert any(a["action"] == "llm.warmup" for a in actions)

    @pytest.mark.parametrize("key", [_SECURITY_KEY, _ANALYST_KEY])
    def test_it_needs_the_operations_capability(self, admin_client, fake_agent, key):
        backend = RecordingBackend()
        fake_agent(backend)
        assert admin_client.post("/admin/llm/warmup", headers=_auth(key)).status_code == 403
        assert backend.calls == []

    def test_it_needs_a_key_at_all(self, admin_client):
        assert admin_client.post("/admin/llm/warmup").status_code in (401, 403)

    def test_a_warm_up_already_running_is_a_409_and_sends_nothing(self, admin_client, fake_agent):
        release = threading.Event()
        slow = RecordingBackend(block=release)
        thread = warmup.start_background_warmup(SYSTEM_PROMPT, lambda: _router(slow))
        other = RecordingBackend()
        fake_agent(other)
        try:
            assert slow.entered.wait(5)
            resp = admin_client.post("/admin/llm/warmup", headers=_auth(_OPS_KEY))
            assert resp.status_code == 409
            assert other.calls == []
        finally:
            release.set()
            thread.join(5)

    def test_prompt_not_loaded_is_a_503(self, admin_client):
        import api.admin_ops_routes as ops_routes

        ops_routes._system_prompt = ""
        resp = admin_client.post("/admin/llm/warmup", headers=_auth(_OPS_KEY))
        assert resp.status_code == 503
