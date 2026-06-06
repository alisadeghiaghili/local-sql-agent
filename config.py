"""Runtime configuration — all values read from environment or .env.

Never hardcode credentials here. Copy .env.example → .env and fill in.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
OLLAMA_URL: str = os.getenv(
    "OLLAMA_URL",
    "http://localhost:11434/api/generate",
)
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_CONNECTION_URL: str = os.getenv(
    "DB_CONNECTION_URL",
    "mssql+pyodbc://sa@localhost:1433/Auction_DM"
    "?driver=ODBC+Driver+17+for+SQL+Server",
)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
QUERY_TIMEOUT_SECONDS: int = int(os.getenv("QUERY_TIMEOUT_SECONDS", "60"))
MAX_ROWS_RETURNED: int     = int(os.getenv("MAX_ROWS_RETURNED", "1000"))
