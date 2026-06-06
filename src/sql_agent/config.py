"""Centralised settings for sql_agent.

No side-effects on import.  Callers create a Settings() instance:

    from sql_agent.config import Settings
    cfg = Settings()          # reads os.environ (after python-dotenv loads .env)
    cfg = Settings.from_env() # explicit factory — same thing, clearer call-site

All legacy helpers (get_ollama_config, get_sqlserver_uri, load_env_file,
print_config_status) are re-exported here for backwards-compatibility so
existing code that imports from config.py continues to work.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# .env loader (kept for backwards-compat; prefer python-dotenv when available)
# ---------------------------------------------------------------------------

def load_env_file(env_path: str = ".env") -> None:
    """Load environment variables from *env_path* (silent if file absent)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
        logger.debug("Loaded env via python-dotenv from %s", env_path)
        return
    except ImportError:
        pass

    if not os.path.exists(env_path):
        logger.debug("No %s found — using system environment variables", env_path)
        return
    try:
        with open(env_path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key   = key.strip()
                value = value.strip().strip("'\"")
                os.environ.setdefault(key, value)
        logger.info("Loaded env from %s", env_path)
    except OSError as exc:
        logger.warning("Failed to load %s: %s", env_path, exc)


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    """All runtime settings resolved from environment variables."""

    # -- Ollama ---------------------------------------------------------------
    ollama_model:       str   = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", ""))
    ollama_base_url:    str   = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
    ollama_temperature: float = field(default_factory=lambda: float(os.getenv("OLLAMA_TEMPERATURE", "0.1")))
    ollama_top_p:       float = field(default_factory=lambda: float(os.getenv("OLLAMA_TOP_P", "0.9")))

    # -- SQL Server -----------------------------------------------------------
    db_server:     str = field(default_factory=lambda: os.getenv("DB_SERVER", ""))
    db_name:       str = field(default_factory=lambda: os.getenv("DB_NAME", ""))
    db_user:       str = field(default_factory=lambda: os.getenv("DB_USER", ""))
    db_password:   str = field(default_factory=lambda: os.getenv("DB_PASSWORD", ""))
    db_port:       str = field(default_factory=lambda: os.getenv("DB_PORT", "1433"))
    db_driver:     str = field(default_factory=lambda: os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server"))
    db_trusted:    str = field(default_factory=lambda: os.getenv("DB_TRUSTED_CONNECTION", "").lower())

    # -- SQLite (local testing) -----------------------------------------------
    sqlite_db_path: str = field(default_factory=lambda: os.getenv("SQLITE_DB_PATH", "sample.db"))

    # -------------------------------------------------------------------------

    @classmethod
    def from_env(cls, env_path: str = ".env") -> "Settings":
        """Load .env then return a fully-populated Settings instance."""
        load_env_file(env_path)
        return cls()

    def validate(self) -> None:
        """Raise ValueError listing every missing / placeholder value."""
        _PLACEHOLDERS = {"your_", "example", "placeholder", "changeme", "change_me"}

        def _bad(val: str) -> bool:
            return not val or any(p in val.lower() for p in _PLACEHOLDERS)

        errors: list[str] = []
        if _bad(self.ollama_model):
            errors.append("OLLAMA_MODEL")
        if _bad(self.db_server):
            errors.append("DB_SERVER")
        if _bad(self.db_name):
            errors.append("DB_NAME")
        if _bad(self.db_user):
            errors.append("DB_USER")
        if self.db_trusted != "yes" and _bad(self.db_password):
            errors.append("DB_PASSWORD (or set DB_TRUSTED_CONNECTION=yes)")

        if errors:
            raise ValueError(
                f"❌ Missing / invalid env vars: {', '.join(errors)}\n"
                f"Copy .env.example → .env and fill in your values."
            )

    def sqlserver_uri(self) -> str:
        """Return a fully-encoded SQLAlchemy mssql+pyodbc URI."""
        driver_enc = quote_plus(self.db_driver)
        user_enc   = quote_plus(self.db_user)
        if self.db_trusted == "yes":
            return (
                f"mssql+pyodbc://{user_enc}@{self.db_server}:{self.db_port}/{self.db_name}"
                f"?driver={driver_enc}&trusted_connection=yes"
            )
        pass_enc = quote_plus(self.db_password)
        return (
            f"mssql+pyodbc://{user_enc}:{pass_enc}"
            f"@{self.db_server}:{self.db_port}/{self.db_name}"
            f"?driver={driver_enc}"
        )

    def print_status(self) -> None:
        """Print config status to stdout (passwords masked)."""
        print("\n" + "=" * 60)
        print("Configuration Status")
        print("=" * 60)
        env_exists = os.path.exists(".env")
        print(f"\n.env file: {'\u2713 Found' if env_exists else '\u2717 Not found'}")
        if not env_exists:
            print("  → Copy .env.example to .env and configure")
        print("\nOllama / LLM:")
        for k, v in [
            ("OLLAMA_MODEL",       self.ollama_model),
            ("OLLAMA_BASE_URL",    self.ollama_base_url),
            ("OLLAMA_TEMPERATURE", self.ollama_temperature),
            ("OLLAMA_TOP_P",       self.ollama_top_p),
        ]:
            print(f"  {k}: {v or '\u2717 Not set'}")
        print("\nSQL Server:")
        for k, v in [
            ("DB_SERVER",              self.db_server),
            ("DB_PORT",                self.db_port),
            ("DB_NAME",                self.db_name),
            ("DB_USER",                self.db_user),
            ("DB_DRIVER",              self.db_driver),
            ("DB_TRUSTED_CONNECTION",  self.db_trusted),
            ("DB_PASSWORD",            "*" * min(len(self.db_password), 8) if self.db_password else ""),
        ]:
            print(f"  {k}: {v or '\u2717 Not set'}")
        print("\n" + "=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Backwards-compatible helpers (used by config.py at root and agents/main.py)
# ---------------------------------------------------------------------------

def get_ollama_config() -> dict:
    """Legacy helper — returns dict identical to the old config.py output."""
    cfg = Settings()
    if not cfg.ollama_model:
        raise ValueError(
            "❌ Missing: OLLAMA_MODEL\nExample: OLLAMA_MODEL=gemma3:12b"
        )
    return {
        "model":       cfg.ollama_model,
        "base_url":    cfg.ollama_base_url,
        "temperature": cfg.ollama_temperature,
        "top_p":       cfg.ollama_top_p,
    }


def get_sqlserver_uri() -> str:
    """Legacy helper — validates and returns the SQL Server URI string."""
    cfg = Settings()
    cfg.validate()
    return cfg.sqlserver_uri()


def print_config_status() -> None:
    """Legacy helper — delegates to Settings.print_status()."""
    Settings().print_status()


# ---------------------------------------------------------------------------
# CLI self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    load_env_file()
    print("Testing configuration...\n")
    try:
        cfg = Settings()
        cfg.print_status()
        cfg.validate()
        print(f"✓ Ollama: model={cfg.ollama_model}, url={cfg.ollama_base_url}")
        print("✓ SQL Server URI built successfully")
    except ValueError as exc:
        print(f"\n❌ Configuration Error:\n{exc}\n")
        raise SystemExit(1)
