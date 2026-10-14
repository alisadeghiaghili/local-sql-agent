# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A deterministic dev / test split of a golden set.

When a reviewer fixes the failures a round shows, the fixes are tuned to
those cases: accuracy on them goes up whether or not the fix generalises.
The honest number is the accuracy on cases nobody looked at. This module
cuts the golden set in two halves so that one stays unseen:

* within each category (the case's first tag, ``tags[0]``), cases are sorted
  by ``id``;
* the cases at even positions (0, 2, 4, ...) go to **dev**, those at odd
  positions to **test**.

Nothing is random and nothing depends on the order of the file, so the same
golden set always gives the same split, on any machine. Every case is
placed, whatever its ``status``, so that verifying or activating cases later
does not move the others. Category sizes of 1 give a dev case and no test
case, so give each category at least two cases.

The reviewer is shown dev results in full; ``test`` is only compared
(``eval.cli compare --split test``), never diagnosed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from eval.models import GoldenCase

__all__ = ["SplitResult", "category_of", "split_cases"]

#: The category of a case with no tags, as it appears in the counts.
UNTAGGED = "(untagged)"


def category_of(case: GoldenCase) -> str:
    """The case's category: its first tag, or ``(untagged)``.

    Examples
    --------
    >>> category_of(GoldenCase(id="a", question="q", expected_sql="SELECT 1", tags=["count", "x"]))
    'count'
    >>> category_of(GoldenCase(id="b", question="q", expected_sql="SELECT 1"))
    '(untagged)'
    """
    return case.tags[0] if case.tags else UNTAGGED


@dataclass(frozen=True, slots=True)
class SplitResult:
    """The two halves and how each category divided.

    Parameters
    ----------
    dev_ids, test_ids:
        Case ids, sorted.
    categories:
        ``{category: {"dev": n, "test": n}}``, sorted by category.
    """

    dev_ids: list[str]
    test_ids: list[str]
    categories: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        """``{"total", "dev", "test"}``."""
        return {
            "total": len(self.dev_ids) + len(self.test_ids),
            "dev": len(self.dev_ids),
            "test": len(self.test_ids),
        }


def split_cases(cases: Sequence[GoldenCase]) -> SplitResult:
    """Split *cases* into dev (even positions) and test (odd positions) per category.

    Parameters
    ----------
    cases:
        Every case of the golden set, whatever its status.

    Returns
    -------
    SplitResult

    Raises
    ------
    ValueError
        If two cases share an id (the split would be ambiguous).

    Examples
    --------
    >>> cases = [GoldenCase(id=i, question=i, expected_sql="SELECT 1", tags=[t])
    ...          for i, t in [("c3", "count"), ("c1", "count"), ("c2", "count"),
    ...                       ("r2", "rank"), ("r1", "rank")]]
    >>> result = split_cases(cases)
    >>> result.dev_ids, result.test_ids
    (['c1', 'c3', 'r1'], ['c2', 'r2'])
    >>> result.categories["count"]
    {'dev': 2, 'test': 1}
    >>> split_cases(list(reversed(cases))) == result
    True
    """
    seen: set[str] = set()
    by_category: dict[str, list[str]] = {}
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id in the golden set: {case.id!r}")
        seen.add(case.id)
        by_category.setdefault(category_of(case), []).append(case.id)

    dev: list[str] = []
    test: list[str] = []
    categories: dict[str, dict[str, int]] = {}
    for category in sorted(by_category):
        ordered = sorted(by_category[category])
        category_dev = ordered[0::2]
        category_test = ordered[1::2]
        dev.extend(category_dev)
        test.extend(category_test)
        categories[category] = {"dev": len(category_dev), "test": len(category_test)}
    return SplitResult(sorted(dev), sorted(test), categories)
