# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
import re
from knowledge.aliases import RING_ALIASES

# Translates Persian (۰-۹) and Arabic-Indic (٠-٩) digits to ASCII so years
# like ۱۴۰۲ or ١٤٠٢ are matched.
_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# Full Shamsi date 'YYYY/MM/DD' or 'YYYY-MM-DD' (digits in any script). The
# lookarounds reuse the extract_year convention and block matches inside digit
# runs (e.g. phone numbers). Day bound is month-aware: months 1-6 have 31 days,
# 7-12 have 30 (month 12 = Esfand has 29/30; 30 is allowed so leap years pass).
_PERSIAN_DATE_RE = re.compile(r"(?<!\d)(13\d{2}|14\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)")

# Canonical Persian month names in Shamsi order (1-12), as stored in
# General_Dim.Date.PersianMonthName.
PERSIAN_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]

# Persian day-of-week names mapped to their PersianDayOfWeek value
# (1=شنبه Saturday ... 7=جمعه Friday).
PERSIAN_DAYS_OF_WEEK = {
    "یکشنبه": 2, "دوشنبه": 3, "سه‌شنبه": 4, "چهارشنبه": 5, "پنجشنبه": 6,
    "شنبه": 1, "جمعه": 7,
}

# Persian season names as stored in General_Dim.Date.PersianSeasonName.
PERSIAN_SEASONS = ["بهار", "تابستان", "پاییز", "زمستان"]


class ValueRetriever:

    @staticmethod
    def extract_ring(question: str):
        for canonical_name, aliases in RING_ALIASES.items():
            for alias in aliases:
                if alias in question:
                    return canonical_name
        return None

    @staticmethod
    def extract_year(question: str):
        # Lookarounds instead of \b: \b is Unicode-aware and Persian letters
        # adjacent to digits would suppress the word boundary.
        match = re.search(r"(?<!\d)(13\d{2}|14\d{2})(?!\d)", question.translate(_DIGIT_TRANSLATION))
        return int(match.group(1)) if match else None

    @staticmethod
    def _normalize(text: str) -> str:
        """Strip ZWNJ and spaces so 'چهار شنبه' / 'چهارشنبه' / 'چهارشنبه' match alike."""
        return text.replace("\u200c", "").replace(" ", "")

    @classmethod
    def extract_month_name(cls, question: str) -> str | None:
        q = cls._normalize(question)
        for name in PERSIAN_MONTHS:
            if cls._normalize(name) in q:
                return name
        return None

    @classmethod
    def extract_day_of_week(cls, question: str) -> int | None:
        q = cls._normalize(question)
        # Longest names first so e.g. "یکشنبه" matches before its substring "شنبه".
        for name, day_number in sorted(
            PERSIAN_DAYS_OF_WEEK.items(), key=lambda kv: len(kv[0]), reverse=True
        ):
            if cls._normalize(name) in q:
                return day_number
        return None

    @classmethod
    def extract_season_name(cls, question: str) -> str | None:
        q = cls._normalize(question)
        for name in PERSIAN_SEASONS:
            if cls._normalize(name) in q:
                return name
        return None

    @staticmethod
    def extract_persian_date(question: str) -> str | None:
        match = _PERSIAN_DATE_RE.search(question.translate(_DIGIT_TRANSLATION))
        if not match:
            return None
        year, month, day = (int(g) for g in match.groups())
        if not (1 <= month <= 12 and 1 <= day <= (31 if month <= 6 else 30)):
            return None
        return f"{year:04d}/{month:02d}/{day:02d}"

    @classmethod
    def retrieve(cls, question: str) -> dict:
        filters = {}
        ring = cls.extract_ring(question)
        if ring:
            filters["Ring"] = ring
        full_date = cls.extract_persian_date(question)
        if full_date:
            filters["PersianDate"] = full_date
        else:
            year = cls.extract_year(question)
            if year:
                filters["PersianYear"] = year
        month = cls.extract_month_name(question)
        if month:
            filters["PersianMonthName"] = month
        day = cls.extract_day_of_week(question)
        if day:
            filters["PersianDayOfWeek"] = day
        season = cls.extract_season_name(question)
        if season:
            filters["PersianSeasonName"] = season
        return filters
