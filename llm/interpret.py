# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Plain-language summary of a result set.

Lifted out of ``api/runner.py``, where it was reachable only from the
``/query`` path. The conversational engine (``session/engine.py``) needs
the same thing and cannot import from ``api`` -- the API layer sits above
the session layer, not beside it -- so the logic lives here, below both,
and ``api.runner._interpret`` is now a thin wrapper over it.

The move is deliberately a move and not a copy. This function carries a
data-governance gate (see :func:`interpret_rows`), and two copies of a
gate is one copy that gets fixed and one that does not.

What this sends
---------------
Up to :data:`MAX_PREVIEW_ROWS` rows of **real query results** go to
whichever backend the interpretation task routes to. That is the whole
premise of the product when the backend is local, and a genuine
exfiltration path the moment it is not -- which is why the refusal below
is loud and separate from ordinary failure handling.
"""

from __future__ import annotations

import logging
import re

from llm.router import LLMRouter, RemoteProviderNotAllowedError, TaskType

logger = logging.getLogger(__name__)

#: How many result rows are shown to the model. A summary does not improve
#: with more, and every additional row is more real data crossing the
#: boundary this module's docstring describes.
MAX_PREVIEW_ROWS = 20

INTERPRET_TEMPLATE = """
You are a helpful data analyst. The user asked:

{question}

The database returned these results (up to 20 rows shown):

{rows}

Write a concise one-paragraph summary in the same language as the question.
Do not repeat column names literally — describe findings in plain language.
All monetary values are in Iranian Rials (ریال): always express amounts with
the unit Rial/ریال and never use toman/تومان.
If the result is empty, say so clearly.
"""

#: space, NBSP, NNBSP, thin space, ٬
_THOUSAND_SEP = r"[ \u00A0\u202F\u2009\u066C]"  # space, NBSP, NNBSP, thin space, ٬

#: Numbers already written with thousands separators (e.g. "143 066 295 000").
_SEPARATED_NUMBER_RE = re.compile(
    rf"(?<!\d)\d{{1,3}}(?:{_THOUSAND_SEP}\d{{3}})+(?!\d)"
)


def thousands_separate(number: str) -> str:
    """Insert comma thousands separators into a digit string (ASCII or Persian).

    >>> thousands_separate("12000000")
    '12,000,000'
    """
    digits = list(number)
    for i in range(len(digits) - 3, 0, -3):
        digits.insert(i, ",")
    return "".join(digits)


def format_numbers(text: str) -> str:
    """Normalize large numbers to comma thousands-separators.

    Handles both bare runs (``12000000000``) and numbers already separated
    with spaces / NBSP / thin space (``143 066 295 000``).  4-digit Persian
    years like ``1402`` are left alone.

    >>> format_numbers("total 12000000000 rial in 1402")
    'total 12,000,000,000 rial in 1402'
    """
    def _to_commas(match: re.Match) -> str:
        digits = "".join(ch for ch in match.group(0) if ch.isdigit())
        return thousands_separate(digits)

    text = _SEPARATED_NUMBER_RE.sub(_to_commas, text)
    text = re.sub(r"\d{5,}", lambda m: thousands_separate(m.group(0)), text)
    return text


def interpret_rows(router: LLMRouter, question: str, rows: list[dict]) -> str:
    """Ask *router* to summarise *rows* in natural language. Never raises.

    Routed via ``generate_text_for_task(TaskType.INTERPRETATION, ...)`` --
    the same task-based chain, fallback and governance machinery SQL
    generation uses, applied to the interpretation task.
    ``generate_text_for_task`` rather than ``generate_for_task`` because
    this prompt is entirely per-request row data and a question, with no
    static prefix worth segmenting for provider-side caching.

    Data-governance gate
    --------------------
    This sends up to :data:`MAX_PREVIEW_ROWS` rows of real query results
    to whichever backend the interpretation task routes to. While that
    backend is local or otherwise trusted, this is exactly what the
    product promises. The moment it is a hosted provider, it is a genuine
    exfiltration path, and it must not happen silently just because
    ``LLM_PROVIDER`` was set.

    ``LLMRouter._governance_check`` already refuses unless
    ``cfg.settings.llm_allow_remote`` is explicitly true, raising before
    any backend method is called. This function catches that and is loud
    on its own terms -- an ``ERROR`` naming the backend and the row count
    that was about to be sent -- kept distinct from the generic
    "non-fatal" warning below so a refusal is never mistaken for a
    transport hiccup.

    Returns
    -------
    str
        The summary, or ``""`` when it could not be produced. Empty is
        always a valid answer here: an interpretation is an addition to a
        result the caller already has, and failing the whole turn because
        the prose did not come back would trade something for nothing.
    """
    preview_text = "\n".join(str(r) for r in rows[:MAX_PREVIEW_ROWS]) or "(empty result set)"
    prompt = INTERPRET_TEMPLATE.format(question=question, rows=preview_text)
    try:
        route_result = router.generate_text_for_task(TaskType.INTERPRETATION, prompt)
    except RemoteProviderNotAllowedError as exc:
        logger.error(
            "REFUSED interpretation: %d result row(s) would be sent to a remote LLM "
            "provider without LLM_ALLOW_REMOTE=true (%s). Set LLM_ALLOW_REMOTE=true to "
            "explicitly opt this deployment into sending query-result data to a hosted "
            "provider. Interpretation skipped for this request.",
            len(rows), exc,
        )
        return ""
    except Exception as exc:  # noqa: BLE001 - see the Returns note
        logger.warning("Interpretation failed (non-fatal): %s", exc)
        return ""

    summary = (route_result.text or "").strip()
    # Belt-and-suspenders: the model may still write toman despite the prompt rule.
    summary = re.sub(r"toman", "Rial", summary.replace("تومان", "ریال"), flags=re.IGNORECASE)
    return format_numbers(summary)
