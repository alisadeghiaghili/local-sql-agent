# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""
llm/wizard_llm.py — LLM abstraction layer for the interactive setup wizard.

Supports two providers:

* ``openai``    — the OpenAI-compatible transport (a local ``gpt-oss``/
                   vLLM/llama.cpp server, LM Studio, or OpenAI's own
                   hosted API — selected entirely by ``base_url``)
* ``mock``      — returns empty stubs, no network needed (CI / tests)

All calls enforce a 30-second timeout.  On JSON parse failure the call is
retried ONCE with an explicit repair prompt before raising ValueError.

Consolidation note
-------------------
This module used to support four providers (``ollama`` / ``openai`` /
``anthropic`` / ``mock``), each built on :mod:`llm.providers` /
``llm.ollama_backend`` — the single place that transport logic lived,
rather than a second, wizard-only copy of it. The Ollama-specific and
Anthropic transports are gone now (see ``llm/providers.py``'s module
docstring): OpenAI-compatible is this project's only protocol, so the
provider surface shrinks to match. What stays here, because it is
genuinely wizard-specific and not something the production engine needs,
is the JSON-parse-then-repair-and-retry-once loop in
:meth:`WizardLLM.generate` — the wizard talks to the model in a very
different shape (one-shot alias/rule extraction) than the engine's
SQL-generation self-correction loop.

Typical usage::

    from llm.wizard_llm import WizardLLM

    llm = WizardLLM(provider="openai", model="gpt-oss-20b")
    result = llm.generate("Return JSON with key x equal to 1", expect_json=True)
    # {"x": 1}

    # Override endpoint for a local server:
    llm = WizardLLM(provider="openai", model="local-model",
                    base_url="http://localhost:8000/v1")
"""

from __future__ import annotations

import json
import logging
import os
from typing import Union

from llm.base import LLMBackend
from llm.providers import MockBackend, OpenAIBackend, parse_json_response

logger = logging.getLogger(__name__)

#: Stub JSON returned by the ``mock`` provider — no network needed (CI / tests).
_MOCK_STUB = json.dumps(
    {"aliases": [], "description": "", "rules": {"rule_text": ""}, "examples": []}
)


def build_backend(model: str, base_url: str | None = None) -> LLMBackend:
    """Construct the SQL-generation backend used by ``app.py``'s REPL.

    A thin, non-router wrapper: unlike ``api/runner.py`` and
    ``session/engine.py`` (which route every call through
    :class:`~llm.router.LLMRouter` for fallback chains and remote-provider
    governance), the REPL has always talked to one backend directly — see
    ``generate_sql`` below. ``base_url``/``api_key`` default to
    :mod:`config`'s plain ``OPENAI_*`` settings when not given.

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(openai_api_key="k"):
    ...     backend = build_backend(model="m")
    >>> backend.name
    'openai:m'
    """
    import config as cfg

    return OpenAIBackend(
        model=model,
        api_key=cfg.settings.openai_api_key,
        base_url=base_url or cfg.settings.openai_base_url,
    )


def generate_sql(question: str, system_prompt: str) -> str:
    """Generate SQL for *question* using the configured OpenAI-compatible backend.

    This function only calls the LLM — it does **not** execute the
    generated SQL against any database.

    The returned SQL has already been passed through
    :func:`~security.sql_guard.ensure_top` (capped at
    ``cfg.settings.default_top_n`` if the model didn't include its own
    row-limit clause) — callers such as ``app.py``'s REPL, which execute
    this SQL directly rather than through :class:`~llm.sql_agent.SQLAgent`,
    would otherwise get no server-side row cap at all.

    Raises
    ------
    ValueError("OUT_OF_SCOPE")
        Passed through from the model sentinel.
    RuntimeError
        When the endpoint is unreachable after all retries.
    """
    import config as cfg
    from retrieval.context_retriever import ContextRetriever
    from prompt_engine.builder import PromptBuilder
    from security.sql_guard import clean_sql, ensure_top, validate_sql

    context = ContextRetriever.retrieve(question)
    prompt = PromptBuilder.build(
        question=question,
        system_prompt=system_prompt,
        context=context,
    )
    backend = build_backend(model=cfg.settings.openai_model)
    raw = backend.generate(prompt)
    sql = clean_sql(raw)
    validate_sql(sql)
    sql = ensure_top(sql, cfg.settings.default_top_n)
    return sql


# ---------------------------------------------------------------------------
# Public WizardLLM class
# ---------------------------------------------------------------------------

class WizardLLM:
    """Unified LLM interface supporting openai and mock.

    A thin wrapper around a single :class:`~llm.base.LLMBackend` instance
    (built from :mod:`llm.providers` — see the module docstring) that adds
    the wizard's own JSON-parse-and-retry contract on top of
    ``backend.generate()``.

    Parameters
    ----------
    provider:
        One of ``"openai"``, ``"mock"``.
    model:
        Model identifier, e.g. ``"gpt-oss-20b"``.
    base_url:
        Optional override for the provider's default endpoint. Useful for
        LM Studio, vLLM, or a custom server.

    Raises
    ------
    ValueError
        If *provider* is not one of the two supported values, or if a
        required API key environment variable is missing.
    """

    _SUPPORTED = frozenset({"openai", "mock"})

    def __init__(
        self,
        provider: str,
        model: str,
        base_url: str | None = None,
    ) -> None:
        provider = provider.strip().lower()
        if provider not in self._SUPPORTED:
            raise ValueError(
                f"Unsupported provider {provider!r}. "
                f"Choose one of: {', '.join(sorted(self._SUPPORTED))}."
            )

        self.provider = provider
        self.model = model
        self._backend: LLMBackend = self._build_backend(provider, model, base_url)

    @staticmethod
    def _build_backend(provider: str, model: str, base_url: str | None) -> LLMBackend:
        """Construct the underlying :class:`~llm.base.LLMBackend` for *provider*."""
        if provider == "openai":
            key = os.getenv("OPENAI_API_KEY", "")
            if not key:
                raise ValueError(
                    "OPENAI_API_KEY environment variable is required "
                    "for provider='openai'."
                )
            url = base_url or os.getenv("WIZARD_LLM_BASE_URL", "https://api.openai.com/v1")
            return OpenAIBackend(model=model, api_key=key, base_url=url)

        return MockBackend(response=_MOCK_STUB)  # provider == "mock"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        """Return True if the configured backend is reachable, False otherwise."""
        return self._backend.test_connection()

    def generate(
        self,
        prompt: str,
        expect_json: bool = True,
    ) -> Union[str, dict, list]:
        """Send *prompt* to the LLM and return the response.

        Parameters
        ----------
        prompt:
            The full prompt string.
        expect_json:
            When True (default), parse the response as JSON and return a
            ``dict`` or ``list``.  On the first parse failure the call is
            retried ONCE with a repair prompt prepended.  Raises
            ``ValueError`` if the retry also fails.
            When False, the raw response string is returned as-is.

        Returns
        -------
        str | dict | list
            Raw string when ``expect_json=False``, otherwise parsed JSON.

        Raises
        ------
        requests.Timeout
            If the backend does not respond within 30 seconds.
        requests.HTTPError
            On non-2xx HTTP responses.
        ValueError
            If ``expect_json=True`` and valid JSON cannot be extracted
            after the retry.
        """
        raw = self._backend.generate(prompt)

        if not expect_json:
            return raw

        # --- first extraction attempt ---
        try:
            return parse_json_response(raw)
        except ValueError:
            logger.debug(
                "[WizardLLM] First JSON parse failed for provider=%s; retrying.",
                self.provider,
            )

        # --- single retry with explicit repair instruction ---
        repair_prompt = (
            "The text below must be a valid JSON object or array but failed to parse.\n"
            "Return ONLY the corrected JSON — no explanation, no markdown, no code fences.\n\n"
            + raw
        )
        raw2 = self._backend.generate(repair_prompt)
        try:
            return parse_json_response(raw2)
        except ValueError as exc:
            raise ValueError(
                f"[WizardLLM] Could not extract valid JSON after retry "
                f"(provider={self.provider!r}, model={self.model!r}).\n"
                f"Last raw response (first 500 chars): {raw2[:500]!r}"
            ) from exc

    # ------------------------------------------------------------------
    # Convenience factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls) -> "WizardLLM":
        """Construct a WizardLLM from the project's config / environment.

        Reads (in order of precedence):
        * ``WIZARD_LLM_PROVIDER``  — default ``openai``
        * ``WIZARD_LLM_MODEL``     — default ``gpt-4o-mini`` (or ``OPENAI_MODEL``)
        * ``WIZARD_LLM_BASE_URL``  — optional endpoint override
        """
        try:
            import config as cfg  # noqa: PLC0415
            default_model = cfg.settings.openai_model
        except Exception:  # noqa: BLE001
            default_model = "gpt-4o-mini"

        return cls(
            provider=os.getenv("WIZARD_LLM_PROVIDER", "openai"),
            model=os.getenv("WIZARD_LLM_MODEL", default_model),
            base_url=os.getenv("WIZARD_LLM_BASE_URL") or None,
        )
