# -*- coding: utf-8 -*-
"""
Secure Configuration Module for Local SQL Agent (Gemma 3 via Ollama + SQL Server).

Usage:
    from config import get_ollama_config, get_sqlserver_uri
"""

import os
import logging
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


def load_env_file(env_path: str = ".env") -> None:
    """Load environment variables from .env file."""
    if not os.path.exists(env_path):
        logger.debug(f"No {env_path} found - using system environment variables")
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                if key not in os.environ:
                    os.environ[key] = value
        logger.info(f"Loaded env from {env_path}")
    except Exception as e:
        logger.warning(f"Failed to load {env_path}: {e}")


def validate_no_placeholders(value: str, var_name: str) -> None:
    """Raise ValueError if value looks like a placeholder."""
    placeholders = ["your_", "example", "placeholder", "changeme", "CHANGE_ME"]
    if any(ph in value.lower() for ph in placeholders):
        raise ValueError(
            f"⚠️  {var_name} contains a placeholder: '{value}'\n"
            f"Please update your .env file with actual values."
        )


def get_ollama_config() -> dict:
    """
    Read Ollama config from environment.

    Required:
        OLLAMA_MODEL    : e.g. gemma3:12b
    Optional:
        OLLAMA_BASE_URL    : default http://localhost:11434
        OLLAMA_TEMPERATURE : default 0.2
        OLLAMA_TOP_P       : default 0.95
    """
    model    = os.getenv("OLLAMA_MODEL")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    if not model:
        raise ValueError(
            "❌ Missing: OLLAMA_MODEL\n"
            "Example: OLLAMA_MODEL=gemma3:12b"
        )
    validate_no_placeholders(model, "OLLAMA_MODEL")

    return {
        "model":       model,
        "base_url":    base_url,
        "temperature": float(os.getenv("OLLAMA_TEMPERATURE", "0.2")),
        "top_p":       float(os.getenv("OLLAMA_TOP_P", "0.95")),
    }


def get_sqlserver_uri() -> str:
    """
    Build SQL Server URI from environment variables.

    Required:
        DB_SERVER   : server hostname or IP
        DB_NAME     : database name
        DB_USER     : SQL Server username
        DB_PASSWORD : SQL Server password
    Optional:
        DB_PORT               : default 1433
        DB_DRIVER             : default ODBC Driver 17 for SQL Server
        DB_TRUSTED_CONNECTION : set to yes for Windows Auth
    """
    server   = os.getenv("DB_SERVER")
    database = os.getenv("DB_NAME")
    user     = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    port     = os.getenv("DB_PORT", "1433")
    driver   = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
    trusted  = os.getenv("DB_TRUSTED_CONNECTION", "").lower()

    missing = []
    if not server:   missing.append("DB_SERVER")
    if not database: missing.append("DB_NAME")
    if not user:     missing.append("DB_USER")
    if not password and trusted != "yes":
        missing.append("DB_PASSWORD  (or set DB_TRUSTED_CONNECTION=yes)")

    if missing:
        raise ValueError(
            f"❌ Missing required env vars: {', '.join(missing)}\n"
            f"Please set them in your .env file."
        )

    driver_encoded   = quote_plus(driver)
    user_encoded     = quote_plus(user)
    password_encoded = quote_plus(password) if password else ""

    if trusted == "yes":
        uri = (
            f"mssql+pyodbc://{user_encoded}@{server}:{port}/{database}"
            f"?driver={driver_encoded}&trusted_connection=yes"
        )
    else:
        uri = (
            f"mssql+pyodbc://{user_encoded}:{password_encoded}"
            f"@{server}:{port}/{database}"
            f"?driver={driver_encoded}"
        )

    logger.debug("SQL Server URI built successfully")
    return uri


def print_config_status() -> None:
    """Print configuration status (passwords masked) for debugging."""
    print("\n" + "=" * 60)
    print("Configuration Status")
    print("=" * 60)

    env_exists = os.path.exists(".env")
    print(f"\n.env file: {'✓ Found' if env_exists else '✗ Not found'}")
    if not env_exists:
        print("  → Copy .env.example to .env and configure")

    print("\nOllama / LLM:")
    for var in ["OLLAMA_MODEL", "OLLAMA_BASE_URL", "OLLAMA_TEMPERATURE", "OLLAMA_TOP_P"]:
        val = os.getenv(var)
        print(f"  {var}: {val or '✗ Not set'}")

    print("\nSQL Server:")
    for var in ["DB_SERVER", "DB_PORT", "DB_NAME", "DB_USER",
                "DB_DRIVER", "DB_TRUSTED_CONNECTION", "DB_PASSWORD"]:
        val = os.getenv(var)
        if val and "PASSWORD" in var:
            print(f"  {var}: {'*' * min(len(val), 8)}")
        else:
            print(f"  {var}: {val or '✗ Not set'}")

    print("\n" + "=" * 60 + "\n")


# Auto-load .env when module is imported
load_env_file()


if __name__ == "__main__":
    print("Testing configuration...\n")
    try:
        print_config_status()
        cfg = get_ollama_config()
        print(f"✓ Ollama: model={cfg['model']}, url={cfg['base_url']}")
        uri = get_sqlserver_uri()
        print("✓ SQL Server URI built successfully")
    except ValueError as e:
        print(f"\n❌ Configuration Error:\n{e}\n")
        exit(1)
