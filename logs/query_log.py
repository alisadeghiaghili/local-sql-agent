# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""QueryLog dataclass — one structured record per engine invocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Status = Literal["SUCCESS", "ERROR", "OUT_OF_SCOPE"]


@dataclass(slots=True)
class QueryLog:
    """Immutable-ish record of a single NLQ round-trip."""

    timestamp:              datetime
    question:               str
    generated_sql:          str
    model_name:             str
    status:                 Status
    execution_time_seconds: float
    row_count:              int        = 0
    excel_file:             str | None = field(default=None)
    error_message:          str | None = field(default=None)

    def as_dict(self) -> dict:
        """Return a JSON-serialisable dict (timestamp as ISO string)."""
        return {
            "timestamp":              self.timestamp.isoformat(),
            "question":               self.question,
            "generated_sql":          self.generated_sql,
            "model_name":             self.model_name,
            "status":                 self.status,
            "execution_time_seconds": self.execution_time_seconds,
            "row_count":              self.row_count,
            "excel_file":             self.excel_file,
            "error_message":          self.error_message,
        }
