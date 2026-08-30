"""Pydantic request / response models for the Auction NLQ API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# /query
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Natural-language question (Persian or English)",
    )
    mode: Literal["sql", "result", "full"] = Field(
        default="full",
        description="'sql'=generate only, 'result'=execute only, 'full'=both",
    )
    interpret: bool = Field(
        default=False,
        description="If True, add a plain-language summary of the result rows",
    )

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be blank")
        return v


class QueryResponse(BaseModel):
    question: str
    sql: str | None = None
    result: list[dict[str, Any]] | None = None
    interpretation: str | None = None
    row_count: int | None = None
    correction_attempts: int | None = None
    elapsed_seconds: float | None = None
    model: str | None = None
    llm: dict[str, Any] | None = Field(
        default=None,
        description=(
            "docs/api-contract-v2.md §6 LLM status block (prompt_tokens, "
            "prefix_cache_hit, timings, ...). The LLM endpoint returns this data on "
            "every call; surfacing it here is what makes Phase 2's latency "
            "work observable from the API response, not just the audit log."
        ),
    )


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    openai: bool
    database: bool
    model: str | None = None


# ---------------------------------------------------------------------------
# /cache/*
# ---------------------------------------------------------------------------

class CacheStatsResponse(BaseModel):
    """Snapshot of cache metrics returned by /cache/stats and /cache/clear."""
    hits: int = Field(..., description="Total cache hits since last restart")
    misses: int = Field(..., description="Total cache misses since last restart")
    evictions: int = Field(..., description="Entries evicted by TTL or LRU")
    size: int = Field(..., description="Current number of entries in cache")
    enabled: bool = Field(..., description="False when TTL=0 (cache disabled)")


class CacheInvalidateRequest(BaseModel):
    """Body for POST /cache/invalidate."""
    question: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Exact question string to evict",
    )
    mode: Literal["sql", "result", "full"] = Field(
        default="full",
        description="Cache mode key used when the entry was stored",
    )
