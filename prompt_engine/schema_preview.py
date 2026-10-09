# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Build the prompt as it would be for a ``schema.yaml`` that is not on disk yet.

The static prefix (:func:`prompt_engine.static_prefix.build_static_prefix`)
is assembled from the schema the process loaded at start-up. An operator who
is about to replace ``schema.yaml`` (``scripts/sync_schema.py`` writes a
proposal next to it) wants to know what the replacement does to the size of
that prefix *before* replacing it, using the very builder the server uses
rather than a second implementation of it. :func:`schema_text_in_effect`
makes that possible: for the length of a ``with`` block the registry, and the
prefix cache, answer for the given schema text.

It changes process-wide state, as :func:`config.override_settings` does, so
it is for single-threaded tools and tests, never for code serving requests.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import prompt_engine.static_prefix as static_prefix
from schema_data.registry import get_table_columns, schema_in_effect, validate_schema_yaml_text

__all__ = ["schema_text_in_effect"]


@contextmanager
def schema_text_in_effect(text: str) -> Iterator[None]:
    """Make the prompt builders see the ``schema.yaml`` *text* instead of the loaded one.

    Inside the block :class:`~schema_data.registry.SchemaRegistry`, the
    ``get_*`` accessors and :func:`~prompt_engine.static_prefix.build_static_prefix`
    (and so :func:`~prompt_engine.static_prefix.static_prefix_token_estimate` and
    :func:`~prompt_engine.static_prefix.should_use_static_prefix`) describe
    *text*. The data sources, business rules, metrics and examples are the
    deployment's own. On leaving the block everything is as it was; the
    prefix cache is emptied on the way in and out, so no prefix built for
    one schema is ever served for another.

    Parameters
    ----------
    text:
        The ``schema.yaml`` text, byte-order mark removed.

    Yields
    ------
    None

    Raises
    ------
    ValueError
        If *text* is not a valid ``schema.yaml``.

    Examples
    --------
    >>> text = "tables: {Widget: {description: d, columns: {ID: key}}}"
    >>> with schema_text_in_effect(text):
    ...     "Table: Widget" in static_prefix.build_static_prefix("You are a T-SQL expert.")
    True
    """
    config = validate_schema_yaml_text(text)
    static_prefix.build_static_prefix.cache_clear()
    # ``static_prefix`` imported ``TABLE_COLUMNS`` by name, which is a
    # snapshot of the loaded schema's columns; the single-source prefix
    # lists its relationships from it.
    saved_columns = static_prefix.TABLE_COLUMNS
    try:
        with schema_in_effect(config):
            static_prefix.TABLE_COLUMNS = get_table_columns()
            yield
    finally:
        static_prefix.TABLE_COLUMNS = saved_columns
        static_prefix.build_static_prefix.cache_clear()
