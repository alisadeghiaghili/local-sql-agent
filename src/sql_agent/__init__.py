"""sql_agent — core library for local NLQ → SQL Server."""

from .config import Settings
from .db import get_engine
from .llm import call_ollama
from .validator import clean_sql, validate_sql

__all__ = ["Settings", "get_engine", "call_ollama", "clean_sql", "validate_sql"]
__version__ = "0.3.0"
