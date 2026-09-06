# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Runtime configuration — all values read from environment / .env.

Never hardcode credentials here.
Copy .env.example → .env and fill in real values.

Usage::

    import config as cfg
    print(cfg.settings.openai_model)

Testing::

    Use ``override_settings()`` to safely replace the singleton in tests.
    Because every consumer accesses ``cfg.settings`` at call-time (not at
    import-time), the patch is visible to ALL modules immediately.

        with override_settings(max_rows_returned=5):
            ...  # every cfg.settings access sees the patched value

Three layers, not two
----------------------
This codebase separates three kinds of thing that are easy to lump
together into "configuration":

1. **Engine** — the code itself (``api/``, ``llm/``, ``retrieval/``,
   ``security/``, ...). No domain names, no tuning literals: it must work
   unmodified against any warehouse.
2. **Domain data** — what a *particular* warehouse *is*: its tables,
   columns, business rules, aliases, few-shot examples
   (``project_config/*.yaml``, loaded by :mod:`knowledge.config_loader`
   and :mod:`schema_data.registry`). Swapping warehouses means swapping
   this directory, never editing engine code.
3. **Tuning** — how aggressively the engine retrieves, matches, caches,
   and retries against *this* warehouse and *this* hardware. This module.

A module-level numeric constant elsewhere in the source is not
automatically "tuning" just because layer 3 exists. Sorting a candidate
constant into one of three buckets:

* **Tuning** — a knob an operator might legitimately want different for
  their warehouse or their hardware (a bigger cache, a longer timeout, a
  looser regression tolerance on slower CI hardware, ...). Becomes a
  ``Settings`` field: ``field(default_factory=lambda: os.getenv(...))``,
  documented, read through ``cfg.settings`` at call time so
  ``override_settings()`` reaches it in tests. See
  ``eval_max_accuracy_drop_pct`` / ``eval_max_latency_p95_increase_pct`` /
  ``eval_max_guard_rejection_increase`` below for an example promoted out
  of ``eval/baseline.py`` under exactly this reasoning: how much
  latency/accuracy regression is tolerable is a property of *this*
  deployment's hardware and traffic, not a fixed technical fact.
* **Invariant** — a value that is part of the design's *correctness* and
  must not be tuned, even though it happens to be a number. Stays a
  source constant, with a docstring saying why it is deliberately not
  configurable. The clearest example is
  :data:`security.auth.MIN_KEY_LENGTH` (32): lowering it weakens a
  security property (structural entropy enforced once, at key-issue
  time), so exposing it as an env-overridable knob would let a deployment
  quietly weaken its own auth by setting one variable. Also in this
  bucket: :data:`eval.determinism.MIN_REPEATS` (a statistical floor —
  fewer than 2 repeats cannot measure determinism at all, it is not a
  "less thorough" setting) and :data:`eval.fingerprint.DEFAULT_FLOAT_PRECISION`
  (a golden set's ``expected_fingerprint`` values are hashed at a fixed
  precision; making this env-tunable would silently desynchronise a
  deployment's environment from its own golden set's pinned hashes,
  turning "regression" into "someone's shell profile" with no error at
  either end).
* **Implementation detail** — a value nobody outside the module cares
  about, or one whose only meaningful comparison is against itself /
  against a value already tunable elsewhere. Stays put, no docstring
  justification required beyond the ordinary one.
  :data:`prompt_engine.static_prefix._CHARS_PER_TOKEN` lives here: it is
  a rough characters-per-token heuristic (no real tokenizer dependency),
  used only in comparisons against itself and against
  :attr:`prompt_retrieval_token_budget` below — a deployment whose text
  tokenizes at a different real ratio (e.g. Persian vs English) already
  has the one knob it needs in :attr:`prompt_retrieval_token_budget`;
  giving the ratio its own env var would add a second dial over the same
  effective threshold rather than a genuinely independent one.
  :data:`database.schema_inspector._MAX_SAMPLE_LEN` is the same kind of
  thing one level further out: a display-truncation length inside the
  interactive, untested, coverage-excluded setup wizard
  (``setup_project.py``), never read by the running engine at all.

This rule is enforced, not just documented: ``tests/test_tuning_layer.py``
walks first-party source for a *new* bare module-level numeric constant
outside its allowlist and fails the build, the same way
``tests/test_persian_normalization.py`` already does for a second
translation table built the same way ``core.persian`` builds its own.
See that new test's module docstring for exactly which existing constants
are allowlisted and why — several are legitimately tuning-shaped but
already have a more appropriate, narrower configuration surface than a
process-wide environment variable (e.g. the eval CLI's own
``--determinism-repeats`` flag; see :data:`eval.determinism.DEFAULT_REPEATS`)
and were deliberately left alone rather than duplicated into ``Settings``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Generator

# ---------------------------------------------------------------------------
# .env loading — must happen before Settings() is ever constructed below,
# so that os.getenv() calls in the field default_factories see values from
# .env.  This is the single, earliest point every entry point (app.py,
# api/server.py, tests) goes through, since they all ``import config``.
#
# python-dotenv is a required dependency (see requirements.txt), but we
# degrade gracefully instead of crashing the whole application if it is
# ever missing from an environment (e.g. a minimal container image that
# only sets real environment variables and has no .env file at all).
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - exercised only when dependency missing
    pass


#: Origins the bundled ``web/`` UI is documented to be served from, and the
#: default for :attr:`Settings.cors_allowed_origins`.
#:
#: This used to default to empty -- the most restrictive choice, and the
#: wrong one, because the layout this project *documents* is the API on one
#: port and the static UI on another. A browser on 8080 calling 8000 is
#: cross-origin, so the out-of-the-box experience was a preflight answered
#: "400 Disallowed CORS origin", which every browser reports to the page as
#: the uninformative "Failed to fetch". The UI showed API, LLM and DB all
#: down while the CLI, which is not a browser and never sends an Origin,
#: worked perfectly against the same server.
#:
#: Loopback only, and that is what makes it safe to default: a page cannot
#: choose the ``Origin`` the browser sends, so allowing these helps only a
#: page genuinely served from this machine's own 8080 -- and every route it
#: could then reach still requires an API key. Anything else, including any
#: non-loopback host, still has to be named explicitly.
DEFAULT_CORS_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://localhost:8080",
    "http://127.0.0.1:8080",
)


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings resolved from environment variables."""

    # ── LLM provider (OpenAI-compatible endpoint only) ──────────────────
    openai_base_url: str = field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    """Base URL of the single, trivial-case OpenAI-compatible endpoint
    (:class:`~llm.endpoints.EndpointConfig` named ``"default"``) — a local
    ``gpt-oss``/vLLM/llama.cpp server, or OpenAI's own hosted API. See
    ``LLM_ENDPOINTS``/``LLM_ROUTES`` (:attr:`llm_endpoints_json` /
    :attr:`llm_routes_json`) for routing across more than one endpoint."""

    openai_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    )
    """Model tag sent to :attr:`openai_base_url`."""

    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    """Bearer token for :attr:`openai_base_url`. Empty is valid — many
    self-hosted OpenAI-compatible servers don't check it."""

    db_connection_url: str = field(
        default_factory=lambda: os.getenv(
            "DB_CONNECTION_URL",
            "mssql+pyodbc://username@server:1433/Auction_DM"
            "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes",
        )
    )
    sql_dialect: str = field(
        default_factory=lambda: os.getenv("SQL_DIALECT", "tsql")
    )
    """The single SQL dialect this deployment targets, as a
    `sqlglot <https://sqlglot.com/>`_ dialect key -- one of ``"tsql"``
    (default), ``"postgres"``, ``"mysql"``, ``"sqlite"`` (see
    :data:`security.dialects.DIALECT_PROFILES`). Resolved once from config
    at start-up, the same way :class:`~llm.router.LLMRouter` resolves an
    endpoint -- this is portability (one dialect per deployment), not
    runtime routing across several live databases.

    SQL generation itself is unaffected by this setting: the model always
    generates ``tsql`` regardless (see ``prompts/system_prompt.md`` and the
    multi-dialect phase report for why the static prompt prefix must stay
    byte-identical across every deployment). When this resolves to
    anything other than ``"tsql"``, ``llm.sql_agent.SQLAgent`` transpiles
    the guard-approved ``tsql`` SQL to this dialect with sqlglot and
    re-validates the **transpiled** text with
    :func:`~security.sql_guard.validate_sql` pinned to this dialect before
    executing it -- never executing anything the guard has not approved in
    the dialect it will actually run in. When this is ``"tsql"`` (the
    default), that transpile-and-revalidate step is skipped entirely and
    the guard-approved SQL is executed exactly as produced, unchanged from
    this deployment's original, single-dialect behaviour.

    Validated at start-up by :meth:`validate` via
    :func:`security.dialects.require_dialect_supported`, which fails
    closed for an unknown dialect or one with no system-catalogue
    blocklist configured -- see that function's docstring."""
    query_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("QUERY_TIMEOUT_SECONDS", "60"))
    )
    max_rows_returned: int = field(
        default_factory=lambda: int(os.getenv("MAX_ROWS_RETURNED", "1000"))
    )
    default_top_n: int = field(
        default_factory=lambda: int(
            os.getenv("DEFAULT_TOP_N", os.getenv("MAX_ROWS_RETURNED", "1000"))
        )
    )
    """Row cap injected by :func:`security.sql_guard.ensure_top` into
    generated SQL that has no ``TOP``/row-limit clause of its own.
    Defaults to whatever ``MAX_ROWS_RETURNED`` resolves to (the same
    number the client-side ``fetchmany`` cap already uses), so the two
    caps agree unless ``DEFAULT_TOP_N`` is set independently."""
    log_dir: str = field(
        default_factory=lambda: os.getenv("LOG_DIR", "logs")
    )
    export_dir: str = field(
        default_factory=lambda: os.getenv("EXPORT_DIR", "exports")
    )
    project_config_dir: str = field(
        default_factory=lambda: os.getenv("PROJECT_CONFIG_DIR", "project_config")
    )
    """Directory :mod:`knowledge.config_loader` and :mod:`schema_data.registry`
    read their YAML files from (``aliases.yaml``, ``entities.yaml``,
    ``business_rules.yaml``, ``examples.yaml``, ``metrics.yaml``,
    ``schema.yaml``). Defaults to ``project_config`` — the git-ignored
    directory holding this deployment's real domain data and warehouse
    schema. Point this at ``project_config.example`` to run against the
    committed, sample-data template instead (a fresh clone with no
    ``project_config/`` at all, or CI).

    Deliberately **not** an automatic fallback: when this resolves to
    ``project_config`` (the default) and that directory or one of its
    files is missing, loading still raises ``ConfigNotFoundError`` exactly
    as before — silently running on sample aliases/business rules against
    a real warehouse would produce confidently wrong SQL, which is worse
    than refusing to start. The example directory is only ever reached by
    explicitly setting this variable."""
    # ── query result cache ────────────────────────────────────────────────
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("CACHE_TTL_SECONDS", "300"))
    )
    """How long (seconds) a cached query result stays valid.  0 = disabled."""

    cache_max_size: int = field(
        default_factory=lambda: int(os.getenv("CACHE_MAX_SIZE", "256"))
    )
    """Maximum number of distinct (question, mode) pairs to keep in memory."""

    # ── JSONL log rotation ──────────────────────────────────────────────────
    log_max_bytes: int = field(
        default_factory=lambda: int(os.getenv("LOG_MAX_BYTES", str(50 * 1024 * 1024)))
    )
    """Size cap (bytes) for a JSONL log file (``query_log.jsonl``,
    ``audit_log.jsonl``) before it is rotated. ``<= 0`` disables rotation
    (the file grows without bound). Read at call time by
    ``logs.logger._rotation_settings()``, per this project's
    read-through-``cfg.settings``-at-call-time convention -- this field was
    previously read directly via ``os.getenv`` in ``logs/logger.py``
    because ``config.py`` was locked by concurrent work when that module
    was written; it is free now, so the field lives here like every other
    setting.

    Raised from ``10 MiB`` alongside :attr:`log_backup_count` below —
    together they bound how much audit history a rotation can ever discard
    (see that field's docstring for the arithmetic and the incident that
    prompted it). Raise this further on a deployment with materially higher
    traffic than the ~1 KB/record this project has actually observed;
    lower it only if disk space is the tighter constraint than audit
    history, which for a compliance/analysis log is rarely the right
    trade."""

    log_backup_count: int = field(
        default_factory=lambda: int(os.getenv("LOG_BACKUP_COUNT", "20"))
    )
    """Number of rotated log backups to retain. ``<= 0`` keeps no history
    (the file is cleared in place instead of shifted to ``.1`` on
    rotation). See :attr:`log_max_bytes` for why this lives here rather
    than behind a direct ``os.getenv`` call.

    Raised from ``5`` together with :attr:`log_max_bytes`'s five-fold
    increase (``10 MiB -> 50 MiB``): a first production deployment's
    ``audit_log.jsonl`` is the only source this project has ever had for
    real accuracy/latency numbers, and losing its earliest records to a
    rotation nobody was watching would be silent and unrecoverable —
    there is no second chance at "the first week". Observed audit records
    average roughly 1 KB each (per-stage timings, the full ``llm`` status
    block, guard verdict); the old ``10 MiB x 5`` = 50 MiB ceiling (about
    50,000 records total) could plausibly be exhausted within the first
    production week by traffic well short of anything unusual, especially
    stacked on top of whatever pre-deployment dev/test traffic already
    shares the same file. The new ``50 MiB x 20`` = 1 GiB ceiling (on the
    order of a million records) is not "unbounded" — a genuinely runaway
    write loop still eventually rotates its oldest history away rather
    than filling the disk forever — but comfortably outlasts any real
    single-organisation deployment's first weeks. Reassess this number
    once real production volume is known; do not simply raise it further
    "to be safe" without knowing the actual record rate, since that trades
    away the runaway-growth backstop for no measured benefit."""

    # ── Phase 2: static prompt prefix / prefix-cache latency win ────────────
    prompt_retrieval_token_budget: int = field(
        default_factory=lambda: int(os.getenv("PROMPT_RETRIEVAL_TOKEN_BUDGET", "6000"))
    )
    """Token-estimate threshold (see ``prompt_engine.static_prefix.estimate_tokens``)
    above which :class:`~prompt_engine.builder.PromptBuilder` falls back to
    per-question retrieval instead of the static, byte-identical prefix. The
    knowledge base measured at Phase 2 kickoff (system prompt + full schema +
    relationships + business rules + examples) is ~4.6k real tokens, comfortably
    under this default — today's 12-table schema always takes the static path.
    A larger schema added in a later phase can exceed the budget and
    transparently falls back to the six-retriever pipeline (still exercised,
    never dead code) instead of blowing up the prompt or losing accuracy."""

    # ── Phase 2: deterministic decoding (docs/api-contract-v2.md §6) ───────
    llm_temperature: float = field(
        default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.0"))
    )
    """Sampling temperature sent to the backend. ``0.0`` (the default) makes
    decoding deterministic given a fixed seed — required so that the same
    question produces byte-identical SQL on every run."""

    llm_top_p: float = field(
        default_factory=lambda: float(os.getenv("LLM_TOP_P", "1.0"))
    )
    """Nucleus-sampling cutoff. ``1.0`` disables nucleus sampling, which
    matters for determinism only when combined with ``llm_temperature=0.0``
    and a fixed ``llm_seed``."""

    llm_seed: int = field(
        default_factory=lambda: int(os.getenv("LLM_SEED", "7"))
    )
    """Fixed sampling seed, sent on every request. Honoured by vLLM and
    llama.cpp; accepted but not guaranteed-deterministic by OpenAI's own
    hosted API (see its ``system_fingerprint`` field) — this project sends
    it regardless (never harmful) and reports whether the endpoint *claims*
    to honour it in the ``llm`` status block's ``seed_honored`` rather than
    asserting determinism this process cannot verify on its own. Combined
    with ``temperature=0`` and a genuinely deterministic endpoint, this is
    what makes ``the same question, run twice, produce byte-identical SQL``
    (a Phase 2 exit criterion) rather than merely "usually similar"."""

    llm_num_predict: int = field(
        default_factory=lambda: int(os.getenv("LLM_NUM_PREDICT", "512"))
    )
    """Bounded max completion length (sent as ``max_tokens``). A generated
    SQL statement is a few dozen tokens at most; capping this bounds worst-case
    decode latency and protects against a runaway generation looping forever."""

    llm_stop: tuple[str, ...] = field(default_factory=tuple)
    """Optional stop sequences appended to every request's ``stop`` field.
    Empty by default — most models terminate cleanly at the SQL statement's
    natural end; set this only if a specific model needs an explicit fence."""

    # ── Phase 2: query-result cache normalisation ───────────────────────────
    cache_normalize_questions: bool = field(
        default_factory=lambda: os.getenv("CACHE_NORMALIZE_QUESTIONS", "true").lower()
        not in ("0", "false", "no")
    )
    """When true (default), cache keys are built from a normalised form of
    the question (collapsed whitespace, Persian/Arabic digits folded to
    ASCII, ZWNJ stripped, ي/ك folded to ی/ک) so trivially different
    phrasings of the same question share a cache entry. See
    ``api.query_cache._normalize_question``."""

    # ── Phase 2: LLM router / endpoint-trust governance ─────────────────────
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "openai")
    )
    """One of ``"openai"`` (the default — route through :mod:`llm.endpoints`)
    or ``"mock"`` (every task answered by :class:`~llm.providers.MockBackend`,
    no endpoint configuration needed — for tests and offline runs). See
    ``llm.router.LLMRouter.from_settings``."""

    llm_allow_remote: bool = field(
        default_factory=lambda: os.getenv("LLM_ALLOW_REMOTE", "false").lower()
        in ("1", "true", "yes")
    )
    """Explicit per-deployment opt-in gate for sending schema, business
    rules, or query-result rows to an untrusted (not on this deployment's
    own infrastructure) LLM endpoint. This product's premise is "runs on
    your infrastructure" — pointing ``OPENAI_BASE_URL``/``LLM_ENDPOINTS``
    at a hosted API alone must never be sufficient to start exfiltrating
    data; this flag is the separate, deliberate switch, and every call to
    an untrusted endpoint made while it is true is written to the audit
    trail (see ``llm.router.LLMRouter``). Defaults to ``False``."""

    llm_trusted: bool | None = field(
        default_factory=lambda: (
            {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
            .get(os.getenv("LLM_TRUSTED", "").strip().lower())
        )
    )
    """Explicit trust override for the single ``"default"`` endpoint (see
    :attr:`openai_base_url`). ``None`` (unset — the default) defers to
    :func:`~llm.trust.default_trust_for_url`, which trusts loopback/private/
    ``.local`` addresses and nothing else. Set to ``true``/``false`` to
    override that heuristic explicitly — e.g. a local endpoint reachable
    under a public-looking hostname that should still count as trusted, or
    a loopback endpoint that should not. Endpoints declared in
    :attr:`llm_endpoints_json` each carry their own independent ``trusted``
    override instead of sharing this one."""

    llm_endpoints_json: str = field(
        default_factory=lambda: os.getenv("LLM_ENDPOINTS", "")
    )
    """Additional named endpoints beyond the trivial ``"default"`` one, as
    a JSON array of ``{"name", "base_url", "model", "api_key", "trusted"}``
    objects (``api_key``/``trusted`` optional). Empty (the default) means
    only ``"default"`` exists. See :mod:`llm.endpoints`."""

    llm_routes_json: str = field(
        default_factory=lambda: os.getenv("LLM_ROUTES", "")
    )
    """Per-task endpoint fallback chains, as a JSON object mapping a
    :class:`~llm.router.TaskType` value (``"sql_generation"``,
    ``"interpretation"``, ``"assumption_extraction"``) to a list of
    endpoint names tried in order. Empty (the default) routes every task to
    ``["default"]`` alone. See :mod:`llm.endpoints`."""

    llm_task_budget_seconds: float | None = field(
        default_factory=lambda: (
            float(os.getenv("LLM_TASK_BUDGET_SECONDS", ""))
            if os.getenv("LLM_TASK_BUDGET_SECONDS", "").strip()
            else None
        )
    )
    """Per-task latency budget (seconds) applied uniformly to every
    :class:`~llm.router.TaskType` by :meth:`~llm.router.LLMRouter.from_settings`.
    ``None`` (default, unset) disables the budget check entirely — see
    ``llm.router.LLMRouter._call_chain``."""

    # ── Phase 3: conversational sessions (docs/api-contract-v2.md §9) ──────
    session_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("SESSION_TTL_SECONDS", "1800"))
    )
    """Idle-expiry window (seconds) for an in-memory conversational session
    (``session.store.SessionStore``). A session not touched (no new turn,
    no transcript read) for this long is evicted, freeing its cached
    turns/memory. ``0`` disables sessions entirely (every lookup misses)."""

    session_max_count: int = field(
        default_factory=lambda: int(os.getenv("SESSION_MAX_COUNT", "500"))
    )
    """Maximum number of concurrent sessions kept in memory. Beyond this,
    the least-recently-active session is evicted to make room for a new
    one — the same LRU discipline ``api.query_cache.QueryCache`` already
    applies to cached responses."""

    session_max_turns: int = field(
        default_factory=lambda: int(os.getenv("SESSION_MAX_TURNS", "50"))
    )
    """Transcript cap per session. Beyond this many turns, the oldest turn
    (and its session-memory sidecar) is dropped from the session so a
    long-lived conversation cannot grow without bound."""

    session_prompt_turns: int = field(
        default_factory=lambda: int(os.getenv("SESSION_PROMPT_TURNS", "3"))
    )
    """How many of the most recent turns are rendered into the prompt's
    session-context block (question, SQL, result *column names* — never
    row data; see ``docs/api-contract-v2.md`` §8). Older turns fall out of
    the prompt window but remain readable via ``GET /v2/sessions/{sid}``."""

    refinement_scan_cap: int = field(
        default_factory=lambda: int(
            os.getenv(
                "REFINEMENT_SCAN_CAP",
                str(int(os.getenv("MAX_ROWS_RETURNED", "1000")) * 100),
            )
        )
    )
    """§2's inner-scan bound: when a refining turn drops the previous
    turn's display ``TOP`` to compute a result over *all* matching rows
    (not just the ones previously shown), the resulting unbounded scan is
    re-capped at this many rows instead of being left unbounded. Defaults
    to ``max_rows_returned * 100``, mirroring ``default_top_n``'s own
    fallback-to-``MAX_ROWS_RETURNED`` pattern above."""

    # ── Phase 9: session persistence + cross-session memory ─────────────────
    session_store_path: str = field(
        default_factory=lambda: os.getenv("SESSION_STORE_PATH", "logs/sessions.db")
    )
    """SQLite file backing :class:`session.persistence.SessionPersistence`
    (question, generated SQL, result column names/row_count/truncated, the
    ``TurnMemory`` sidecar, and memory entries — never result rows; see that
    module's docstring). Defaults to ``logs/sessions.db`` — ``*.db`` is
    already gitignored, and ``logs/`` is where the audit log already lives,
    so this introduces no new category of stored data. Empty string
    disables persistence entirely: ``session.store.SessionStore`` then
    behaves exactly as it did before this phase (TTL expiry deletes, no
    rehydration, ``GET /v2/sessions`` reflects only the in-memory hot set).
    Read once, at ``api.v2_routes.get_session_store``'s lazy-construction
    time, like every other singleton-constructor setting in this codebase
    (``session_ttl_seconds``, ``session_max_count``, ...)."""

    session_retention_days: int = field(
        default_factory=lambda: int(os.getenv("SESSION_RETENTION_DAYS", "30"))
    )
    """How long (days) a persisted conversation stays listable and
    reopenable — the third, previously-conflated lifetime alongside
    :attr:`session_prompt_turns` (the prompt window) and
    :attr:`session_ttl_seconds` (how long the in-memory record stays hot).
    ``session.store.SessionStore.purge_expired`` permanently deletes a
    persisted session past this many days since its ``last_active_at``;
    it does not affect the (much shorter) in-memory TTL, which only
    demotes a session out of memory, never deletes it, when persistence is
    attached."""

    session_title_max_length: int = field(
        default_factory=lambda: int(os.getenv("SESSION_TITLE_MAX_LENGTH", "80"))
    )
    """Cap on a session title's length — both the auto-derived title (the
    first question, truncated at a word boundary) and a title supplied via
    ``PATCH /v2/sessions/{sid}``. A title is presentation only: it is
    validated under the same length/control-character rules as a memory
    value (see :attr:`memory_value_max_length`) precisely because it is
    user text of the same shape, but it never enters a prompt."""

    memory_enabled: bool = field(
        default_factory=lambda: os.getenv("MEMORY_ENABLED", "true").lower()
        not in ("0", "false", "no")
    )
    """Master switch for cross-session memory (``docs/api-contract-v2.md``
    §5). When ``False``, a stored entry is never applied to a turn's
    assumptions/filters (``session.engine.TurnEngine.ask`` skips
    :func:`session.memory.apply_memory_to_assumptions` entirely) — the
    ``GET``/``PUT``/``DELETE /v2/memory*`` endpoints themselves are
    unaffected, so an operator can disable the feature's *effect* without
    losing an analyst's already-pinned entries."""

    memory_max_entries_per_principal: int = field(
        default_factory=lambda: int(os.getenv("MEMORY_MAX_ENTRIES_PER_PRINCIPAL", "20"))
    )
    """Cap on how many distinct memory keys one principal may have pinned
    at once. Exceeding it on ``PUT /v2/memory/{key}`` (a *new* key; updating
    an already-pinned key never counts against this cap) is an explicit
    422, never a silent eviction of an older entry — see
    ``docs/api-contract-v2.md`` §5."""

    memory_value_max_length: int = field(
        default_factory=lambda: int(os.getenv("MEMORY_VALUE_MAX_LENGTH", "120"))
    )
    """Default per-key cap on a memory value's length, in
    ``project_config/memory_policy.yaml``'s absence of a narrower
    ``max_length`` for that specific key. A memory value is untrusted text
    that reaches the prompt's variable suffix (never the static prefix),
    so it is also rejected outright for a newline or control character —
    see ``session.memory.validate_memory_value``."""

    cors_allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
        )
        or DEFAULT_CORS_ALLOWED_ORIGINS
    )
    """Origins allowed to call this API cross-origin (``CORSMiddleware`` in
    ``api/server.py``), comma-separated.

    Defaults to :data:`DEFAULT_CORS_ALLOWED_ORIGINS` — the loopback
    origins the bundled ``web/`` UI is documented to be served from.
    Setting the variable replaces that list entirely rather than adding
    to it, so a real deployment naming its own origin does not silently
    keep localhost allowed as well.

    Same-origin requests never need CORS at all, so this only matters for
    the split-origin layout ``web/README.md`` and
    ``docs/fa/getting-started.md`` describe: the API on one port, the
    static UI on another."""

    llm_structured_output: bool = field(
        default_factory=lambda: os.getenv("LLM_STRUCTURED_OUTPUT", "false").lower()
        in ("1", "true", "yes")
    )
    """Phase 2 task 3 feature flag: when ``True``, SQL generation asks for
    a single constrained JSON object (``{"sql", "out_of_scope",
    "confidence", "assumptions"}``, see ``llm.structured_schema``) instead
    of free text plus ``clean_sql`` regex surgery. Defaults to ``False``
    until the golden-set evaluation (``python -m eval.cli run --live
    --structured``) demonstrates it does not regress accuracy — see the
    Phase 2 report for whether that evaluation has been run yet."""

    # ── Deployment readiness: per-(principal, ip) rate limiting ─────────────
    # Previously ``api/middleware.py``'s own module-level ``os.getenv()``
    # reads, evaluated once at that module's import time -- which is why
    # ``tests/conftest.py`` had to set these env vars *before* anything
    # imported ``api.middleware`` at all (see that file's own comment).
    # Moved here so the values are read through ``cfg.settings`` at call
    # time like every other tuning knob (see this module's "Three layers,
    # not two" section) -- ``RateLimitMiddleware.__init__`` now resolves
    # them itself when it is actually constructed, so
    # ``config.override_settings(rate_limit_requests=...)`` reaches a
    # middleware instance built with no explicit constructor kwargs, not
    # just one a test hand-configures. See ``api/middleware.py``'s module
    # docstring for why the *shared* ``api.server.app`` instance still
    # needs its generous test-suite value in place before the first
    # request of a whole pytest session, not merely "at some point before
    # a given test" -- Starlette builds and caches that app's middleware
    # stack exactly once, on the first request it ever serves.
    rate_limit_requests: int = field(
        default_factory=lambda: int(os.getenv("RATE_LIMIT_REQUESTS", "600"))
    )
    """Sustained request allowance per :attr:`rate_limit_window_seconds`,
    per rate-limit bucket (see :attr:`rate_limit_burst` for capacity above
    this, and ``api.middleware.RateLimitMiddleware._bucket_key`` for what a
    "bucket" is).

    Raise this if legitimate traffic is getting 429s under normal use
    (more concurrent analysts, or a UI that legitimately fires several
    requests per interaction); lower it if a single caller should be
    throttled harder than this — e.g. a deployment that expects only a
    handful of analysts and wants a tighter ceiling on a misbehaving
    client or a scripting mistake.

    Old default was ``60`` — chosen back when the bucket was keyed on raw
    IP alone. In the shape this product actually ships in (one web UI,
    one shared service key, ten-plus analysts sitting behind it), the
    bucket key becomes ``principal:<id>|ip:<ip>`` (see
    ``RateLimitMiddleware._bucket_key``'s docstring) but a single web UI
    still means every analyst using it shares **one IP** too — so ``60``
    req/min was, in practice, 60 requests per minute for the *entire
    organisation*, not per analyst. Ten people each asking one question
    every few seconds blew through it. ``600`` (10 req/sec sustained)
    assumes up to roughly 30 analysts each firing an interactive query —
    a human clicking "ask" every few seconds, not a batch client — at
    once: 30 analysts x one request every 3s is exactly 600/min. A
    deployment with a different expected head-count should raise or
    lower this proportionally rather than trust the default blindly."""

    rate_limit_window_seconds: float = field(
        default_factory=lambda: float(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
    )
    """Length (seconds) of the sliding window :attr:`rate_limit_requests`
    refills over. Unchanged from the original default (``60``) — the
    *rate* (:attr:`rate_limit_requests` per this many seconds) is what
    needed raising for real deployment traffic, not the window length
    itself; a shorter window makes the same requests/window ratio bursts
    tighter over any sub-window, a longer one loosens it, but ``60`` is a
    reasonable "per minute" unit for a human operator reading a 429 body
    to reason about either way."""

    rate_limit_burst: int = field(
        default_factory=lambda: int(os.getenv("RATE_LIMIT_BURST", "40"))
    )
    """Extra tokens above :attr:`rate_limit_requests` a bucket can spend
    instantly (bucket capacity = ``rate_limit_requests + rate_limit_burst``
    — see ``api.middleware.RateLimitMiddleware``). Old default was ``10``.
    Raised to ``40`` alongside :attr:`rate_limit_requests`'s five-fold
    increase so the burst allowance scales with it — enough to absorb a
    web UI firing a handful of parallel calls on one page load (e.g. an
    initial dashboard fetch) without immediately eating into the
    sustained rate, while staying far short of "unlimited": a genuinely
    runaway or scripted caller still exhausts 640 tokens (600 + 40) in
    well under a minute at any real request rate and starts getting 429s,
    it just is not punished for one legitimate burst."""

    # ── Admin panel, phase 2: the application database ──────────────────────
    app_db_url: str = field(
        default_factory=lambda: os.getenv("APP_DB_URL", "")
    )
    """A SQLAlchemy URL for the application database — the key store, role
    grants, and (see :mod:`session.persistence`) conversational sessions.
    Empty (the default) falls back to a SQLite file at
    :attr:`app_db_sqlite_path`, created automatically. A configured value
    is expected to name a database that **already exists** — this
    application creates tables inside it, never the database itself (see
    ``docs/admin-panel-architecture.md`` §5.3): ``CREATE DATABASE`` needs
    rights a DBA will not grant an application.

    Must never resolve to the same server+database as
    :attr:`db_connection_url` — :func:`appdb.engine.raise_if_same_database`
    is checked at start-up (``api/server.py``'s ``lifespan``) and refuses to
    start otherwise, because the warehouse connection is deliberately
    read-only (``docs/db-hardening.md``) and the application database needs
    writes."""

    app_db_sqlite_path: str = field(
        default_factory=lambda: os.getenv("APP_DB_SQLITE_PATH", "logs/app.db")
    )
    """SQLite file used when :attr:`app_db_url` is unset. Defaults to
    ``logs/app.db`` — alongside ``logs/sessions.db`` and the audit log,
    introducing no new category of stored-data location. ``*.db`` is
    already gitignored (see ``tests/test_no_runtime_artifacts_tracked.py``)."""

    key_cache_ttl_seconds: float = field(
        default_factory=lambda: float(os.getenv("KEY_CACHE_TTL_SECONDS", "5.0"))
    )
    """How long (seconds) the in-memory API-key cache
    (:mod:`appdb.key_store`) serves a database-backed key set before
    re-querying. Keys move into the application database precisely so a
    revoked/disabled key can be shut off without a restart
    (``docs/admin-panel-architecture.md`` §5.5/§5.6) — reading the key
    table on every single request would mean a network round trip to the
    application database per request, so this cache exists to avoid that.
    Every mutation (issue/disable/enable/revoke/ACL change/role grant)
    invalidates the cache explicitly rather than waiting out this TTL, so
    it bounds only the *unforced* staleness window — a revocation always
    takes effect on the very next request regardless of this value. Kept
    short by default because it is a safety-relevant staleness bound, not
    a raw performance knob; raise it only if the application database is
    under measured load from key lookups alone."""

    # ── Phase 8: API-key authentication ─────────────────────────────────────
    api_keys_json: str = field(
        default_factory=lambda: os.getenv("API_KEYS_JSON", "")
    )
    """JSON array of ``{"id", "name", "key_sha256", "denied_columns"?}``
    objects — the deployment's configured API keys. Only the SHA-256 hex
    digest of a key is ever stored here, never the raw key itself; issue
    new keys with ``scripts/issue_api_key.py``. Parsed at call time by
    :func:`security.auth.load_api_keys`, per this module's
    read-through-``cfg.settings``-at-call-time convention. Empty (the
    default) means no caller can authenticate — see :attr:`auth_required`
    for what that implies at startup."""

    auth_required: bool = field(
        default_factory=lambda: os.getenv("AUTH_REQUIRED", "true").lower()
        not in ("0", "false", "no")
    )
    """Whether every non-``/health`` route requires a valid API key.
    Defaults to ``True`` — this is a fail-closed system, not a fail-open
    one. Setting this to ``False`` is a deliberate escape hatch (local
    development, an isolated network with its own perimeter security)
    that ``api/server.py``'s ``lifespan`` logs a ``WARNING`` for on
    *every* startup, not just the first, so a silently-disabled front
    door is never quiet in the logs. When ``True`` and :attr:`api_keys_json`
    resolves to zero configured keys, ``lifespan`` raises ``RuntimeError``
    instead of starting a server nobody could ever authenticate to."""

    app_docs_public: bool = field(
        default_factory=lambda: os.getenv("APP_DOCS_PUBLIC", "false").lower()
        in ("1", "true", "yes")
    )
    """When ``False`` (the default), ``/docs``, ``/redoc``, and
    ``/openapi.json`` require the same authentication as every other
    non-``/health`` route — the generated API documentation describes
    exactly what a caller can do to production data, which is not
    something to publish to an unauthenticated network. Set ``True`` to
    serve them without credentials (e.g. a deployment that already sits
    behind its own perimeter auth)."""

    # ── Phase 5b: deterministic value resolution ────────────────────────────
    resolve_value_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("RESOLVE_VALUE_TIMEOUT_SECONDS", "2.0"))
    )
    """Per-(table, column) deadline for ``retrieval.value_resolver.resolve_value``'s
    database round trip. Deliberately much tighter than
    :attr:`query_timeout_seconds` (which bounds a full analytic query) —
    this is a small lookup on the critical path *before* SQL generation
    even starts, so a breach falls back to the no-match path (see
    ``resolve_value``'s docstring) rather than blocking the request for as
    long as a real query is allowed to run."""

    resolve_value_max_concurrency: int = field(
        default_factory=lambda: int(os.getenv("RESOLVE_VALUE_MAX_CONCURRENCY", "8"))
    )
    """How many value-resolution queries may be in flight at once,
    process-wide. This is the bound a ``ThreadPoolExecutor``'s
    ``max_workers`` used to provide, before
    ``retrieval.value_resolver._run_under_deadline`` replaced that pool
    with daemon threads (see its docstring for why it had to). Read at
    call time, so it responds to ``override_settings``. Waiting for a
    free slot spends the caller's own
    :attr:`resolve_value_timeout_seconds`: a saturated resolver must
    report a miss on time rather than queue past its deadline."""

    resolve_value_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("RESOLVE_VALUE_CACHE_TTL_SECONDS", "300"))
    )
    """How long (seconds) a cached entity-value resolution stays valid.
    ``<= 0`` disables the cache. Dimension tables (customers, brokers,
    symbols, ...) change slowly, so this can reasonably sit much longer
    than an individual request without serving stale data in practice."""

    resolve_value_cache_max_size: int = field(
        default_factory=lambda: int(os.getenv("RESOLVE_VALUE_CACHE_MAX_SIZE", "512"))
    )
    """Maximum number of distinct ``(mention, table, column, scope_key)``
    resolutions kept in memory before LRU eviction — mirrors
    :attr:`cache_max_size`'s discipline for the unrelated query-result
    cache."""

    dimension_vocabulary_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("DIMENSION_VOCABULARY_TTL_SECONDS", "3600"))
    )
    """How long (seconds) a small dimension's prefetched value set
    (``retrieval.dimension_vocabulary``) is served without triggering a
    refresh. Unlike :attr:`resolve_value_cache_ttl_seconds`, this is
    **not** a per-request latency knob and, since the stale-while-revalidate
    redesign, it is **not an expiry either**: a cached value past this TTL
    is still served (a stale trading-hall or commodity name beats no name
    at all) while a background refresh for it is triggered — see that
    module's docstring's "Stale-while-revalidate, warmed lazily" section.
    ``<= 0`` makes every read stale immediately (a background refresh
    fires on every match that consults it, rate-limited on failure by
    :data:`retrieval.dimension_vocabulary._BACKGROUND_REFRESH_BACKOFF_SECONDS`),
    which is the closest this module comes to "disabled" — it still stores
    and serves whatever was last fetched rather than discarding it."""

    dimension_vocabulary_warm_on_startup: bool = field(
        default_factory=lambda: os.getenv(
            "DIMENSION_VOCABULARY_WARM_ON_STARTUP", "false",
        ).lower() in ("1", "true", "yes")
    )
    """When ``True``, ``api/server.py``'s ``lifespan`` calls
    ``retrieval.dimension_vocabulary.warm_all`` once during startup, before
    the server begins accepting requests.

    Its meaning changed with the stale-while-revalidate redesign: the
    vocabulary cache is no longer inert without this flag — a cold or
    stale entry now self-heals via a background refresh triggered from the
    request path itself (never awaited, so no request blocks on it; see
    ``retrieval.dimension_vocabulary``'s module docstring). This flag is
    now a pure **optimisation**: it pre-pays the very first cold-cache miss
    for each dimension at startup instead of leaving it to whichever
    request happens to ask about that dimension first. A deployment that
    never sets this still gets working, self-healing dimension resolution
    — just with one extra miss per dimension after each restart, and (per
    the TTL note above) after each TTL expiry regardless.

    Defaults to ``False`` so this codebase's test suite — which builds a
    ``TestClient(app)`` against no live database in dozens of existing
    tests, and whose ``lifespan`` has made zero database calls up to this
    phase — is unaffected until an operator deliberately opts in for a
    real deployment. A warm-up failure (e.g. the database is unreachable
    at startup) is logged as a warning, never raised — unlike the
    auth/config checks above this one, an empty vocabulary cache degrades
    every dimension it would have covered to "no match" (this phase's
    universal safe-miss behaviour), not a server that cannot start at
    all."""

    # ── Evaluation harness: golden-set regression gate (see eval/baseline.py) ─
    eval_max_accuracy_drop_pct: float = field(
        default_factory=lambda: float(os.getenv("EVAL_MAX_ACCURACY_DROP_PCT", "5.0"))
    )
    """Percentage-point drop in ``accuracy_pct`` (current vs baseline) above
    which ``eval.baseline.compare_to_baseline`` treats a run as regressed.
    E.g. ``5.0`` means baseline 90% -> current 84% (a 6-point drop) fails,
    but baseline 90% -> current 86% (a 4-point drop) does not. A per-deployment
    tuning knob rather than a fixed technical fact: how much accuracy
    variance is tolerable is a product decision that can reasonably differ
    across warehouses. Still overridable per invocation via
    ``python -m eval.cli run --max-accuracy-drop-pct``, which takes
    precedence when passed explicitly."""

    eval_max_latency_p95_increase_pct: float = field(
        default_factory=lambda: float(os.getenv("EVAL_MAX_LATENCY_P95_INCREASE_PCT", "20.0"))
    )
    """Relative percentage increase in ``latency_p95`` (current vs baseline)
    above which a run is considered regressed. E.g. ``20.0`` means a
    baseline p95 of 2.0s tolerates up to 2.4s before failing. Slower or
    more variable hardware legitimately wants a looser tolerance here than
    a deployment on fast, dedicated hardware -- this is the textbook
    "how aggressively to retry/tolerate for *this* hardware" tuning knob.
    Overridable per invocation via
    ``python -m eval.cli run --max-latency-p95-increase-pct``."""

    eval_max_guard_rejection_increase: int = field(
        default_factory=lambda: int(os.getenv("EVAL_MAX_GUARD_REJECTION_INCREASE", "0"))
    )
    """Absolute increase in ``guard_rejections`` (current vs baseline) above
    which a run is considered regressed. Defaults to ``0`` -- any new guard
    rejection versus baseline is treated as safety-relevant and flagged,
    since it means SQL that used to pass the security guard no longer does
    (or the generator started producing worse SQL). Overridable per
    invocation via ``python -m eval.cli run --max-guard-rejection-increase``."""

    eval_golden_path: str = field(
        default_factory=lambda: os.getenv("EVAL_GOLDEN_PATH", "eval_data/golden.jsonl")
    )
    """Path to the golden-set ``.jsonl`` file the admin panel's config-version
    dry-run (``docs/admin-panel-architecture.md`` §6.2, phase 3 spec §5)
    runs a candidate ``project_config/`` bundle against before it can be
    applied. Read at call time by :mod:`appdb.config_versions`, the same
    ``eval_data/golden.jsonl`` a deployment already maintains for
    ``python -m eval.cli run`` -- this setting exists so the panel can find
    it without a request body needing to name a filesystem path. Point this
    at ``eval_data.example/golden.jsonl`` (alongside ``PROJECT_CONFIG_DIR``)
    to run the dry-run against the committed example data instead."""

    eval_baseline_path: str = field(
        default_factory=lambda: os.getenv("EVAL_BASELINE_PATH", "eval_data/baseline.json")
    )
    """Path to the baseline JSON file ``python -m eval.cli run --save-baseline``
    writes (:mod:`eval.baseline`). Read at call time by the admin panel's
    feedback-loop stats endpoint (``docs/admin-panel-architecture.md`` §3's
    "closing the loop visibly" -- phase 4 spec §5) to show the golden set's
    most recently recorded accuracy alongside its size and flag volume,
    without re-running the harness against a live endpoint on every panel
    load. A missing file (no baseline has been recorded yet) degrades to
    "no baseline recorded" rather than an error -- this setting exists so
    the panel can find the file without a request needing to name a
    filesystem path, the same reasoning as ``eval_golden_path`` above."""

    config_version_cache_ttl_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("CONFIG_VERSION_CACHE_TTL_SECONDS", "5")
        )
    )
    """How long :mod:`appdb.config_versions` may reuse a cached active
    version id before re-reading it, mirroring ``key_cache_ttl_seconds``
    and existing for the same reason: the id is on the per-request path
    (``api/runner.py`` folds it into every query-cache key, so an applied
    configuration version invalidates stale answers), and reading it from
    the application database on every question would be a network round
    trip per request on an external backend.

    Applying a version invalidates this cache explicitly, so a change is
    visible immediately in the process that made it; the TTL is what bounds
    staleness in *other* processes, where the only cost of being late is a
    few cache hits that should have been misses. Set to ``0`` to disable
    the cache and read the id every time."""

    config_export_dir: str = field(
        default_factory=lambda: os.getenv("CONFIG_EXPORT_DIR", "")
    )
    """Directory :mod:`appdb.config_versions` writes the ``project_config/``
    YAML bundle to on every applied version (spec §7) -- offline inspection
    with familiar tools, and an off-box backup, without this system's
    correctness depending on git being installed. Empty (the default)
    disables the export write entirely; a deployment that wants it points
    this at a directory of its own -- optionally one under version control
    with its own remote (``docs/admin-panel-architecture.md`` §6.3: "git as
    an output, not as the engine" -- never this project's own repository)."""

    # ── Admin panel, phase 5: migration between backends ────────────────────
    migration_quiet_window_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("MIGRATION_QUIET_WINDOW_SECONDS", "60")
        )
    )
    """How recent a write to the application database may be before
    :mod:`appdb.migrate` refuses to run (``docs/admin-panel-architecture.md``
    §5.4/§5.4.1, §3 tier 3's not-yet-built maintenance mode). The tool scans
    every migrated table's own timestamp columns (``created_at``,
    ``updated_at``, ...) for the most recent one and compares its age
    against this window; anything younger means the application was still
    writing a moment ago and the copy this tool is about to take may not
    include everything, with no error at the time it is lost (§7).

    ``60`` is a deliberately generous default: this check exists to catch
    "the operator forgot to stop the application", not to shave seconds off
    a maintenance window, and a false refusal here costs an operator one
    re-run a minute later while a false pass risks silently dropped writes.
    A deployment confident in its own shutdown discipline may lower it; one
    whose application takes longer than a minute to fully quiesce should
    raise it instead of routinely overriding the refusal by other means."""

    def validate(self) -> None:
        """Raise ValueError if any required setting is missing or still a placeholder.

        Two independent checks guard ``db_connection_url``: an exact match
        against ``placeholders`` (single unfilled tokens such as
        ``"change_me"``) and a substring check for ``"username@server"``,
        the literal host baked into this module's own factory default (see
        ``db_connection_url``'s ``default_factory`` above). The default is
        a *full connection string*, not a bare token, so it never equals
        any entry in ``placeholders`` and used to pass validation silently
        whenever a user copied ``.env.example`` and forgot to fill in
        ``DB_CONNECTION_URL``.
        """
        placeholders = {
            "your_password_here", "your_server_here",
            "your_db_here", "change_me", "",
        }
        if not self.openai_model or self.openai_model in placeholders:
            raise ValueError("OPENAI_MODEL is not configured")
        if not self.db_connection_url or self.db_connection_url in placeholders:
            raise ValueError("DB_CONNECTION_URL is not configured")
        if "username@server" in self.db_connection_url.lower():
            raise ValueError(
                "DB_CONNECTION_URL still has the factory-default placeholder "
                "host (username@server) — set a real connection string in .env"
            )
        # Fail closed for an unconfigured/unsupported SQL_DIALECT -- an
        # unknown dialect, or one whose profile has no system-catalogue
        # blocklist, must never reach request-serving code (see
        # security.dialects.require_dialect_supported's docstring for why
        # an empty blocklist is refused rather than treated as "nothing to
        # block"). Deferred import: security.dialects has no dependency on
        # config, but importing it at module scope here would still make
        # every `import config` pay for importing security.dialects even
        # when nothing ever calls validate() -- matches this module's own
        # "read at call time" convention for cross-module dependencies.
        from security.dialects import require_dialect_supported

        require_dialect_supported(self.sql_dialect)

        # Catch the "set SQL_DIALECT but forgot to update DB_CONNECTION_URL"
        # misconfiguration -- a mismatch here means every single query
        # would fail at execution (or, worse, be silently misinterpreted),
        # so this fails closed the same way the placeholder checks above
        # do, rather than starting a server that will simply not work.
        # Parses the connection URL string only (sqlalchemy.engine.make_url
        # needs no driver import and opens no connection) -- an exotic
        # backend this module has no sqlglot-dialect mapping for is not an
        # error on its own (a different concern to this check), so that
        # case is silently skipped rather than treated as a mismatch.
        from sqlalchemy.engine import make_url

        from security.dialects import sqlglot_dialect_for_backend

        try:
            backend_name = make_url(self.db_connection_url).get_backend_name()
            expected_dialect = sqlglot_dialect_for_backend(backend_name)
        except Exception:  # noqa: BLE001 - an unrecognised backend is a
            # different, unrelated concern -- not something this
            # SQL_DIALECT/DB_CONNECTION_URL consistency check can judge.
            expected_dialect = None
        if expected_dialect is not None and expected_dialect != self.sql_dialect:
            raise ValueError(
                f"SQL_DIALECT={self.sql_dialect!r} does not match "
                f"DB_CONNECTION_URL's backend ({backend_name!r}, which "
                f"this deployment targets as {expected_dialect!r}) -- "
                "every query would fail at execution against the wrong "
                "dialect. Set SQL_DIALECT to match DB_CONNECTION_URL, or "
                "fix DB_CONNECTION_URL to point at the intended database."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (cached after first call)."""
    return Settings()


# Module-level singleton — ALL modules must access settings as ``cfg.settings``
# (i.e. ``import config as cfg``) so that override_settings() patches are
# visible at call-time rather than being captured at import-time.
settings: Settings = get_settings()


@contextmanager
def override_settings(**kwargs: Any) -> Generator[Settings, None, None]:
    """Context manager for tests: temporarily replace ``cfg.settings``.

    Because all consumers read ``cfg.settings`` lazily (not via a local
    ``from config import settings`` binding), every module sees the new
    value for the lifetime of the ``with`` block.

    The original singleton is restored on exit, even on exception.

    Usage::

        import config as cfg
        from config import override_settings

        with override_settings(max_rows_returned=5) as s:
            assert s.max_rows_returned == 5
            assert cfg.settings.max_rows_returned == 5
    """
    import config as _cfg  # always the real module object
    original = _cfg.settings
    patched  = Settings(**{
        **{f: getattr(original, f) for f in original.__slots__},
        **kwargs,
    })
    _cfg.settings = patched
    try:
        yield patched
    finally:
        _cfg.settings = original
