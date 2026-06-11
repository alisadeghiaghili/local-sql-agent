"""Backward-compatible shim — delegates to OllamaBackend for SQL generation only.

``generate_sql`` is a *generation-only* helper: it produces the SQL string
but does **not** execute it against the database.  Callers that need both
generation and execution should use ``SQLAgent.run()`` directly.

Existing callers (tests, app.py) that import ``generate_sql`` continue to
work unchanged.
"""

from __future__ import annotations

from retrieval.context_retriever import ContextRetriever
from prompt_engine.builder import PromptBuilder
from security.sql_guard import clean_sql, validate_sql
from llm.ollama_backend import OllamaBackend

_backend = OllamaBackend()


def generate_sql(question: str, system_prompt: str) -> str:
    """Generate SQL for *question* using the Ollama backend.

    This function only calls the LLM — it does **not** execute the
    generated SQL against any database.

    Raises
    ------
    ValueError("OUT_OF_SCOPE")
        Passed through from the model sentinel.
    RuntimeError
        When the Ollama endpoint is unreachable after all retries.
    """
    context = ContextRetriever.retrieve(question)
    prompt = PromptBuilder.build(
        question=question,
        system_prompt=system_prompt,
        context=context,
    )
    raw = _backend.generate(prompt)
    sql = clean_sql(raw)
    validate_sql(sql)
    return sql
