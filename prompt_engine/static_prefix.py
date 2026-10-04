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

One prefix per data source
--------------------------
With several data sources configured, a question is routed to one source
before its prompt is built (:mod:`retrieval.source_selector`), and every
function here takes that ``source``: :func:`build_static_prefix` renders
only the tables, relationships and few-shot examples that can run on it,
and is cached per ``(system_prompt, source)`` -- still byte-identical across
requests for the same source, so each source keeps its own prefix cache.
:func:`should_use_static_prefix` then asks the budget question per source:
a source whose prefix fits ``prompt_retrieval_token_budget`` uses its
static prefix, a larger one uses the retrieval path restricted to its
tables. ``source=None`` (what every single-source deployment passes) is the
whole schema, exactly as before. :func:`log_prompt_paths` states each
source's estimate and path in the log.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from functools import lru_cache

import config as cfg
from knowledge.business_rules import BUSINESS_RULES
from knowledge.examples import EXAMPLES
from knowledge.metrics import METRICS
from prompt_engine.source_scope import examples_for_source, scoped_source
from prompt_engine.templates import STATIC_PREFIX_TEMPLATE
from schema_data.columns import TABLE_COLUMNS
from schema_data.registry import SchemaRegistry

logger = logging.getLogger(__name__)

#: Rough characters-per-token ratio for the heuristic estimator below.
#: There is no tokenizer dependency in this project (no ``tiktoken`` /
#: model-specific vocabulary is available), so this is deliberately a
#: *heuristic*, documented as such everywhere it is used. It is only ever
#: compared against itself (the same estimator on both sides of the
#: token-budget check) or used as a rough per-skill-version constant fed
#: into ``observability.llm_status.build_llm_status``'s cache-hit ratio —
#: never presented as an exact model token count.
#:
#: An implementation detail, not a ``config.Settings`` field: a deployment
#: whose text tokenizes at a different real ratio (e.g. Persian vs
#: English) already has the one knob it needs in
#: ``cfg.settings.prompt_retrieval_token_budget`` -- giving this ratio its
#: own env var would add a second dial over the same effective threshold
#: rather than a genuinely independent one. See ``config.py``'s "Three
#: layers, not two" section.
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


def _all_examples_text(source: str | None = None) -> str:
    """Every few-shot example as a Question/SQL pair, in configured order.

    With *source*, only the examples that can run on that data source.
    """
    parts = [
        f"Question:\n{ex['question']}\n\nSQL:\n{ex['sql']}"
        for ex in (EXAMPLES if source is None else examples_for_source(EXAMPLES, source))
    ]
    return "\n\n".join(parts)


def _full_schema_text(source: str | None = None) -> str:
    """The complete schema for every known table (all 12 today), or for
    every table of data source *source*."""
    return SchemaRegistry.build_schema_context(None, source=source)


def _full_relationships_text(source: str | None = None) -> str:
    """JOIN clauses for every FK edge between every known table, or between
    the tables of data source *source*."""
    tables = (
        list(TABLE_COLUMNS.keys())
        if source is None
        else SchemaRegistry.tables_for_source(source)
    )
    return "\n".join(SchemaRegistry.get_relationships(tables))


@lru_cache(maxsize=32)
def build_static_prefix(system_prompt: str, source: str | None = None) -> str:
    """Assemble the byte-identical static prefix for *system_prompt*.

    Cached (``lru_cache``) on the system prompt text and the data source:
    the prefix depends on nothing else that changes at runtime
    (schema/rules/examples are static module data loaded once at import
    time), so repeated calls with the same arguments — the common case,
    since the system prompt is loaded once at server startup — return the
    exact same string object without re-assembling it. ``maxsize=32``
    allows a handful of distinct system prompts (e.g. across tests) times
    a handful of data sources without unbounded growth.

    Parameters
    ----------
    system_prompt:
        The domain system prompt text (``<PROJECT_CONFIG_DIR>/system_prompt.md``).
    source:
        A data source name, when several are configured: the prefix then
        holds only the tables that live in that source (a table in several
        sources is in each one's prefix), the relationships whose two ends
        are both among them, and the few-shot examples whose SQL reads
        only such tables, under a ``Data source: <name>`` line. Business
        rules and metrics are not table-scoped and are included whole.
        ``None`` (the default, and the only value that does anything in a
        single-source deployment) is the whole schema.

    Returns
    -------
    str
        The complete static prefix: system prompt, business rules,
        metrics, the schema, relationships, and the few-shot examples,
        in that fixed order (``docs/api-contract-v2.md`` §8).

    Raises
    ------
    ValueError
        If several sources are configured and *source* is not one of them.

    Examples
    --------
    >>> prefix = build_static_prefix("You are a T-SQL expert.")
    >>> "You are a T-SQL expert." in prefix
    True
    >>> "Table: Customer" in prefix
    True
    >>> build_static_prefix("You are a T-SQL expert.") is prefix
    True

    With one data source configured a source name changes nothing:

    >>> build_static_prefix("You are a T-SQL expert.", "default") == prefix
    True
    """
    source = scoped_source(source)
    return STATIC_PREFIX_TEMPLATE.format(
        system_prompt=system_prompt,
        business_rules=_all_business_rules_text(),
        metrics=_all_metrics_text(),
        schema=_full_schema_text(source),
        relationships=_full_relationships_text(source),
        examples=_all_examples_text(source),
    )


def static_prefix_token_estimate(system_prompt: str, source: str | None = None) -> int:
    """:func:`estimate_tokens` of :func:`build_static_prefix` for *system_prompt*.

    Parameters
    ----------
    system_prompt, source:
        As for :func:`build_static_prefix`.

    Examples
    --------
    >>> static_prefix_token_estimate("You are a T-SQL expert.") > 0
    True
    """
    return estimate_tokens(build_static_prefix(system_prompt, source))


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


def prefix_version_for_config(system_prompt: str, config_version: int | str) -> str:
    """The prefix version an admin panel config change should be identified
    by (admin panel phase 3 -- ``docs/admin-panel-architecture.md`` §6,
    spec §6): derived from *config_version* -- the ``project_config/``
    bundle's own version identifier
    (:func:`appdb.config_versions.get_active_version_id`) -- so that
    applying a new configuration version always changes the identity this
    function returns, and a setting that is not part of that versioned
    bundle never does.

    This deliberately does NOT replace :func:`prefix_version` (which stays
    exactly as it was: a pure content hash of the assembled prefix, with no
    knowledge of config versioning at all) -- it composes that hash with
    *config_version* instead, so the result carries both properties at
    once: it changes whenever the *bundle* version changes (even if, for a
    file that happens not to feed the prefix, the rendered text is
    unchanged -- see ``appdb.config_versions``'s module docstring on why a
    ``session_policy.yaml``-only edit still bumps the bundle version), and
    it also changes whenever the rendered prefix text itself changes for
    any other reason. Callers that want "an identity for this exact prompt
    cache entry, tied to the configuration that produced it" (the LLM
    status block, the admin panel's pre-apply warning, an audit record's
    provenance -- §6's three bullet points) use this function; callers that
    only ever cared about the raw content hash (existing cache-key
    plumbing) are unaffected and keep calling :func:`prefix_version`
    exactly as before.

    Parameters
    ----------
    system_prompt:
        The domain system prompt text.
    config_version:
        The active ``project_config/`` bundle's version identifier. Any
        hashable, string-formattable value -- an integer version number in
        production, or a fixed sentinel in a test that only cares whether
        two calls agree or disagree.

    Returns
    -------
    str
        A 12-character hex fingerprint.

    Examples
    --------
    >>> v1 = prefix_version_for_config("You are a T-SQL expert.", 1)
    >>> v2 = prefix_version_for_config("You are a T-SQL expert.", 1)
    >>> v1 == v2
    True

    Moves when the config version does, even with the same system prompt:

    >>> v3 = prefix_version_for_config("You are a T-SQL expert.", 2)
    >>> v3 != v1
    True

    Does not move for a change that never reaches this function at all --
    an "unrelated setting" is, by construction, anything that is neither
    *system_prompt* nor *config_version*:

    >>> prefix_version_for_config("You are a T-SQL expert.", 1) == v1
    True
    """
    combined = f"{config_version}:{prefix_version(system_prompt)}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]


def should_use_static_prefix(system_prompt: str, source: str | None = None) -> bool:
    """True when the static prefix fits ``cfg.settings.prompt_retrieval_token_budget``.

    This is the single gate between the two prompt-assembly paths: below
    budget (today's 12-table schema, always), :class:`~prompt_engine.builder.PromptBuilder`
    uses the static, cacheable prefix; at or above it, a future larger
    schema falls back to the six-retriever pipeline in :mod:`retrieval`
    instead of shipping an ever-growing static prompt on every request.

    With several data sources the budget applies to each source's own
    prefix, not to the sum: pass *source* and the estimate is that
    source's :func:`build_static_prefix`.

    Parameters
    ----------
    system_prompt:
        The domain system prompt text.
    source:
        The data source the prompt is for; ``None`` is the whole schema.

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
    return static_prefix_token_estimate(system_prompt, source) <= budget


#: Prompts whose per-source paths were already logged (by a hash of the
#: system prompt), so :func:`log_prompt_paths` says it once per process.
_logged_prompt_paths: set[str] = set()
_logged_lock = threading.Lock()


def log_prompt_paths(system_prompt: str) -> None:
    """Log, once per process, each data source's prefix estimate and prompt path.

    With several data sources the budget decision is made per source, and
    the estimate is a heuristic (:func:`estimate_tokens`), so an operator
    needs to see the numbers rather than infer them: one INFO line per
    source says its estimated static-prefix tokens, the budget, and which
    path its prompts take (the cacheable static prefix, or retrieval
    restricted to its tables). Does nothing with a single data source, and
    on every call after the first for the same *system_prompt*. Never
    raises: a failure to describe the paths must not stop a request.

    Parameters
    ----------
    system_prompt:
        The domain system prompt text.

    Examples
    --------
    >>> log_prompt_paths("You are a T-SQL expert.")  # one source: silent
    """
    import database.datasources as datasources

    try:
        names = datasources.datasource_names()
        if len(names) < 2:
            return
        key = prefix_version(system_prompt)
        with _logged_lock:
            if key in _logged_prompt_paths:
                return
            _logged_prompt_paths.add(key)
        budget = cfg.settings.prompt_retrieval_token_budget
        for name in names:
            estimate = static_prefix_token_estimate(system_prompt, name)
            tables = len(SchemaRegistry.tables_for_source(name))
            if should_use_static_prefix(system_prompt, name):
                path = "static prefix (cacheable)"
            else:
                path = "retrieval restricted to its tables"
            logger.info(
                "Prompt path for data source %r: %s -- %d table(s), static prefix "
                "estimate %d tokens, PROMPT_RETRIEVAL_TOKEN_BUDGET %d (per source)",
                name, path, tables, estimate, budget,
            )
    except Exception:  # noqa: BLE001 - describing the paths must never fail a request
        logger.exception("Could not describe the per-source prompt paths")


def _reset_logged_prompt_paths_for_testing() -> None:
    """Forget which prompts :func:`log_prompt_paths` already described. Test-only."""
    with _logged_lock:
        _logged_prompt_paths.clear()
