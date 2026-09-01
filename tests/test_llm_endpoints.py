# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for llm/endpoints.py — named endpoints + per-task routes from config."""

from __future__ import annotations

import pytest

import config as cfg
from llm.endpoints import DEFAULT_ENDPOINT_NAME, EndpointConfig, build_backend, load_endpoints, load_routes
from llm.providers import OpenAIBackend


class TestLoadEndpoints:
    def test_trivial_case_is_just_default(self):
        with cfg.override_settings(llm_endpoints_json=""):
            endpoints = load_endpoints()
        assert list(endpoints) == [DEFAULT_ENDPOINT_NAME]
        assert endpoints["default"].base_url == cfg.settings.openai_base_url

    def test_additional_endpoint_is_added_alongside_default(self):
        raw = '[{"name": "local", "base_url": "http://localhost:8000/v1", "model": "m"}]'
        with cfg.override_settings(llm_endpoints_json=raw):
            endpoints = load_endpoints()
        assert set(endpoints) == {"default", "local"}
        assert endpoints["local"].base_url == "http://localhost:8000/v1"
        assert endpoints["local"].api_key == ""
        assert endpoints["local"].trusted is None

    def test_endpoint_named_default_overrides_the_plain_settings_default(self):
        raw = '[{"name": "default", "base_url": "http://localhost:9000/v1", "model": "m2"}]'
        with cfg.override_settings(llm_endpoints_json=raw):
            endpoints = load_endpoints()
        assert list(endpoints) == ["default"]
        assert endpoints["default"].base_url == "http://localhost:9000/v1"

    def test_optional_fields_are_read(self):
        raw = '[{"name": "e", "base_url": "http://x/v1", "model": "m", "api_key": "k", "trusted": true}]'
        with cfg.override_settings(llm_endpoints_json=raw):
            endpoints = load_endpoints()
        assert endpoints["e"].api_key == "k"
        assert endpoints["e"].trusted is True

    def test_malformed_json_raises(self):
        with cfg.override_settings(llm_endpoints_json="{not valid json"):
            with pytest.raises(ValueError, match="not valid JSON"):
                load_endpoints()

    def test_non_array_json_raises(self):
        with cfg.override_settings(llm_endpoints_json='{"name": "x"}'):
            with pytest.raises(ValueError, match="JSON array"):
                load_endpoints()

    def test_missing_required_key_raises(self):
        raw = '[{"name": "e", "base_url": "http://x/v1"}]'  # no "model"
        with cfg.override_settings(llm_endpoints_json=raw):
            with pytest.raises(ValueError, match="missing a required key"):
                load_endpoints()


class TestLoadRoutes:
    def test_trivial_case_routes_every_task_to_default(self):
        with cfg.override_settings(llm_routes_json=""):
            routes = load_routes()
        assert routes["sql_generation"] == ["default"]
        assert routes["interpretation"] == ["default"]
        assert routes["assumption_extraction"] == ["default"]

    def test_configured_task_overrides_only_itself(self):
        raw = '{"sql_generation": ["local", "hosted"]}'
        with cfg.override_settings(llm_routes_json=raw):
            routes = load_routes()
        assert routes["sql_generation"] == ["local", "hosted"]
        assert routes["interpretation"] == ["default"]

    def test_malformed_json_raises(self):
        with cfg.override_settings(llm_routes_json="[not json"):
            with pytest.raises(ValueError, match="not valid JSON"):
                load_routes()

    def test_non_object_json_raises(self):
        with cfg.override_settings(llm_routes_json="[1, 2]"):
            with pytest.raises(ValueError, match="JSON object"):
                load_routes()


class TestBuildBackend:
    def test_builds_an_openai_backend(self):
        endpoint = EndpointConfig(name="e", base_url="http://localhost:8000/v1", model="m", api_key="k")
        backend = build_backend(endpoint)
        assert isinstance(backend, OpenAIBackend)
        assert backend.name == "openai:m"
        assert backend.endpoint == "http://localhost:8000/v1"
        assert backend.trusted is True  # loopback default

    def test_explicit_trust_override_is_honoured(self):
        endpoint = EndpointConfig(
            name="e", base_url="http://localhost:8000/v1", model="m", trusted=False,
        )
        backend = build_backend(endpoint)
        assert backend.trusted is False
