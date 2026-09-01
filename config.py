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
        default_factory=lambda: int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
    )
    """Size cap (bytes) for a JSONL log file (``query_log.jsonl``,
    ``audit_log.jsonl``) before it is rotated. ``<= 0`` disables rotation
    (the file grows without bound). Read at call time by
    ``logs.logger._rotation_settings()``, per this project's
    read-through-``cfg.settings``-at-call-time convention -- this field was
    previously read directly via ``os.getenv`` in ``logs/logger.py``
    because ``config.py`` was locked by concurrent work when that module
    was written; it is free now, so the field lives here like every other
    setting."""

    log_backup_count: int = field(
        default_factory=lambda: int(os.getenv("LOG_BACKUP_COUNT", "5"))
    )
    """Number of rotated log backups to retain. ``<= 0`` keeps no history
    (the file is cleared in place instead of shifted to ``.1`` on
    rotation). See :attr:`log_max_bytes` for why this lives here rather
    than behind a direct ``os.getenv`` call."""

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

    cors_allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
        )
    )
    """Origins allowed to call this API cross-origin (``CORSMiddleware`` in
    ``api/server.py``). Empty by default — most restrictive: no cross-origin
    caller is allowed until an operator explicitly lists one or more origins
    (comma-separated), e.g. ``CORS_ALLOWED_ORIGINS=http://localhost:8080``
    for the bundled ``web/`` UI served locally. Same-origin requests never
    need CORS at all, so this only matters for the split-origin deployment
    ``web/README.md`` describes."""

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
