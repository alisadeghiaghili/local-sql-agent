# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Export a DataFrame to a timestamped Excel file.

Output directory defaults to ``exports/`` (configurable via EXPORT_DIR env var).
Column widths are auto-fitted for readability.
"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd

import config as cfg

# Expose settings at module level so tests can patch "exporters.excel_exporter.settings"
settings = cfg.settings


def export_excel(df: pd.DataFrame) -> str:
    """Write *df* to ``<EXPORT_DIR>/result_YYYYMMDD_HHMMSS.xlsx``.

    Returns
    -------
    str
        Absolute path of the created file.
    """
    # Always read through the module-level name so patches take effect
    _settings = settings
    os.makedirs(_settings.export_dir, exist_ok=True)
    filename = os.path.join(
        _settings.export_dir,
        f"result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
    )

    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Result")
        ws = writer.sheets["Result"]
        for col_cells in ws.columns:
            max_len = max(
                (len(str(cell.value)) for cell in col_cells if cell.value),
                default=10,
            )
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 60)

    return os.path.abspath(filename)
