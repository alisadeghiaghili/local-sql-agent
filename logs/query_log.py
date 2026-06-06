"""QueryLog dataclass — one record per engine invocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class QueryLog:
    timestamp:                datetime
    question:                 str
    generated_sql:            str
    model_name:               str
    status:                   str          # SUCCESS | ERROR | OUT_OF_SCOPE
    execution_time_seconds:   float
    row_count:                int          = 0
    excel_file:               str | None   = field(default=None)
    error_message:            str | None   = field(default=None)
