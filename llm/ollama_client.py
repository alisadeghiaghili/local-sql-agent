"""Backward-compatible shim — delegates to SQLAgent + OllamaBackend.

Existing callers (app.py) that import ``generate_sql`` continue to work
unchanged.  New code should use ``SQLAgent`` directly.
"""

from __future__ import annotations

from llm.ollama_backend import OllamaBackend
from llm.sql_agent import SQLAgent

_agent = SQLAgent(backend=OllamaBackend())


def generate_sql(question: str, system_prompt: str) -> str:
    """Legacy entry point — returns only the SQL string.

    Raises
    ------
    ValueError("OUT_OF_SCOPE")
        Passed through from the model.
    RuntimeError
        When execution fails after all self-correction attempts.
    """
    df, result = _agent.run(question, system_prompt)
    return result.sql
