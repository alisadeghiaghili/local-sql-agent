# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Classify a built retrieval prompt against ``PROMPT_RETRIEVAL_TOKEN_BUDGET``.

:func:`prompt_engine.static_prefix.should_use_static_prefix` only proves that
the *static prefix* is too big for the budget. Nothing checks that the
per-question retrieval prompt which replaces it fits: an empty retrieval falls
back to the whole schema, and the number of retrieved tables is a count, not a
token cap. This module answers that one question for a prompt that has already
been built, as a pure function, so the caller decides what to do with the
answer (today: log it; see :func:`llm.router.build_prompt_segments`).

The estimate is :func:`prompt_engine.static_prefix.estimate_tokens`
(``len(text) // 4``), the same heuristic that gates the static path, so the two
sides of the comparison stay consistent. It undercounts Persian text (about
15%, see the README), so a prompt reported as within budget may still be over
it in real tokens.
"""

from __future__ import annotations

from dataclasses import dataclass

from prompt_engine.static_prefix import estimate_tokens


@dataclass(frozen=True)
class PromptSize:
    """How a prompt's estimated size compares with the token budget.

    Attributes
    ----------
    estimated_tokens:
        :func:`~prompt_engine.static_prefix.estimate_tokens` of the prompt.
    budget:
        The budget it was compared with, as given. ``<= 0`` means retrieval is
        forced and there is no budget to compare against.
    over_budget:
        ``True`` only when ``budget > 0`` and ``estimated_tokens > budget``.
    overshoot:
        ``estimated_tokens - budget`` when over budget, else ``0``.
    """

    estimated_tokens: int
    budget: int
    over_budget: bool
    overshoot: int


def classify_prompt_size(prompt: str, budget: int) -> PromptSize:
    """Compare a built prompt's estimated token count with *budget*.

    A prompt exactly at the budget is within it, matching
    :func:`~prompt_engine.static_prefix.should_use_static_prefix`, which accepts
    an estimate ``<= budget``. A *budget* ``<= 0`` is the setting that forces the
    retrieval path, so there is nothing to compare against and the prompt is
    never reported as over budget.

    The estimate is the ``len(text) // 4`` heuristic, which undercounts Persian
    text by about 15%. Treat the result as a signal, not a measurement.

    Parameters
    ----------
    prompt:
        The assembled prompt text, including the empty string.
    budget:
        ``cfg.settings.prompt_retrieval_token_budget``.

    Returns
    -------
    PromptSize
        The estimate, the budget, whether it is exceeded and by how much.

    Raises
    ------
    TypeError
        If *prompt* is a truthy value without a length (not a string).

    Examples
    --------
    >>> classify_prompt_size("a" * 400, 100).over_budget
    False
    >>> size = classify_prompt_size("a" * 440, 100)
    >>> size.over_budget, size.overshoot
    (True, 10)
    >>> classify_prompt_size("a" * 400, 0).over_budget
    False
    """
    estimated = estimate_tokens(prompt)
    over = budget > 0 and estimated > budget
    return PromptSize(
        estimated_tokens=estimated,
        budget=budget,
        over_budget=over,
        overshoot=estimated - budget if over else 0,
    )
