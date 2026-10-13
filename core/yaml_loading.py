# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""YAML loading for operator-edited configuration files.

``yaml.safe_load`` keeps the last of two identical keys in one mapping and
says nothing. In a config file that silently discards whatever the first
block described -- an operator who pasted a second ``datasources:`` heading
lost a whole data source with no error. :func:`safe_load_strict` is
``yaml.safe_load`` that refuses such a file instead.

Public API
----------
safe_load_strict(stream) -> Any

Every repeated key in the document is reported in one error, not just the
first: a schema draft with seventy repeated table names should cost the
operator one run to learn about, not seventy.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import IO, Any

import yaml
from yaml.constructor import ConstructorError

__all__ = ["safe_load_strict"]

_MERGE_TAG = "tag:yaml.org,2002:merge"

#: Duplicate keys listed in a multi-key error before "... and N more".
_MAX_LISTED_DUPLICATES = 20


class _StrictLoader(yaml.SafeLoader):
    """``SafeLoader`` that records every repeated explicit key in a mapping.

    Construction is never interrupted by a duplicate: the loader collects
    them all and :meth:`raise_for_duplicates` reports the lot once the whole
    document has been read.
    """

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        # Nodes already checked. PyYAML's flatten_mapping rewrites a merge
        # target's node in place, so a node reached a second time already
        # holds its merged keys and an explicit override of one would look
        # like a duplicate.
        self._checked: set[yaml.Node] = set()
        # (key, 1-based lines of every occurrence), one entry per repeated
        # key per mapping, in the order PyYAML reached the mappings.
        self._duplicates: list[tuple[Any, list[int]]] = []

    def _note_duplicate_keys(self, node: yaml.Node) -> None:
        if not isinstance(node, yaml.MappingNode) or node in self._checked:
            return
        self._checked.add(node)
        lines: dict[Any, list[int]] = {}
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, Hashable):
                continue  # PyYAML raises its own "unhashable key" error
            lines.setdefault(key, []).append(key_node.start_mark.line + 1)
        self._duplicates.extend(
            (key, found) for key, found in lines.items() if len(found) > 1
        )

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        # Reached for merge targets too, which construct_mapping never sees.
        self._note_duplicate_keys(node)
        super().flatten_mapping(node)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        # Before flattening: afterwards merged keys sit among the node's own.
        self._note_duplicate_keys(node)
        return super().construct_mapping(node, deep=deep)

    def raise_for_duplicates(self) -> None:
        """Raise one ``ConstructorError`` naming every duplicate key found.

        No marks are attached: a mark renders as a multi-line snippet that
        names "<unicode string>" rather than the file, and the lines are
        already in the message.
        """
        if not self._duplicates:
            return
        # File order: PyYAML builds shallow mappings before deep ones, so
        # the order they were reached in is not the order they are written.
        found = sorted(self._duplicates, key=lambda item: (item[1][0], repr(item[0])))
        if len(found) == 1:
            key, lines = found[0]
            again = (
                f"line {lines[1]}"
                if len(lines) == 2
                else "lines " + ", ".join(map(str, lines[1:]))
            )
            raise ConstructorError(
                problem=f"duplicate key {key!r} (first on line {lines[0]}, again on {again})"
            )
        listed = [
            f"{key!r} (lines {', '.join(map(str, lines))})"
            for key, lines in found[:_MAX_LISTED_DUPLICATES]
        ]
        if len(found) > _MAX_LISTED_DUPLICATES:
            listed.append(f"... and {len(found) - _MAX_LISTED_DUPLICATES} more")
        raise ConstructorError(
            problem=f"{len(found)} duplicate keys: " + "; ".join(listed)
        )


def safe_load_strict(stream: str | bytes | IO[str] | IO[bytes]) -> Any:
    """Parse one YAML document like ``yaml.safe_load``, refusing duplicate keys.

    Only keys written out explicitly in the same mapping count. A key that
    overrides one pulled in by a merge key (``<<: *anchor``) is legitimate,
    and so is the same key in sibling mappings or in list items.

    Parameters
    ----------
    stream:
        YAML text, or an open file, exactly as ``yaml.safe_load`` accepts.

    Returns
    -------
    Any
        The parsed document; ``None`` for an empty one.

    Raises
    ------
    yaml.YAMLError
        For anything ``yaml.safe_load`` refuses. Repeated keys raise
        :class:`yaml.constructor.ConstructorError`, after the whole document
        has been read so that every one is reported at once, at every
        mapping level. A single repeated key is named with both 1-based line
        numbers; several give ``N duplicate keys:`` followed by each key and
        all its lines, in file order, at most 20 and then ``... and M more``.

    Examples
    --------
    >>> safe_load_strict("a: 1\\nb: {c: 2}\\n")
    {'a': 1, 'b': {'c': 2}}
    >>> safe_load_strict("base: &b {x: 1, y: 2}\\nuse: {<<: *b, x: 9}\\n")["use"]
    {'x': 9, 'y': 2}
    >>> safe_load_strict("datasources:\\n  a: {}\\ndatasources:\\n  b: {}\\n")
    Traceback (most recent call last):
        ...
    yaml.constructor.ConstructorError: duplicate key 'datasources' (first on line 1, again on line 3)
    >>> safe_load_strict("a: 1\\nb: 1\\na: 2\\nb: 2\\n")
    Traceback (most recent call last):
        ...
    yaml.constructor.ConstructorError: 2 duplicate keys: 'a' (lines 1, 3); 'b' (lines 2, 4)
    """
    # SafeLoader subclass, so it builds the same plain types safe_load does.
    loader = _StrictLoader(stream)
    try:
        data = loader.get_single_data()
    finally:
        loader.dispose()
    loader.raise_for_duplicates()
    return data
