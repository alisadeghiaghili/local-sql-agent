"""Runtime configuration — all values read from environment / .env.

Never hardcode credentials here.
Copy .env.example → .env and fill in real values.

Usage::

    from config import settings
    print(settings.ollama_model)

Testing::

    Use ``override_settings()`` context manager to safely swap settings
    in tests without touching the ``lru_cache`` singleton directly.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Generator


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings resolved from environment variables."""

    ollama_url: str = field(
        default_factory=lambda: os.getenv(
            "OLLAMA_URL", "http://localhost:11434/api/generate"
        )
    )
    ollama_model: str = field(
        default_factory=lambda: os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
    )
    db_connection_url: str = field(
        default_factory=lambda: os.getenv(
            "DB_CONNECTION_URL",
            "mssql+pyodbc://sa@localhost:1433/Auction_DM"
            "?driver=ODBC+Driver+17+for+SQL+Server",
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

    def validate(self) -> None:
        """Raise ValueError if any required setting is missing or still a placeholder."""
        placeholders = {
            "your_password_here", "your_server_here",
            "your_db_here", "change_me", "",
        }
        if not self.ollama_model or self.ollama_model in placeholders:
            raise ValueError("OLLAMA_MODEL is not configured")
        if not self.db_connection_url or self.db_connection_url in placeholders:
            raise ValueError("DB_CONNECTION_URL is not configured")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (cached after first call)."""
    return Settings()


# Module-level convenience alias — import this in all other modules.
settings = get_settings()


@contextmanager
def override_settings(**kwargs: Any) -> Generator[Settings, None, None]:
    """Context manager for tests: temporarily replace the cached Settings.

    Usage::

        with override_settings(max_rows_returned=5) as s:
            assert s.max_rows_returned == 5
            # code under test uses the patched singleton

    All fields not listed in *kwargs* keep their current values.
    The original singleton is restored on exit, even if an exception occurs.
    """
    original = get_settings()
    patched  = Settings(**{**{f: getattr(original, f) for f in original.__slots__}, **kwargs})
    get_settings.cache_clear()
    # Temporarily replace the cached value
    get_settings.__wrapped__   # ensure the function is unwrapped
    # Store patched into the cache by calling through a shim
    import config as _cfg
    _original_fn = _cfg.get_settings

    # Replace module-level alias and cache entry
    _cfg.get_settings.cache_clear()
    # monkey-patch cache to return patched
    _patched_called = False

    @lru_cache(maxsize=1)
    def _patched_get_settings() -> Settings:
        return patched

    _cfg.get_settings = _patched_get_settings  # type: ignore[assignment]
    _cfg.settings     = patched
    try:
        yield patched
    finally:
        _cfg.get_settings = _original_fn       # type: ignore[assignment]
        _cfg.settings     = original
        _original_fn.cache_clear()
