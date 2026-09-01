# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for exporters/excel_exporter.py."""

from __future__ import annotations

import os
from unittest.mock import patch

import pandas as pd
import pytest

from exporters.excel_exporter import export_excel


class TestExportExcel:
    def test_creates_file(self, tmp_path):
        with patch("exporters.excel_exporter.settings") as m:
            m.export_dir = str(tmp_path)
            df = pd.DataFrame({"A": [1, 2], "B": ["x", "y"]})
            path = export_excel(df)
        assert os.path.exists(path)

    def test_returns_absolute_path(self, tmp_path):
        with patch("exporters.excel_exporter.settings") as m:
            m.export_dir = str(tmp_path)
            df = pd.DataFrame({"col": [1]})
            path = export_excel(df)
        assert os.path.isabs(path)

    def test_filename_pattern(self, tmp_path):
        with patch("exporters.excel_exporter.settings") as m:
            m.export_dir = str(tmp_path)
            df = pd.DataFrame({"col": [1]})
            path = export_excel(df)
        filename = os.path.basename(path)
        assert filename.startswith("result_")
        assert filename.endswith(".xlsx")

    def test_file_is_readable_excel(self, tmp_path):
        with patch("exporters.excel_exporter.settings") as m:
            m.export_dir = str(tmp_path)
            df = pd.DataFrame({"Name": ["Ali", "Sara"], "Score": [95, 88]})
            path = export_excel(df)
        result = pd.read_excel(path, engine="openpyxl")
        assert list(result.columns) == ["Name", "Score"]
        assert len(result) == 2

    def test_empty_dataframe(self, tmp_path):
        with patch("exporters.excel_exporter.settings") as m:
            m.export_dir = str(tmp_path)
            df = pd.DataFrame()
            path = export_excel(df)
        assert os.path.exists(path)

    def test_creates_export_dir_if_missing(self, tmp_path):
        new_dir = tmp_path / "new_exports"
        with patch("exporters.excel_exporter.settings") as m:
            m.export_dir = str(new_dir)
            df = pd.DataFrame({"x": [1]})
            export_excel(df)
        assert new_dir.exists()

    def test_unicode_content(self, tmp_path):
        with patch("exporters.excel_exporter.settings") as m:
            m.export_dir = str(tmp_path)
            df = pd.DataFrame({"نام": ["علی", "سارا"], "امتیاز": [90, 85]})
            path = export_excel(df)
        result = pd.read_excel(path, engine="openpyxl")
        assert "نام" in result.columns
