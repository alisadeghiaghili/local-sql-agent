"""Unit tests for observability/llm_status.py."""

from __future__ import annotations

import pytest

from observability.llm_status import build_llm_status


def _raw(**overrides) -> dict:
    defaults = dict(
        usage={"prompt_tokens": 4612, "completion_tokens": 148},
        system_fingerprint="fp_test",
    )
    defaults.update(overrides)
    return defaults


class TestHappyPath:
    def test_maps_openai_usage_fields_to_contract_names(self):
        status = build_llm_status(
            _raw(), model="gpt-oss-20b", endpoint="http://localhost:8000/v1", trusted=True,
            endpoint_status=200, finish_reason="stop", structured_output=True,
            static_prefix_tokens=4600, temperature=0.0, seed=7, total_ms=2310,
        )
        assert status["prompt_tokens"] == 4612
        assert status["completion_tokens"] == 148
        assert status["prefill_ms"] is None
        assert status["decode_ms"] is None
        assert status["total_ms"] == 2310
        assert status["backend"] == "openai"
        assert status["model"] == "gpt-oss-20b"
        assert status["endpoint"] == "http://localhost:8000/v1"
        assert status["trusted"] is True
        assert status["endpoint_status"] == 200
        assert status["structured_output"] is True
        assert status["temperature"] == 0.0
        assert status["seed"] == 7
        assert status["seed_honored"] is True

    def test_tokens_per_second_is_none_without_decode_ms(self):
        status = build_llm_status(
            _raw(), model="m", endpoint_status=200, finish_reason="stop",
        )
        assert status["tokens_per_second"] is None

    def test_all_contract_keys_present(self):
        status = build_llm_status(_raw(), model="m", endpoint_status=200, finish_reason="stop")
        assert set(status.keys()) == {
            "backend", "model", "endpoint", "trusted", "endpoint_status", "attempts",
            "finish_reason", "structured_output", "prompt_tokens", "completion_tokens",
            "prefill_ms", "decode_ms", "total_ms", "tokens_per_second",
            "prefix_cache_hit", "temperature", "seed", "seed_honored", "corrections",
            "provider", "fallback_used",
        }

    def test_provider_defaults_to_backend(self):
        status = build_llm_status(_raw(), model="m", backend="openai:m", finish_reason="stop")
        assert status["provider"] == "openai:m"
        assert status["fallback_used"] is False

    def test_provider_and_fallback_used_are_threaded_through(self):
        status = build_llm_status(
            _raw(), model="m", finish_reason="stop",
            provider="openai:gpt-4o-mini", fallback_used=True,
        )
        assert status["provider"] == "openai:gpt-4o-mini"
        assert status["fallback_used"] is True

    def test_default_attempts_is_one(self):
        status = build_llm_status(_raw(), model="m", endpoint_status=200, finish_reason="stop")
        assert status["attempts"] == 1

    def test_retried_attempts_recorded(self):
        status = build_llm_status(
            _raw(), model="m", endpoint_status=200, finish_reason="stop", attempts=3,
        )
        assert status["attempts"] == 3

    def test_corrections_passed_through(self):
        status = build_llm_status(
            _raw(), model="m", endpoint_status=200, finish_reason="stop", corrections=2,
        )
        assert status["corrections"] == 2

    def test_endpoint_defaults_to_none(self):
        status = build_llm_status(_raw(), model="m", finish_reason="stop")
        assert status["endpoint"] is None

    def test_trusted_defaults_to_false(self):
        """An unspecified endpoint must not read as trusted -- fail closed."""
        status = build_llm_status(_raw(), model="m", finish_reason="stop")
        assert status["trusted"] is False

    def test_total_ms_none_when_not_supplied(self):
        status = build_llm_status(_raw(), model="m", finish_reason="stop")
        assert status["total_ms"] is None


class TestSeedHonored:
    def test_true_when_system_fingerprint_present(self):
        status = build_llm_status(
            _raw(system_fingerprint="fp_abc"), model="m", finish_reason="stop",
        )
        assert status["seed_honored"] is True

    def test_none_when_system_fingerprint_absent(self):
        raw = _raw()
        del raw["system_fingerprint"]
        status = build_llm_status(raw, model="m", finish_reason="stop")
        assert status["seed_honored"] is None

    def test_none_on_total_transport_failure(self):
        status = build_llm_status(None, model="m", finish_reason="error")
        assert status["seed_honored"] is None


class TestPrefixCacheHit:
    def test_true_when_prompt_tokens_well_below_half_prefix(self):
        status = build_llm_status(
            _raw(usage={"prompt_tokens": 120, "completion_tokens": 148}), model="m",
            endpoint_status=200, finish_reason="stop", static_prefix_tokens=4600,
        )
        assert status["prefix_cache_hit"] is True

    def test_false_when_prompt_tokens_at_or_above_half_prefix(self):
        status = build_llm_status(
            _raw(usage={"prompt_tokens": 2300, "completion_tokens": 148}), model="m",
            endpoint_status=200, finish_reason="stop", static_prefix_tokens=4600,
        )
        assert status["prefix_cache_hit"] is False

    def test_false_at_exactly_zero_prompt_tokens_even_though_formula_is_vacuously_true(self):
        """The core regression this module exists to prevent: 0 < prefix*0.5
        is arithmetically true for any positive prefix, but a request that
        never reached the model must never read as a cache hit.
        Mirrors the fix already applied client-side in
        web/js/render/llm-status.js (commit d78c70f).
        """
        status = build_llm_status(
            _raw(usage={"prompt_tokens": 0, "completion_tokens": 0}), model="m",
            endpoint_status=0, finish_reason="error", static_prefix_tokens=4600,
        )
        assert status["prefix_cache_hit"] is False

    def test_false_on_total_transport_failure_with_no_raw_body(self):
        status = build_llm_status(
            None, model="m", endpoint_status=0, attempts=3,
            finish_reason="error", static_prefix_tokens=4600,
        )
        assert status["prompt_tokens"] == 0
        assert status["prefix_cache_hit"] is False

    def test_false_when_static_prefix_tokens_unset(self):
        """static_prefix_tokens=0 (unset/unknown) disables the computation
        rather than deriving a meaningless ratio."""
        status = build_llm_status(
            _raw(usage={"prompt_tokens": 10, "completion_tokens": 5}), model="m",
            endpoint_status=200, finish_reason="stop", static_prefix_tokens=0,
        )
        assert status["prefix_cache_hit"] is False

    def test_false_when_static_prefix_tokens_negative(self):
        status = build_llm_status(
            _raw(usage={"prompt_tokens": 10, "completion_tokens": 5}), model="m",
            endpoint_status=200, finish_reason="stop", static_prefix_tokens=-1,
        )
        assert status["prefix_cache_hit"] is False


class TestPartialOrFailedResponse:
    def test_none_raw_still_constructs_a_full_block(self):
        status = build_llm_status(
            None, model="gpt-oss-20b", endpoint_status=0, attempts=3, finish_reason="error",
        )
        assert status["prompt_tokens"] == 0
        assert status["completion_tokens"] == 0
        assert status["prefill_ms"] is None
        assert status["decode_ms"] is None
        assert status["total_ms"] is None
        assert status["tokens_per_second"] is None
        assert status["endpoint_status"] == 0
        assert status["attempts"] == 3
        assert status["finish_reason"] == "error"

    def test_empty_dict_raw_behaves_like_none(self):
        status_none = build_llm_status(None, model="m", endpoint_status=0, finish_reason="error")
        status_empty = build_llm_status({}, model="m", endpoint_status=0, finish_reason="error")
        assert status_none == status_empty

    def test_missing_usage_defaults_to_zero(self):
        status = build_llm_status(
            {}, model="m", endpoint_status=200, finish_reason="stop",
        )
        assert status["prompt_tokens"] == 0
        assert status["completion_tokens"] == 0

    def test_missing_individual_usage_fields_default_to_zero(self):
        status = build_llm_status(
            {"usage": {"prompt_tokens": 100}}, model="m", endpoint_status=200, finish_reason="stop",
        )
        assert status["prompt_tokens"] == 100
        assert status["completion_tokens"] == 0

    def test_non_numeric_field_degrades_to_zero_instead_of_raising(self):
        status = build_llm_status(
            {"usage": {"prompt_tokens": "not-a-number", "completion_tokens": None}},
            model="m", endpoint_status=200, finish_reason="stop",
        )
        assert status["prompt_tokens"] == 0
        assert status["completion_tokens"] == 0


class TestFinishReasonValidation:
    @pytest.mark.parametrize("reason", ["stop", "length", "schema_violation", "error"])
    def test_valid_reasons_accepted(self, reason):
        status = build_llm_status(_raw(), model="m", endpoint_status=200, finish_reason=reason)
        assert status["finish_reason"] == reason

    def test_invalid_reason_raises(self):
        with pytest.raises(ValueError, match="finish_reason"):
            build_llm_status(_raw(), model="m", endpoint_status=200, finish_reason="bogus")


class TestDefaults:
    def test_backend_defaults_to_openai(self):
        status = build_llm_status(_raw(), model="m", endpoint_status=200, finish_reason="stop")
        assert status["backend"] == "openai"

    def test_seed_none_by_default(self):
        status = build_llm_status(_raw(), model="m", endpoint_status=200, finish_reason="stop")
        assert status["seed"] is None

    def test_structured_output_defaults_false(self):
        status = build_llm_status(_raw(), model="m", endpoint_status=200, finish_reason="stop")
        assert status["structured_output"] is False
