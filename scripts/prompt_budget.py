# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Show what each data source's prompt really costs, and what budget to set.

Usage (from the repo root, with the server's environment active)::

    python scripts/prompt_budget.py
    python scripts/prompt_budget.py --no-model
    python scripts/prompt_budget.py --context-length 32768 --headroom 15
    python scripts/prompt_budget.py --json

``PROMPT_RETRIEVAL_TOKEN_BUDGET`` decides, for each data source, whether its
questions use the cacheable static prefix or the retrieval path
(:func:`prompt_engine.static_prefix.should_use_static_prefix`). The gate
compares the budget with :func:`prompt_engine.static_prefix.estimate_tokens`,
which is ``len(text) // 4`` -- a heuristic that undercounts Persian text (the
model's real ``prompt_tokens`` measured about 1.14 times the estimate in
production). Until now an operator had no way to see what a source's prompt
costs in real tokens, or which number to put in ``.env``. This script builds
each source's static prefix exactly as the server does, asks the model how
many tokens it really is, and works out a budget.

What it reports
---------------
1. **Per data source** (or the one implicit source when there is no
   ``datasources.yaml``): the prefix's characters, its table count, the
   heuristic estimate, and the path it takes under the current budget. The
   system prompt is the one the server loads
   (:func:`knowledge.config_loader.load_system_prompt`), and the prefix is
   :func:`prompt_engine.static_prefix.build_static_prefix`'s own output.
2. **Real tokens** (``--no-model`` skips this and every other contact with
   the model endpoint): the prefix is sent as one chat completion with
   ``max_tokens=1`` to the endpoint the router uses for SQL generation (the
   first endpoint of ``LLM_ROUTES``'s ``sql_generation`` chain, else the
   default ``OPENAI_*`` endpoint) and ``usage.prompt_tokens`` is read back.
   The request body is :meth:`llm.providers.OpenAIBackend._build_payload`'s
   own -- model, sampling fields, ``LLM_STOP`` and ``LLM_EXTRA_BODY`` -- with
   only ``max_tokens`` replaced, the headers are the backend's, and the
   data-governance gate of :class:`llm.router.LLMRouter` applies: an endpoint
   that is not trusted is sent nothing unless ``LLM_ALLOW_REMOTE`` is true
   (and then the call is written to the audit trail like a routed one).
   One attempt per source, no retries: a timeout must not be repeated. If the
   endpoint cannot be reached or returns no usage, that source's count is
   reported as ``unavailable`` and the run goes on. The count includes the
   few tokens the chat template wraps around the message. **Side effect:**
   the request prefills the prefix, so the model server's prefix cache is
   warm for that source afterwards.
3. **Context length**: ``--context-length N``, else ``GET {base}/models`` and
   the entry for the configured model (``max_model_len`` as vLLM reports it,
   or ``context_length`` / ``context_window``), else unknown. A server that
   splits its context between parallel slots may accept less per request than
   it reports: pass ``--context-length`` then.

The recommendation
------------------
* **Real/estimate ratio** per source, where the real count is known.
* **Fit check** (only with a known context length), per source::

      needs = prefix tokens + --question-room + LLM_NUM_PREDICT  <=  context

  *Prefix tokens* are the measured count; failing that the estimate times the
  largest ratio measured on another source (the Persian-heaviest one: the
  safe choice); failing that the estimate times ``--assumed-ratio``
  (default 1.15, the production measurement rounded up). The table says
  which. ``--question-room`` (default 2000) is what follows the prefix in a
  prompt (``prompt_engine.templates.SUFFIX_TEMPLATE`` and the question
  history): the fixed suffix text is about 210 tokens; the question is at
  most 1000 characters (``api/models.py``), so about 250 tokens, 290 in
  Persian; the session context repeats the last ``SESSION_PROMPT_TURNS`` (3)
  turns, each a question, its SQL and its column names, about 450 tokens a
  turn at the long end, so about 1350; detected filters and resolved values
  take a few hundred at most. That is 1800 to 2100 for a long question in a
  long conversation, which 2000 covers; most requests use far less. Raise it
  if sessions are long or questions are pasted text.
* **Recommended budget** = the largest estimate among the sources that fit,
  times ``1 + --headroom/100`` (default 10%), rounded up to a multiple of
  ``--round`` (default 500). It is in the estimator's units, not real tokens,
  because that is what the gate compares; Persian's undercount is handled by
  the fit check, not by the budget. The headroom is for growth: a few tables,
  rules or examples added later must not flip a source to retrieval with one
  log line to say so. A source that does not fit is **left out** of the
  calculation and warned about: it should stay on the retrieval path. The
  budget is one number for all sources, so if an excluded source's estimate is
  not above the recommended budget it would take the static path and
  overflow; the warning says so and gives the range that would work, if any.
* The exact ``.env`` line, and each source's path now and with it.

Output
------
A table by default; ``--json`` prints one JSON document on standard output
(progress lines go to standard error in both modes). No API key, and no URL
credentials, query string or fragment, is ever printed; the endpoint is
shown as ``scheme://host:port/path``, as the other operator tools show theirs.

Exit codes
----------
0 -- done; 1 -- the context length is known and some source cannot fit it
even alone (it is left out of the recommendation); 2 -- configuration
errors: the system prompt, ``schema.yaml``, ``datasources.yaml`` or the LLM
settings cannot be loaded, or an option is invalid.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import requests

# Run as `python scripts/prompt_budget.py` from the repo root: Python puts
# only this script's own directory on sys.path, so the repo root is added
# for `import config` and the packages below -- same as
# scripts/assign_datasources.py and scripts/verify_deployment.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.console import use_utf8_console
import config as cfg  # noqa: E402 - loads .env, like every entry point
import database.datasources as datasources  # noqa: E402
from knowledge.config_loader import load_system_prompt, resolve_system_prompt_path  # noqa: E402
from llm.endpoints import (  # noqa: E402
    DEFAULT_ENDPOINT_NAME,
    EndpointConfig,
    build_backend,
    load_endpoints,
    load_routes,
)
from llm.providers import OpenAIBackend  # noqa: E402
from llm.router import LLMRouter, PromptSegments, TaskType, is_trusted_backend  # noqa: E402
from prompt_engine.static_prefix import (  # noqa: E402
    build_static_prefix,
    should_use_static_prefix,
    static_prefix_token_estimate,
)
from schema_data.registry import SchemaRegistry  # noqa: E402

__all__ = [
    "EXIT_DOES_NOT_FIT",
    "EXIT_ERROR",
    "EXIT_OK",
    "ModelClient",
    "ModelInfo",
    "ModelUnavailable",
    "OpenAICompatibleClient",
    "PATH_RETRIEVAL",
    "PATH_STATIC",
    "Recommendation",
    "Report",
    "SourceFit",
    "SourceSize",
    "UnavailableClient",
    "build_default_client",
    "main",
    "measure_prefixes",
    "parse_context_length",
    "paths_under_budget",
    "recommend",
    "render_json",
    "render_text",
    "round_up_budget",
]

EXIT_OK = 0
EXIT_DOES_NOT_FIT = 1
EXIT_ERROR = 2

#: The two paths a source's prompts can take (``prompt_engine.static_prefix``).
PATH_STATIC = "static prefix"
PATH_RETRIEVAL = "retrieval"

#: Response fields, in the order tried, that carry a model's context length:
#: vLLM's ``max_model_len``, then the names other servers use.
_CONTEXT_FIELDS = ("max_model_len", "context_length", "context_window")

_USERINFO = re.compile(r"(?<=://)[^/@\s]+@")


class ModelUnavailable(Exception):
    """The model endpoint gave no answer worth using; the message says why.

    The message is safe to print: the default client scrubs the API key and
    any URL credentials out of it.
    """


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceSize:
    """One data source's static prefix: size now, and (maybe) in real tokens.

    Attributes
    ----------
    name:
        The data source (``default`` when there is no ``datasources.yaml``).
    tables:
        How many tables its prefix describes.
    chars:
        Length of the prefix in characters.
    estimate:
        :func:`~prompt_engine.static_prefix.static_prefix_token_estimate`.
    path_now:
        :data:`PATH_STATIC` or :data:`PATH_RETRIEVAL` under the current
        budget.
    real_tokens:
        The model's ``usage.prompt_tokens`` for the prefix; ``None`` when not
        asked or not available.
    real_note:
        Why ``real_tokens`` is ``None`` (empty when it is known).

    Examples
    --------
    >>> SourceSize("sales", 9, 21300, 5325, PATH_STATIC, 6070).ratio
    1.14
    >>> SourceSize("sales", 9, 21300, 5325, PATH_STATIC).ratio is None
    True
    """

    name: str
    tables: int
    chars: int
    estimate: int
    path_now: str
    real_tokens: int | None = None
    real_note: str = ""

    @property
    def ratio(self) -> float | None:
        """``real_tokens / estimate`` to two places, or ``None`` when unknown."""
        fraction = self._ratio_exact()
        return None if fraction is None else round(float(fraction), 2)

    def _ratio_exact(self) -> Fraction | None:
        if self.real_tokens is None or self.estimate <= 0:
            return None
        return Fraction(self.real_tokens, self.estimate)


def measure_prefixes(system_prompt: str, names: Sequence[str]) -> list[SourceSize]:
    """Build each source's static prefix as the server does and size it.

    Parameters
    ----------
    system_prompt:
        The system prompt text the server loads.
    names:
        The data source names, in ``datasources.yaml`` order.

    Returns
    -------
    list[SourceSize]
        One per name, without real token counts.

    Raises
    ------
    ValueError
        If several sources are configured and a name is not one of them.

    Examples
    --------
    >>> [s.name for s in measure_prefixes("You are a T-SQL expert.", ["default"])]
    ['default']
    >>> size = measure_prefixes("You are a T-SQL expert.", ["default"])[0]
    >>> size.chars > 0 and size.estimate == size.chars // 4
    True
    """
    sizes: list[SourceSize] = []
    for name in names:
        prefix = build_static_prefix(system_prompt, name)
        static = should_use_static_prefix(system_prompt, name)
        sizes.append(SourceSize(
            name=name,
            tables=len(SchemaRegistry.tables_for_source(name)),
            chars=len(prefix),
            estimate=static_prefix_token_estimate(system_prompt, name),
            path_now=PATH_STATIC if static else PATH_RETRIEVAL,
        ))
    return sizes


def paths_under_budget(
    system_prompt: str, names: Sequence[str], budget: int,
) -> dict[str, str]:
    """The path each source would take if ``PROMPT_RETRIEVAL_TOKEN_BUDGET`` were *budget*.

    Asks the server's own gate (:func:`~prompt_engine.static_prefix.should_use_static_prefix`)
    with the setting temporarily replaced, so the answer cannot drift from
    what the server decides.

    Parameters
    ----------
    system_prompt:
        The system prompt text the server loads.
    names:
        The data source names.
    budget:
        The budget to ask about.

    Returns
    -------
    dict[str, str]
        ``{source: PATH_STATIC | PATH_RETRIEVAL}``.

    Examples
    --------
    >>> paths_under_budget("You are a T-SQL expert.", ["default"], 10**9)
    {'default': 'static prefix'}
    >>> paths_under_budget("You are a T-SQL expert.", ["default"], 1)
    {'default': 'retrieval'}
    """
    with cfg.override_settings(prompt_retrieval_token_budget=budget):
        return {
            name: PATH_STATIC if should_use_static_prefix(system_prompt, name) else PATH_RETRIEVAL
            for name in names
        }


# ---------------------------------------------------------------------------
# The model endpoint
# ---------------------------------------------------------------------------

class ModelClient(Protocol):
    """What the script needs from the model endpoint; tests inject a fake."""

    @property
    def model(self) -> str:
        """The configured model name."""

    @property
    def endpoint(self) -> str:
        """The endpoint as it may be printed (no credentials)."""

    def count_prompt_tokens(self, prompt: str, timeout: float) -> int:
        """The model's ``usage.prompt_tokens`` for *prompt*.

        Raises
        ------
        ModelUnavailable
            If there is no usable answer.
        """

    def context_length(self, timeout: float) -> int | None:
        """The configured model's context length, or ``None`` if not reported.

        Raises
        ------
        ModelUnavailable
            If the endpoint cannot be asked.
        """


def _safe_endpoint(url: str) -> str:
    """*url* without credentials, query string or fragment.

    Examples
    --------
    >>> _safe_endpoint("http://user:pw@host:8000/v1/?key=1#x")
    'http://host:8000/v1'
    >>> _safe_endpoint("https://api.example.com/v1")
    'https://api.example.com/v1'
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host + (f":{port}" if port else "")
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def _scrub(text: str, secrets: Sequence[str]) -> str:
    """One short line from *text*, with every secret and URL credential removed.

    Examples
    --------
    >>> _scrub("401 for http://u:p@h/v1 with key sk-1", ["sk-1"])
    '401 for http://***@h/v1 with key ***'
    """
    line = (text.strip().splitlines() or [""])[0]
    for secret in secrets:
        if secret:
            line = line.replace(secret, "***")
    return _USERINFO.sub("***@", line)[:200]


def parse_context_length(payload: object, model: str) -> int | None:
    """The context length a ``GET /models`` response gives for *model*.

    Looks in the entry whose ``id`` is *model* (or in the only entry when
    exactly one model is listed and none matches by name -- a server that
    names its model by file path) for ``max_model_len`` (vLLM),
    ``context_length`` or ``context_window``; the first positive integer wins.

    Parameters
    ----------
    payload:
        The decoded JSON body.
    model:
        The configured model name.

    Returns
    -------
    int | None
        ``None`` when the body has no such field for the model.

    Examples
    --------
    >>> parse_context_length({"data": [{"id": "m", "max_model_len": 32768}]}, "m")
    32768
    >>> parse_context_length({"data": [{"id": "m", "context_window": 8192}]}, "m")
    8192
    >>> parse_context_length({"data": [{"id": "other", "max_model_len": 4096}]}, "m")
    4096
    >>> parse_context_length({"data": [{"id": "a", "max_model_len": 1}, {"id": "b"}]}, "m") is None
    True
    >>> parse_context_length({"data": [{"id": "m"}]}, "m") is None
    True
    """
    if not isinstance(payload, Mapping):
        return None
    listed = payload.get("data")
    if not isinstance(listed, list):
        return None
    entries = [entry for entry in listed if isinstance(entry, Mapping)]
    chosen = [entry for entry in entries if entry.get("id") == model]
    if not chosen and len(entries) == 1:
        chosen = entries
    for entry in chosen:
        for key in _CONTEXT_FIELDS:
            value = entry.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return None


class UnavailableClient:
    """A client for a deployment with no endpoint to ask (``LLM_PROVIDER=mock``).

    Parameters
    ----------
    reason:
        Why there is nothing to ask; becomes every call's failure message.

    Examples
    --------
    >>> UnavailableClient("no endpoint").count_prompt_tokens("x", 1.0)  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    ModelUnavailable: no endpoint
    """

    model = ""
    endpoint = ""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def count_prompt_tokens(self, prompt: str, timeout: float) -> int:  # noqa: ARG002
        raise ModelUnavailable(self._reason)

    def context_length(self, timeout: float) -> int | None:  # noqa: ARG002
        raise ModelUnavailable(self._reason)


class OpenAICompatibleClient:
    """The application's own OpenAI-compatible transport, asked for token counts.

    The request is built by the application's provider code, not copied from
    it: :meth:`llm.providers.OpenAIBackend._build_payload` (model, sampling
    fields, ``LLM_STOP``, ``LLM_EXTRA_BODY``) with ``max_tokens`` replaced by
    1, and the backend's own headers (the API key, when there is one), so the
    count is what production sends for the same text. Those two members are
    private in the provider module and have no public accessor; the tests pin
    the contract.

    Parameters
    ----------
    endpoint:
        The endpoint's configuration (:mod:`llm.endpoints`).
    backend:
        The backend built from it (:func:`llm.endpoints.build_backend`).
    http_post, http_get:
        ``requests.post`` / ``requests.get`` by default; tests pass fakes.

    Examples
    --------
    >>> config = EndpointConfig(name="default", base_url="http://u:p@h:8000/v1?k=1", model="m")
    >>> client = OpenAICompatibleClient(config, build_backend(config))
    >>> client.model, client.endpoint
    ('m', 'http://h:8000/v1')
    """

    def __init__(
        self,
        endpoint: EndpointConfig,
        backend: OpenAIBackend,
        *,
        http_post: Callable[..., Any] = requests.post,
        http_get: Callable[..., Any] = requests.get,
    ) -> None:
        self._config = endpoint
        self._backend = backend
        self._post = http_post
        self._get = http_get
        self._base = backend.endpoint or endpoint.base_url.rstrip("/")
        parts = urlsplit(self._base)
        self._secrets = [
            endpoint.api_key, parts.username or "", parts.password or "",
            *(value for _, value in parse_qsl(parts.query)),
        ]

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def endpoint(self) -> str:
        return _safe_endpoint(self._base)

    def _clean(self, text: str) -> str:
        """The first line of *text*, short, without credentials or the raw URL."""
        return _scrub(text.replace(self._base, self.endpoint), self._secrets)

    def _describe(self, exc: BaseException) -> str:
        """``Class: first line`` of *exc*, safe to print."""
        if isinstance(exc, requests.ConnectionError):
            return f"cannot connect to {self.endpoint}"
        return f"{type(exc).__name__}: {self._clean(str(exc))}"

    def validate(self) -> None:
        """Build one request body, so a bad ``LLM_EXTRA_BODY`` fails up front.

        Raises
        ------
        ValueError
            :class:`llm.providers.ExtraBodyConfigError` for an invalid
            ``LLM_EXTRA_BODY``.
        """
        self._backend._build_payload("")

    def count_prompt_tokens(self, prompt: str, timeout: float) -> int:
        """Send *prompt* with ``max_tokens=1``; return ``usage.prompt_tokens``.

        Parameters
        ----------
        prompt:
            The text to measure (a static prefix).
        timeout:
            Seconds to wait for the response.

        Returns
        -------
        int

        Raises
        ------
        ModelUnavailable
            If the endpoint is not trusted and ``LLM_ALLOW_REMOTE`` is false
            (nothing is sent), cannot be reached, answers with an error or
            after *timeout*, or the response has no ``usage.prompt_tokens``.
        """
        trusted = is_trusted_backend(self._backend)
        if not trusted and not cfg.settings.llm_allow_remote:
            raise ModelUnavailable(
                "not sent: the endpoint is not trusted and LLM_ALLOW_REMOTE is false "
                "(the prefix is schema text; see llm/router.py, 'Data governance')"
            )
        payload = self._backend._build_payload(prompt)
        payload["max_tokens"] = 1
        if not trusted:
            LLMRouter._audit_remote_use(
                self._backend, TaskType.SQL_GENERATION, PromptSegments(static_prefix=prompt),
            )
        try:
            response = self._post(
                f"{self._base}/chat/completions",
                headers=self._backend._headers,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            body = response.json()
        except requests.ConnectionError as exc:
            raise ModelUnavailable(self._describe(exc)) from None
        except requests.Timeout:
            raise ModelUnavailable(
                f"no answer within {timeout:g}s (a cold prefill of a large prefix can "
                "be slow: raise --timeout)"
            ) from None
        except requests.HTTPError as exc:
            detail = self._clean(getattr(exc.response, "text", "") or "")
            raise ModelUnavailable(
                f"{self._describe(exc)}" + (f" -- {detail}" if detail else "")
            ) from None
        except (requests.RequestException, ValueError) as exc:
            raise ModelUnavailable(self._describe(exc)) from None
        usage = body.get("usage") if isinstance(body, Mapping) else None
        tokens = usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
            raise ModelUnavailable("the response carries no usage.prompt_tokens")
        return tokens

    def context_length(self, timeout: float) -> int | None:
        """The configured model's context length from ``GET {base}/models``.

        Raises
        ------
        ModelUnavailable
            If the endpoint cannot be asked or does not answer with JSON.
        """
        try:
            response = self._get(
                f"{self._base}/models", headers=self._backend._headers, timeout=timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ModelUnavailable(self._describe(exc)) from None
        return parse_context_length(body, self.model)


def build_default_client(
    *,
    http_post: Callable[..., Any] = requests.post,
    http_get: Callable[..., Any] = requests.get,
) -> ModelClient:
    """The client for the endpoint the router uses for SQL generation.

    That is the first endpoint of the ``sql_generation`` route
    (:func:`llm.endpoints.load_routes`): the ``default`` endpoint built from
    ``OPENAI_BASE_URL`` / ``OPENAI_MODEL`` / ``OPENAI_API_KEY`` unless
    ``LLM_ROUTES`` says otherwise. Later endpoints of the chain (fallbacks)
    are not measured. With ``LLM_PROVIDER=mock`` there is no endpoint and
    every call reports it.

    Parameters
    ----------
    http_post, http_get:
        Passed to :class:`OpenAICompatibleClient`.

    Returns
    -------
    ModelClient

    Raises
    ------
    ValueError
        If ``LLM_PROVIDER``, ``LLM_ENDPOINTS``, ``LLM_ROUTES`` or
        ``LLM_EXTRA_BODY`` is invalid, or the route names an unknown endpoint.

    Examples
    --------
    >>> with cfg.override_settings(llm_provider="mock"):
    ...     type(build_default_client()).__name__
    'UnavailableClient'
    >>> with cfg.override_settings(llm_provider="x"):
    ...     build_default_client()
    Traceback (most recent call last):
        ...
    ValueError: Unsupported LLM_PROVIDER 'x'. Choose one of: openai, mock.
    """
    provider = cfg.settings.llm_provider.strip().lower()
    if provider == "mock":
        return UnavailableClient("LLM_PROVIDER=mock: there is no model endpoint")
    if provider != "openai":
        raise ValueError(f"Unsupported LLM_PROVIDER {provider!r}. Choose one of: openai, mock.")
    endpoints = load_endpoints()
    chain = load_routes().get(TaskType.SQL_GENERATION.value) or [DEFAULT_ENDPOINT_NAME]
    name = chain[0]
    if name not in endpoints:
        raise ValueError(
            f"LLM_ROUTES references unknown endpoint {name!r} for task "
            f"'sql_generation'. Known endpoints: {sorted(endpoints)}"
        )
    config = endpoints[name]
    backend = build_backend(config)
    if not isinstance(backend, OpenAIBackend):  # the only transport there is today
        raise ValueError(f"endpoint {name!r} is not an OpenAI-compatible endpoint")
    client = OpenAICompatibleClient(config, backend, http_post=http_post, http_get=http_get)
    client.validate()
    return client


# ---------------------------------------------------------------------------
# The recommendation
# ---------------------------------------------------------------------------

def round_up_budget(largest: int, headroom_percent: float, round_to: int) -> int:
    """*largest* plus headroom, rounded up to a multiple of *round_to*.

    Exact arithmetic (``Fraction``): ``5000`` with 10% headroom is ``5500``,
    not the ``6000`` that ``5000 * 1.1`` would round up to in floating point.

    Parameters
    ----------
    largest:
        The largest estimate that must stay on the static path.
    headroom_percent:
        Extra room, in percent (``10`` is 10%).
    round_to:
        The multiple to round up to (at least 1).

    Returns
    -------
    int

    Examples
    --------
    >>> round_up_budget(5100, 10, 500)
    6000
    >>> round_up_budget(5000, 10, 500)
    5500
    >>> round_up_budget(5100, 0, 500)
    5500
    >>> round_up_budget(5100, 10, 1)
    5610
    """
    scaled = Fraction(largest) * (1 + Fraction(str(headroom_percent)) / 100)
    return math.ceil(scaled / round_to) * round_to


@dataclass(frozen=True)
class SourceFit:
    """Whether one source's prompt fits the model's context window.

    Attributes
    ----------
    name:
        The data source.
    prefix_tokens:
        Measured, or projected from the estimate (see *basis*).
    basis:
        ``measured``, ``estimate x1.14 (largest ratio measured)`` or
        ``estimate x1.15 (assumed)``.
    needed:
        ``prefix_tokens`` + the per-question room + ``LLM_NUM_PREDICT``.
    fits:
        ``needed <= context length``; ``None`` when the length is unknown.
    """

    name: str
    prefix_tokens: int
    basis: str
    needed: int
    fits: bool | None


@dataclass(frozen=True)
class Recommendation:
    """What :func:`recommend` works out.

    Attributes
    ----------
    budget:
        The ``PROMPT_RETRIEVAL_TOKEN_BUDGET`` to set; ``None`` when no source
        fits the context.
    largest_estimate:
        The largest estimate among the sources that fit (``None`` likewise).
    fits:
        One :class:`SourceFit` per source, in order.
    excluded:
        Sources that do not fit, left out of the calculation.
    conflicts:
        Excluded sources whose estimate is not above *budget*: the budget
        would send them down the static path.
    observed_ratio:
        The largest real/estimate ratio measured (``None`` when none was).
    warnings:
        Plain sentences for the operator.
    """

    budget: int | None
    largest_estimate: int | None
    fits: tuple[SourceFit, ...]
    excluded: tuple[str, ...]
    conflicts: tuple[str, ...]
    observed_ratio: float | None
    warnings: tuple[str, ...]


def recommend(
    sources: Sequence[SourceSize],
    *,
    context_length: int | None,
    question_room: int,
    num_predict: int,
    headroom_percent: float,
    round_to: int,
    assumed_ratio: float,
) -> Recommendation:
    """Fit-check every source and recommend a ``PROMPT_RETRIEVAL_TOKEN_BUDGET``.

    Parameters
    ----------
    sources:
        The sizes, real counts filled in where known.
    context_length:
        The model's context window in tokens, or ``None`` when unknown (the
        fit check is then skipped and every source is included).
    question_room:
        Tokens reserved for what follows the prefix in a prompt.
    num_predict:
        ``LLM_NUM_PREDICT``: tokens reserved for the answer.
    headroom_percent:
        Growth allowance on the largest estimate, in percent.
    round_to:
        The budget is rounded up to a multiple of this.
    assumed_ratio:
        real/estimate used for a source with no measurement when no other
        source was measured either.

    Returns
    -------
    Recommendation

    Examples
    --------
    Two measured sources, a 8192-token context: the second (``needs`` 9100)
    does not fit, so the budget follows the first alone.

    >>> a = SourceSize("a", 5, 8000, 2000, PATH_STATIC, 2280)
    >>> b = SourceSize("b", 9, 20000, 5000, PATH_STATIC, 5700)
    >>> r = recommend([a, b], context_length=8192, question_room=2000,
    ...               num_predict=512, headroom_percent=10, round_to=500,
    ...               assumed_ratio=1.15)
    >>> r.budget, r.excluded, [f.needed for f in r.fits]
    (2500, ('b',), [4792, 8212])

    With an unknown context length nothing is excluded:

    >>> r = recommend([a, b], context_length=None, question_room=2000,
    ...               num_predict=512, headroom_percent=10, round_to=500,
    ...               assumed_ratio=1.15)
    >>> r.budget, r.excluded
    (5500, ())
    """
    exact = [s._ratio_exact() for s in sources]
    measured = [ratio for ratio in exact if ratio is not None]
    observed = max(measured) if measured else None
    fits: list[SourceFit] = []
    for size in sources:
        if size.real_tokens is not None:
            tokens, basis = size.real_tokens, "measured"
        elif observed is not None:
            tokens = math.ceil(size.estimate * observed)
            basis = f"estimate x{float(observed):.2f} (largest ratio measured)"
        else:
            tokens = math.ceil(size.estimate * Fraction(str(assumed_ratio)))
            basis = f"estimate x{assumed_ratio:.2f} (assumed)"
        needed = tokens + question_room + num_predict
        fits.append(SourceFit(
            name=size.name,
            prefix_tokens=tokens,
            basis=basis,
            needed=needed,
            fits=None if context_length is None else needed <= context_length,
        ))

    by_name = {s.name: s for s in sources}
    excluded = tuple(f.name for f in fits if f.fits is False)
    included = [by_name[f.name] for f in fits if f.fits is not False]
    largest = max((s.estimate for s in included), default=None)
    budget = (
        None if largest is None else round_up_budget(largest, headroom_percent, round_to)
    )
    conflicts = tuple(
        name for name in excluded if budget is not None and by_name[name].estimate <= budget
    )

    warnings: list[str] = []
    if not measured:
        warnings.append(
            "no real token count: prefix sizes are projected from the estimate "
            f"(x{assumed_ratio:.2f}, assumed); run without --no-model against a "
            "reachable endpoint for measured numbers"
        )
    if context_length is None:
        warnings.append(
            "the context length is unknown, so the fit check was skipped and every "
            "source is included; pass --context-length N (or use an endpoint whose "
            "/models reports it)"
        )
    for fit in fits:
        if fit.fits is False:
            warnings.append(
                f"source {fit.name!r} does not fit: it needs about {fit.needed} tokens "
                f"({fit.prefix_tokens} prefix, {fit.basis}; {question_room} per-question "
                f"room; {num_predict} LLM_NUM_PREDICT) and the context length is "
                f"{context_length}. It is left out of the recommendation: keep it on "
                "the retrieval path, or move tables to another source."
            )
    if budget is None:
        warnings.append(
            "no source fits the context length, so no budget is recommended: every "
            "source should stay on the retrieval path"
        )
    for name in conflicts:
        if budget is None or largest is None:
            continue  # unreachable: a conflict needs a budget
        own = by_name[name].estimate
        if own > largest:
            hint = (
                f"a budget from {largest} to {own - 1} keeps every source that fits on "
                f"the static path and {name!r} on retrieval"
            )
        else:
            hint = (
                f"no budget can do both, because {name!r} is not larger than a source "
                "that fits: move tables out of it"
            )
        warnings.append(
            f"the recommended budget {budget} is not below the estimate of source "
            f"{name!r} ({own}), so it would take the static path and overflow the "
            f"context; {hint}."
        )
    return Recommendation(
        budget=budget,
        largest_estimate=largest,
        fits=tuple(fits),
        excluded=excluded,
        conflicts=conflicts,
        observed_ratio=None if observed is None else round(float(observed), 2),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelInfo:
    """What was learned about (or from) the model endpoint.

    Attributes
    ----------
    queried:
        ``False`` with ``--no-model``: the endpoint was not contacted.
    model, endpoint:
        The configured model and the printable endpoint (empty when not
        queried).
    context_length:
        Tokens, or ``None`` when unknown.
    context_source:
        Where the length came from, or why it is unknown.
    requests_sent:
        How many prefixes were counted successfully (each warmed the
        server's prefix cache for its source).
    """

    queried: bool
    model: str
    endpoint: str
    context_length: int | None
    context_source: str
    requests_sent: int


@dataclass(frozen=True)
class Report:
    """Everything the two renderers print.

    Attributes
    ----------
    system_prompt_path:
        Where the system prompt was read from.
    current_budget:
        ``PROMPT_RETRIEVAL_TOKEN_BUDGET`` as configured now.
    sources:
        The sizes, in ``datasources.yaml`` order.
    model:
        The endpoint facts.
    recommendation:
        The fit check and the budget.
    paths_after:
        Each source's path under the recommended budget (empty when none).
    question_room, num_predict, headroom_percent, round_to:
        The parameters the recommendation used.
    """

    system_prompt_path: str
    current_budget: int
    sources: tuple[SourceSize, ...]
    model: ModelInfo
    recommendation: Recommendation
    paths_after: Mapping[str, str]
    question_room: int
    num_predict: int
    headroom_percent: float
    round_to: int


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], align: str) -> list[str]:
    """Fixed-width rows; *align* is one ``l``/``r`` per column."""
    widths = [max([len(h), *(len(r[i]) for r in rows)]) for i, h in enumerate(headers)]

    def fmt(cells: Sequence[str]) -> str:
        parts = [
            c.ljust(w) if a == "l" else c.rjust(w)
            for c, w, a in zip(cells, widths, align, strict=True)
        ]
        return "  " + "  ".join(parts).rstrip()

    return [fmt(headers), *(fmt(r) for r in rows)]


def render_text(report: Report) -> str:
    """The readable report.

    Parameters
    ----------
    report:
        What :func:`main` assembled.

    Returns
    -------
    str
        Multi-line text without a trailing newline.
    """
    rec, model = report.recommendation, report.model
    out = [
        f"PROMPT_RETRIEVAL_TOKEN_BUDGET is {report.current_budget} "
        "(compared with each source's own estimate)",
        f"system prompt: {report.system_prompt_path}",
        "",
        f"== static prefix of each data source ({len(report.sources)}) ==",
    ]
    rows = [
        [
            s.name, str(s.tables), str(s.chars), str(s.estimate),
            "unavailable" if s.real_tokens is None else str(s.real_tokens),
            "-" if s.ratio is None else f"{s.ratio:.2f}",
            s.path_now,
        ]
        for s in report.sources
    ]
    out += _table(
        ["source", "tables", "chars", "estimate", "real tokens", "real/est", "path now"],
        rows, "lrrrrrl",
    )
    notes = [f"  {s.name}: {s.real_note}" for s in report.sources if s.real_note]
    if notes:
        out += ["", "real token counts not available:", *notes]

    out += ["", "== model =="]
    if model.queried:
        out.append(f"endpoint: {model.endpoint} (model {model.model})")
    else:
        out.append("endpoint: not contacted (--no-model)")
    if model.context_length is None:
        out.append(f"context length: unknown -- {model.context_source}")
    else:
        out.append(f"context length: {model.context_length} ({model.context_source})")
    out.append(
        f"per-question room: {report.question_room} tokens (--question-room); "
        f"LLM_NUM_PREDICT: {report.num_predict}"
    )
    if model.requests_sent:
        out.append(
            f"{model.requests_sent} prefix(es) were sent to count their tokens: the "
            "model server's prefix cache is now warm for those sources"
        )

    out += ["", "== fit in the context window =="]
    out.append("  needs = prefix tokens + per-question room + LLM_NUM_PREDICT")
    fit_rows = [
        [
            f.name, str(f.prefix_tokens), f.basis, str(f.needed),
            "?" if f.fits is None else ("yes" if f.fits else "NO"),
        ]
        for f in rec.fits
    ]
    out += _table(["source", "prefix tokens", "basis", "needs", "fits"], fit_rows, "lrlrl")
    if rec.observed_ratio is not None:
        out.append(f"  largest real/estimate ratio measured: {rec.observed_ratio:.2f}")

    out += ["", "== recommendation =="]
    if rec.budget is None:
        out.append("no PROMPT_RETRIEVAL_TOKEN_BUDGET recommended: no source fits")
    else:
        out.append(f"PROMPT_RETRIEVAL_TOKEN_BUDGET={rec.budget}")
        out.append(
            f"  = the largest estimate that fits ({rec.largest_estimate}) plus "
            f"{report.headroom_percent:g}% headroom, rounded up to a multiple of "
            f"{report.round_to}; put the line in .env and restart the server"
        )
        if rec.budget == report.current_budget:
            out.append("  (this is the current value)")
        change_rows = []
        for s in report.sources:
            after = report.paths_after.get(s.name, "")
            if s.name in rec.conflicts:
                verdict = "static path, does not fit"
            else:
                verdict = "unchanged" if s.path_now == after else "changes"
            change_rows.append([s.name, s.path_now, after, verdict])
        out.append("")
        out += _table(
            ["source", "path now", f"path with {rec.budget}", ""], change_rows, "llll",
        )
    if rec.warnings:
        out += ["", "warnings:", *(f"  - {w}" for w in rec.warnings)]
    return "\n".join(out)


def render_json(report: Report) -> dict[str, Any]:
    """The report as a JSON-serialisable mapping.

    Parameters
    ----------
    report:
        What :func:`main` assembled.

    Returns
    -------
    dict[str, Any]
        Keys: ``system_prompt``, ``current_budget``, ``model``,
        ``parameters``, ``sources`` (one object per source), ``recommendation``.
    """
    rec, model = report.recommendation, report.model
    fits = {f.name: f for f in rec.fits}
    sources = []
    for s in report.sources:
        fit = fits[s.name]
        sources.append({
            "name": s.name,
            "tables": s.tables,
            "chars": s.chars,
            "estimate": s.estimate,
            "path_now": s.path_now,
            "real_tokens": s.real_tokens,
            "real_note": s.real_note or None,
            "ratio": s.ratio,
            "prefix_tokens": fit.prefix_tokens,
            "prefix_basis": fit.basis,
            "needs": fit.needed,
            "fits": fit.fits,
            "excluded": s.name in rec.excluded,
            "path_after": report.paths_after.get(s.name),
        })
    return {
        "system_prompt": report.system_prompt_path,
        "current_budget": report.current_budget,
        "model": {
            "queried": model.queried,
            "model": model.model or None,
            "endpoint": model.endpoint or None,
            "context_length": model.context_length,
            "context_source": model.context_source,
            "requests_sent": model.requests_sent,
        },
        "parameters": {
            "question_room": report.question_room,
            "num_predict": report.num_predict,
            "headroom_percent": report.headroom_percent,
            "round_to": report.round_to,
        },
        "sources": sources,
        "recommendation": {
            "budget": rec.budget,
            "env_line": (
                None if rec.budget is None
                else f"PROMPT_RETRIEVAL_TOKEN_BUDGET={rec.budget}"
            ),
            "largest_estimate": rec.largest_estimate,
            "observed_ratio": rec.observed_ratio,
            "excluded": list(rec.excluded),
            "conflicts": list(rec.conflicts),
            "warnings": list(rec.warnings),
        },
    }


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _describe(exc: BaseException) -> str:
    """One short line about *exc*: its class and the start of its message."""
    first = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first[:160]}"


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def _non_negative_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prompt_budget.py",
        description=(
            "Report each data source's static prompt prefix (characters, tables, "
            "heuristic estimate, real tokens from the model) and recommend a "
            "PROMPT_RETRIEVAL_TOKEN_BUDGET. Read-only; counting real tokens sends "
            "each prefix to the model once, which warms the server's prefix cache."
        ),
    )
    parser.add_argument(
        "--no-model", action="store_true",
        help=(
            "Never contact the model endpoint: no real token counts and no /models "
            "lookup (give --context-length for the fit check)."
        ),
    )
    parser.add_argument(
        "--timeout", type=_positive_float, default=300.0, metavar="SECONDS",
        help=(
            "How long to wait for each token count (default: 300; a cold prefill "
            "of ~20k tokens can take about a minute)."
        ),
    )
    parser.add_argument(
        "--models-timeout", type=_positive_float, default=10.0, metavar="SECONDS",
        help="How long to wait for GET /models (default: 10).",
    )
    parser.add_argument(
        "--context-length", type=_positive_int, default=None, metavar="N",
        help="The model's context length in tokens; skips the /models lookup.",
    )
    parser.add_argument(
        "--headroom", type=_non_negative_float, default=10.0, metavar="PERCENT",
        help=(
            "Growth allowance added to the largest estimate, in percent "
            "(default: 10, i.e. 10%%)."
        ),
    )
    parser.add_argument(
        "--round", dest="round_to", type=_positive_int, default=500, metavar="N",
        help="Round the recommended budget up to a multiple of N (default: 500).",
    )
    parser.add_argument(
        "--question-room", type=_non_negative_int, default=2000, metavar="TOKENS",
        help=(
            "Tokens reserved for what follows the prefix: the fixed suffix text, "
            "the question and the last SESSION_PROMPT_TURNS turns (default: 2000)."
        ),
    )
    parser.add_argument(
        "--assumed-ratio", type=_positive_float, default=1.15, metavar="RATIO",
        help=(
            "real/estimate used for a fit check when no source could be measured "
            "(default: 1.15, the Persian-heavy production measurement rounded up)."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print one JSON document on standard output instead of the table.",
    )
    return parser


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _context_length(
    args: argparse.Namespace, client: ModelClient | None,
) -> tuple[int | None, str]:
    """The context length and where it came from, or why it is unknown."""
    if args.context_length is not None:
        return args.context_length, "--context-length"
    if client is None:
        return None, "not asked (--no-model); pass --context-length N"
    try:
        found = client.context_length(args.models_timeout)
    except ModelUnavailable as exc:
        return None, f"GET /models failed: {exc}; pass --context-length N"
    except Exception as exc:  # noqa: BLE001 - a lookup must never stop the report
        return None, f"GET /models failed: {type(exc).__name__}; pass --context-length N"
    if found is None:
        return None, "/models does not report it; pass --context-length N"
    return found, "from the endpoint's /models"


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], ModelClient] | None = None,
) -> int:
    """Run the script; returns the process exit code.

    Parameters
    ----------
    argv:
        Command-line arguments; ``sys.argv[1:]`` when ``None``.
    client_factory:
        Builds the model client. Defaults to :func:`build_default_client`;
        tests inject a fake. Never called with ``--no-model``.

    Returns
    -------
    int
        ``EXIT_OK``, ``EXIT_DOES_NOT_FIT`` or ``EXIT_ERROR`` (see the module
        docstring). An invalid option exits through :mod:`argparse` with 2.
    """
    args = _build_parser().parse_args(argv)

    try:
        system_prompt = load_system_prompt()
        names = datasources.datasource_names()
        sizes = measure_prefixes(system_prompt, names)
    except Exception as exc:  # noqa: BLE001 - every failure here is a configuration problem
        print(f"cannot build the static prefixes: {_describe(exc)}", file=sys.stderr)
        return EXIT_ERROR

    client: ModelClient | None = None
    if not args.no_model:
        try:
            client = (client_factory or build_default_client)()
        except ValueError as exc:
            print(f"cannot use the LLM configuration: {_describe(exc)}", file=sys.stderr)
            return EXIT_ERROR

    context_length, context_source = _context_length(args, client)

    requests_sent = 0
    measured: list[SourceSize] = []
    for size in sizes:
        if client is None:
            measured.append(replace(size, real_note="not asked (--no-model)"))
            continue
        _progress(f"counting the tokens of {size.name!r} ({size.chars} characters)...")
        prefix = build_static_prefix(system_prompt, size.name)
        try:
            tokens = client.count_prompt_tokens(prefix, args.timeout)
        except ModelUnavailable as exc:
            measured.append(replace(size, real_note=str(exc)))
        except Exception as exc:  # noqa: BLE001 - one source must not stop the others
            measured.append(replace(size, real_note=f"unexpected error ({type(exc).__name__})"))
        else:
            requests_sent += 1
            measured.append(replace(size, real_tokens=tokens))

    recommendation = recommend(
        measured,
        context_length=context_length,
        question_room=args.question_room,
        num_predict=cfg.settings.llm_num_predict,
        headroom_percent=args.headroom,
        round_to=args.round_to,
        assumed_ratio=args.assumed_ratio,
    )
    paths_after = (
        {} if recommendation.budget is None
        else paths_under_budget(system_prompt, names, recommendation.budget)
    )
    report = Report(
        system_prompt_path=str(resolve_system_prompt_path()),
        current_budget=cfg.settings.prompt_retrieval_token_budget,
        sources=tuple(measured),
        model=ModelInfo(
            queried=client is not None,
            model="" if client is None else client.model,
            endpoint="" if client is None else client.endpoint,
            context_length=context_length,
            context_source=context_source,
            requests_sent=requests_sent,
        ),
        recommendation=recommendation,
        paths_after=paths_after,
        question_room=args.question_room,
        num_predict=cfg.settings.llm_num_predict,
        headroom_percent=args.headroom,
        round_to=args.round_to,
    )
    if args.json:
        print(json.dumps(render_json(report), indent=2, ensure_ascii=False))
    else:
        print(render_text(report))
    return EXIT_DOES_NOT_FIT if recommendation.excluded else EXIT_OK


if __name__ == "__main__":
    use_utf8_console()
    sys.exit(main())
