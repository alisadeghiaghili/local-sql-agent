# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression: ``PromptSegments`` annotations in ``llm.base`` / ``llm.providers``.

``generate_structured`` and ``generate_with_meta_segments`` were annotated
with the string ``"PromptSegments"`` in modules that never imported the
name (``ruff`` F821). Under ``from __future__ import annotations`` nothing
evaluates an annotation at call time, so the code ran -- but any tool that
resolves hints (``typing.get_type_hints``, ``inspect.signature(...,
eval_str=True)``, Sphinx, pydantic) raised ``NameError``.

The name cannot be imported at runtime in ``llm.base`` (``llm.router``
imports ``llm.base``), so both modules import it under ``TYPE_CHECKING``.
These tests resolve the hints the way a type checker's runtime counterpart
would, by evaluating each module's ``TYPE_CHECKING`` block.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import typing
from types import ModuleType
from typing import Any

import pytest

from llm.router import PromptSegments


def _type_checking_namespace(module: ModuleType) -> dict[str, Any]:
    """Names a module imports only under ``if TYPE_CHECKING:``.

    Args:
        module: An imported module whose source is available.

    Returns:
        Mapping of bound name to the imported object, so it can be passed
        as ``localns`` to :func:`typing.get_type_hints`.

    Raises:
        OSError: If the module's source cannot be read.

    Examples:
        >>> import llm.base
        >>> "PromptSegments" in _type_checking_namespace(llm.base)
        True
    """
    tree = ast.parse(inspect.getsource(module))
    namespace: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            block = ast.Module(body=node.body, type_ignores=[])
            exec(compile(block, module.__file__ or "<module>", "exec"), namespace)  # noqa: S102
    namespace.pop("__builtins__", None)
    return namespace


_TARGETS = [
    ("llm.base", "LLMBackend", "generate_with_meta_segments"),
    ("llm.base", "LLMBackend", "generate_structured"),
    ("llm.providers", "OpenAIBackend", "generate_structured"),
    ("llm.providers", "MockBackend", "generate_structured"),
]


@pytest.mark.parametrize(("module_name", "class_name", "method"), _TARGETS)
def test_prompt_segments_annotation_resolves(
    module_name: str, class_name: str, method: str
) -> None:
    module = importlib.import_module(module_name)
    function = getattr(getattr(module, class_name), method)

    hints = typing.get_type_hints(
        function, localns=_type_checking_namespace(module)
    )

    assert hints["segments"] is PromptSegments


def test_importing_the_modules_does_not_import_router_first() -> None:
    """The ``TYPE_CHECKING`` guard must stay a guard.

    ``llm.router`` imports ``llm.base`` at module level; a runtime import of
    ``llm.router`` from ``llm.base`` would be a cycle.
    """
    source = inspect.getsource(importlib.import_module("llm.base"))
    tree = ast.parse(source)
    top_level_imports = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "llm.router"
        for node in top_level_imports
    )
