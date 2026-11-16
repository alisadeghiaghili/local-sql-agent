# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Read rules and colours out of the shipped stylesheets, for the UI tests.

The stylesheet tests used to find a rule with ``selector\\s*\\{``. That only
matches a rule written on its own, so grouping two selectors
(``.confirm-toggle, .flag-shortcut { ... }``) made a test report "no rule"
for a rule that was there, and a second rule for the same selector was never
read at all. The helpers here read every rule a selector takes part in, and
resolve the colour expressions those rules use (``var()``, ``color-mix()``)
to a hex value, so a test can state a requirement (the box does not change,
the glyph has 3:1 against its ground) without depending on how the CSS
happens to be written.

This is not a CSS parser. It handles the flat rules and the two colour
functions the UI uses, and fails with a message when it meets something
else, so that a new construct is added here on purpose.
"""

from __future__ import annotations

import re

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z|#[0-9a-fA-F]{3}\Z")
_COLOR_MIX = re.compile(r"color-mix\(\s*in\s+srgb\s*,(.*)\)\Z", re.S)
_WEIGHTED = re.compile(r"(.*?)\s+(\d+(?:\.\d+)?)%\Z", re.S)

# Properties that change the size of a box, or of the text inside it. A rule
# for a pressed or flagged state must set none of them, or the control moves
# what is next to it when it changes state.
_BOX_PROPERTY = re.compile(
    r"\A(?:border|border-width|border-(?:block|inline)(?:-(?:start|end))?(?:-width)?"
    r"|padding(?:-[a-z-]+)?|margin(?:-[a-z-]+)?"
    r"|(?:min-|max-)?(?:width|height|inline-size|block-size)"
    r"|font(?:-size|-weight)?|line-height)\Z"
)


def rules(css: str, selector: str) -> list[str]:
    """Return the body of every rule that *selector* takes part in.

    A rule counts when *selector* is one of its comma-separated selectors,
    compared exactly: ``.a`` does not match ``.a:hover`` or ``.a::before``.
    Rules inside ``@media`` blocks are included.

    Args:
        css: The stylesheet text.
        selector: One selector, written as in the stylesheet.

    Returns:
        The declaration blocks, in source order. Empty if there is no rule.

    Examples:
        >>> rules(".a, .b { color: red; } .a:hover { color: blue; }", ".a")
        [' color: red; ']
        >>> rules("/* .c { } */ .a { x: 1 } .a { y: 2 }", ".a")
        [' x: 1 ', ' y: 2 ']
    """
    found = []
    for match in _RULE.finditer(_COMMENT.sub("", css)):
        if selector in [part.strip() for part in match.group(1).split(",")]:
            found.append(match.group(2))
    return found


def declarations(css: str, selector: str) -> dict[str, str]:
    """Return the declarations *selector* ends up with, later rules winning.

    Specificity is not modelled: the rules are those of one exact selector,
    which all have the same specificity, so source order decides.

    Args:
        css: The stylesheet text.
        selector: One selector, written as in the stylesheet.

    Returns:
        ``{property: value}``.

    Raises:
        AssertionError: If no rule has *selector*.

    Examples:
        >>> declarations(".a { color: red; top: 0 } .a { color: blue }", ".a")
        {'color': 'blue', 'top': '0'}
    """
    bodies = rules(css, selector)
    assert bodies, f"no rule for {selector}"
    merged: dict[str, str] = {}
    for body in bodies:
        for declaration in _split_top_level(body, ";"):
            name, colon, value = declaration.partition(":")
            if colon:
                merged[name.strip()] = value.strip()
    return merged


def box_properties(css: str, selector: str) -> list[str]:
    """Return the box-changing properties that the rules of *selector* set.

    Args:
        css: The stylesheet text.
        selector: One selector, written as in the stylesheet.

    Returns:
        The property names, sorted. Empty when a state only changes colours,
        shadows and the like.

    Raises:
        AssertionError: If no rule has *selector*.

    Examples:
        >>> box_properties(".a { color: red; border-color: blue }", ".a")
        []
        >>> box_properties(".a { border-width: 2px; padding-block: 5px }", ".a")
        ['border-width', 'padding-block']
    """
    bodies = rules(css, selector)
    assert bodies, f"no rule for {selector}"
    names = set()
    for body in bodies:
        for declaration in _split_top_level(body, ";"):
            name = declaration.partition(":")[0].strip()
            if _BOX_PROPERTY.match(name):
                names.add(name)
    return sorted(names)


def base_layer(background: str) -> str:
    """Return the bottom layer of a ``background`` value.

    Layers are separated by commas outside parentheses, and the last one is
    drawn first, under the others.

    Args:
        background: The value of a ``background`` declaration.

    Returns:
        The last layer, stripped.

    Examples:
        >>> base_layer("radial-gradient(red, blue), color-mix(in srgb, red 10%, white)")
        'color-mix(in srgb, red 10%, white)'
        >>> base_layer("var(--card)")
        'var(--card)'
    """
    return _split_top_level(background, ",")[-1]


def resolve_colour(value: str, palette: dict[str, str]) -> str:
    """Resolve a colour expression to ``#rrggbb``.

    Handles a hex literal, ``var(--token)`` for a token *palette* gives as
    hex, and ``color-mix(in srgb, A P%, B)`` of two such colours, nested to
    any depth. The second colour's share is the rest of 100%.

    Args:
        value: The colour expression.
        palette: ``{token name without the leading dashes: hex}`` for one
            theme.

    Returns:
        The colour as lowercase ``#rrggbb``.

    Raises:
        AssertionError: If *value* is not one of the forms above, or names a
            token that *palette* does not give as hex.

    Examples:
        >>> resolve_colour("#FFF", {})
        '#ffffff'
        >>> resolve_colour("var(--ink)", {"ink": "#102030"})
        '#102030'
        >>> resolve_colour("color-mix(in srgb, var(--ink) 50%, #ffffff)", {"ink": "#000000"})
        '#808080'
    """
    value = value.strip()
    if _HEX.match(value):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        return "#" + digits.lower()
    if value.startswith("var(--") and value.endswith(")"):
        token = value[len("var(--"):-1].strip()
        assert token in palette, f"--{token} is not a plain hex token in this theme"
        return resolve_colour(palette[token], palette)
    mix = _COLOR_MIX.match(value)
    if mix:
        parts = _split_top_level(mix.group(1), ",")
        assert len(parts) == 2, f"color-mix() with {len(parts)} colours: {value!r}"
        weighted = _WEIGHTED.match(parts[0])
        assert weighted, f"the first colour of color-mix() has no percentage: {value!r}"
        share = float(weighted.group(2)) / 100
        first = resolve_colour(weighted.group(1), palette)
        second = resolve_colour(parts[1], palette)
        channels = (
            round(int(first[i:i + 2], 16) * share + int(second[i:i + 2], 16) * (1 - share))
            for i in (1, 3, 5)
        )
        return "#" + "".join(f"{channel:02x}" for channel in channels)
    raise AssertionError(
        f"cannot resolve the colour {value!r}. tests/web_ui/css_rules.py reads hex, "
        "var(--token) and color-mix(in srgb, A P%, B); add the new form there"
    )


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split *text* on *separator* where it is outside parentheses.

    Args:
        text: The text to split.
        separator: A single character.

    Returns:
        The non-empty parts, stripped.

    Examples:
        >>> _split_top_level("a(1, 2), b", ",")
        ['a(1, 2)', 'b']
    """
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]
