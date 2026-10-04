# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``scripts/prompt_budget.py``.

The model layer is injected: ``main(..., client_factory=...)`` takes a fake
client, and the real transport class (``OpenAICompatibleClient``) takes fake
``requests.post`` / ``requests.get`` functions, so nothing here opens a
socket. Several data sources come from ``tests/_source_fixtures.py``
(``sales``, ``inventory``, ``archive`` spread over whichever schema the run
loads).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest
import requests

import config as cfg
from database.datasources import DataSourceConfigError
from knowledge.config_loader import ConfigNotFoundError, load_system_prompt
from llm.endpoints import EndpointConfig, build_backend
from llm.providers import OpenAIBackend
from prompt_engine.static_prefix import build_static_prefix, static_prefix_token_estimate
from schema_data.registry import SchemaRegistry
from scripts import prompt_budget as script
from scripts.prompt_budget import (
    EXIT_DOES_NOT_FIT,
    EXIT_ERROR,
    EXIT_OK,
    PATH_RETRIEVAL,
    PATH_STATIC,
    ModelUnavailable,
    OpenAICompatibleClient,
    SourceSize,
    UnavailableClient,
    build_default_client,
    main,
    measure_prefixes,
    parse_context_length,
    recommend,
    round_up_budget,
)
from tests._source_fixtures import SOURCES, configured_sources, tables_of

SECRET_KEY = "sk-secret-key-0123"
SECRET_PASSWORD = "hunter2pw"
SECRET_QUERY = "tokenabc987"
LOCAL_URL = "http://localhost:8000/v1"
REMOTE_URL = "https://api.example.com/v1"

RECOMMEND_DEFAULTS: dict[str, Any] = {
    "context_length": None,
    "question_room": 2000,
    "num_predict": 512,
    "headroom_percent": 10,
    "round_to": 500,
    "assumed_ratio": 1.15,
}


def _size(name: str, estimate: int, real: int | None = None) -> SourceSize:
    return SourceSize(name, 5, estimate * 4, estimate, PATH_STATIC, real)


def _recommend(sources: list[SourceSize], **overrides: Any):
    return recommend(sources, **{**RECOMMEND_DEFAULTS, **overrides})


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClient:
    """A model client with scripted answers."""

    def __init__(
        self,
        *,
        tokens: Callable[[str], int] | None = None,
        context: int | None = None,
        count_error: Exception | None = None,
        context_error: Exception | None = None,
    ) -> None:
        self.model = "fake-model"
        self.endpoint = "http://models.example/v1"
        self._tokens = tokens or (lambda prompt: len(prompt) // 3)
        self._context = context
        self._count_error = count_error
        self._context_error = context_error
        self.counted: list[str] = []
        self.timeouts: list[float] = []
        self.context_calls = 0

    def count_prompt_tokens(self, prompt: str, timeout: float) -> int:
        self.counted.append(prompt)
        self.timeouts.append(timeout)
        if self._count_error is not None:
            raise self._count_error
        return self._tokens(prompt)

    def context_length(self, timeout: float) -> int | None:  # noqa: ARG002
        self.context_calls += 1
        if self._context_error is not None:
            raise self._context_error
        return self._context


class FakeResponse:
    def __init__(self, body: Any = None, *, status: int = 200, text: str = "") -> None:
        self._body = body
        self.status_code = status
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Error for url: {LOCAL_URL}/chat/completions", response=self,
            )

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body


class Recorder:
    """A stand-in for ``requests.post`` / ``requests.get`` that remembers its calls."""

    def __init__(self, response: FakeResponse | None = None, raises: Exception | None = None):
        self._response = response
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response


def _usage(tokens: int) -> dict[str, Any]:
    return {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": tokens}}


def _prefix_tokens(system_prompt: str, per_source: Mapping[str, int]) -> Callable[[str], int]:
    by_prefix = {build_static_prefix(system_prompt, name): n for name, n in per_source.items()}
    return lambda prompt: by_prefix[prompt]


def _default_settings(**overrides: Any):
    values: dict[str, Any] = {
        "openai_base_url": LOCAL_URL,
        "openai_api_key": SECRET_KEY,
        "openai_model": "the-model",
        "llm_endpoints_json": "",
        "llm_routes_json": "",
        "llm_extra_body_json": "",
        "llm_provider": "openai",
        "llm_trusted": None,
        "llm_allow_remote": False,
        "llm_num_predict": 512,
    }
    return cfg.override_settings(**{**values, **overrides})


@pytest.fixture
def system_prompt() -> str:
    return load_system_prompt()


# ---------------------------------------------------------------------------
# Per-source prefix sizes
# ---------------------------------------------------------------------------

class TestMeasurePrefixes:
    def test_each_source_is_sized_from_its_own_prefix(self, system_prompt):
        with configured_sources() as sets:
            sizes = measure_prefixes(system_prompt, SOURCES)
            assert [s.name for s in sizes] == list(SOURCES)
            for size in sizes:
                prefix = build_static_prefix(system_prompt, size.name)
                assert size.chars == len(prefix)
                assert size.estimate == static_prefix_token_estimate(system_prompt, size.name)
                assert size.tables == len(tables_of(size.name, sets))
            # a source's prefix is smaller than the whole schema's
            whole = static_prefix_token_estimate(system_prompt, None)
            assert all(s.estimate < whole for s in sizes)

    def test_the_path_follows_the_current_budget(self, system_prompt):
        with configured_sources():
            with cfg.override_settings(prompt_retrieval_token_budget=10**9):
                assert {s.path_now for s in measure_prefixes(system_prompt, SOURCES)} == {PATH_STATIC}
            with cfg.override_settings(prompt_retrieval_token_budget=1):
                assert {s.path_now for s in measure_prefixes(system_prompt, SOURCES)} == {PATH_RETRIEVAL}
            sizes = measure_prefixes(system_prompt, SOURCES)
            smallest = min(s.estimate for s in sizes)
            with cfg.override_settings(prompt_retrieval_token_budget=smallest):
                paths = {s.name: s.path_now for s in measure_prefixes(system_prompt, SOURCES)}
            assert PATH_STATIC in paths.values()
            assert all(
                paths[s.name] == (PATH_STATIC if s.estimate <= smallest else PATH_RETRIEVAL)
                for s in sizes
            )

    def test_a_single_source_deployment_has_one_implicit_source(self, system_prompt):
        with configured_sources(names=("default",)):
            sizes = measure_prefixes(system_prompt, ["default"])
        assert [s.name for s in sizes] == ["default"]
        assert sizes[0].tables == len(SchemaRegistry.tables_for_source(None))
        assert sizes[0].estimate == static_prefix_token_estimate(system_prompt, None)


# ---------------------------------------------------------------------------
# The recommendation maths
# ---------------------------------------------------------------------------

class TestRoundUpBudget:
    @pytest.mark.parametrize(
        ("largest", "headroom", "round_to", "expected"),
        [
            (5100, 10, 500, 6000),    # 5610 -> next multiple
            (5000, 10, 500, 5500),    # exactly 5500: float 5000*1.1 would give 6000
            (5100, 0, 500, 5500),     # no headroom: only rounding
            (5500, 0, 500, 5500),     # already a multiple
            (5100, 10, 1, 5610),      # round to 1: no rounding
            (4545, 10, 500, 5000),    # 4999.5 -> 5000
            (1, 10, 500, 500),
            (5100, 12.5, 100, 5800),  # 5737.5 -> 5800
        ],
    )
    def test_headroom_then_round_up(self, largest, headroom, round_to, expected):
        assert round_up_budget(largest, headroom, round_to) == expected


class TestRecommend:
    def test_budget_is_the_largest_estimate_plus_headroom_rounded_up(self):
        a, b = _size("a", 2000, 2300), _size("b", 5100, 5800)
        rec = _recommend([a, b], context_length=100_000)
        assert rec.largest_estimate == 5100
        assert rec.budget == 6000
        assert rec.excluded == () and rec.conflicts == ()
        assert [f.fits for f in rec.fits] == [True, True]

    def test_headroom_and_round_are_parameters(self):
        sources = [_size("a", 5000, 5700)]
        assert _recommend(sources, headroom_percent=0, round_to=100).budget == 5000
        assert _recommend(sources, headroom_percent=20, round_to=100).budget == 6000
        assert _recommend(sources, headroom_percent=10, round_to=1000).budget == 6000

    def test_needs_is_prefix_plus_room_plus_num_predict(self):
        rec = _recommend([_size("a", 2000, 2300)], context_length=10_000,
                         question_room=1500, num_predict=300)
        fit = rec.fits[0]
        assert (fit.prefix_tokens, fit.basis, fit.needed) == (2300, "measured", 4100)

    def test_the_fit_boundary_is_inclusive(self):
        source = [_size("a", 2000, 2300)]
        needed = 2300 + 2000 + 512
        assert _recommend(source, context_length=needed).fits[0].fits is True
        assert _recommend(source, context_length=needed - 1).fits[0].fits is False

    def test_a_source_that_does_not_fit_is_left_out_and_the_budget_follows_the_rest(self):
        small, big = _size("small", 2000, 2300), _size("big", 5100, 5800)
        rec = _recommend([small, big], context_length=8192)
        assert rec.excluded == ("big",)
        assert rec.largest_estimate == 2000
        assert rec.budget == 2500
        assert rec.conflicts == ()
        assert any("'big' does not fit" in w and "retrieval" in w for w in rec.warnings)

    def test_order_of_sources_does_not_change_the_budget(self):
        small, big = _size("small", 2000, 2300), _size("big", 5100, 5800)
        assert _recommend([big, small], context_length=8192).budget == 2500

    def test_every_source_excluded_gives_no_budget(self):
        rec = _recommend([_size("a", 5000, 5700), _size("b", 6000, 6900)], context_length=4096)
        assert rec.budget is None and rec.largest_estimate is None
        assert rec.excluded == ("a", "b")
        assert any("no source fits" in w for w in rec.warnings)

    def test_excluded_source_that_the_budget_would_not_keep_on_retrieval(self):
        # b's real/estimate ratio is high, so it overflows with a smaller
        # estimate than a's: no single budget separates them.
        a, b = _size("a", 3000, 3300), _size("b", 2900, 6000)
        rec = _recommend([a, b], context_length=6000)
        assert rec.excluded == ("b",)
        assert rec.budget == 3500
        assert rec.conflicts == ("b",)
        assert any("no budget can do both" in w for w in rec.warnings)

    def test_conflict_names_the_budget_range_that_would_work(self):
        a, b = _size("a", 3000, 3300), _size("b", 3400, 7000)
        rec = _recommend([a, b], context_length=6000)
        assert rec.excluded == ("b",)
        assert rec.budget == 3500 and rec.conflicts == ("b",)
        assert any("from 3000 to 3399" in w for w in rec.warnings)

    def test_unknown_context_skips_the_fit_check_and_includes_everything(self):
        rec = _recommend([_size("a", 2000, 2300), _size("b", 5100, 5800)])
        assert [f.fits for f in rec.fits] == [None, None]
        assert rec.excluded == ()
        assert rec.budget == 6000
        assert any("context length is unknown" in w for w in rec.warnings)

    def test_missing_real_counts_use_the_assumed_ratio_and_say_so(self):
        rec = _recommend([_size("a", 2000), _size("b", 1000)], context_length=100_000)
        assert [f.prefix_tokens for f in rec.fits] == [2300, 1150]
        assert all("assumed" in f.basis and "1.15" in f.basis for f in rec.fits)
        assert rec.observed_ratio is None
        assert any("no real token count" in w for w in rec.warnings)

    def test_assumed_ratio_is_a_parameter(self):
        rec = _recommend([_size("a", 2000)], assumed_ratio=1.5)
        assert rec.fits[0].prefix_tokens == 3000

    def test_an_unmeasured_source_uses_the_largest_ratio_measured_elsewhere(self):
        measured_low = _size("low", 1000, 1050)
        measured_high = _size("high", 1000, 1200)
        unknown = _size("unknown", 2000)
        rec = _recommend([measured_low, measured_high, unknown], context_length=100_000)
        assert rec.observed_ratio == 1.2
        fit = next(f for f in rec.fits if f.name == "unknown")
        assert fit.prefix_tokens == 2400
        assert fit.basis == "estimate x1.20 (largest ratio measured)"
        assert not any("no real token count" in w for w in rec.warnings)

    def test_an_unmeasured_source_can_be_excluded_on_the_projection(self):
        rec = _recommend([_size("a", 1000, 1200), _size("b", 4000)], context_length=6000)
        assert rec.excluded == ("b",)  # 4000 x 1.2 + 2512 > 6000
        assert rec.budget == 1500

    def test_ratio_property_is_rounded_and_absent_without_a_count(self):
        assert _size("a", 5325, 6070).ratio == 1.14
        assert _size("a", 5325).ratio is None


# ---------------------------------------------------------------------------
# /models parsing
# ---------------------------------------------------------------------------

class TestParseContextLength:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"object": "list", "data": [{"id": "m", "max_model_len": 32768}]}, 32768),
            ({"data": [{"id": "m", "context_length": 16384}]}, 16384),
            ({"data": [{"id": "m", "context_window": 8192}]}, 8192),
            ({"data": [{"id": "m", "context_window": 8192, "max_model_len": 4096}]}, 4096),
            ({"data": [{"id": "a", "max_model_len": 1000}, {"id": "m", "max_model_len": 2000}]}, 2000),
            # one model listed under another name (a file path, say): use it
            ({"data": [{"id": "/models/x.gguf", "context_length": 4096}]}, 4096),
        ],
    )
    def test_reads_the_configured_models_entry(self, payload, expected):
        assert parse_context_length(payload, "m") == expected

    @pytest.mark.parametrize(
        "payload",
        [
            {"data": [{"id": "m"}]},                                    # field absent
            {"data": [{"id": "m", "max_model_len": None}]},
            {"data": [{"id": "m", "max_model_len": 0}]},
            {"data": [{"id": "m", "max_model_len": -5}]},
            {"data": [{"id": "m", "max_model_len": True}]},
            {"data": [{"id": "m", "max_model_len": "32768"}]},
            {"data": [{"id": "a", "max_model_len": 1}, {"id": "b", "max_model_len": 2}]},
            {"data": []},
            {"data": "nope"},
            {},
            [],
            None,
            "text",
        ],
    )
    def test_absent_or_unusable_fields_give_none(self, payload):
        assert parse_context_length(payload, "m") is None


# ---------------------------------------------------------------------------
# The real transport, with fake HTTP
# ---------------------------------------------------------------------------

def _client(post: Recorder, get: Recorder | None = None, **settings: Any):
    with _default_settings(**settings):
        client = build_default_client(http_post=post, http_get=get or Recorder(FakeResponse({})))
    return client


class TestOpenAICompatibleClient:
    def test_the_request_is_the_providers_own_with_max_tokens_one(self):
        extra = '{"chat_template_kwargs": {"enable_thinking": false}}'
        post = Recorder(FakeResponse(_usage(321)))
        with _default_settings(llm_extra_body_json=extra, llm_stop=("###",)):
            client = build_default_client(http_post=post, http_get=Recorder(FakeResponse({})))
            tokens = client.count_prompt_tokens("PREFIX TEXT", 77.0)
            expected = OpenAIBackend(
                model="the-model", api_key=SECRET_KEY, base_url=LOCAL_URL,
            )._build_payload("PREFIX TEXT")
            headers = OpenAIBackend(
                model="the-model", api_key=SECRET_KEY, base_url=LOCAL_URL,
            )._headers
        assert tokens == 321
        (call,) = post.calls
        assert call["url"] == f"{LOCAL_URL}/chat/completions"
        assert call["timeout"] == 77.0
        assert call["headers"] == headers
        assert call["headers"]["Authorization"] == f"Bearer {SECRET_KEY}"
        assert call["json"] == {**expected, "max_tokens": 1}
        assert call["json"]["messages"] == [{"role": "user", "content": "PREFIX TEXT"}]
        assert call["json"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert call["json"]["stop"] == ["###"]
        assert call["json"]["model"] == "the-model"

    def test_no_api_key_sends_no_authorization_header(self):
        post = Recorder(FakeResponse(_usage(5)))
        client = _client(post, openai_api_key="")
        client.count_prompt_tokens("x", 1.0)
        assert "Authorization" not in post.calls[0]["headers"]

    def test_the_first_endpoint_of_the_sql_generation_route_is_used(self):
        endpoints = json.dumps([
            {"name": "fast", "base_url": "http://localhost:9000/v1/", "model": "fast-model"},
            {"name": "slow", "base_url": "http://localhost:9001/v1", "model": "slow-model"},
        ])
        routes = json.dumps({"sql_generation": ["fast", "slow"], "interpretation": ["slow"]})
        post = Recorder(FakeResponse(_usage(9)))
        client = _client(post, llm_endpoints_json=endpoints, llm_routes_json=routes)
        assert (client.model, client.endpoint) == ("fast-model", "http://localhost:9000/v1")
        client.count_prompt_tokens("x", 1.0)
        assert post.calls[0]["url"] == "http://localhost:9000/v1/chat/completions"
        assert post.calls[0]["json"]["model"] == "fast-model"

    def test_unknown_route_endpoint_is_a_configuration_error(self):
        with _default_settings(llm_routes_json='{"sql_generation": ["nope"]}'):
            with pytest.raises(ValueError, match="unknown endpoint 'nope'"):
                build_default_client()

    def test_invalid_extra_body_is_a_configuration_error(self):
        with _default_settings(llm_extra_body_json="[1, 2]"):
            with pytest.raises(ValueError, match="LLM_EXTRA_BODY"):
                build_default_client()

    def test_mock_provider_has_no_endpoint_to_ask(self):
        with _default_settings(llm_provider="mock"):
            client = build_default_client()
        assert isinstance(client, UnavailableClient)
        with pytest.raises(ModelUnavailable, match="mock"):
            client.count_prompt_tokens("x", 1.0)
        with pytest.raises(ModelUnavailable, match="mock"):
            client.context_length(1.0)

    def test_an_untrusted_endpoint_is_sent_nothing_unless_remote_is_allowed(self):
        post = Recorder(FakeResponse(_usage(5)))
        client = _client(post, openai_base_url=REMOTE_URL)
        with _default_settings(openai_base_url=REMOTE_URL):
            with pytest.raises(ModelUnavailable, match="LLM_ALLOW_REMOTE"):
                client.count_prompt_tokens("schema text", 1.0)
        assert post.calls == []

    def test_an_allowed_untrusted_endpoint_is_audited_like_a_routed_call(self, monkeypatch):
        audited: list[tuple[str, str, str]] = []

        def record(backend, task, segments) -> None:
            audited.append((backend.name, task.value, segments.static_prefix))

        monkeypatch.setattr(script.LLMRouter, "_audit_remote_use", staticmethod(record))
        post = Recorder(FakeResponse(_usage(5)))
        client = _client(post, openai_base_url=REMOTE_URL, llm_allow_remote=True)
        with _default_settings(openai_base_url=REMOTE_URL, llm_allow_remote=True):
            assert client.count_prompt_tokens("schema text", 1.0) == 5
        assert audited == [("openai:the-model", "sql_generation", "schema text")]
        assert len(post.calls) == 1

    def test_a_trusted_endpoint_writes_no_audit_record(self, monkeypatch):
        monkeypatch.setattr(
            script.LLMRouter, "_audit_remote_use",
            staticmethod(lambda *a: pytest.fail("audited a trusted call")),
        )
        client = _client(Recorder(FakeResponse(_usage(5))))
        assert client.count_prompt_tokens("x", 1.0) == 5

    @pytest.mark.parametrize(
        "body",
        [{}, {"usage": {}}, {"usage": {"prompt_tokens": 0}}, {"usage": {"prompt_tokens": "9"}},
         {"usage": None}, [], {"usage": {"prompt_tokens": True}}],
    )
    def test_a_response_without_usage_is_unavailable(self, body):
        client = _client(Recorder(FakeResponse(body)))
        with pytest.raises(ModelUnavailable, match=r"no usage\.prompt_tokens"):
            client.count_prompt_tokens("x", 1.0)

    def test_a_response_that_is_not_json_is_unavailable(self):
        client = _client(Recorder(FakeResponse(None)))
        with pytest.raises(ModelUnavailable, match="ValueError"):
            client.count_prompt_tokens("x", 1.0)

    def test_timeout_tells_the_operator_to_raise_it(self):
        client = _client(Recorder(raises=requests.ReadTimeout("slow")))
        with pytest.raises(ModelUnavailable, match=r"no answer within 12s .*--timeout"):
            client.count_prompt_tokens("x", 12.0)

    def test_unreachable_endpoint_is_unavailable_without_the_library_noise(self):
        error = requests.ConnectionError(
            "HTTPConnectionPool(host='localhost', port=8000): Max retries exceeded "
            "(Caused by NewConnectionError('refused'))"
        )
        client = _client(Recorder(raises=error))
        with pytest.raises(ModelUnavailable) as caught:
            client.count_prompt_tokens("x", 1.0)
        assert str(caught.value) == f"cannot connect to {LOCAL_URL}"

    def test_http_error_includes_the_servers_reason(self):
        response = FakeResponse(
            status=400, text='{"error": "maximum context length is 8192 tokens"}',
        )
        client = _client(Recorder(response))
        with pytest.raises(ModelUnavailable, match="maximum context length is 8192"):
            client.count_prompt_tokens("x", 1.0)

    def test_context_length_comes_from_models(self):
        get = Recorder(FakeResponse({"data": [{"id": "the-model", "max_model_len": 40960}]}))
        client = _client(Recorder(FakeResponse({})), get)
        assert client.context_length(3.0) == 40960
        (call,) = get.calls
        assert call["url"] == f"{LOCAL_URL}/models"
        assert call["timeout"] == 3.0
        assert call["headers"]["Authorization"] == f"Bearer {SECRET_KEY}"

    def test_context_length_is_none_when_models_does_not_say(self):
        get = Recorder(FakeResponse({"data": [{"id": "the-model"}]}))
        assert _client(Recorder(FakeResponse({})), get).context_length(1.0) is None

    def test_context_length_failure_is_unavailable(self):
        get = Recorder(raises=requests.ConnectionError("boom"))
        client = _client(Recorder(FakeResponse({})), get)
        with pytest.raises(ModelUnavailable, match="cannot connect"):
            client.context_length(1.0)

    def test_printable_endpoint_has_no_credentials_or_query(self):
        config = EndpointConfig(
            name="default",
            base_url=f"http://user:{SECRET_PASSWORD}@gpu.example:8000/v1?key={SECRET_QUERY}",
            model="m", api_key=SECRET_KEY,
        )
        client = OpenAICompatibleClient(config, build_backend(config))
        assert client.endpoint == "http://gpu.example:8000/v1"


# ---------------------------------------------------------------------------
# main(): behaviour, output, exit codes
# ---------------------------------------------------------------------------

def _run(capsys, argv: list[str], factory: Callable[[], Any] | None = None):
    code = main(argv, client_factory=factory)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestMain:
    def test_per_source_sizes_and_real_counts_in_the_table(self, capsys, system_prompt):
        with configured_sources():
            sizes = {s.name: s for s in measure_prefixes(system_prompt, SOURCES)}
            tokens = {name: int(size.estimate * 1.14) for name, size in sizes.items()}
            client = FakeClient(
                tokens=_prefix_tokens(system_prompt, tokens), context=1_000_000,
            )
            code, out, err = _run(capsys, ["--timeout", "42"], lambda: client)
        assert code == EXIT_OK
        assert client.timeouts == [42.0] * len(SOURCES)
        for name, size in sizes.items():
            row = next(line for line in out.splitlines() if line.split()[:1] == [name])
            assert str(size.estimate) in row.split()
            assert str(tokens[name]) in row.split()
            assert "1.14" in row.split()
        assert "warm" in out                       # the cache side effect is stated
        assert "counting the tokens of 'sales'" in err and "counting" not in out

    def test_recommendation_and_env_line(self, capsys, system_prompt):
        with configured_sources():
            sizes = measure_prefixes(system_prompt, SOURCES)
            per_source = {s.name: int(s.estimate * 1.14) for s in sizes}
            client = FakeClient(tokens=_prefix_tokens(system_prompt, per_source), context=10**6)
            code, out, _ = _run(capsys, ["--round", "100"], lambda: client)
        largest = max(s.estimate for s in sizes)
        expected = round_up_budget(largest, 10, 100)
        assert code == EXIT_OK
        assert f"\nPROMPT_RETRIEVAL_TOKEN_BUDGET={expected}\n" in out
        assert f"largest estimate that fits ({largest})" in out

    def test_what_changes_for_each_source(self, capsys, system_prompt):
        with configured_sources():
            sizes = measure_prefixes(system_prompt, SOURCES)
            smallest = min(s.estimate for s in sizes)
            largest = max(s.estimate for s in sizes)
            assert smallest < largest
            with cfg.override_settings(prompt_retrieval_token_budget=smallest):
                client = FakeClient(context=10**6)
                code, out, _ = _run(capsys, [], lambda: client)
        assert code == EXIT_OK
        rows: dict[str, list[str]] = {}
        for line in out.splitlines():
            cells = line.split()
            if cells and cells[0] in SOURCES and cells[-1] in {"unchanged", "changes"}:
                rows[cells[0]] = cells
        assert set(rows) == set(SOURCES)
        for size in sizes:
            if size.estimate > smallest:
                assert rows[size.name][-1] == "changes"
                assert PATH_RETRIEVAL in " ".join(rows[size.name])
            else:
                assert rows[size.name][-1] == "unchanged"

    def test_a_source_that_does_not_fit_is_excluded_and_exit_code_is_one(
        self, capsys, system_prompt,
    ):
        with configured_sources():
            sizes = {s.name: s for s in measure_prefixes(system_prompt, SOURCES)}
            biggest = max(sizes.values(), key=lambda s: s.estimate)
            tokens = {name: size.estimate for name, size in sizes.items()}
            tokens[biggest.name] = 50_000
            client = FakeClient(tokens=_prefix_tokens(system_prompt, tokens), context=20_000)
            code, out, _ = _run(capsys, ["--json"], lambda: client)
        assert code == EXIT_DOES_NOT_FIT
        document = json.loads(out)
        excluded = [s["name"] for s in document["sources"] if s["excluded"]]
        assert excluded == [biggest.name]
        rest = [s for s in sizes.values() if s.name != biggest.name]
        assert document["recommendation"]["largest_estimate"] == max(s.estimate for s in rest)
        assert any(biggest.name in w for w in document["recommendation"]["warnings"])

    def test_context_length_option_overrides_models(self, capsys):
        with configured_sources():
            client = FakeClient(context=10**6)
            code, out, _ = _run(capsys, ["--context-length", "1000", "--json"], lambda: client)
        assert client.context_calls == 0
        assert code == EXIT_DOES_NOT_FIT           # nothing fits in 1000 tokens
        document = json.loads(out)
        assert document["model"]["context_length"] == 1000
        assert document["model"]["context_source"] == "--context-length"
        assert document["recommendation"]["budget"] is None
        assert document["recommendation"]["env_line"] is None

    def test_context_length_from_models_when_not_given(self, capsys):
        with configured_sources():
            client = FakeClient(context=32768)
            code, out, _ = _run(capsys, ["--json"], lambda: client)
        assert code == EXIT_OK
        assert client.context_calls == 1
        model = json.loads(out)["model"]
        assert model["context_length"] == 32768
        assert "/models" in model["context_source"]

    def test_unknown_context_length_is_reported_and_not_an_error(self, capsys):
        with configured_sources():
            client = FakeClient(context=None)
            code, out, _ = _run(capsys, [], lambda: client)
        assert code == EXIT_OK
        assert "context length: unknown" in out
        assert "fit check was skipped" in out

    def test_models_failure_leaves_the_context_unknown_and_goes_on(self, capsys):
        with configured_sources():
            client = FakeClient(context_error=ModelUnavailable("cannot connect to x"))
            code, out, _ = _run(capsys, ["--json"], lambda: client)
        assert code == EXIT_OK
        model = json.loads(out)["model"]
        assert model["context_length"] is None
        assert "GET /models failed: cannot connect to x" in model["context_source"]

    def test_unreachable_endpoint_reports_unavailable_for_every_source_and_goes_on(
        self, capsys,
    ):
        with configured_sources():
            client = FakeClient(
                count_error=ModelUnavailable("cannot connect to http://models.example/v1"),
                context_error=ModelUnavailable("cannot connect to http://models.example/v1"),
            )
            code, out, _ = _run(capsys, [], lambda: client)
        assert code == EXIT_OK
        assert len(client.counted) == len(SOURCES)          # each source was still tried
        assert out.count("unavailable") >= len(SOURCES)
        assert "cannot connect to http://models.example/v1" in out
        assert "PROMPT_RETRIEVAL_TOKEN_BUDGET=" in out        # a budget from the estimates
        assert "no real token count" in out

    def test_one_failing_source_does_not_stop_the_others(self, capsys, system_prompt):
        with configured_sources():
            first = build_static_prefix(system_prompt, SOURCES[0])

            def tokens(prompt: str) -> int:
                if prompt == first:
                    raise ModelUnavailable("the response carries no usage.prompt_tokens")
                return 1234

            code, out, _ = _run(capsys, ["--json"], lambda: FakeClient(tokens=tokens, context=10**6))
        assert code == EXIT_OK
        by_name = {s["name"]: s for s in json.loads(out)["sources"]}
        assert by_name[SOURCES[0]]["real_tokens"] is None
        assert "no usage.prompt_tokens" in by_name[SOURCES[0]]["real_note"]
        assert by_name[SOURCES[1]]["real_tokens"] == 1234
        assert by_name[SOURCES[0]]["prefix_basis"].startswith("estimate x")

    def test_an_unexpected_client_error_is_contained_and_not_printed_in_full(self, capsys):
        with configured_sources():
            client = FakeClient(count_error=RuntimeError(f"leaked {SECRET_KEY}"))
            code, out, err = _run(capsys, [], lambda: client)
        assert code == EXIT_OK
        assert "unexpected error (RuntimeError)" in out
        assert SECRET_KEY not in out + err

    def test_no_model_never_builds_a_client(self, capsys):
        def factory() -> Any:
            pytest.fail("the model endpoint was contacted")

        with configured_sources():
            code, out, err = _run(capsys, ["--no-model", "--json"], factory)
        assert code == EXIT_OK
        document = json.loads(out)
        assert document["model"]["queried"] is False
        assert all(s["real_tokens"] is None for s in document["sources"])
        assert err == ""

    def test_no_model_still_checks_the_fit_with_a_given_context_length(self, capsys):
        with configured_sources():
            code, out, _ = _run(capsys, ["--no-model", "--context-length", "1000", "--json"])
        assert code == EXIT_DOES_NOT_FIT
        document = json.loads(out)
        assert all(s["fits"] is False for s in document["sources"])

    def test_single_source_deployment(self, capsys):
        with configured_sources(names=("default",)):
            client = FakeClient(context=10**6)
            code, out, _ = _run(capsys, ["--json"], lambda: client)
        assert code == EXIT_OK
        assert [s["name"] for s in json.loads(out)["sources"]] == ["default"]
        assert len(client.counted) == 1

    # -- JSON -----------------------------------------------------------

    def test_json_shape(self, capsys):
        with configured_sources():
            client = FakeClient(context=32768)
            code, out, _ = _run(capsys, ["--json"], lambda: client)
        assert code == EXIT_OK
        document = json.loads(out)                          # stdout is only the document
        assert set(document) == {
            "system_prompt", "current_budget", "model", "parameters", "sources", "recommendation",
        }
        assert set(document["model"]) == {
            "queried", "model", "endpoint", "context_length", "context_source", "requests_sent",
        }
        assert document["model"]["endpoint"] == "http://models.example/v1"
        assert document["parameters"] == {
            "question_room": 2000, "num_predict": 512, "headroom_percent": 10.0, "round_to": 500,
        }
        assert [s["name"] for s in document["sources"]] == list(SOURCES)
        for source in document["sources"]:
            assert set(source) == {
                "name", "tables", "chars", "estimate", "path_now", "real_tokens", "real_note",
                "ratio", "prefix_tokens", "prefix_basis", "needs", "fits", "excluded",
                "path_after",
            }
            assert source["path_now"] in {PATH_STATIC, PATH_RETRIEVAL}
            assert source["path_after"] in {PATH_STATIC, PATH_RETRIEVAL}
            assert source["needs"] == source["prefix_tokens"] + 2000 + 512
        recommendation = document["recommendation"]
        assert set(recommendation) == {
            "budget", "env_line", "largest_estimate", "observed_ratio", "excluded",
            "conflicts", "warnings",
        }
        assert recommendation["env_line"] == (
            f"PROMPT_RETRIEVAL_TOKEN_BUDGET={recommendation['budget']}"
        )
        assert recommendation["budget"] % 500 == 0

    def test_parameters_reach_the_recommendation(self, capsys, system_prompt):
        with configured_sources():
            estimate = max(s.estimate for s in measure_prefixes(system_prompt, SOURCES))
            code, out, _ = _run(capsys, [
                "--no-model", "--json", "--headroom", "20", "--round", "250",
                "--question-room", "100", "--assumed-ratio", "1.5",
            ])
        assert code == EXIT_OK
        document = json.loads(out)
        assert document["parameters"] == {
            "question_room": 100, "num_predict": 512, "headroom_percent": 20.0, "round_to": 250,
        }
        assert document["recommendation"]["budget"] == round_up_budget(estimate, 20, 250)
        for source in document["sources"]:
            assert source["prefix_basis"] == "estimate x1.50 (assumed)"
            assert source["needs"] == source["prefix_tokens"] + 100 + 512

    # -- exit code 2 ----------------------------------------------------

    def test_missing_system_prompt_is_a_configuration_error(self, capsys, monkeypatch):
        def missing() -> str:
            raise ConfigNotFoundError("/nowhere/system_prompt.md not found")

        monkeypatch.setattr(script, "load_system_prompt", missing)
        code, out, err = _run(capsys, ["--no-model"])
        assert code == EXIT_ERROR
        assert out == "" and "system_prompt.md not found" in err

    def test_invalid_datasources_are_a_configuration_error(self, capsys, monkeypatch):
        def broken() -> tuple[str, ...]:
            raise DataSourceConfigError("source 'sales': DB_PASSWORD_SALES is not set")

        monkeypatch.setattr("database.datasources.datasource_names", broken)
        code, out, err = _run(capsys, ["--no-model"])
        assert code == EXIT_ERROR
        assert out == "" and "DB_PASSWORD_SALES" in err

    def test_invalid_llm_configuration_is_a_configuration_error(self, capsys):
        with configured_sources(), _default_settings(llm_endpoints_json="{not json"):
            code, out, err = _run(capsys, [], None)
        assert code == EXIT_ERROR
        assert out == "" and "LLM_ENDPOINTS is not valid JSON" in err

    def test_invalid_llm_configuration_does_not_matter_without_the_model(self, capsys):
        with configured_sources(), _default_settings(llm_endpoints_json="{not json"):
            code, _, _ = _run(capsys, ["--no-model"], None)
        assert code == EXIT_OK

    @pytest.mark.parametrize(
        "argv",
        [
            ["--headroom", "-1"], ["--round", "0"], ["--timeout", "0"], ["--context-length", "0"],
            ["--question-room", "-3"], ["--assumed-ratio", "nan"], ["--round", "x"],
            ["--models-timeout", "-1"], ["--nonsense"],
        ],
    )
    def test_invalid_options_exit_with_two(self, argv, capsys):
        with pytest.raises(SystemExit) as caught:
            main(argv)
        assert caught.value.code == EXIT_ERROR
        capsys.readouterr()

    # -- the default client end to end ---------------------------------

    def test_default_client_end_to_end_with_fake_http(self, capsys, system_prompt):
        with configured_sources():
            sizes = {s.name: s for s in measure_prefixes(system_prompt, SOURCES)}
            bodies = {
                build_static_prefix(system_prompt, name): _usage(int(size.estimate * 1.2))
                for name, size in sizes.items()
            }

            def post(url: str, **kwargs: Any) -> FakeResponse:
                assert kwargs["json"]["max_tokens"] == 1
                return FakeResponse(bodies[kwargs["json"]["messages"][0]["content"]])

            get = Recorder(FakeResponse({"data": [{"id": "the-model", "max_model_len": 65536}]}))
            with _default_settings():
                code = main(
                    ["--json"],
                    client_factory=lambda: build_default_client(http_post=post, http_get=get),
                )
        out = capsys.readouterr().out
        assert code == EXIT_OK
        document = json.loads(out)
        assert document["model"]["context_length"] == 65536
        assert document["model"]["endpoint"] == LOCAL_URL
        for source in document["sources"]:
            assert source["ratio"] == 1.2
            assert source["prefix_basis"] == "measured"


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

SECRETS = (SECRET_KEY, SECRET_PASSWORD, SECRET_QUERY)
SECRET_URL = f"http://user:{SECRET_PASSWORD}@localhost:8000/v1?token={SECRET_QUERY}"


class TestNoSecretsInOutput:
    @pytest.mark.parametrize("as_json", [False, True])
    @pytest.mark.parametrize(
        "scenario", ["success", "connection", "timeout", "http_error", "no_usage", "not_json"],
    )
    def test_nothing_secret_is_printed(self, capsys, scenario, as_json):
        leaky = (
            f"boom for url {SECRET_URL}/chat/completions with key {SECRET_KEY} "
            f"and password {SECRET_PASSWORD}"
        )
        responses: dict[str, Recorder] = {
            "success": Recorder(FakeResponse(_usage(1000))),
            "connection": Recorder(raises=requests.ConnectionError(leaky)),
            "timeout": Recorder(raises=requests.ReadTimeout(leaky)),
            "http_error": Recorder(FakeResponse(status=500, text=leaky)),
            "no_usage": Recorder(FakeResponse({"leak": leaky})),
            "not_json": Recorder(FakeResponse(None)),
        }
        get = Recorder(raises=requests.ConnectionError(leaky))
        argv = ["--json"] if as_json else []
        with configured_sources(), _default_settings(
            openai_base_url=SECRET_URL, openai_api_key=SECRET_KEY,
        ):
            code = main(argv, client_factory=lambda: build_default_client(
                http_post=responses[scenario], http_get=get,
            ))
        captured = capsys.readouterr()
        everything = captured.out + captured.err
        assert code == EXIT_OK
        for secret in SECRETS:
            assert secret not in everything
        assert "localhost:8000/v1" in everything            # the endpoint host is shown

    def test_the_api_key_is_sent_but_never_shown(self, capsys):
        post = Recorder(FakeResponse(_usage(1000)))
        with configured_sources(), _default_settings():
            main([], client_factory=lambda: build_default_client(
                http_post=post, http_get=Recorder(FakeResponse({})),
            ))
        captured = capsys.readouterr()
        assert post.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET_KEY}"
        assert SECRET_KEY not in captured.out + captured.err


def test_the_script_has_the_licence_header():
    text = Path(script.__file__).read_text(encoding="utf-8")
    assert text.startswith(
        "# SPDX-License-Identifier: BUSL-1.1\n# Copyright (c) 2024-2026 Ali Sadeghi Aghili\n"
    )
