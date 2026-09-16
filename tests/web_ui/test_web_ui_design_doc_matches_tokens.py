# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Every value `DESIGN.md` tabulates must be the value `tokens.css` ships.

`docs/design/DESIGN.md` §4 is the human-readable token reference: the table
a designer reads, and the one anyone rebuilding a surface implements from.
`web/styles/tokens.css` is what the browser actually loads. Nothing kept
the two in agreement, and they drifted in the worst possible direction.

The drift this module exists to prevent, concretely: the light palette was
darkened to clear WCAG 1.4.3 (commit "fix(web): meet §8-§12 contrast…"),
moving `--teal` #0d9488 -> #0b7a70, `--teal-d` #0b7a70 -> #09675f, and
`--muted` #64748b -> #5b6b81. `tokens.css` was updated; the §4.2 table was
not. For several releases the document therefore published three hexes that
**fail** the contrast floor the same document sets in §8 — 3.74:1 and
4.40:1 against a 4.5:1 requirement. The CSS was conformant the whole time,
so no contrast test fired: `test_web_ui_contrast_and_failure.py` reads
`tokens.css`, which was right. The only artefact that was wrong was the one
a human reads, which is exactly the artefact no test was looking at.

That is the failure mode worth guarding. A stale doc is not a cosmetic
problem here: it is a set of instructions for reintroducing a fixed
accessibility defect by hand, carrying the authority of the design policy.

Scope note: the table is deliberately a *subset* — §4.2 says status and
source tokens stay in `tokens.css` rather than being tabulated. So this
module asserts agreement on every token the document chooses to give a
value, never that the document is exhaustive. Documenting more is allowed;
documenting something false is not.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DESIGN = _REPO_ROOT / "docs" / "design" / "DESIGN.md"
_TOKENS = _REPO_ROOT / "web" / "styles" / "tokens.css"

_VALUE = re.compile(r"(#[0-9a-fA-F]{3,8}\b|\b\d+(?:\.\d+)?(?:px|rem|em)\b)")
_DECL = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")
_DARK = re.compile(r'prefers-color-scheme:\s*dark|\[data-theme="dark"\]')


def _light_tokens() -> dict[str, str]:
    """Declarations before the first dark-mode block, i.e. the `:root` light set.

    Light and dark redefine the *same* custom properties, so reading the whole
    file would collapse both themes onto one key and compare a documented light
    hex against whichever theme happened to be declared last.
    """
    css = _TOKENS.read_text(encoding="utf-8")
    first_dark = min((m.start() for m in _DARK.finditer(css)), default=len(css))
    return {name: value.strip() for name, value in _DECL.findall(css[:first_dark])}


def _documented_tokens() -> dict[str, str]:
    """Token -> value for every `DESIGN.md` line that names exactly one token.

    Requiring exactly one token per line keeps prose out of the result: a
    sentence mentioning two tokens is commentary, not a specification, and a
    table row is by construction one token and its value.
    """
    documented: dict[str, str] = {}
    for line in _DESIGN.read_text(encoding="utf-8").splitlines():
        names = re.findall(r"--[a-z0-9-]+", line)
        if len(set(names)) != 1:
            continue
        match = _VALUE.search(line.split(names[0], 1)[1])
        if match:
            documented[names[0]] = match.group(1)
    return documented


def test_the_design_doc_documents_at_least_the_core_palette() -> None:
    """Guard the guard: a parser that silently matches nothing proves nothing."""
    documented = _documented_tokens()
    for name in ("--navy", "--teal", "--muted", "--bg", "--card", "--ink"):
        assert name in documented, (
            f"DESIGN.md no longer gives {name} a value. Either the §4.2 palette "
            "table was restructured (update this parser) or the token reference "
            "was dropped -- and an empty parse would make every other assertion "
            "in this module vacuously pass."
        )
    assert len(documented) >= 20, (
        f"only {len(documented)} tokens parsed out of DESIGN.md; the §4 tables "
        "carry far more. The table format probably changed and this parser is "
        "now reading almost nothing, which would hide real drift."
    )


def test_every_documented_token_exists_in_tokens_css() -> None:
    light = _light_tokens()
    orphaned = sorted(set(_documented_tokens()) - set(light))
    assert not orphaned, (
        "DESIGN.md documents tokens that tokens.css does not define: "
        f"{orphaned}. A reader implementing from the table would set custom "
        "properties nothing consumes."
    )


def test_documented_values_equal_the_shipped_values() -> None:
    light = _light_tokens()
    documented = _documented_tokens()
    mismatched = [
        (name, doc_value, light[name])
        for name, doc_value in sorted(documented.items())
        if name in light and doc_value.strip().lower() != light[name].strip().lower()
    ]
    assert not mismatched, (
        "DESIGN.md publishes values that differ from what tokens.css ships.\n"
        + "\n".join(
            f"  {name}: DESIGN.md says {doc!r}, tokens.css ships {css!r}"
            for name, doc, css in mismatched
        )
        + "\n\nThe CSS is what renders, so the document is what is wrong. Fix the "
        "table -- and check WHY it moved before copying: this drift last happened "
        "because the palette was darkened to satisfy WCAG 1.4.3 and the table kept "
        "publishing the failing values."
    )
