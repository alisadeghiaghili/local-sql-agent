# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Per-request data-source routing shared by both engines.

With several data sources configured, each question is first routed to one
source (:mod:`retrieval.source_selector`) and its prompt is built for that
source alone (:func:`llm.router.build_prompt_segments` with ``source=``).
This module is the glue the two engines -- ``api/runner.py`` /
:class:`~llm.sql_agent.SQLAgent` for ``/query`` and ``session/engine.py``
for conversations -- share, so they cannot drift apart:

* :class:`SourceRouting` holds one request's state: the selection, the
  source currently in use, the prompt built for it, and whether the one
  permitted retry has been used.
* :func:`generate_with_source_fallback` makes the model call and, when the
  model answers ``OUT_OF_SCOPE``, retries once with the next candidate
  source. **At most one extra attempt per request**: the second
  ``OUT_OF_SCOPE`` is raised as it always was, and a deployment with one
  source never retries (``plan`` returns ``None`` for it, and nothing here
  runs).
* :meth:`SourceRouting.audit` is the ``datasource_selection`` block of the
  audit record.

Why the retry exists: source selection is a heuristic (keywords, retrieval
evidence), and a model shown the wrong source's tables can only say it has
nothing to answer with. The retry costs one more model call, only for that
case, and only when another source exists.

The statement the model then writes is still checked by the SQL guard and
routed by the tables it reads (:mod:`database.routing`); the selection
decides what the model *sees*, never where a statement runs.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from llm.router import PromptSegments, RouteResult, _is_out_of_scope
from retrieval.source_selector import (
    REASON_SESSION,
    SourceSelection,
    select_source_for_question,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SourceRouting",
    "choose_source",
    "continuity_audit",
    "generate_with_source_fallback",
    "is_out_of_scope_failure",
]


def is_out_of_scope_failure(exc: BaseException) -> bool:
    """True when *exc*, or the exception it wraps, is the model's ``OUT_OF_SCOPE``.

    The router re-raises a decline unwrapped, but a caller further out may
    see it as the ``__cause__`` of a chain-exhausted ``RuntimeError``; both
    shapes count.

    Examples
    --------
    >>> is_out_of_scope_failure(ValueError("OUT_OF_SCOPE"))
    True
    >>> wrapper = RuntimeError("Every backend in the chain failed")
    >>> wrapper.__cause__ = ValueError("OUT_OF_SCOPE")
    >>> is_out_of_scope_failure(wrapper)
    True
    >>> is_out_of_scope_failure(ValueError("something else"))
    False
    """
    return _is_out_of_scope(exc.__cause__ or exc)


class SourceRouting:
    """One request's data-source routing: selection, prompt, and retry state.

    Build one with :meth:`plan`; construct directly only in tests.

    Parameters
    ----------
    selection:
        What :func:`retrieval.source_selector.select_source` decided.
    build:
        ``build(source) -> PromptSegments``: assembles the request's prompt
        for that source (the caller's closure over its question, system
        prompt, retrieval context and session context).

    Attributes
    ----------
    selection:
        The selection as made.
    current:
        The source the prompt in :attr:`segments` is for.
    fallback_from:
        The source that answered ``OUT_OF_SCOPE`` when the retry happened,
        else ``None``.
    segments:
        The base prompt for :attr:`current` (the caller appends any
        correction text to its question segment, never to the prefix).

    Examples
    --------
    >>> sel = SourceSelection("sales", "keyword", ("sales", "inventory"))
    >>> built = []
    >>> def build(source):
    ...     built.append(source)
    ...     return PromptSegments(static_prefix=f"prefix for {source}", question="q")
    >>> routing = SourceRouting(sel, build)
    >>> routing.current, routing.segments.static_prefix
    ('sales', 'prefix for sales')
    >>> routing.fall_back().static_prefix
    'prefix for inventory'
    >>> routing.audit()
    {'chosen': 'inventory', 'reason': 'keyword', 'candidates': ['sales', 'inventory'], 'fallback_from': 'sales'}
    >>> routing.can_fall_back()
    False
    >>> built
    ['sales', 'inventory']
    """

    def __init__(
        self, selection: SourceSelection, build: Callable[[str], PromptSegments],
    ) -> None:
        self.selection = selection
        self._build = build
        self.current = selection.chosen
        self.fallback_from: str | None = None
        self.segments = build(selection.chosen)

    @classmethod
    def plan(
        cls,
        question: str,
        context: object,
        *,
        build: Callable[[str], PromptSegments],
        previous_source: str | None = None,
    ) -> "SourceRouting | None":
        """Select a source for *question* and build its prompt.

        Parameters
        ----------
        question:
            The natural-language question.
        context:
            The retrieval context over the whole schema.
        build:
            See the class docstring.
        previous_source:
            The previous turn's source, for a follow-up turn.

        Returns
        -------
        SourceRouting | None
            ``None`` when fewer than two data sources are configured: the
            caller then builds its prompt exactly as it always did.
        """
        selection = select_source_for_question(
            question, context, previous_source=previous_source,
        )
        if selection is None:
            return None
        logger.debug(
            "Data source %r selected (%s); candidates %s",
            selection.chosen, selection.reason, selection.candidates,
        )
        return cls(selection, build)

    def can_fall_back(self) -> bool:
        """Whether an ``OUT_OF_SCOPE`` may still be retried on another source."""
        return self.fallback_from is None and self.selection.after(self.current) is not None

    def fall_back(self) -> PromptSegments:
        """Switch to the next candidate source and return its prompt.

        Raises
        ------
        RuntimeError
            If no retry is left (:meth:`can_fall_back` is false).
        """
        following = self.selection.after(self.current) if self.fallback_from is None else None
        if following is None:
            raise RuntimeError("no data source left to fall back to")
        logger.info(
            "Model answered OUT_OF_SCOPE for data source %r; retrying once with %r",
            self.current, following,
        )
        self.fallback_from = self.current
        self.current = following
        self.segments = self._build(following)
        return self.segments

    def audit(self) -> dict[str, Any]:
        """The ``datasource_selection`` block for the audit record."""
        return self.selection.audit(chosen=self.current, fallback_from=self.fallback_from)


def generate_with_source_fallback(
    generate: Callable[[PromptSegments], RouteResult],
    routing: SourceRouting | None,
    segments: PromptSegments,
) -> tuple[RouteResult, PromptSegments | None]:
    """Call *generate* with *segments*; on ``OUT_OF_SCOPE`` retry once on the next source.

    Parameters
    ----------
    generate:
        The model call: ``generate(segments) -> RouteResult``. May raise
        anything the router raises.
    routing:
        The request's :class:`SourceRouting`, or ``None`` for a
        single-source deployment, in which case this is just
        ``generate(segments)``.
    segments:
        The prompt for this attempt (the routing's base prompt, plus any
        correction text the caller appended to its question segment).

    Returns
    -------
    tuple[RouteResult, PromptSegments | None]
        The model's result, and ``None`` -- or, when the retry was used and
        answered, the new source's base prompt. A caller that keeps
        correction state must restart it from that prompt: the corrections
        it accumulated describe tables of the other source.

    Raises
    ------
    Exception
        Whatever *generate* raises, unchanged. In particular the second
        ``OUT_OF_SCOPE`` (the retry's) propagates, so a request costs at
        most one extra model call and still ends in ``OUT_OF_SCOPE`` when
        no source can answer.

    Examples
    --------
    >>> sel = SourceSelection("sales", "default", ("sales", "inventory"))
    >>> routing = SourceRouting(sel, lambda s: PromptSegments(static_prefix=s, question="q"))
    >>> calls = []
    >>> def generate(segments):
    ...     calls.append(segments.static_prefix)
    ...     if segments.static_prefix == "sales":
    ...         raise ValueError("OUT_OF_SCOPE")
    ...     return RouteResult(text="SELECT 1", structured=None, meta={}, provider="p", fallback_used=False)
    >>> result, replaced = generate_with_source_fallback(generate, routing, routing.segments)
    >>> result.text, replaced.static_prefix, calls
    ('SELECT 1', 'inventory', ['sales', 'inventory'])

    With no routing there is no retry:

    >>> generate_with_source_fallback(generate, None, PromptSegments(static_prefix="sales", question="q"))
    Traceback (most recent call last):
        ...
    ValueError: OUT_OF_SCOPE
    """
    try:
        return generate(segments), None
    except Exception as exc:  # noqa: BLE001 - only OUT_OF_SCOPE is handled here
        if routing is None or not is_out_of_scope_failure(exc) or not routing.can_fall_back():
            raise
    # The retry. Its own failure -- a second OUT_OF_SCOPE included -- is not
    # caught: one extra attempt, never more.
    replacement = routing.fall_back()
    return generate(replacement), replacement


def choose_source(
    question: str, context: object, *, previous_source: str | None = None,
) -> str | None:
    """The source to build *question*'s prompt for, or ``None`` for a single source.

    For the callers that make one model call and keep no retry state (the
    evaluation harness, the REPL's :mod:`llm.wizard_llm`): they build their
    prompt with ``source=choose_source(...)``.

    Examples
    --------
    >>> import config as cfg
    >>> from core.models import RetrievalContext
    >>> with cfg.override_settings(project_config_dir="/nonexistent"):
    ...     choose_source("anything", RetrievalContext()) is None
    True
    """
    selection = select_source_for_question(
        question, context, previous_source=previous_source,
    )
    return None if selection is None else selection.chosen


def continuity_audit(previous_source: str | None) -> dict[str, Any] | None:
    """The ``datasource_selection`` of a turn that inherits the previous turn's source.

    A refinement composed over the previous turn's SQL (``session.composer``)
    reads whatever that statement read, so it stays on the previous source
    without any selection.

    Parameters
    ----------
    previous_source:
        The previous turn's source, or ``None`` when unknown.

    Returns
    -------
    dict | None
        ``{"chosen": previous_source, "reason": "session", "candidates":
        [previous_source], "fallback_from": None}``; ``None`` when
        *previous_source* is ``None`` or fewer than two sources are
        configured (the single-source audit record has no selection).

    Examples
    --------
    >>> continuity_audit(None) is None
    True
    >>> from unittest.mock import patch
    >>> with patch("database.datasources.datasource_names", return_value=("sales", "inventory")):
    ...     continuity_audit("inventory")
    {'chosen': 'inventory', 'reason': 'session', 'candidates': ['inventory'], 'fallback_from': None}
    """
    if previous_source is None:
        return None
    import database.datasources as datasources

    if len(datasources.datasource_names()) < 2:
        return None
    return SourceSelection(
        previous_source, REASON_SESSION, (previous_source,),
    ).audit()
