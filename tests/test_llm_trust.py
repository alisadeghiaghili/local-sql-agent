# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for llm/trust.py — default per-endpoint trust resolution."""

from __future__ import annotations

import pytest

from llm.trust import default_trust_for_url


class TestDefaultTrustForUrl:
    @pytest.mark.parametrize("url", [
        "http://localhost:8000/v1",
        "http://localhost:11434/v1",
        "https://localhost/v1",
    ])
    def test_localhost_is_trusted(self, url):
        assert default_trust_for_url(url) is True

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
    ])
    def test_loopback_ip_is_trusted(self, url):
        assert default_trust_for_url(url) is True

    @pytest.mark.parametrize("url", [
        "http://192.168.1.50:8000/v1",
        "http://10.0.0.5:8000/v1",
        "http://172.16.0.1:8000/v1",
    ])
    def test_private_ip_is_trusted(self, url):
        assert default_trust_for_url(url) is True

    def test_dot_local_hostname_is_trusted(self):
        assert default_trust_for_url("http://gpu-box.local:8000/v1") is True

    @pytest.mark.parametrize("url", [
        "https://api.openai.com/v1",
        "https://some-vendor.example.com/v1",
        "http://8.8.8.8/v1",
    ])
    def test_public_host_is_untrusted(self, url):
        assert default_trust_for_url(url) is False

    def test_empty_url_is_untrusted(self):
        assert default_trust_for_url("") is False

    def test_malformed_url_is_untrusted(self):
        assert default_trust_for_url("not a url at all") is False
