# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Classify a turn's ``basis`` (§2, §4) — fresh, or refining a prior turn.

This module owns the one decision the whole phase hinges on: does the new
question refer back to a previous turn, and if so, which of the two §2
semantics applies?

* **CTE refinement** ("از بین آن‌ها" / "among those") — the new question
  ranks/aggregates over the *set of rows* the previous turn's filter
  matched, not the previous turn's own output shape. Composition:
  ``"cte"`` — see :mod:`session.composer`.
* **Carry-forward refinement** ("همین را برای سال قبل" / "same, but for
  last year") — the new question wants the same *kind* of question
  answered again with one dimension changed (typically the period). No
  previous SQL is reused structurally; the previous turn's *filters* are
  inherited into a freshly generated query. Composition: ``"none"``.

Detection here is deliberately a fixed, deterministic cue-phrase match
rather than an LLM call: it needs to run before any model round-trip (the
SSE ``resolved``/``assumptions`` events fire in "the first few hundred
milliseconds", per §7), and it must be exercised by tests without a live
model. A cue list is inherently incomplete — a question with no exact
substring match is simply "fresh" (never wrongly refuses to answer, per
§5), and the gap is one of accuracy, not safety: the §2 CTE composition
and its scan cap and audit trail described below are what make the wrong
guess visible rather than a value the user quietly trusts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from knowledge.session_policy import DEFAULT_SCOPE_FIELD_NAME, DEFAULT_SCOPE_FILTER_KEY
from session.models import Turn
from session.store import TurnMemory

# ---------------------------------------------------------------------------
# Cue phrases
# ---------------------------------------------------------------------------

#: "Among/from those [rows]" — triggers §2's CTE-composition semantic.
CTE_REFINEMENT_CUES: tuple[str, ...] = (
    "از بین آن‌ها", "از بین آنها", "از بین اونا", "از بین این‌ها", "از بین اینا",
    "از میان آن‌ها", "از میان آنها", "از میان این‌ها",
    "در بین آن‌ها", "در بین این‌ها", "در میان آن‌ها",
    "among those", "among these", "of those", "from those", "out of those",
)

#: "Same [query], but ..." — triggers carry-forward refinement (composition "none").
CARRY_FORWARD_CUES: tuple[str, ...] = (
    "همین را", "همین رو", "همین‌طور", "همینو",
    "همون سوال", "همان سوال", "همون رو", "همان‌طور",
    "به همین ترتیب", "دوباره", "همون",
    "same for", "same as before", "same query", "same question", "again",
)

#: Relative-period phrase -> signed year delta, applied when a
#: carry-forward turn changes only the time period (the exit-criteria
#: example: "همین را برای سال قبل" — "the same, for last year").
_RELATIVE_PERIOD_DELTAS: dict[str, int] = {
    "سال قبل": -1, "سال گذشته": -1, "پارسال": -1,
    "سال آینده": 1, "سال بعد": 1,
}


def _contains_any(question: str, cues: tuple[str, ...]) -> bool:
    return any(cue in question for cue in cues)


def relative_period_delta(question: str) -> int:
    """Signed year offset requested by *question*, or ``0`` if none.

    Examples
    --------
    >>> relative_period_delta("همین را برای سال قبل")
    -1
    >>> relative_period_delta("چیزی درباره سال")
    0
    """
    for phrase, delta in _RELATIVE_PERIOD_DELTAS.items():
        if phrase in question:
            return delta
    return 0


@dataclass(frozen=True)
class BasisDecision:
    """Result of :func:`classify_basis` — everything ``session.engine`` needs."""

    kind: Literal["fresh", "refines"]
    refines_turn_id: str | None
    composition: Literal["cte", "none"]
    inherited_filters: dict[str, object] = field(default_factory=dict)
    period_delta: int = 0

    @property
    def inherited(self) -> list[str]:
        """``Turn.basis.inherited`` display strings, e.g. ``["ring=...", "period=..."]``."""
        return [f"{_display_field(k)}={v}" for k, v in self.inherited_filters.items()]


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


def classify_basis(
    question: str,
    previous_turn: Turn | None,
    previous_memory: TurnMemory | None,
) -> BasisDecision:
    """Decide whether *question* is fresh or refines *previous_turn*.

    Parameters
    ----------
    question:
        The new turn's raw question text.
    previous_turn:
        The session's most recent turn, or ``None`` for the first turn in
        a session (always "fresh" in that case — there is nothing to
        refine).
    previous_memory:
        The :class:`~session.store.TurnMemory` sidecar for *previous_turn*,
        carrying the filters that were in effect for it.

    Returns
    -------
    BasisDecision

    Examples
    --------
    >>> classify_basis("چیزی", None, None).kind
    'fresh'

    >>> from session.models import Turn
    >>> prev = Turn(turn_id="t_01", session_id="s_1", index=1, question="q")
    >>> mem = TurnMemory(turn_id="t_01", filters={"Ring": "تالار سیمان"})
    >>> d = classify_basis("از بین آن‌ها ۱۰ مشتری برتر", prev, mem)
    >>> d.kind, d.composition, d.refines_turn_id
    ('refines', 'cte', 't_01')
    >>> d.inherited
    ['ring=تالار سیمان']

    >>> d2 = classify_basis("همین را برای سال قبل", prev, mem)
    >>> d2.kind, d2.composition, d2.period_delta
    ('refines', 'none', -1)
    """
    if previous_turn is None:
        return BasisDecision(kind="fresh", refines_turn_id=None, composition="none")

    filters = dict(previous_memory.filters) if previous_memory else {}

    if _contains_any(question, CTE_REFINEMENT_CUES):
        return BasisDecision(
            kind="refines",
            refines_turn_id=previous_turn.turn_id,
            composition="cte",
            inherited_filters=filters,
        )

    if _contains_any(question, CARRY_FORWARD_CUES):
        return BasisDecision(
            kind="refines",
            refines_turn_id=previous_turn.turn_id,
            composition="none",
            inherited_filters=filters,
            period_delta=relative_period_delta(question),
        )

    return BasisDecision(kind="fresh", refines_turn_id=None, composition="none")
