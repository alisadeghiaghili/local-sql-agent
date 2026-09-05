# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A local model endpoint must work with no ``OPENAI_API_KEY`` at all.

``docs/fa/getting-started.md`` tells a reader running a local model to
leave ``OPENAI_API_KEY`` empty, and says that in such a deployment the
analyst key is the only real token in the whole system. That is a claim
about how this code behaves, so it is pinned here rather than left to
drift.

The guide previously listed ``OPENAI_API_KEY`` among four values to fill
in, which reads as required. It is not, and saying so sent people
looking for a credential their LM Studio / Ollama / llama.cpp server
never asked for -- and invited the genuinely dangerous shortcut of
reusing the analyst key for it.

Three separate things have to hold for that advice to be true, and each
lives in a different module:

1. ``Settings.validate()`` must not demand it.
2. The provider must send **no** ``Authorization`` header when it is
   empty -- not an empty ``Bearer``.
3. The health probe must do the same, or the UI reports a working
   endpoint as down (which it did; see 4.1.2).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import config as cfg


class TestLocalModelNeedsNoLlmKey:
    def test_validate_does_not_require_the_llm_key(self):
        """Only OPENAI_MODEL and DB_CONNECTION_URL are mandatory."""
        with cfg.override_settings(
            openai_api_key="",
            openai_model="some-local-model",
            db_connection_url="sqlite:///:memory:",
            sql_dialect="sqlite",
        ):
            cfg.settings.validate()  # must not raise

    def test_the_provider_omits_the_header_entirely(self):
        """Not ``Bearer `` with nothing after it -- no header at all.

        A malformed header is not the same as an absent one: plenty of
        servers answer the first with 401 and the second with 200.
        """
        from llm.providers import OpenAIBackend

        backend = OpenAIBackend(model="m", api_key="", base_url="http://localhost:9/v1")
        assert "Authorization" not in backend._headers

    def test_the_health_probe_omits_it_too(self):
        """The probe and the engine must agree, or the light lies.

        They did not, and the symptom was a red LLM indicator on a
        deployment whose CLI was answering questions through that exact
        endpoint.
        """
        from api.health import _ping_openai

        resp = MagicMock()
        resp.status_code = 200
        with cfg.override_settings(openai_api_key=""):
            with patch("requests.get", return_value=resp) as get:
                ok, _ = _ping_openai()

        assert ok is True
        assert "Authorization" not in get.call_args.kwargs["headers"]

    def test_the_two_credentials_are_read_from_different_settings(self):
        """The LLM key and the analyst keys never share a source.

        They point in opposite directions -- ``openai_api_key`` is what
        this server presents *outward* to the model endpoint,
        ``api_keys_json`` is what it checks on the way *in* from a
        browser -- so nothing should ever let one stand in for the other.
        """
        with cfg.override_settings(
            openai_api_key="outward-to-the-model",
            api_keys_json="",
        ):
            assert cfg.settings.openai_api_key == "outward-to-the-model"
            assert cfg.settings.api_keys_json == ""

        from security.auth import load_api_keys

        # An LLM key configured is not, and must never become, a principal.
        with cfg.override_settings(openai_api_key="outward-to-the-model", api_keys_json=""):
            assert load_api_keys() == {}
