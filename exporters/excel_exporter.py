"""Export a DataFrame to a timestamped Excel file in exports/."""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd

_EXPORT_DIR = "exports"


def export_excel(df: pd.DataFrame) -> str:
    """Write *df* to ``exports/result_YYYYMMDD_HHMMSS.xlsx`` and return the path."""
    os.makedirs(_EXPORT_DIR, exist_ok=True)
    filename = os.path.join(
        _EXPORT_DIR,
        f"result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
    )
    df.to_excel(filename, index=False)
    return filename
