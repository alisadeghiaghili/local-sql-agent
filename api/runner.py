"""Core request handler — wires SQLAgent, executor, and interpreter together.

Kept separate from ``server.py`` so it can be called from both the
FastAPI app and the CLI (``app.py``) without importing FastAPI.
"""

from __future__ import annotations

import logging
from typing import Literal

import config as cfg
from llm.sql_agent import SQLAgent
from api.models import QueryResponse

logger = logging.getLogger(__name__)

# Module-level agent (shared across requests; stateless)
_agent = SQLAgent()

_INTERPRET_TEMPLATE = """
You are a helpful data analyst. The user asked the following question:

{question}

The database returned these results (up to 20 rows shown):

{rows}

Write a concise one-paragraph summary in the same language as the question.
Do not repeat column names literally — describe the findings in plain language.
If the result is empty, say so clearly.
"""


def run_query(
    question: str,
    system_prompt: str,
    mode: Literal["sql", "result", "full"] = "full",
    interpret: bool = False,
) -> QueryResponse:
    """Run the full pipeline and return a :class:`QueryResponse`.

    Raises
    ------
    ValueError("OUT_OF_SCOPE") | ValueError | RuntimeError
        Propagated from SQLAgent / executor for the caller to handle.
    """

    # --- sql-only mode: skip execution entirely ---
    if mode == "sql":
        from llm.base import SQLGenerationResult
        from retrieval.context_retriever import ContextRetriever
        from prompt_engine.builder import PromptBuilder
        from security.sql_guard import clean_sql, validate_sql

        context = ContextRetriever.retrieve(question)
        prompt = PromptBuilder.build(
            question=question,
            system_prompt=system_prompt,
            context=context,
        )
        raw = _agent._backend.generate(prompt)
        sql = clean_sql(raw)
        validate_sql(sql)
        return QueryResponse(
            question=question,
            sql=sql,
            model=_agent._backend.name,
        )

    # --- result / full mode: generate + execute (with self-correction) ---
    df, result = _agent.run(question, system_prompt)

    rows: list[dict] = df.to_dict(orient="records")

    interpretation: str | None = None
    if interpret and mode in ("result", "full"):
        interpretation = _interpret(question, rows)

    return QueryResponse(
        question=question,
        sql=result.sql if mode == "full" else None,
        result=rows,
        interpretation=interpretation,
        row_count=len(rows),
        correction_attempts=result.attempt,
        model=_agent._backend.name,
    )


def _interpret(question: str, rows: list[dict]) -> str:
    """Ask the LLM to summarise the result in plain language."""
    # Show at most 20 rows to keep the prompt short
    preview = rows[:20]
    rows_text = "\n".join(str(r) for r in preview)
    if not rows_text:
        rows_text = "(empty result set)"

    prompt = _INTERPRET_TEMPLATE.format(
        question=question,
        rows=rows_text,
    )
    try:
        return _agent._backend.generate(prompt).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Interpretation failed: %s", exc)
        return ""
