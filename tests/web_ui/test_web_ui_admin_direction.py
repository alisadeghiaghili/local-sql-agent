# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The admin panel is Persian; some of what it displays is not.

`dir="rtl"` sets the paragraph direction for the whole page, and the bidi
algorithm then reorders neutral characters — punctuation, brackets,
slashes, dots — around the strong characters near them. For Persian prose
that is exactly right. For the English technical output this panel
displays, it is wrong in a way that is easy to miss and hard to read past.

Two shapes were live, both found by looking at a running panel rather than
at the code:

* Deployment-check cards carry `scripts/verify_deployment.py`'s own
  output — exception text, SQL, connection URLs, Windows file paths.
  Rendered RTL, ``Settings.validate()`` displayed as
  ``()Settings.validate``, and a connection URL broke across lines in the
  wrong order. This is output an operator reads character by character to
  find a typo in.

* The keys table's source column shows ``.env``. Its leading dot is a
  neutral character, so under an RTL paragraph it moved to the end and
  the cell read ``env.`` — a filename, displayed wrong, in the column that
  says where a key came from.

The second is the reason these are CSS assertions rather than a review
habit: it is four characters long, it looks almost right, and no amount of
reading the markup reveals it. It was caught by rendering the panel and
looking.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ADMIN_CSS = _REPO_ROOT / "web" / "admin" / "admin.css"


def _rule(selector: str) -> str:
    """The declaration block for *selector*, as written."""
    css = _ADMIN_CSS.read_text(encoding="utf-8")
    pattern = re.escape(selector) + r"\s*\{(.*?)\}"
    match = re.search(pattern, css, re.S)
    assert match, f"{selector} is gone from web/admin/admin.css"
    return match.group(1)


class TestEnglishOutputIsNotLaidOutRightToLeft:
    def test_deployment_check_cards_are_ltr(self):
        rule = _rule(".admin-check")
        assert re.search(r"direction\s*:\s*ltr", rule), (
            "the deployment-check card no longer pins direction: ltr. Its "
            "content is verify_deployment's English output -- exception "
            "text, SQL, URLs, file paths -- and RTL reorders it: "
            "Settings.validate() renders as ()Settings.validate"
        )

    def test_deployment_check_cards_align_with_their_direction(self):
        """LTR text right-aligned is readable but wrong-looking, and the
        card is one English sentence across name, pill and detail."""
        assert re.search(r"text-align\s*:\s*left", _rule(".admin-check"))


class TestMixedLanguageFieldsTakeDirectionFromContent:
    """`plaintext` rather than a forced direction, because the markup
    genuinely cannot know: the same field holds a Persian maintenance note
    on one row and a Windows path on the next."""

    @pytest.mark.parametrize("selector", [".admin-kv dd", "#keys-body td"])
    def test_the_field_uses_plaintext(self, selector):
        assert re.search(r"unicode-bidi\s*:\s*plaintext", _rule(selector)), (
            f"{selector} no longer takes its direction from its content. A "
            "leading dot is a neutral character, so '.env' reorders to "
            "'env.' under RTL"
        )


class TestIdentifiersAreIsolated:
    """A principal id or role name is a token, not prose. Isolating each
    one keeps a hyphen or dot inside it from being reordered by whatever
    surrounds it — the treatment `style.css` already gives numbers."""

    @pytest.mark.parametrize("selector", [".key-role", "#keys-body code"])
    def test_the_identifier_is_isolated(self, selector):
        rule = _rule(selector)
        assert re.search(r"unicode-bidi\s*:\s*isolate", rule)
        assert re.search(r"direction\s*:\s*ltr", rule)


def test_the_panel_is_still_rtl():
    """The reason all of the above is needed. If the panel ever stops
    being RTL, these rules should be revisited rather than kept out of
    habit."""
    html = (_REPO_ROOT / "web" / "admin" / "index.html").read_text(encoding="utf-8")
    assert 'dir="rtl"' in html
