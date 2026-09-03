# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Shared YAML loader + Pydantic v2 models for the knowledge layer.

Usage
-----
    from knowledge.config_loader import load_aliases, ConfigNotFoundError

Each ``load_*`` function:
  1. Looks for  ``<PROJECT_CONFIG_DIR>/<name>.yaml``  (``PROJECT_CONFIG_DIR``
     defaults to ``project_config``, git-ignored, real data — see
     :attr:`config.Settings.project_config_dir`).
  2. If missing → raises ``ConfigNotFoundError`` immediately.
     There is NO silent fallback to ``project_config.example/``.
  3. Validates structure with a Pydantic v2 model.
  4. On validation failure → raises ``ValueError`` with filename + field.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError, field_validator

logger = logging.getLogger(__name__)


def _project_config_dir() -> Path:
    """Return the configured project-config directory, resolved at call time.

    Reads ``cfg.settings.project_config_dir`` on every call (not once at
    import time) so that :func:`config.override_settings` and a changed
    ``PROJECT_CONFIG_DIR`` environment variable both take effect
    immediately, per this codebase's read-through-``cfg.settings``
    convention (see ``config.py``'s module docstring). A relative value
    (the default, ``"project_config"``) is resolved against the current
    working directory, matching how :attr:`config.Settings.log_dir` and
    :attr:`config.Settings.export_dir` are already resolved elsewhere in
    this codebase; an absolute path is used as-is.
    """
    import config as cfg  # deferred: avoids a hard import-time dependency

    return Path(cfg.settings.project_config_dir)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConfigNotFoundError(Exception):
    """Raised when a required project_config YAML file is missing."""


# ---------------------------------------------------------------------------
# Low-level loader
# ---------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    """Load and parse a YAML file.

    Raises
    ------
    ConfigNotFoundError
        If the file does not exist.
    """
    if not path.exists():
        raise ConfigNotFoundError(
            f"{path} not found. "
            f"Copy from project_config.example/ and fill in your data."
        )
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Pydantic v2 models
# ---------------------------------------------------------------------------

class AliasesConfig(BaseModel):
    ring_aliases: dict[str, list[str]]
    synonyms: dict[str, list[str]]

    @field_validator("ring_aliases", "synonyms", mode="before")
    @classmethod
    def _ensure_list_values(cls, v: Any) -> Any:
        if isinstance(v, dict):
            for key, val in v.items():
                if not isinstance(val, list):
                    raise ValueError(
                        f"Value for key '{key}' must be a list, got {type(val).__name__}"
                    )
        return v


class EntityDefinition(BaseModel):
    aliases: list[str]
    table: str
    schema_name: str | None = None  # optional: e.g. "dim" or "fact"


class EntitiesConfig(BaseModel):
    entities: dict[str, EntityDefinition]


class RuleDefinition(BaseModel):
    rule_text: str


class BusinessRulesConfig(BaseModel):
    rules: dict[str, RuleDefinition]


class ExampleDefinition(BaseModel):
    tags: list[str]
    question: str
    sql: str


class ExamplesConfig(BaseModel):
    examples: list[ExampleDefinition]


class MetricDefinition(BaseModel):
    aliases: list[str]
    expression: str


class MetricsConfig(BaseModel):
    metrics: dict[str, MetricDefinition]


class RetrievalHintsConfig(BaseModel):
    fact_tables: list[str]
    always_include: dict[str, list[str]]
    fact_patterns: dict[str, list[str]]

    @field_validator("always_include", "fact_patterns", mode="before")
    @classmethod
    def _ensure_list_values(cls, v: Any) -> Any:
        if isinstance(v, dict):
            for key, val in v.items():
                if not isinstance(val, list):
                    raise ValueError(
                        f"Value for key '{key}' must be a list, got {type(val).__name__}"
                    )
        return v


# ---------------------------------------------------------------------------
# Typed loader functions
# ---------------------------------------------------------------------------

def _load_validated(filename: str, model: type[BaseModel]) -> BaseModel:
    path = _project_config_dir() / filename
    raw = load_yaml(path)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        # Surface the first error with filename context
        first = exc.errors()[0]
        field = " -> ".join(str(x) for x in first["loc"])
        raise ValueError(
            f"[{filename}] validation error at '{field}': {first['msg']}"
        ) from exc


def load_aliases() -> AliasesConfig:
    return _load_validated("aliases.yaml", AliasesConfig)  # type: ignore[return-value]


def load_entities() -> EntitiesConfig:
    return _load_validated("entities.yaml", EntitiesConfig)  # type: ignore[return-value]


def load_business_rules() -> BusinessRulesConfig:
    return _load_validated("business_rules.yaml", BusinessRulesConfig)  # type: ignore[return-value]


def load_examples() -> ExamplesConfig:
    return _load_validated("examples.yaml", ExamplesConfig)  # type: ignore[return-value]


def load_metrics() -> MetricsConfig:
    return _load_validated("metrics.yaml", MetricsConfig)  # type: ignore[return-value]


def load_retrieval_hints() -> RetrievalHintsConfig:
    return _load_validated("retrieval_hints.yaml", RetrievalHintsConfig)  # type: ignore[return-value]
