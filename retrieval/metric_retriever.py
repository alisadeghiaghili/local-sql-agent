# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Metric retriever: the tables a mentioned metric's expression depends on.

Every metric definition is always part of the prompt (``METRICS`` section),
so a metric never has to be *found*. What a question that says "total
invoiced" does not say is where that figure lives: the metric's expression
(``SUM(Invoice.Amount)``) names the table, the question does not, and the
table retrievers never see the metric's words. Without the table the model
holds a definition it cannot use, because the schema block and the JOIN
lines do not cover it.

This module closes that gap in two steps. :func:`mentioned_metrics` finds the
metrics a question names, by key or alias, as whole words. :func:`expression_tables`
reads the tables out of a metric's expression, which is the single source of
truth for them -- ``metrics.yaml`` carries no separate list that could drift
from the expression.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from knowledge.metrics import METRICS
from retrieval.source_selector import keyword_hits
from retrieval.table_keys import resolve_table_ref

logger = logging.getLogger(__name__)

#: SQL Server is the only execution target (see ``security.sql_guard``), and
#: metric expressions are written for it.
_DIALECT = "tsql"


def _phrases(key: str, spec: Mapping[str, Any]) -> list[str]:
    """The words that name one metric: its key, the key read as words, its aliases."""
    names = [key, key.replace("_", " "), *(spec.get("aliases") or [])]
    return list(dict.fromkeys(name for name in names if name))


def mentioned_metrics(
    question: str,
    metrics: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Keys of the metrics *question* names, by key or by alias.

    Parameters
    ----------
    question:
        The natural-language question, in Persian or English.
    metrics:
        ``{key: {"aliases": [...], "expression": "..."}}``. Defaults to the
        configured ``METRICS``.

    Returns
    -------
    list[str]
        Metric keys in configuration order, without duplicates. A name matches
        as whole words after Persian/Arabic letter folding and case folding, so
        ``total_value`` does not match ``total_values`` and ``SUM OF VALUE``
        matches ``sum of value``.

    Examples
    --------
    >>> metrics = {"total_value": {"aliases": ["sum of value"], "expression": "SUM(T.V)"}}
    >>> mentioned_metrics("What is the SUM OF VALUE per month?", metrics)
    ['total_value']
    >>> mentioned_metrics("the total_values column", metrics)
    []
    >>> mentioned_metrics("anything", {})
    []
    """
    configured = METRICS if metrics is None else metrics
    names = {key: _phrases(key, spec) for key, spec in configured.items()}
    hits = keyword_hits(question, names)
    return [key for key in configured if hits[key]]


def expression_tables(expression: str) -> list[str]:
    """Schema table keys a metric *expression* references as ``Table.Column``.

    Parameters
    ----------
    expression:
        A SQL scalar expression in T-SQL, e.g. ``SUM(Invoice.Amount)`` or
        ``SUM(Invoice.Amount) / NULLIF(SUM(Order.Qty), 0)``.

    Returns
    -------
    list[str]
        ``schema.yaml`` keys in order of first appearance, without duplicates.
        A name the schema does not know is left out, and a bare name that
        several schemas share yields every key it can mean (see
        :func:`retrieval.table_keys.resolve_table_ref`). An expression that
        does not parse gives ``[]`` and a logged warning: this runs on every
        request, so a malformed definition must degrade to "no extra tables",
        not to an error.

    Examples
    --------
    >>> expression_tables("SUM(NoSuchTable.Amount)")
    []
    >>> expression_tables("SUM(")
    []
    """
    try:
        tree = sqlglot.parse_one(f"SELECT {expression}", read=_DIALECT)
    except SqlglotError as exc:
        logger.warning(
            "metric expression %r could not be parsed; its tables are not added (%s)",
            expression, exc,
        )
        return []
    if tree is None:
        return []
    tables: list[str] = []
    for column in tree.find_all(exp.Column):
        if not column.table:
            continue
        reference = ".".join(part for part in (column.catalog, column.db, column.table) if part)
        tables.extend(resolve_table_ref(reference))
    return list(dict.fromkeys(tables))


def metric_tables(
    question: str,
    metrics: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Tables the metrics named in *question* need, in the order found.

    Parameters
    ----------
    question:
        The natural-language question.
    metrics:
        As for :func:`mentioned_metrics`.

    Returns
    -------
    list[str]
        Schema table keys, without duplicates; empty when the question names
        no metric.

    Examples
    --------
    >>> metric_tables("nothing about any metric", {})
    []
    """
    configured = METRICS if metrics is None else metrics
    tables: list[str] = []
    for key in mentioned_metrics(question, configured):
        tables.extend(expression_tables(configured[key].get("expression", "")))
    return list(dict.fromkeys(tables))
