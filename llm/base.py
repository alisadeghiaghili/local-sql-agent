"""Abstract base for all LLM backends.

To add a new backend (OpenAI, Anthropic, vLLM, …) implement ``LLMBackend``
and register it in ``llm/sql_agent.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SQLGenerationResult:
    """Outcome of one LLM call."""

    sql: str
    """Cleaned, sanitised SQL string."""

    raw_response: str
    """Verbatim text the model returned (useful for debugging)."""

    attempt: int = 1
    """Which attempt produced this result (1 = first try, 2+ = correction)."""

    correction_prompts: list[str] = field(default_factory=list)
    """The correction prompts sent in previous failed attempts."""


class LLMBackend(ABC):
    """Contract every LLM backend must satisfy.

    Backends are stateless — all state lives in the prompt.
    ``generate`` is the only required method; it must:

    * Accept a complete prompt string.
    * Return the model's raw text output (no cleaning).
    * Raise ``RuntimeError`` on unrecoverable transport/API failures.
    * Raise ``ValueError("OUT_OF_SCOPE")`` when the model signals it cannot
      answer (pass the sentinel through unchanged).
    """

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Send *prompt* to the model and return raw text."""
        ...

    @property
    def name(self) -> str:
        """Human-readable identifier shown in logs and REPL header."""
        return type(self).__name__
