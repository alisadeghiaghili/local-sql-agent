"""Pydantic request / response models for the API."""

from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

_MAX_QUESTION_LEN: int = int(os.getenv("MAX_QUESTION_LENGTH", "1000"))
_MIN_QUESTION_LEN: int = 2


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    """Body sent to ``POST /query``."""

    question: str = Field(
        ...,
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

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        v = v.strip()
        if len(v) < _MIN_QUESTION_LEN:
            raise ValueError(
                f"Question must be at least {_MIN_QUESTION_LEN} characters."
            )
        if len(v) > _MAX_QUESTION_LEN:
            raise ValueError(
                f"Question must not exceed {_MAX_QUESTION_LEN} characters."
            )
        return v


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

class QueryResponse(BaseModel):
    """Body returned by ``POST /query``."""

    question: str = Field(description="The original question, echoed back.")
    sql: str | None = Field(
        default=None,
        description="Generated T-SQL. Present for mode='sql' and mode='full'.",
    )
    result: list[dict[str, Any]] | None = Field(
        default=None,
        description="Result rows. Present for mode='result' and mode='full'.",
    )
    interpretation: str | None = Field(
        default=None,
        description="LLM plain-language summary. Present only when interpret=true.",
    )
    row_count: int = Field(default=0)
    correction_attempts: int = Field(
        default=1,
        description="How many generation attempts were needed (1 = no correction).",
    )
    elapsed_seconds: float = Field(default=0.0)
    model: str = Field(default="")


# ---------------------------------------------------------------------------
# Error envelope (returned by all exception handlers)
# ---------------------------------------------------------------------------

class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    path: str


class ErrorResponse(BaseModel):
    """Shape of every error body — documented in Swagger."""
    error: ErrorDetail


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    ollama: bool
    database: bool
    model: str
