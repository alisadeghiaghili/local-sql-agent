"""Business rules injected into the schema context.

These rules guide the model toward correct SQL patterns for
domain-specific queries (latest period, ranking, aggregation).
"""

from __future__ import annotations

BUSINESS_RULES: str = """
# BUSINESS RULES

- اگر کاربر سال یا ماه را مشخص نکرد، از جدیدترین سال و ماه موجود در [general_Dim].[Date] استفاده شود.
- همیشه از aliases برای جداول استفاده کن.
- هیچوقت SELECT * ننویس.
- همیشه فقط ستون‌های مورد نیاز را SELECT کن.
- برای رتبه‌بندی Top N در گروه: از CTE با ROW_NUMBER() OVER (PARTITION BY ...) استفاده کن.
- هیچوقت LIMIT ننویس. فقط TOP بنویس.
- همیشه نام جدول را با براکت: [Schema].[Table]
"""
