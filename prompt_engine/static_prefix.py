# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The Phase 2 latency win: a byte-identical static prompt prefix.

``docs/api-contract-v2.md`` §8 measured the entire knowledge base at 4,588
tokens: full schema (1,417), relationships (468), business rules (625),
examples (998), system prompt (1,078). The six-retriever pipeline in
``retrieval/`` exists to shrink that to only the tables a given question
needs — saving roughly 3k tokens — but at three costs: it can miss the
right tables for an odd phrasing, it is non-deterministic (see
``prompt_engine.builder``), and, most expensively, it makes the prompt's
early content vary per request, which defeats llama.cpp's KV-cache reuse
and forces a full prefill on every call.

This module builds the alternative: **everything** the knowledge base
holds — system prompt, full schema, every relationship, every business
rule, every metric, every few-shot example — assembled once, in a fixed
order, and cached in memory (:func:`build_static_prefix` is
``functools.lru_cache``-backed, keyed on the system prompt text, which is
the only input that ever varies and only does so once per process,
loaded at startup). Because the prefix is now identical across every
request for a given system-prompt version, llama.cpp/vLLM can reuse the
KV cache for that shared span and only pay prefill cost for the short
variable suffix (session context, filters, the question) that
:mod:`prompt_engine.builder` appends after it.

Retrieval is not deleted — it remains the scaling escape hatch for a
schema too large to fit comfortably in context. :func:`should_use_static_prefix`
is the single gate: below ``cfg.settings.prompt_retrieval_token_budget``
(default large enough that today's 12-table schema always qualifies), the
static prefix is used; above it, :mod:`prompt_engine.builder` falls back to
the six-retriever pipeline, which keeps working unchanged and is exercised
by ``tests/test_static_prefix.py``'s forced-large-schema test.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

import config as cfg
from knowledge.business_rules import BUSINESS_RULES
from knowledge.examples import EXAMPLES
from knowledge.metrics import METRICS
from prompt_engine.templates import STATIC_PREFIX_TEMPLATE
from schema_data.columns import TABLE_COLUMNS
from schema_data.registry import SchemaRegistry

#: Rough characters-per-token ratio for the heuristic estimator below.
#: There is no tokenizer dependency in this project (no ``tiktoken`` /
#: model-specific vocabulary is available), so this is deliberately a
#: *heuristic*, documented as such everywhere it is used. It is only ever
#: compared against itself (the same estimator on both sides of the
#: token-budget check) or used as a rough per-skill-version constant fed
#: into ``observability.llm_status.build_llm_status``'s cache-hit ratio —
#: never presented as an exact model token count.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Heuristic token-count estimate: roughly one token per 4 characters.

    Not a real tokenizer — this project has no model-specific vocabulary
    available, and pulling one in only to estimate a budget threshold and
    a cache-hit ratio would be a heavy dependency for a rough number. Used
    for two purposes only, both tolerant of approximation: (1) the
    static-prefix-vs-retrieval budget gate in
    :func:`should_use_static_prefix`, and (2) ``static_prefix_tokens`` fed
    to ``observability.llm_status.build_llm_status``, whose
    ``prefix_cache_hit`` rule only needs the ratio to be roughly right
    (``prompt_eval_count < static_prefix_tokens * 0.5``).

    Parameters
    ----------
    text:
        Any string, including the empty string.

    Returns
    -------
    int
        ``0`` for empty input, otherwise ``max(1, len(text) // 4)``.

    Examples
    --------
    >>> estimate_tokens("")
    0
    >>> estimate_tokens("SQL")
    1
    >>> estimate_tokens("a" * 400)
    100
    """
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _all_business_rules_text() -> str:
    """Every business rule, one per paragraph — order follows dict iteration.

    ``BUSINESS_RULES`` is a lazily-loaded module-level dict
    (``knowledge.business_rules``); Python dicts preserve insertion order,
    and that order comes from ``project_config/business_rules.yaml``, so
    this is stable across calls within one process.
    """
    return "\n\n".join(BUSINESS_RULES.values())


def _all_metrics_text() -> str:
    """Every configured metric as ``key: expression (aliases: ...)`` lines."""
    lines: list[str] = []
    for key, spec in METRICS.items():
        expression = spec.get("expression", "")
        aliases = spec.get("aliases") or []
        line = f"{key}: {expression}"
        if aliases:
            line += f" (aliases: {', '.join(aliases)})"
        lines.append(line)
    return "\n".join(lines)


def _all_examples_text() -> str:
    """Every few-shot example as a Question/SQL pair, in configured order."""
    parts = [
        f"Question:\n{ex['question']}\n\nSQL:\n{ex['sql']}" for ex in EXAMPLES
    ]
    return "\n\n".join(parts)


def _full_schema_text() -> str:
    """The complete schema for every known table (all 12 today)."""
    return SchemaRegistry.build_schema_context(None)


def _full_relationships_text() -> str:
    """JOIN clauses for every FK edge between every known table."""
    all_tables = list(TABLE_COLUMNS.keys())
    return "\n".join(SchemaRegistry.get_relationships(all_tables))


@lru_cache(maxsize=8)
def build_static_prefix(system_prompt: str) -> str:
    """Assemble the byte-identical static prefix for *system_prompt*.

    Cached (``lru_cache``) on the system prompt text: the prefix depends
    on nothing else that changes at runtime (schema/rules/examples are
    static module data loaded once at import time), so repeated calls
    with the same system prompt — the common case, since it is loaded
    once at server startup — return the exact same string object without
    re-assembling it. ``maxsize=8`` allows a handful of distinct system
    prompts (e.g. across tests) without unbounded growth.

    Parameters
    ----------
    system_prompt:
        The domain system prompt text (``prompts/system_prompt.md``).

    Returns
    -------
    str
        The complete static prefix: system prompt, business rules,
        metrics, full schema, relationships, and every few-shot example,
        in that fixed order (``docs/api-contract-v2.md`` §8).

    Examples
    --------
    >>> prefix = build_static_prefix("You are a T-SQL expert.")
    >>> "You are a T-SQL expert." in prefix
    True
    >>> "Table: Contract" in prefix
    True
    >>> build_static_prefix("You are a T-SQL expert.") is prefix
    True
    """
    return STATIC_PREFIX_TEMPLATE.format(
        system_prompt=system_prompt,
        business_rules=_all_business_rules_text(),
        metrics=_all_metrics_text(),
        schema=_full_schema_text(),
        relationships=_full_relationships_text(),
        examples=_all_examples_text(),
    )


def static_prefix_token_estimate(system_prompt: str) -> int:
    """:func:`estimate_tokens` of :func:`build_static_prefix` for *system_prompt*.

    Examples
    --------
    >>> static_prefix_token_estimate("You are a T-SQL expert.") > 0
    True
    """
    return estimate_tokens(build_static_prefix(system_prompt))


@lru_cache(maxsize=8)
def prefix_version(system_prompt: str) -> str:
    """Short, stable fingerprint of the static prefix built from *system_prompt*.

    Used by ``api.query_cache.QueryCache`` as part of every cache key (Phase
    2 task 6) so that a knowledge-base change — a business rule edited, a
    table added, a new few-shot example — automatically invalidates stale
    cache entries built under the old prefix, instead of silently serving a
    result computed under knowledge that no longer applies. Cached like
    :func:`build_static_prefix`, on the same key, so computing it costs
    nothing beyond the one-time prefix assembly.

    Parameters
    ----------
    system_prompt:
        The domain system prompt text.

    Returns
    -------
    str
        A 12-character hex fingerprint (truncated SHA-256), short enough to
        embed in a cache key without dominating it.

    Examples
    --------
    >>> v1 = prefix_version("You are a T-SQL expert.")
    >>> v2 = prefix_version("You are a T-SQL expert.")
    >>> v1 == v2
    True
    >>> len(v1)
    12
    >>> prefix_version("A different system prompt.") != v1
    True
    """
    digest = hashlib.sha256(build_static_prefix(system_prompt).encode("utf-8")).hexdigest()
    return digest[:12]


def should_use_static_prefix(system_prompt: str) -> bool:
    """True when the static prefix fits ``cfg.settings.prompt_retrieval_token_budget``.

    This is the single gate between the two prompt-assembly paths: below
    budget (today's 12-table schema, always), :class:`~prompt_engine.builder.PromptBuilder`
    uses the static, cacheable prefix; at or above it, a future larger
    schema falls back to the six-retriever pipeline in :mod:`retrieval`
    instead of shipping an ever-growing static prompt on every request.

    Parameters
    ----------
    system_prompt:
        The domain system prompt text.

    Returns
    -------
    bool

    Examples
    --------
    >>> should_use_static_prefix("You are a T-SQL expert.")
    True

    A pathologically small budget forces the retrieval fallback even for
    today's schema:

    >>> import config as cfg
    >>> from config import override_settings
    >>> with override_settings(prompt_retrieval_token_budget=1):
    ...     should_use_static_prefix("You are a T-SQL expert.")
    False
    """
    budget = cfg.settings.prompt_retrieval_token_budget
    if budget <= 0:
        return False
    return static_prefix_token_estimate(system_prompt) <= budget
