# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Named LLM endpoints and per-task fallback routes, built from :mod:`config`.

The trivial case — one endpoint for every task — needs no configuration
beyond ``OPENAI_BASE_URL`` / ``OPENAI_MODEL`` / ``OPENAI_API_KEY``: that
alone gives every :class:`~llm.router.TaskType` a one-entry chain pointing
at a single :class:`~llm.providers.OpenAIBackend`, exactly as if this
module didn't exist. A deployment that wants a real router — a fast local
model for one task, a fallback for when it's down, a different model
entirely for another task — declares additional named endpoints in
``LLM_ENDPOINTS`` (a JSON array) and, optionally, which endpoints answer
which task and in what order in ``LLM_ROUTES`` (a JSON object). Nothing
about the trivial case changes when neither is set.

Example
-------
::

    LLM_ENDPOINTS=[
      {"name": "local", "base_url": "http://localhost:8000/v1", "model": "gpt-oss-20b"},
      {"name": "hosted", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": "sk-..."}
    ]
    LLM_ROUTES={"sql_generation": ["local", "hosted"], "interpretation": ["local"]}

Every :class:`~llm.router.TaskType` not mentioned in ``LLM_ROUTES`` still
defaults to ``["default"]`` — the single endpoint built from the plain
``OPENAI_*`` variables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import config as cfg
from llm.base import LLMBackend
from llm.providers import OpenAIBackend

#: The always-present endpoint name, built from OPENAI_BASE_URL / OPENAI_MODEL
#: / OPENAI_API_KEY / LLM_TRUSTED — the trivial single-endpoint case.
DEFAULT_ENDPOINT_NAME = "default"

#: Every ``TaskType`` value string, duplicated here as plain strings (rather
#: than importing ``llm.router.TaskType``) so this module has no dependency
#: on :mod:`llm.router` at all -- :mod:`llm.router` is the one that depends
#: on THIS module, inside ``LLMRouter.from_settings``, and a two-way import
#: would be a cycle.
_ALL_TASK_NAMES: tuple[str, ...] = ("sql_generation", "interpretation", "assumption_extraction")

__all__ = [
    "DEFAULT_ENDPOINT_NAME",
    "EndpointConfig",
    "build_backend",
    "load_endpoints",
    "load_routes",
]


@dataclass(frozen=True)
class EndpointConfig:
    """One named LLM endpoint.

    Parameters
    ----------
    name:
        Unique key used by ``LLM_ROUTES`` to reference this endpoint.
    base_url, model, api_key:
        As :class:`~llm.providers.OpenAIBackend`.
    trusted:
        Explicit override for whether this endpoint may see
        schema/business-rule/row data. ``None`` (the default) defers to
        :func:`~llm.trust.default_trust_for_url` at backend-construction
        time — see :meth:`~llm.providers.OpenAIBackend.trusted`.
    """

    name: str
    base_url: str
    model: str
    api_key: str = ""
    trusted: bool | None = None


def _default_endpoint() -> EndpointConfig:
    """The always-present ``"default"`` endpoint, built from plain OPENAI_* settings."""
    return EndpointConfig(
        name=DEFAULT_ENDPOINT_NAME,
        base_url=cfg.settings.openai_base_url,
        model=cfg.settings.openai_model,
        api_key=cfg.settings.openai_api_key,
        trusted=cfg.settings.llm_trusted,
    )


def load_endpoints() -> dict[str, EndpointConfig]:
    """Build the named-endpoint registry from :mod:`config`.

    Always includes :data:`DEFAULT_ENDPOINT_NAME`. Additional endpoints
    come from ``LLM_ENDPOINTS`` — a JSON array of objects, each requiring
    at least ``"name"``, ``"base_url"``, and ``"model"`` — and, if one of
    them is itself named ``"default"``, it overrides the plain-settings
    default entirely (letting a deployment fold everything into
    ``LLM_ENDPOINTS`` if it prefers one config surface over two).

    Returns
    -------
    dict[str, EndpointConfig]

    Raises
    ------
    ValueError
        If ``LLM_ENDPOINTS`` is set but is not valid JSON, or an entry is
        missing a required key.

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(llm_endpoints_json=""):
    ...     endpoints = load_endpoints()
    >>> list(endpoints)
    ['default']
    """
    endpoints: dict[str, EndpointConfig] = {DEFAULT_ENDPOINT_NAME: _default_endpoint()}
    raw = cfg.settings.llm_endpoints_json.strip()
    if not raw:
        return endpoints
    try:
        declared = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM_ENDPOINTS is not valid JSON: {exc}") from exc
    if not isinstance(declared, list):
        raise ValueError("LLM_ENDPOINTS must be a JSON array of endpoint objects")
    for item in declared:
        try:
            name = item["name"]
            base_url = item["base_url"]
            model = item["model"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"LLM_ENDPOINTS entry missing a required key (name/base_url/model): {item!r}"
            ) from exc
        endpoints[name] = EndpointConfig(
            name=name,
            base_url=base_url,
            model=model,
            api_key=item.get("api_key", ""),
            trusted=item.get("trusted"),
        )
    return endpoints


def load_routes() -> dict[str, list[str]]:
    """Per-task endpoint fallback chains, keyed by ``TaskType`` value string.

    Every known task defaults to ``["default"]``. ``LLM_ROUTES`` — a JSON
    object mapping a task name to a list of endpoint names, tried in order
    — overrides only the tasks it mentions; any task it doesn't mention
    keeps the ``["default"]`` fallback.

    Returns
    -------
    dict[str, list[str]]

    Raises
    ------
    ValueError
        If ``LLM_ROUTES`` is set but is not valid JSON.

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(llm_routes_json=""):
    ...     routes = load_routes()
    >>> routes["sql_generation"]
    ['default']
    """
    routes: dict[str, list[str]] = {name: [DEFAULT_ENDPOINT_NAME] for name in _ALL_TASK_NAMES}
    raw = cfg.settings.llm_routes_json.strip()
    if not raw:
        return routes
    try:
        declared = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM_ROUTES is not valid JSON: {exc}") from exc
    if not isinstance(declared, dict):
        raise ValueError("LLM_ROUTES must be a JSON object mapping task name to endpoint list")
    for task_name, names in declared.items():
        routes[task_name] = list(names)
    return routes


def build_backend(endpoint: EndpointConfig) -> LLMBackend:
    """Construct the :class:`~llm.base.LLMBackend` for one named endpoint.

    Examples
    --------
    >>> backend = build_backend(EndpointConfig(name="d", base_url="http://x/v1", model="m"))
    >>> backend.name
    'openai:m'
    """
    return OpenAIBackend(
        model=endpoint.model,
        api_key=endpoint.api_key,
        base_url=endpoint.base_url,
        trusted=endpoint.trusted,
    )
