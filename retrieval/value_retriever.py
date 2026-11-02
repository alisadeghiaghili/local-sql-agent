# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
import re
from core.persian import normalize_compact, normalize_for_matching
from knowledge.aliases import RING_ALIASES

# Full Shamsi date 'YYYY/MM/DD' or 'YYYY-MM-DD' (digits in any script). The
# lookarounds reuse the extract_year convention and block matches inside digit
# runs (e.g. phone numbers). Day bound is month-aware: months 1-6 have 31 days,
# 7-12 have 30 (month 12 = Esfand has 29/30; 30 is allowed so leap years pass).
_PERSIAN_DATE_RE = re.compile(r"(?<!\d)(13\d{2}|14\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)")

# Canonical Persian month names in Shamsi order (1-12), as stored in
# sales.Date.PersianMonthName.
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

# Persian season names as stored in sales.Date.PersianSeasonName.
PERSIAN_SEASONS = ["بهار", "تابستان", "پاییز", "زمستان"]

# A name from one of the fixed vocabularies above is found only as a whole
# word: no letter directly before or after it. Whitespace is allowed between
# any two of its letters, so "پنج شنبه" and "اردی بهشت" still match the
# one-word canonical forms (ZWNJ is already removed by the normaliser), and a
# month name may carry "ماه", attached or not ("دیماه", "دی ماه",
# "اسفندماه"). Issue #244: the earlier substring match on whitespace-free text
# found "دی" inside "جدیدترین" and "تولیدی" and across "تعداد یک", "تیر"
# inside "تیرآهن" and "آذر" inside "آذربایجان", and added a month filter to
# questions that named no month. Digits may touch a name ("اسفند۱۴۰۲").
_NO_LETTER_BEFORE = r"(?<![^\W\d_])"
_NO_LETTER_AFTER = r"(?![^\W\d_])"
_MONTH_SUFFIX = "ماه"


def _spaced(word: str) -> str:
    """*word*'s letters as a regex that allows whitespace between them."""
    return r"\s*".join(re.escape(ch) for ch in normalize_compact(word))


def _whole_word(name: str, optional_suffix: str = "") -> re.Pattern[str]:
    """A pattern finding *name* as a whole word in normalised text.

    Args:
        name: The canonical name, e.g. ``"دی"`` or ``"پنجشنبه"``.
        optional_suffix: A word that may follow *name*, attached or after
            whitespace, without breaking the match (``"ماه"`` for months).

    Returns:
        A compiled pattern for :func:`core.persian.normalize_for_matching`
        output.

    Examples:
        >>> bool(_whole_word("دی", "ماه").search("معاملات دیماه"))
        True
        >>> bool(_whole_word("دی", "ماه").search("جدیدترین قراردادها"))
        False
    """
    tail = f"(?:\\s*{_spaced(optional_suffix)})?" if optional_suffix else ""
    return re.compile(_NO_LETTER_BEFORE + _spaced(name) + tail + _NO_LETTER_AFTER)


_MONTH_PATTERNS = [(name, _whole_word(name, _MONTH_SUFFIX)) for name in PERSIAN_MONTHS]
# Longest names first so e.g. "یکشنبه" is tried before "شنبه".
_DAY_OF_WEEK_PATTERNS = [
    (number, _whole_word(name))
    for name, number in sorted(PERSIAN_DAYS_OF_WEEK.items(), key=lambda kv: len(kv[0]), reverse=True)
]
_SEASON_PATTERNS = [(name, _whole_word(name)) for name in PERSIAN_SEASONS]


class ValueRetriever:

    @staticmethod
    def extract_ring(question: str):
        # normalize_for_matching so an alias/question pair that differs only
        # by ي/ك vs ی/ک or ZWNJ still matches -- otherwise the same ring
        # could be recognised for one spelling of a question and missed for
        # another, spelling-equivalent one.
        q = normalize_for_matching(question)
        for canonical_name, aliases in RING_ALIASES.items():
            for alias in aliases:
                if normalize_for_matching(alias) in q:
                    return canonical_name
        return None

    @staticmethod
    def extract_year(question: str):
        # Lookarounds instead of \b: \b is Unicode-aware and Persian letters
        # adjacent to digits would suppress the word boundary.
        match = re.search(r"(?<!\d)(13\d{2}|14\d{2})(?!\d)", normalize_for_matching(question))
        return int(match.group(1)) if match else None

    @classmethod
    def extract_month_name(cls, question: str) -> str | None:
        q = normalize_for_matching(question)
        for name, pattern in _MONTH_PATTERNS:
            if pattern.search(q):
                return name
        return None

    @classmethod
    def extract_day_of_week(cls, question: str) -> int | None:
        q = normalize_for_matching(question)
        for day_number, pattern in _DAY_OF_WEEK_PATTERNS:
            if pattern.search(q):
                return day_number
        return None

    @classmethod
    def extract_season_name(cls, question: str) -> str | None:
        q = normalize_for_matching(question)
        for name, pattern in _SEASON_PATTERNS:
            if pattern.search(q):
                return name
        return None

    @staticmethod
    def extract_persian_date(question: str) -> str | None:
        match = _PERSIAN_DATE_RE.search(normalize_for_matching(question))
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
