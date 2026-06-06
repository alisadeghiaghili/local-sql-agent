"""Runtime configuration — all values read from environment / .env.

Never hardcode credentials here.
Copy .env.example → .env and fill in real values.

Usage::

    from config import settings
    print(settings.ollama_model)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


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
