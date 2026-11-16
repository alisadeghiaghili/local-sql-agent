# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The stylesheet helpers the UI tests rely on (``tests/web_ui/css_rules.py``).

CI runs doctests for the application packages only, so the examples in the
helper module are run from here. The cases below are the ones the two
callers depend on and an example would bury.
"""

from __future__ import annotations

import doctest

import pytest

from tests.web_ui import css_rules
from tests.web_ui.css_rules import base_layer, box_properties, declarations, resolve_colour, rules


def test_the_examples_in_the_module_hold() -> None:
    result = doctest.testmod(css_rules)
    assert result.attempted > 0 and result.failed == 0


def test_a_grouped_selector_is_found_and_a_longer_one_is_not() -> None:
    css = ".a, .b { color: red; } .a:hover { color: blue; } .a::after { opacity: 0; }"
    assert rules(css, ".b") == [" color: red; "]
    assert rules(css, ".a::after") == [" opacity: 0; "]
    assert rules(css, ".c") == []


def test_every_rule_of_a_selector_is_read_not_only_the_first() -> None:
    css = ".s { border-width: 2px; color: red; } .s { color: blue; }"
    assert declarations(css, ".s") == {"border-width": "2px", "color": "blue"}
    assert box_properties(css, ".s") == ["border-width"]


def test_colours_and_shadows_are_not_box_properties() -> None:
    css = ".s { color: red; border-color: blue; background: none; box-shadow: 0 0 0 3px red; }"
    assert box_properties(css, ".s") == []


def test_a_missing_rule_fails_instead_of_passing_empty() -> None:
    with pytest.raises(AssertionError, match="no rule for .x"):
        box_properties(".a { color: red; }", ".x")


def test_the_flat_layer_under_a_gradient_is_resolved() -> None:
    background = (
        "radial-gradient(120% 120% at 30% 18%, color-mix(in srgb, var(--g) 24%, transparent) 0%, transparent 60%),"
        " color-mix(in srgb, var(--g) 13%, var(--card))"
    )
    palette = {"g": "#10b981", "card": "#ffffff"}
    assert resolve_colour(base_layer(background), palette) == "#e0f6ef"


def test_an_unknown_colour_form_fails_with_where_to_add_it() -> None:
    with pytest.raises(AssertionError, match="css_rules.py"):
        resolve_colour("rgba(0, 0, 0, .5)", {})
    with pytest.raises(AssertionError, match="--missing"):
        resolve_colour("var(--missing)", {})
