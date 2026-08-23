"""Runtime configuration — all values read from environment / .env.

Never hardcode credentials here.
Copy .env.example → .env and fill in real values.

Usage::

    import config as cfg
    print(cfg.settings.ollama_model)

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

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover — python-dotenv is in requirements.txt
    load_dotenv = None

if load_dotenv is not None and (Path(__file__).resolve().parent / ".env").is_file():
    # Never overrides real environment variables; only fills gaps.
    load_dotenv(Path(__file__).resolve().parent / ".env")


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings resolved from environment variables."""

    # ── LLM provider ─────────────────────────────────────────────────────
    # "auto" probes the configured providers at runtime and picks the first
    # accessible one, preferring `llm_provider_prefer` (ollama by default).
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "auto")
    )
    llm_provider_prefer: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER_PREFER", "ollama")
    )
    ollama_url: str = field(
        default_factory=lambda: os.getenv(
            "OLLAMA_URL", "http://localhost:11434/api/generate"
        )
    )
    ollama_model: str = field(
        default_factory=lambda: os.getenv("OLLAMA_MODEL", "llama3")
    )
    openai_base_url: str = field(
        default_factory=lambda: os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )
    )
    openai_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    )
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    anthropic_api_key: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )
    anthropic_model: str = field(
        default_factory=lambda: os.getenv(
            "ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"
        )
    )
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

    def validate(self) -> None:
        """Raise ValueError if any required setting is missing or still a placeholder."""
        placeholders = {
            "your_password_here", "your_server_here",
            "your_db_here", "change_me", "",
        }
        if self.llm_provider not in ("auto", "ollama", "openai", "anthropic", "mock"):
            raise ValueError(
                f"LLM_PROVIDER must be one of auto/ollama/openai/anthropic/mock, "
                f"got {self.llm_provider!r}"
            )
        if not self.ollama_model or self.ollama_model in placeholders:
            raise ValueError("OLLAMA_MODEL is not configured")
        if self.llm_provider == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is not configured for provider='openai'")
        if not self.db_connection_url or self.db_connection_url in placeholders:
            raise ValueError("DB_CONNECTION_URL is not configured")


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
