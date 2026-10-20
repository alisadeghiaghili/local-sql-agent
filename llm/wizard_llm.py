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
retried ONCE with an explicit repair prompt before raising ValueError.  The
alias call (:meth:`WizardLLM.generate_aliases`) is the exception: a model that
loops on the same phrases until ``max_tokens`` stops it leaves JSON that cannot
be repaired, so that call keeps the complete strings written before the cut,
caps its own completion length, and when nothing is usable asks again with the
original prompt and a stricter instruction rather than sending the garbage back.

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
from dataclasses import dataclass, field
from typing import Any, Union

from core.wizard_drafts import ALIAS_LIMIT, tidy_aliases
from llm.base import LLMBackend
from llm.providers import MockBackend, OpenAIBackend, parse_json_response, salvage_string_list

logger = logging.getLogger(__name__)

#: Completion ceiling, in tokens, for one alias call (``max_tokens``), in place
#: of ``LLM_NUM_PREDICT`` -- which is sized for a SQL statement and, when a
#: model loops, is how long the loop runs. The answer is ``{"aliases": [up to
#: 8 short phrases], "description": "one sentence"}``. Worst case, written out
#: in Persian (8 phrases of 3-4 words, a 130-character sentence): 386
#: characters, 671 UTF-8 bytes; the same in English: 232 characters. At 0.5-0.7
#: tokens per Persian character that is about 190-270 tokens, so 320 fits
#: with room to spare. These are estimates from character counts: no
#: tokenizer for the deployed model was available to count tokens. A cap that
#: cuts an answer anyway costs little, because the complete aliases before the
#: cut are kept (:func:`~llm.providers.salvage_string_list`).
ALIAS_MAX_TOKENS = 320

#: The default of ``LLM_NUM_PREDICT``. A larger value is how the setup guide
#: (section 2.9) tells an operator to make room for a reasoning model, whose
#: thinking counts against ``max_tokens``; see :func:`alias_token_cap`.
_SQL_SIZED_BUDGET = 512

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

    from llm.source_routing import choose_source

    context = ContextRetriever.retrieve(question)
    prompt = PromptBuilder.build(
        question=question,
        system_prompt=system_prompt,
        context=context,
        # None (nothing changes) unless several data sources are configured.
        source=choose_source(question, context),
    )
    backend = build_backend(model=cfg.settings.openai_model)
    raw = backend.generate(prompt)
    sql = clean_sql(raw)
    validate_sql(sql)
    sql = ensure_top(sql, cfg.settings.default_top_n)
    return sql


# ---------------------------------------------------------------------------
# The alias call
# ---------------------------------------------------------------------------

def alias_token_cap(configured: int, extra_body: str = "") -> int | None:
    """The ``max_tokens`` for one alias call, or ``None`` to leave ``LLM_NUM_PREDICT`` alone.

    A short answer should not be allowed the whole SQL budget: when the model
    loops, the loop runs until that budget is spent. But a reasoning model
    spends the same ``max_tokens`` on thinking before it writes a character
    of the answer, and a small cap would starve it. The setup guide's two
    ways to make room for such a model are to raise ``LLM_NUM_PREDICT`` and to
    set a reasoning switch in ``LLM_EXTRA_BODY``; either is taken as a sign
    that the operator has made room, and the cap then stays out of the way.
    Otherwise the cap is :data:`ALIAS_MAX_TOKENS`, and never more than the
    configured value.

    Parameters
    ----------
    configured:
        ``LLM_NUM_PREDICT``.
    extra_body:
        ``LLM_EXTRA_BODY``, as the text of the setting.

    Returns
    -------
    int | None
        The ceiling for this call; ``None`` when none should be sent.

    Raises
    ------
    Nothing. This function never raises.

    Examples
    --------
    >>> alias_token_cap(512)
    320
    >>> alias_token_cap(256)
    256
    >>> alias_token_cap(2048) is None  # raised for a reasoning model: no cap
    True
    >>> alias_token_cap(512, '{"think": false}') is None  # a reasoning switch is set: no cap
    True
    """
    if configured > _SQL_SIZED_BUDGET or extra_body.strip():
        return None
    return min(configured, ALIAS_MAX_TOKENS)


def _strict_instruction(limit: int) -> str:
    """What is added to the original alias prompt when the first answer was unusable."""
    return (
        "\n\nYour previous answer could not be used: it repeated itself and never finished. "
        f"Answer again with at most {limit} aliases, every one different from the others. "
        "Do not repeat a phrase and do not reorder its words. "
        "Close the JSON right after the list and write nothing after it."
    )


class UnusableAnswerError(ValueError):
    """The model gave no usable alias list, even after one stricter retry.

    ``str(error)`` says what each answer was, with a short excerpt of the
    last one; :attr:`reason` is the one-line version for a progress line.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class AliasAnswer:
    """What the alias call produced, and how it got there.

    Attributes
    ----------
    aliases:
        The aliases to use: text, no repeats, at most the limit
        (:func:`~core.wizard_drafts.tidy_aliases`).
    answered:
        The items of the list as the model wrote them, before that cleaning.
    description:
        The model's one-sentence description; empty when the answer was cut
        off before it.
    salvaged:
        ``True`` when the answer was cut off or malformed and the list was
        recovered from its complete strings.
    retried:
        ``True`` when the first answer gave nothing and the second one did.
    cut_off:
        ``True`` when the server stopped the answer at the token limit.
    repeated:
        ``True`` when the salvaged list repeats an item (after folding).
    """

    aliases: list[str]
    answered: list[Any] = field(default_factory=list)
    description: str = ""
    salvaged: bool = False
    retried: bool = False
    cut_off: bool = False
    repeated: bool = False

    @classmethod
    def from_parsed(cls, parsed: Any, limit: int = ALIAS_LIMIT) -> "AliasAnswer":
        """The answer for a reply that parsed as JSON: ``{"aliases": [...], "description": "..."}``.

        Parameters
        ----------
        parsed:
            What :func:`~llm.providers.parse_json_response` returned. A bare
            list is read as the aliases themselves.
        limit:
            The most aliases to keep.

        Returns
        -------
        AliasAnswer
            With nothing salvaged; an answer of another shape has no aliases.

        Raises
        ------
        Nothing. This function never raises.

        Examples
        --------
        >>> AliasAnswer.from_parsed({"aliases": ["a", "A", "b"], "description": "d"}).aliases
        ['a', 'b']
        >>> AliasAnswer.from_parsed(["x"]).aliases, AliasAnswer.from_parsed({"aliases": "x"}).aliases
        (['x'], [])
        """
        listed = parsed.get("aliases") if isinstance(parsed, dict) else parsed
        answered = list(listed) if isinstance(listed, list) else []
        description = parsed.get("description") if isinstance(parsed, dict) else ""
        return cls(
            aliases=tidy_aliases(answered, limit),
            answered=answered,
            description=description if isinstance(description, str) else "",
        )


def _why_unusable(raw: str, meta: dict[str, Any]) -> str:
    """One short phrase for an answer that gave no alias."""
    if meta.get("finish_reason") == "length":
        what = "cut off at the token limit"
    elif not raw.strip():
        return "empty"
    else:
        what = "not valid JSON"
    return f"{what}, {len(raw)} characters"


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

    def generate_aliases(self, prompt: str, *, limit: int = ALIAS_LIMIT) -> AliasAnswer:
        """Ask for the aliases of one table; survive an answer that loops and is cut off.

        The reply should be ``{"aliases": [...], "description": "..."}``. A
        model that repeats the same phrases until ``max_tokens`` stops it
        leaves JSON that never closes, and :meth:`generate` would send that
        text back to be "corrected", which loops again. Here:

        1. The call carries its own, small ``max_tokens`` (:func:`alias_token_cap`).
        2. A reply that does not parse is searched for the complete strings of
           its ``aliases`` list (:func:`~llm.providers.salvage_string_list`);
           those are kept, deduplicated and capped at *limit*.
        3. Only when that gives nothing, the model is asked ONCE more with the
           original prompt and a stricter instruction. The text of the first
           answer is never sent back.

        Parameters
        ----------
        prompt:
            The alias prompt.
        limit:
            The most aliases to keep.

        Returns
        -------
        AliasAnswer
            The aliases and how they were obtained.

        Raises
        ------
        UnusableAnswerError
            If neither answer held a complete alias (a ``ValueError``).
        requests.Timeout
            If the backend does not respond in time.
        requests.HTTPError
            On a non-2xx response.
        RuntimeError
            If the endpoint cannot be reached.

        Examples
        --------
        >>> llm = WizardLLM(provider="mock", model="mock")
        >>> llm.generate_aliases("anything").aliases
        []
        """
        import config as cfg  # noqa: PLC0415

        cap = alias_token_cap(cfg.settings.llm_num_predict, cfg.settings.llm_extra_body_json or "")
        raw, meta = self._ask(prompt, cap)
        answer = self._read_aliases(raw, meta, limit)
        if answer is not None:
            return answer
        logger.debug(
            "[WizardLLM] No usable alias list (%s); asking once more with a stricter prompt.",
            _why_unusable(raw, meta),
        )

        raw2, meta2 = self._ask(prompt + _strict_instruction(limit), cap)
        answer = self._read_aliases(raw2, meta2, limit)
        if answer is not None:
            answer.retried = True
            return answer

        first, second = _why_unusable(raw, meta), _why_unusable(raw2, meta2)
        hint = ""
        if meta.get("reasoning_detected") or (meta.get("reasoning_tokens") or 0) > 0:
            hint = " The model spent its tokens on reasoning; see LLM_EXTRA_BODY and LLM_NUM_PREDICT."
        raise UnusableAnswerError(
            f"[WizardLLM] No usable alias list (provider={self.provider!r}, model={self.model!r}): "
            f"first answer {first}; second answer, after a stricter prompt, {second}.{hint} "
            f"Start of the last answer: {raw2[:80]!r}",
            reason=f"no usable answer: {first}; again {second}",
        )

    def _ask(self, prompt: str, max_tokens: int | None) -> tuple[str, dict[str, Any]]:
        """One backend call, with a per-call ``max_tokens`` only when there is one."""
        if max_tokens is None:
            return self._backend.generate_with_meta(prompt)
        return self._backend.generate_with_meta(prompt, max_tokens=max_tokens)

    @staticmethod
    def _read_aliases(raw: str, meta: dict[str, Any], limit: int) -> AliasAnswer | None:
        """The alias list in *raw*: parsed whole, else salvaged; ``None`` when there is none.

        A reply that parses is taken as it is (an empty list is an answer). A
        reply that does not is searched for the complete strings of its list.
        """
        cut_off = meta.get("finish_reason") == "length"
        try:
            answer = AliasAnswer.from_parsed(parse_json_response(raw), limit)
            answer.cut_off = cut_off
            return answer
        except ValueError:
            pass
        items = salvage_string_list(raw, "aliases")
        if not items:
            return None
        return AliasAnswer(
            aliases=tidy_aliases(items, limit),
            answered=items,
            salvaged=True,
            cut_off=cut_off,
            repeated=len(tidy_aliases(items, len(items))) < len(items),
        )

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
