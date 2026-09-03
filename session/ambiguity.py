# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Ambiguity detection and assumption extraction — §5.

"Never block on a vague question. Produce the best answer under explicit
assumptions and surface them." This module builds those assumptions (and,
where useful, one-click ``clarifications``) for the two situations
``session.engine`` hands off to it:

1. A **fresh** question with no session to inherit from. If it asks for a
   ranked/"top" answer without naming a measure, scope, or period, those
   three dimensions default (source ``"default"``) and are offered as
   ``clarifications`` — this is the "same question asked with no prior
   context must be answered under declared assumptions rather than
   refused" requirement.
2. A **refining** question (§2). The mandatory, non-editable scope rule
   (source ``"policy"``) is always present for a ``"cte"``-composed
   refinement; inherited filters are ``"session"``-sourced; a measure
   named in the question itself is ``"question"``-sourced.

Every helper here is a small, deterministic, cue-based heuristic — the
same design choice as :mod:`session.refinement` and for the same reason:
it must run without a live model and be exercised by ordinary unit tests.
"""

from __future__ import annotations

from datetime import date

from knowledge.session_policy import (
    DEFAULT_SCOPE_CLARIFICATION_PROMPT,
    DEFAULT_SCOPE_FIELD_NAME,
    DEFAULT_SCOPE_FILTER_KEY,
    DEFAULT_SCOPE_LABEL,
    DEFAULT_SCOPE_OPTIONS,
)
from session.models import Assumption, Clarification

# ---------------------------------------------------------------------------
# Measure resolution
# ---------------------------------------------------------------------------

#: Persian cue words -> the canonical measure label the field ends up with.
#: Checked in order; the first match wins.
_MEASURE_CUES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("حجم", "وزن"), "حجم معامله (HallMatchingWeight)"),
    (("ارزش", "ریال"), "ارزش ریالی معامله"),
    (("تعداد",), "تعداد قرارداد"),
)

DEFAULT_MEASURE: str = "ارزش ریالی معامله"

MEASURE_OPTIONS: tuple[str, ...] = (
    "ارزش ریالی معامله", "حجم معامله", "تعداد قرارداد",
)

RANK_WORDS: tuple[str, ...] = ("برتر", "بیشترین", "کمترین", "بالاترین", "top", "best")


def resolve_measure(question: str) -> tuple[str, str]:
    """Return ``(measure_label, source)`` -- ``source`` is ``"question"`` or ``"default"``.

    Examples
    --------
    >>> resolve_measure("۱۰ مشتری برتر به لحاظ حجم معامله")
    ('حجم معامله (HallMatchingWeight)', 'question')
    >>> resolve_measure("۱۰ مشتری برتر را نشان بده")
    ('ارزش ریالی معامله', 'default')
    """
    for cues, label in _MEASURE_CUES:
        if any(cue in question for cue in cues):
            return label, "question"
    return DEFAULT_MEASURE, "default"


def is_ranking_question(question: str) -> bool:
    """True if *question* asks for a ranked/"top N" answer.

    Examples
    --------
    >>> is_ranking_question("۱۰ مشتری برتر را نشان بده")
    True
    >>> is_ranking_question("معاملات مشتری‌های تالار سیمان را نشان بده")
    False
    """
    return any(w in question for w in RANK_WORDS)


# ---------------------------------------------------------------------------
# Default period — a dependency-free Gregorian -> Jalali (Persian) year
# ---------------------------------------------------------------------------

_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"


def to_persian_digits(n: int) -> str:
    """Render *n* using Persian digit glyphs.

    Examples
    --------
    >>> to_persian_digits(1404)
    '۱۴۰۴'
    """
    return "".join(_PERSIAN_DIGITS[int(ch)] for ch in str(n))


def gregorian_to_jalali_year(g_year: int, g_month: int, g_day: int) -> int:
    """Best-effort Gregorian -> Jalali (Solar Hijri) calendar year.

    A minimal, dependency-free conversion (no ``jdatetime`` in
    ``requirements.txt``) -- accurate for the calendar-year boundary,
    which is all a *default period* assumption needs. Nowruz (Jalali new
    year) falls on March 20 or 21; this uses the fixed March 21 cutover,
    which is right for the overwhelming majority of years and, being only
    a fallback *default* (source ``"default"``, always user-editable), is
    never the value actually used to filter data unless the user leaves
    it unedited.

    Examples
    --------
    >>> gregorian_to_jalali_year(2026, 8, 27)
    1405
    >>> gregorian_to_jalali_year(2026, 1, 1)
    1404
    """
    if (g_month, g_day) >= (3, 21):
        return g_year - 621
    return g_year - 622


def default_period_label(today: date | None = None) -> tuple[str, str]:
    """Return ``(label, raw_year_str)`` for "the current Persian year".

    Parameters
    ----------
    today:
        Injectable for deterministic tests. Defaults to ``date.today()``.

    Examples
    --------
    >>> from datetime import date
    >>> default_period_label(today=date(2026, 8, 27))
    ('سال جاری (۱۴۰۵)', '1405')
    """
    d = today or date.today()
    year = gregorian_to_jalali_year(d.year, d.month, d.day)
    return f"سال جاری ({to_persian_digits(year)})", str(year)


# ---------------------------------------------------------------------------
# Assumption builders
# ---------------------------------------------------------------------------


def assumptions_for_fresh(
    question: str, filters: dict[str, object], *, today: date | None = None,
) -> tuple[list[Assumption], list[Clarification], bool]:
    """Build assumptions/clarifications for a **fresh** (non-refining) turn.

    Only a ranking-style question ("top N ...") with no explicit filters
    triggers default assumptions -- an ordinary, fully-specific question
    (e.g. "معاملات مشتری‌های تالار سیمان را نشان بده", where ``filters``
    already resolved ``Ring``) has nothing to declare and stays
    unambiguous, matching ``docs/api-contract-v2.md``'s own worked example
    (turn 1: zero assumptions).

    Returns
    -------
    tuple[list[Assumption], list[Clarification], bool]
        ``(assumptions, clarifications, is_ambiguous)``.
    """
    if not is_ranking_question(question):
        return [], [], False

    assumptions: list[Assumption] = []
    clarifications: list[Clarification] = []

    if DEFAULT_SCOPE_FILTER_KEY in filters:
        ring_value = str(filters[DEFAULT_SCOPE_FILTER_KEY])
        ring_source = "question"
    else:
        ring_value = DEFAULT_SCOPE_LABEL
        ring_source = "default"
        clarifications.append(
            Clarification(
                field=DEFAULT_SCOPE_FIELD_NAME,
                prompt=DEFAULT_SCOPE_CLARIFICATION_PROMPT,
                options=list(DEFAULT_SCOPE_OPTIONS),
            )
        )
    assumptions.append(
        Assumption(field=DEFAULT_SCOPE_FIELD_NAME, value=ring_value, source=ring_source)
    )

    measure_value, measure_source = resolve_measure(question)
    assumptions.append(Assumption(field="measure", value=measure_value, source=measure_source))
    if measure_source == "default":
        clarifications.append(
            Clarification(
                field="measure", prompt="«برتر» بر اساس کدام معیار؟", options=list(MEASURE_OPTIONS),
            )
        )

    if "PersianYear" not in filters:
        period_label, _ = default_period_label(today=today)
        assumptions.append(Assumption(field="period", value=period_label, source="default"))

    return assumptions, clarifications, True


#: Internal filter-key -> the display field name contract examples use.
#: The default-scope entry is warehouse policy (see
#: knowledge.session_policy); ``PersianYear``/``period`` is a fixed engine
#: mapping, not something project_config/session_policy.yaml governs.
_FIELD_DISPLAY_NAMES: dict[str, str] = {
    DEFAULT_SCOPE_FILTER_KEY: DEFAULT_SCOPE_FIELD_NAME,
    "PersianYear": "period",
}


def _display_field(key: str) -> str:
    return _FIELD_DISPLAY_NAMES.get(key, key.lower())


def assumptions_for_cte_refinement(
    question: str, inherited_filters: dict[str, object],
) -> list[Assumption]:
    """Assumptions for a §2 CTE-composed refinement (always ambiguous).

    Always carries the mandatory, non-editable ``scope`` policy
    assumption -- the whole reason §2 exists -- plus a ``measure``
    assumption (``"question"``-sourced if the question names one) and one
    ``"session"``-sourced assumption per inherited filter.
    """
    assumptions: list[Assumption] = [
        Assumption(
            field="scope",
            value=(
                "همهٔ سطرهای منطبق با فیلتر قبلی، نه فقط سطرهای نمایش‌داده‌شدهٔ پرسش قبل"
            ),
            source="policy",
            editable=False,
        ),
    ]
    measure_value, measure_source = resolve_measure(question)
    assumptions.append(Assumption(field="measure", value=measure_value, source=measure_source))
    for key, value in inherited_filters.items():
        field_name = _display_field(key)
        assumptions.append(Assumption(field=field_name, value=str(value), source="session"))
    return assumptions


def assumptions_for_carry_forward(
    inherited_filters: dict[str, object],
) -> list[Assumption]:
    """Assumptions for a carry-forward (composition ``"none"``) refinement.

    Every inherited (and possibly period-adjusted) filter is
    ``"session"``-sourced -- it came from the conversation, not this
    question's own words.
    """
    assumptions: list[Assumption] = []
    for key, value in inherited_filters.items():
        field_name = _display_field(key)
        assumptions.append(Assumption(field=field_name, value=str(value), source="session"))
    return assumptions
