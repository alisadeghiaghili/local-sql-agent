# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for llm/router.py — TaskType routing, fallback chains, and governance.

Exit criteria this file is directly responsible for:

* "A second provider — mock is fine — answers through the router with
  no change at the call sites" — :class:`TestGenerateForTask`.
* "Segmented prompts survive end to end: a test proving the static
  prefix reaches the provider adapter as its own segment, not
  concatenated" — :class:`TestSegmentsReachAdapterUnflattened`.
* The ``LLM_ALLOW_REMOTE`` gate itself — :class:`TestGovernance`.
* "A local-looking endpoint is trusted and a hosted one is not, by
  configuration — with a test for each direction, and a test that the
  default with no config is untrusted" — :class:`TestTrust`.
* "Two endpoints routed per task with a working fallback, `fallback_used`
  and `endpoint` recorded" — :class:`TestFromSettingsMultiEndpoint`.
"""

from __future__ import annotations

import pytest
import requests

import config as cfg
from llm.base import LLMBackend
from llm.providers import MockBackend, OpenAIBackend
from llm.router import (
    LLMRouter,
    PromptSegments,
    RemoteProviderNotAllowedError,
    TaskType,
    build_prompt_segments,
    is_trusted_backend,
)


class _FailingBackend(LLMBackend):
    """A backend whose every call raises, to exercise fallback chains."""

    name = "failing:stub"

    def generate(self, prompt: str) -> str:
        raise RuntimeError("simulated backend failure")


class _SegmentSpyBackend(LLMBackend):
    """Records the exact object passed to generate_with_meta_segments/generate_structured."""

    def __init__(self) -> None:
        self.received: object | None = None

    @property
    def name(self) -> str:
        return "spy:stub"

    def generate(self, prompt: str) -> str:  # pragma: no cover - unused here
        raise NotImplementedError

    def generate_with_meta_segments(self, segments):  # noqa: ANN001
        self.received = segments
        return "ok", {}

    def generate_structured(self, segments, schema):  # noqa: ANN001
        self.received = segments
        return {"sql": "SELECT 1", "out_of_scope": False}, {}


class TestPromptSegments:
    def test_flatten_joins_nonempty_parts(self):
        segs = PromptSegments(static_prefix="A", session_context="B", question="C")
        assert segs.flatten() == "A\n\nB\n\nC"

    def test_flatten_skips_empty_parts(self):
        segs = PromptSegments(static_prefix="A", question="C")
        assert segs.flatten() == "A\n\nC"


class TestGenerateForTask:
    """Exit criterion: a second provider answers through the router unchanged."""

    def test_mock_backend_answers_through_router(self):
        router = LLMRouter(default_chain=[MockBackend(response="SELECT 1")])
        result = router.generate_for_task(TaskType.SQL_GENERATION, PromptSegments(question="q"))
        assert result.text == "SELECT 1"
        assert result.provider == "mock:stub"
        assert result.fallback_used is False
        assert result.meta["provider"] == "mock:stub"
        assert result.meta["fallback_used"] is False
        assert result.meta["trusted"] is True
        assert result.meta["endpoint"] is None

    def test_structured_call_returns_object(self):
        backend = MockBackend(structured={"sql": "SELECT 2", "out_of_scope": False})
        router = LLMRouter(default_chain=[backend])
        result = router.generate_structured_for_task(
            TaskType.SQL_GENERATION, PromptSegments(question="q"), schema={"type": "object"},
        )
        assert result.structured == {"sql": "SELECT 2", "out_of_scope": False}
        assert result.text is None

    def test_no_chain_configured_raises(self):
        router = LLMRouter()
        with pytest.raises(RuntimeError, match="No backend configured"):
            router.generate_for_task(TaskType.INTERPRETATION, PromptSegments(question="q"))


class TestFallbackChain:
    """Test doubles here have no real endpoint, so they inherit
    ``LLMBackend.trusted``'s ``True`` default and need no ``llm_allow_remote``
    opt-in -- these tests are about fallback-on-failure behaviour, not the
    governance gate itself (see ``TestGovernance``)."""

    def test_falls_back_to_second_backend_on_failure(self):
        router = LLMRouter(default_chain=[_FailingBackend(), MockBackend(response="fallback-ok")])
        result = router.generate_for_task(TaskType.SQL_GENERATION, PromptSegments(question="q"))
        assert result.text == "fallback-ok"
        assert result.fallback_used is True
        assert result.provider == "mock:stub"

    def test_every_backend_failing_raises(self):
        router = LLMRouter(default_chain=[_FailingBackend(), _FailingBackend()])
        with pytest.raises(RuntimeError, match="Every backend in the chain failed"):
            router.generate_for_task(TaskType.SQL_GENERATION, PromptSegments(question="q"))

    def test_out_of_scope_short_circuits_the_chain(self):
        """OUT_OF_SCOPE is a terminal domain decision, not a backend
        failure -- the chain must stop there rather than letting a second
        backend override the first one's refusal."""
        class _OutOfScopeBackend(LLMBackend):
            name = "oos:stub"

            def generate(self, prompt: str) -> str:
                raise ValueError("OUT_OF_SCOPE")

        answering = MockBackend(response="SELECT 1")
        router = LLMRouter(default_chain=[_OutOfScopeBackend(), answering])

        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            router.generate_for_task(
                TaskType.SQL_GENERATION, PromptSegments(question="q")
            )

    def test_out_of_scope_is_reraised_unwrapped_and_keeps_llm_meta(self):
        """The exception object itself must reach the caller: ``llm_meta``
        (attached by OpenAIBackend, read by the audit trail) survives, and
        ``__cause__`` stays None so the call sites' ``exc.__cause__ or exc``
        unwrap contract resolves back to this same ValueError."""
        meta = {"endpoint_status": 200, "attempts": 1}

        class _OutOfScopeWithMetaBackend(LLMBackend):
            name = "oos-meta:stub"

            def generate(self, prompt: str) -> str:
                exc = ValueError("OUT_OF_SCOPE")
                exc.llm_meta = meta
                raise exc

        router = LLMRouter(
            default_chain=[_OutOfScopeWithMetaBackend(), MockBackend(response="SELECT 1")]
        )

        with pytest.raises(ValueError) as exc_info:
            router.generate_for_task(
                TaskType.SQL_GENERATION, PromptSegments(question="q")
            )

        raised = exc_info.value
        assert raised.llm_meta is meta
        assert raised.__cause__ is None
        assert (raised.__cause__ or raised) is raised
        assert "Every backend in the chain failed" not in str(raised)

    def test_out_of_scope_short_circuits_generate_structured_too(self):
        """Same contract on the structured path -- a backend raises the
        sentinel from ``generate_structured`` as well."""
        class _OutOfScopeStructuredBackend(LLMBackend):
            name = "oos-structured:stub"

            def generate(self, prompt: str) -> str:  # pragma: no cover - unused
                raise NotImplementedError

            def generate_structured(self, segments, schema):  # noqa: ANN001
                raise ValueError("OUT_OF_SCOPE")

        answering = MockBackend(structured={"sql": "SELECT 1", "out_of_scope": False})
        router = LLMRouter(default_chain=[_OutOfScopeStructuredBackend(), answering])

        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            router.generate_structured_for_task(
                TaskType.SQL_GENERATION, PromptSegments(question="q"), {"type": "object"}
            )

    def test_a_non_sentinel_value_error_still_falls_through(self):
        """Only the exact OUT_OF_SCOPE sentinel is terminal -- an ordinary
        ValueError is still an ordinary backend failure."""
        class _ValueErrorBackend(LLMBackend):
            name = "verr:stub"

            def generate(self, prompt: str) -> str:
                raise ValueError("model returned garbage")

        router = LLMRouter(
            default_chain=[_ValueErrorBackend(), MockBackend(response="fallback-ok")]
        )

        result = router.generate_for_task(
            TaskType.SQL_GENERATION, PromptSegments(question="q")
        )

        assert result.text == "fallback-ok"
        assert result.fallback_used is True

    def test_budget_exceeded_falls_through_to_next_backend(self):
        class _SlowBackend(LLMBackend):
            name = "slow:stub"

            def generate(self, prompt: str) -> str:
                import time
                time.sleep(0.05)
                return "slow-response"

        router = LLMRouter(
            default_chain=[_SlowBackend(), MockBackend(response="fast-ok")],
            budgets={TaskType.SQL_GENERATION: 0.001},
        )
        result = router.generate_for_task(TaskType.SQL_GENERATION, PromptSegments(question="q"))
        assert result.text == "fast-ok"
        assert result.fallback_used is True


class TestGovernance:
    def test_untrusted_endpoint_refused_without_opt_in(self):
        remote = OpenAIBackend(model="gpt-4o-mini", api_key="sk-fake")
        assert remote.trusted is False
        router = LLMRouter(default_chain=[remote])
        with cfg.override_settings(llm_allow_remote=False):
            with pytest.raises(RemoteProviderNotAllowedError, match="llm_allow_remote"):
                router.generate_for_task(TaskType.SQL_GENERATION, PromptSegments(question="q"))

    def test_untrusted_endpoint_allowed_with_explicit_opt_in(self):
        from unittest.mock import patch

        remote = OpenAIBackend(model="gpt-4o-mini", api_key="sk-fake")
        router = LLMRouter(default_chain=[remote])
        body = {"choices": [{"message": {"content": "remote-ok"}}]}
        response = type("Resp", (), {
            "status_code": 200,
            "json": lambda self: body,
            "raise_for_status": lambda self: None,
        })()
        with cfg.override_settings(llm_allow_remote=True):
            with patch("requests.post", return_value=response):
                result = router.generate_for_task(TaskType.SQL_GENERATION, PromptSegments(question="q"))
        assert result.text == "remote-ok"

    def test_is_trusted_backend_recognises_mock_and_local_endpoint(self):
        assert is_trusted_backend(MockBackend())
        assert is_trusted_backend(
            OpenAIBackend(model="m", api_key="k", base_url="http://localhost:8000/v1")
        )

    def test_is_trusted_backend_rejects_hosted_endpoint(self):
        assert not is_trusted_backend(OpenAIBackend(model="m", api_key="k"))


class TestTrust:
    """Exit criterion 6: a local-looking endpoint is trusted and a hosted
    one is not, by configuration -- one test per direction, plus the
    untrusted-by-default case."""

    def test_default_with_no_config_is_untrusted(self):
        """The factory default base_url (a hosted API) with no explicit
        trust override must be untrusted -- fail closed."""
        backend = OpenAIBackend(model="m", api_key="k")
        assert backend.trusted is False
        assert not is_trusted_backend(backend)

    def test_local_looking_endpoint_is_trusted_by_configuration(self):
        backend = OpenAIBackend(model="m", api_key="k", base_url="http://localhost:8000/v1")
        assert backend.trusted is True
        assert is_trusted_backend(backend)

    def test_hosted_endpoint_is_not_trusted_by_configuration(self):
        backend = OpenAIBackend(model="m", api_key="k", base_url="https://api.openai.com/v1")
        assert backend.trusted is False
        assert not is_trusted_backend(backend)

    def test_explicit_override_wins_over_the_local_default(self):
        """An operator can mark even a loopback endpoint untrusted explicitly."""
        backend = OpenAIBackend(
            model="m", api_key="k", base_url="http://localhost:8000/v1", trusted=False,
        )
        assert backend.trusted is False

    def test_explicit_override_wins_over_the_hosted_default(self):
        """An operator can mark even a hosted-looking endpoint trusted explicitly."""
        backend = OpenAIBackend(
            model="m", api_key="k", base_url="https://api.openai.com/v1", trusted=True,
        )
        assert backend.trusted is True


class TestSegmentsReachAdapterUnflattened:
    """Exit criterion: the static prefix reaches the adapter as its own segment."""

    def test_generate_for_task_passes_segments_object_not_a_string(self):
        spy = _SegmentSpyBackend()
        router = LLMRouter(default_chain=[spy])
        segments = PromptSegments(static_prefix="SCHEMA_BLOCK", question="the question")

        router.generate_for_task(TaskType.SQL_GENERATION, segments)

        assert spy.received is segments
        assert isinstance(spy.received, PromptSegments)
        assert spy.received.static_prefix == "SCHEMA_BLOCK"
        assert spy.received.question == "the question"
        # Never silently joined into one string before reaching the backend.
        assert not isinstance(spy.received, str)

    def test_generate_structured_for_task_passes_segments_object_not_a_string(self):
        spy = _SegmentSpyBackend()
        router = LLMRouter(default_chain=[spy])
        segments = PromptSegments(static_prefix="SCHEMA_BLOCK", question="the question")

        router.generate_structured_for_task(
            TaskType.SQL_GENERATION, segments, schema={"type": "object"},
        )

        assert spy.received is segments
        assert spy.received.static_prefix == "SCHEMA_BLOCK"

    def test_default_generate_with_meta_segments_flattens_internally(self):
        """A backend with NO segment-aware override still works (flattens)."""
        backend = MockBackend(response="flattened-ok")
        text, meta = backend.generate_with_meta_segments(
            PromptSegments(static_prefix="P", question="Q")
        )
        assert text == "flattened-ok"


class TestBuildPromptSegments:
    def test_static_path_separates_prefix_from_question(self):
        from core.models import RetrievalContext

        ctx = RetrievalContext(entities=["Customer"], facts=["Contract"])
        segments = build_prompt_segments("how many?", "You are a T-SQL expert.", ctx)
        assert "You are a T-SQL expert." in segments.static_prefix
        assert "how many?" in segments.question
        # The question segment must not re-contain the (large) static prefix.
        assert "Table: Contract" not in segments.question

    def test_retrieval_fallback_has_no_static_prefix(self):
        from core.models import RetrievalContext

        ctx = RetrievalContext(entities=["Customer"], facts=["Contract"])
        with cfg.override_settings(prompt_retrieval_token_budget=1):
            segments = build_prompt_segments("how many?", "You are a T-SQL expert.", ctx)
        assert segments.static_prefix == ""
        assert "how many?" in segments.question


class TestFromSettings:
    def test_openai_provider_builds_single_default_chain(self):
        with cfg.override_settings(llm_provider="openai", openai_model="m", openai_api_key="k"):
            router = LLMRouter.from_settings()
        chain = router._chain_for(TaskType.SQL_GENERATION)
        assert len(chain) == 1
        assert chain[0].name == "openai:m"

    def test_mock_provider_builds_mock_chain(self):
        with cfg.override_settings(llm_provider="mock"):
            router = LLMRouter.from_settings()
        chain = router._chain_for(TaskType.SQL_GENERATION)
        assert len(chain) == 1
        assert chain[0].name == "mock:stub"

    def test_unsupported_provider_raises(self):
        with cfg.override_settings(llm_provider="carrier-pigeon"):
            with pytest.raises(ValueError, match="Unsupported LLM_PROVIDER"):
                LLMRouter.from_settings()

    def test_task_budget_applied_to_every_task_when_set(self):
        with cfg.override_settings(llm_provider="mock", llm_task_budget_seconds=5.0):
            router = LLMRouter.from_settings()
        assert router._budgets[TaskType.SQL_GENERATION] == 5.0
        assert router._budgets[TaskType.INTERPRETATION] == 5.0
        assert router._budgets[TaskType.ASSUMPTION_EXTRACTION] == 5.0

    def test_no_budget_by_default(self):
        with cfg.override_settings(llm_provider="mock", llm_task_budget_seconds=None):
            router = LLMRouter.from_settings()
        assert router._budgets == {}


class TestFromSettingsMultiEndpoint:
    """Exit criterion 7: two endpoints routed per task with a working
    fallback, ``fallback_used`` and ``endpoint`` recorded."""

    _ENDPOINTS = (
        '[{"name": "local", "base_url": "http://localhost:8000/v1", "model": "gpt-oss-20b"}, '
        '{"name": "hosted", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", '
        '"api_key": "sk-x", "trusted": true}]'
    )
    _ROUTES = '{"sql_generation": ["local", "hosted"], "interpretation": ["hosted"]}'

    def test_builds_a_two_entry_chain_for_the_routed_task(self):
        with cfg.override_settings(
            llm_provider="openai", llm_endpoints_json=self._ENDPOINTS, llm_routes_json=self._ROUTES,
        ):
            router = LLMRouter.from_settings()
        chain = router._chain_for(TaskType.SQL_GENERATION)
        assert [b.name for b in chain] == ["openai:gpt-oss-20b", "openai:gpt-4o-mini"]
        assert chain[0].endpoint == "http://localhost:8000/v1"
        assert chain[1].endpoint == "https://api.openai.com/v1"

    def test_unrouted_task_still_defaults_to_the_single_default_endpoint(self):
        with cfg.override_settings(
            llm_provider="openai", llm_endpoints_json=self._ENDPOINTS, llm_routes_json=self._ROUTES,
            openai_model="d", openai_api_key="k",
        ):
            router = LLMRouter.from_settings()
        chain = router._chain_for(TaskType.ASSUMPTION_EXTRACTION)
        assert len(chain) == 1
        assert chain[0].name == "openai:d"

    def test_fallback_fires_across_the_two_configured_endpoints(self):
        from unittest.mock import patch

        with cfg.override_settings(
            llm_provider="openai", llm_endpoints_json=self._ENDPOINTS, llm_routes_json=self._ROUTES,
            llm_allow_remote=True,
        ):
            router = LLMRouter.from_settings()

        body = {"choices": [{"message": {"content": "answered-by-hosted"}}]}
        response = type("Resp", (), {
            "status_code": 200,
            "json": lambda self: body,
            "raise_for_status": lambda self: None,
        })()

        # OpenAIBackend retries transport failures internally (3 attempts by
        # default) before the router's own fallback ever gets a turn, so the
        # "local" endpoint must exhaust all of its own retries before
        # "hosted" is tried.
        failures = [requests.exceptions.ConnectionError("down")] * 3
        with patch("requests.post", side_effect=[*failures, response]):
            with patch("time.sleep"):
                result = router.generate_for_task(TaskType.SQL_GENERATION, PromptSegments(question="q"))

        assert result.text == "answered-by-hosted"
        assert result.fallback_used is True
        assert result.meta["endpoint"] == "https://api.openai.com/v1"
        assert result.meta["trusted"] is True


class TestAuditRemoteUse:
    def test_remote_call_writes_an_audit_record(self, tmp_path):
        import observability.audit as audit_mod
        from unittest.mock import patch

        audit_file = str(tmp_path / "audit_log.jsonl")
        remote = OpenAIBackend(model="gpt-4o-mini", api_key="sk-x")
        router = LLMRouter(default_chain=[remote])
        body = {"choices": [{"message": {"content": "ok"}}]}
        response = type("Resp", (), {
            "status_code": 200,
            "json": lambda self: body,
            "raise_for_status": lambda self: None,
        })()

        with patch.object(audit_mod, "_AUDIT_LOG_FILE", audit_file):
            with cfg.override_settings(llm_allow_remote=True):
                with patch("requests.post", return_value=response):
                    router.generate_for_task(TaskType.INTERPRETATION, PromptSegments(question="q"))

        with open(audit_file, encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        assert len(lines) == 1
        import json

        record = json.loads(lines[0])
        assert record["llm"]["remote"] is True
        assert record["llm"]["provider"] == "openai:gpt-4o-mini"
        assert record["llm"]["task"] == "interpretation"
