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
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import IO, Any

import yaml
from yaml.constructor import ConstructorError

__all__ = ["safe_load_strict"]

_MERGE_TAG = "tag:yaml.org,2002:merge"


class _StrictLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses a mapping with a repeated explicit key."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        # Nodes already checked. PyYAML's flatten_mapping rewrites a merge
        # target's node in place, so a node reached a second time already
        # holds its merged keys and an explicit override of one would look
        # like a duplicate.
        self._checked: set[yaml.Node] = set()

    def _refuse_duplicate_keys(self, node: yaml.Node) -> None:
        if not isinstance(node, yaml.MappingNode) or node in self._checked:
            return
        self._checked.add(node)
        first_line: dict[Any, int] = {}
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, Hashable):
                continue  # PyYAML raises its own "unhashable key" error
            line = key_node.start_mark.line + 1
            if key in first_line:
                # No marks: a mark renders as a multi-line snippet that names
                # "<unicode string>" rather than the file, and the lines are
                # already in the message.
                raise ConstructorError(
                    problem=(
                        f"duplicate key {key!r} "
                        f"(first on line {first_line[key]}, again on line {line})"
                    )
                )
            first_line[key] = line

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        # Reached for merge targets too, which construct_mapping never sees.
        self._refuse_duplicate_keys(node)
        super().flatten_mapping(node)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        # Before flattening: afterwards merged keys sit among the node's own.
        self._refuse_duplicate_keys(node)
        return super().construct_mapping(node, deep=deep)


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
        For anything ``yaml.safe_load`` refuses. A repeated key raises
        :class:`yaml.constructor.ConstructorError` whose message names the
        key and both 1-based line numbers.

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
    """
    # SafeLoader subclass, so it builds the same plain types safe_load does.
    return yaml.load(stream, Loader=_StrictLoader)  # noqa: S506
