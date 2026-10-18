# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Interval and significance formulas for the evaluation reports.

Pure functions on counts, no I/O and no dependency beyond the standard
library (``scipy`` is not a dependency of this project, so the exact binomial
tail is summed directly). Three questions a bare accuracy percentage cannot
answer are answered here:

* **How precise is this figure?** :func:`wilson_interval` gives the Wilson
  score interval of a proportion. It stays inside ``[0, 1]`` and is sound for
  small ``n`` and for proportions near 0 or 1, where the textbook
  ``p +- z * sqrt(p(1-p)/n)`` interval is not. A stratum (a tag, a data
  source, a database, a difficulty) with fewer than :data:`MIN_STRATUM_N` cases
  is flagged by :func:`is_interpretable`: its interval is so wide that a
  difference between two such strata is noise.
* **Did the second round really beat the first?** The two rounds answer the
  same cases, so they are *paired*. :func:`mcnemar_exact_p` is the exact
  two-sided McNemar test on the cases whose outcome changed (fixed against
  regressed), and :func:`newcombe_paired_interval` is the 95% interval of the
  accuracy difference (Newcombe 1998, method 10).
* **Does the model answer the same way twice?** :func:`repeat_consistency`
  and :func:`repeat_mean_accuracy` summarise repeated runs of one question set.

Notation for a paired table: ``both`` cases pass in both rounds, ``regressed``
pass only in the old round, ``fixed`` pass only in the new round, ``neither``
pass in neither.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

__all__ = [
    "MIN_STRATUM_N",
    "Z_95",
    "is_interpretable",
    "mcnemar_exact_p",
    "newcombe_paired_interval",
    "repeat_consistency",
    "repeat_mean_accuracy",
    "stratum_summary",
    "wilson_interval",
]

#: The 97.5th percentile of the standard normal distribution: the multiplier of a
#: two-sided 95% interval.
Z_95 = 1.959963984540054

#: A stratum with fewer cases than this is reported as "not interpretable": its
#: 95% interval spans tens of percentage points.
MIN_STRATUM_N = 20


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """The Wilson score interval of ``successes`` out of ``n``, as proportions.

    Parameters
    ----------
    successes:
        Cases that passed, ``0 <= successes <= n``.
    n:
        Cases scored. ``n == 0`` gives the uninformative ``(0.0, 1.0)``.
    z:
        Normal quantile; the default is the two-sided 95% one.

    Returns
    -------
    tuple[float, float]
        ``(lower, upper)``, both inside ``[0, 1]``.

    Raises
    ------
    ValueError
        If ``successes`` is not between 0 and ``n``.

    Examples
    --------
    39 of 52 is 75.0%; the 95% interval is about 61.8% to 84.8%. By hand, with
    ``z**2 = 3.84146``: centre ``(0.75 + 3.84146/104) / (1 + 3.84146/52) =
    0.73280``, half-width ``1.95996 * sqrt(0.75*0.25/52 + 3.84146/(4*52**2)) /
    1.07387 = 0.11487``.

    >>> lo, hi = wilson_interval(39, 52)
    >>> round(lo, 4), round(hi, 4)
    (0.6179, 0.8477)

    The interval stays inside the unit interval at the extremes:

    >>> lo, hi = wilson_interval(0, 10)
    >>> lo, round(hi, 4)
    (0.0, 0.2775)
    >>> lo, hi = wilson_interval(10, 10)
    >>> round(lo, 4), hi
    (0.7225, 1.0)
    >>> wilson_interval(0, 0)
    (0.0, 1.0)
    >>> wilson_interval(11, 10)
    Traceback (most recent call last):
        ...
    ValueError: successes must be between 0 and n (got 11 of 10)
    """
    if n < 0 or not 0 <= successes <= n:
        raise ValueError(f"successes must be between 0 and n (got {successes} of {n})")
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denominator
    lower = 0.0 if successes == 0 else max(0.0, centre - half)
    upper = 1.0 if successes == n else min(1.0, centre + half)
    return (lower, upper)


def is_interpretable(n: int) -> bool:
    """Whether a stratum of ``n`` cases is large enough to read (``n >= MIN_STRATUM_N``).

    Examples
    --------
    >>> is_interpretable(19), is_interpretable(20)
    (False, True)
    """
    return n >= MIN_STRATUM_N


def stratum_summary(passed: int, n: int) -> dict[str, object]:
    """One stratum as a JSON-friendly record: counts, percentages and the interval.

    Parameters
    ----------
    passed, n:
        Cases that passed and cases scored.

    Returns
    -------
    dict[str, object]
        ``passed``, ``n``, ``pct``, ``wilson95_low_pct``, ``wilson95_high_pct``
        (percentages, 2 decimals) and ``interpretable`` (``n >= 20``).

    Examples
    --------
    >>> stratum_summary(39, 52)
    {'passed': 39, 'n': 52, 'pct': 75.0, 'wilson95_low_pct': 61.79, 'wilson95_high_pct': 84.77, 'interpretable': True}
    >>> stratum_summary(3, 4)["interpretable"]
    False
    """
    lo, hi = wilson_interval(passed, n)
    return {
        "passed": passed,
        "n": n,
        "pct": round(100.0 * passed / n, 2) if n else 0.0,
        "wilson95_low_pct": round(100.0 * lo, 2),
        "wilson95_high_pct": round(100.0 * hi, 2),
        "interpretable": is_interpretable(n),
    }


def mcnemar_exact_p(regressed: int, fixed: int) -> float:
    """The exact two-sided McNemar p-value from the discordant pairs.

    Under the null hypothesis that the two rounds are equally accurate, each
    case whose outcome changed is as likely to have been fixed as regressed, so
    ``fixed`` is Binomial(``regressed + fixed``, 1/2). The p-value is
    ``min(1, 2 * P(X <= min(regressed, fixed)))``. No normal approximation, so
    it is valid for the small tables a golden set produces.

    Parameters
    ----------
    regressed:
        Cases that passed before and fail now.
    fixed:
        Cases that failed before and pass now.

    Returns
    -------
    float
        ``1.0`` when no case changed.

    Examples
    --------
    One regressed and five fixed: ``2 * (1 + 6) / 2**6 = 14/64``.

    >>> mcnemar_exact_p(1, 5)
    0.21875

    Five fixed and none regressed: ``2 * 1 / 2**5``.

    >>> mcnemar_exact_p(0, 5)
    0.0625
    >>> mcnemar_exact_p(0, 6)
    0.03125

    A tie cannot be significant, and the doubled tail is capped at 1:

    >>> mcnemar_exact_p(3, 3)
    1.0
    >>> mcnemar_exact_p(0, 0)
    1.0

    Two regressed and nine fixed: ``2 * (1 + 11 + 55) / 2**11``.

    >>> round(mcnemar_exact_p(2, 9), 5)
    0.06543
    """
    if regressed < 0 or fixed < 0:
        raise ValueError("counts must not be negative")
    n = regressed + fixed
    if n == 0:
        return 1.0
    k = min(regressed, fixed)
    tail = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, (2 * tail) / (2**n))


def newcombe_paired_interval(
    both: int, regressed: int, fixed: int, neither: int, z: float = Z_95
) -> tuple[float, float]:
    """The 95% interval of the paired accuracy difference ``new - old``, as proportions.

    Newcombe's method 10 (Statistics in Medicine 17, 1998, 2635-2650): the
    Wilson intervals of the two accuracies are combined with a correction for
    the correlation of the paired outcomes. A pair of rounds that pass and fail
    the same cases has a much narrower interval than two independent samples of
    the same size.

    Parameters
    ----------
    both, regressed, fixed, neither:
        The paired table (see the module docstring); ``n`` is their sum.
    z:
        Normal quantile; the default is the two-sided 95% one.

    Returns
    -------
    tuple[float, float]
        ``(lower, upper)`` of ``accuracy_new - accuracy_old``; ``(-1.0, 1.0)``
        when there are no cases.

    Raises
    ------
    ValueError
        If a count is negative.

    Examples
    --------
    20 cases pass in both rounds, 3 regressed, 9 fixed, 18 fail in both
    (n = 50): old 23/50 = 46%, new 29/50 = 58%, difference +12 points. By hand
    the Wilson intervals are (0.3297, 0.5960) for the old round and (0.4423,
    0.7062) for the new one; the correlation term is ``phi = (20*18 - 3*9 -
    50/2) / sqrt(23*27*29*21) = 308 / 614.97 = 0.5008``, so the lower limit is
    ``0.12 - sqrt(0.1377**2 + 0.1360**2 - 2*0.5008*0.1377*0.1360) = -0.0167``
    and the upper ``0.12 + sqrt(0.1262**2 + 0.1303**2 - 2*0.5008*0.1262*0.1303)
    = 0.2482``.

    >>> lo, hi = newcombe_paired_interval(20, 3, 9, 18)
    >>> round(lo, 3), round(hi, 3)
    (-0.017, 0.248)

    No case changed: the difference is zero. The two rounds are almost perfectly
    correlated (``phi = 0.958``), so the interval is far tighter than the
    ``+-0.13`` two independent samples of 50 would give:

    >>> lo, hi = newcombe_paired_interval(30, 0, 0, 20)
    >>> round(lo, 3), round(hi, 3)
    (-0.04, 0.04)
    >>> newcombe_paired_interval(0, 0, 0, 0)
    (-1.0, 1.0)
    """
    if min(both, regressed, fixed, neither) < 0:
        raise ValueError("counts must not be negative")
    n = both + regressed + fixed + neither
    if n == 0:
        return (-1.0, 1.0)
    p_old = (both + regressed) / n
    p_new = (both + fixed) / n
    l_old, u_old = wilson_interval(both + regressed, n, z)
    l_new, u_new = wilson_interval(both + fixed, n, z)

    # Correlation of the two outcomes, with Newcombe's continuity correction.
    a_, b_, c_, d_ = both, regressed, fixed, neither
    spread = (a_ + b_) * (c_ + d_) * (a_ + c_) * (b_ + d_)
    phi = 0.0
    if spread > 0:
        c = a_ * d_ - b_ * c_
        if c > n / 2.0:
            c -= n / 2.0
        elif c >= 0:
            c = 0.0
        phi = c / math.sqrt(spread)

    delta = p_new - p_old
    lower = delta - math.sqrt(
        max(0.0, (p_new - l_new) ** 2 + (u_old - p_old) ** 2
            - 2.0 * phi * (p_new - l_new) * (u_old - p_old))
    )
    upper = delta + math.sqrt(
        max(0.0, (u_new - p_new) ** 2 + (p_old - l_old) ** 2
            - 2.0 * phi * (u_new - p_new) * (p_old - l_old))
    )
    return (max(-1.0, lower), min(1.0, upper))


def repeat_consistency(outcomes: Mapping[str, Sequence[bool]]) -> float:
    """The share of cases with the same outcome on every repeat, as a proportion.

    A case that passes every time and one that fails every time are both
    consistent; one that passes on some repeats and fails on others is not.
    Consistency says nothing about correctness, which is why
    :func:`repeat_mean_accuracy` is reported next to it.

    Parameters
    ----------
    outcomes:
        ``{case id: [passed on repeat 1, passed on repeat 2, ...]}``.

    Returns
    -------
    float
        ``0.0`` for no case.

    Examples
    --------
    >>> repeat_consistency({"a": [True, True, True], "b": [False, False, False],
    ...                     "c": [True, False, True], "d": [True, True, False]})
    0.5
    >>> repeat_consistency({})
    0.0
    """
    if not outcomes:
        return 0.0
    steady = sum(1 for runs in outcomes.values() if len(set(runs)) <= 1)
    return steady / len(outcomes)


def repeat_mean_accuracy(outcomes: Mapping[str, Sequence[bool]]) -> float:
    """The accuracy averaged over the repeats, as a proportion.

    Each repeat's accuracy is its share of passing cases; the mean over repeats
    equals the share of passing (case, repeat) pairs when every case has the
    same number of repeats.

    Examples
    --------
    >>> repeat_mean_accuracy({"a": [True, True], "b": [True, False], "c": [False, False]})
    0.5
    >>> repeat_mean_accuracy({})
    0.0
    """
    if not outcomes:
        return 0.0
    repeats = max(len(runs) for runs in outcomes.values())
    if repeats == 0:
        return 0.0
    per_repeat = [
        sum(1 for runs in outcomes.values() if i < len(runs) and runs[i]) / len(outcomes)
        for i in range(repeats)
    ]
    return sum(per_repeat) / repeats
