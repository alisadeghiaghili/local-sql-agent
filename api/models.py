"""Pydantic request / response models for the API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    """Body sent to ``POST /query``."""

    question: str = Field(
        ...,
        min_length=2,
        description="Natural-language question in Persian or English.",
        examples=["بالاترین قیمت کشف‌شده گندم در ماه گذشته چقدر بود؟"],
    )
    mode: Literal["sql", "result", "full"] = Field(
        default="full",
        description=(
            "**sql** — return only the generated SQL.\n\n"
            "**result** — execute SQL and return only the data rows.\n\n"
            "**full** — return SQL + data rows + optional interpretation."
        ),
    )
    interpret: bool = Field(
        default=False,
        description=(
            "When *true* and mode is 'result' or 'full', ask the LLM to "
            "produce a one-paragraph plain-language summary of the results. "
            "Ignored when mode='sql'."
        ),
    )


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

class QueryResponse(BaseModel):
    """Body returned by ``POST /query``."""

    question: str = Field(description="The original question, echoed back.")

    sql: str | None = Field(
        default=None,
        description="Generated T-SQL query. Present for mode='sql' and mode='full'.",
    )
    result: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Query result rows as a list of dicts. "
            "Present for mode='result' and mode='full'."
        ),
    )
    interpretation: str | None = Field(
        default=None,
        description=(
            "Plain-language summary of the result produced by the LLM. "
            "Present only when interpret=true and mode != 'sql'."
        ),
    )

    row_count: int = Field(default=0, description="Number of rows returned.")
    correction_attempts: int = Field(
        default=1,
        description="How many generation attempts were needed (1 = no correction required).",
    )
    elapsed_seconds: float = Field(default=0.0, description="Wall-clock time in seconds.")
    model: str = Field(default="", description="LLM model tag used for this request.")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    ollama: bool
    database: bool
    model: str
