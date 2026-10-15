# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Keeps the English and Persian setup guides in step.

``docs/fa/getting-started.md`` and ``docs/en/getting-started.md`` are one
guide in two languages. A guide that is edited in one language and not the
other quietly stops being the same guide: a step, a flag or a warning
appears on one side only. This module compares the *structure* of the two
files, which survives translation, and fails with the first section that
differs:

* the headings, level by level, with the same section numbers (Persian
  digits are folded to ASCII first);
* the fenced code blocks, in total and section by section;
* the multiset of command-line flags (``--check``) across the whole file;
* the multiset of environment-variable names written as ``NAME=``;
* the set of documents the guide links to or names (``docs/...md``).

Prose is never compared, so translating a sentence differently never fails
this test; dropping a section, a code block, a flag or a document does.

Each comparison is a pure function over the text of the two guides
(``check_*``), so the tests at the bottom can show that a deliberately broken
copy is caught without touching the real files.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FA_GUIDE = _REPO_ROOT / "docs" / "fa" / "getting-started.md"
_EN_GUIDE = _REPO_ROOT / "docs" / "en" / "getting-started.md"

#: Persian (U+06F0..U+06F9) and Arabic-Indic (U+0660..U+0669) digits to ASCII.
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
#: A section number at the start of a heading, with the word for "part" in
#: either language in front of it ("Part 0", "بخش 0", "0.3.1").
_HEADING_NUMBER = re.compile(r"^(?:Part\s+|بخش\s+)?(\d+(?:\.\d+)*)(?![\d.]*\w)")
_FLAG = re.compile(r"--[a-z][a-z0-9-]+")
_ENV_NAME = re.compile(r"\b([A-Z][A-Z0-9_]{2,})=")
_MD_LINK = re.compile(r"\]\(([^)\s]+)\)")
_DOC_NAME = re.compile(r"(?<![\w/.-])((?:docs/[\w./-]+|[\w-]+)\.md)\b")
#: The language switch links to the other guide; that is the one link the
#: two files are *supposed* to differ in.
_COUNTERPART = re.compile(r"(?:\.\./)?(?:en|fa)/getting-started\.md$")


def normalise_digits(text: str) -> str:
    """Fold Persian and Arabic-Indic digits to ASCII digits.

    Parameters
    ----------
    text : str
        Any text.

    Returns
    -------
    str
        *text* with every ``۰-۹`` and ``٠-٩`` replaced by ``0-9``.

    Examples
    --------
    >>> normalise_digits("بخش ۲.۵.۱ و ٣")
    'بخش 2.5.1 و 3'
    """
    return text.translate(_DIGITS)


@dataclass(frozen=True)
class Heading:
    """One markdown heading outside a code block.

    Attributes
    ----------
    level : int
        Number of ``#`` characters.
    number : str
        Section number with ASCII digits (``"2.5.1"``), or ``""`` when the
        heading is not numbered.
    title : str
        The heading text as written.
    line : int
        1-based line number in the file.
    """

    level: int
    number: str
    title: str
    line: int

    def label(self) -> str:
        """Return a short name for messages, e.g. ``"§2.5.1 (line 874)"``."""
        name = f"§{self.number}" if self.number else repr(self.title)
        return f"{name} (line {self.line})"


@dataclass
class Guide:
    """The structure of one guide, as far as the parity checks need it.

    Attributes
    ----------
    headings : list of Heading
        Every heading outside a fenced block, in file order.
    blocks : list of int
        For each fenced block, the index of the section it sits in (the
        number of headings seen before it; ``0`` is the preamble).
    section_text : list of str
        The full text (code blocks included) of each section; entry ``i`` is
        the text under ``headings[i - 1]``, entry ``0`` the preamble.
    """

    headings: list[Heading] = field(default_factory=list)
    blocks: list[int] = field(default_factory=list)
    section_text: list[str] = field(default_factory=lambda: [""])

    def section_label(self, index: int) -> str:
        """Return the heading label for section *index* (``0`` = preamble)."""
        if index == 0:
            return "the preamble (before the first heading)"
        return self.headings[index - 1].label()


def parse_guide(text: str) -> Guide:
    """Split a guide into headings, fenced blocks and per-section text.

    A fence is a line that, after any blockquote markers and indentation,
    starts with three backticks; a ``#`` line inside a fence is a comment,
    not a heading.

    Parameters
    ----------
    text : str
        The markdown source.

    Returns
    -------
    Guide
        The parsed structure.

    Examples
    --------
    >>> g = parse_guide("# T\\n\\n## بخش ۱ — x\\n\\n```sh\\n# c\\n```\\n")
    >>> [(h.level, h.number) for h in g.headings]
    [(1, ''), (2, '1')]
    >>> g.blocks
    [2]
    """
    guide = Guide()
    in_fence = False
    for number, line in enumerate(text.split("\n"), start=1):
        stripped = line.lstrip(" \t>")
        if stripped.startswith("```"):
            if not in_fence:
                guide.blocks.append(len(guide.headings))
            in_fence = not in_fence
        elif not in_fence:
            match = _HEADING.match(line)
            if match:
                title = match.group(2)
                num = _HEADING_NUMBER.match(normalise_digits(title))
                guide.headings.append(
                    Heading(len(match.group(1)), num.group(1) if num else "", title, number)
                )
                guide.section_text.append("")
        guide.section_text[-1] += line + "\n"
    return guide


def check_headings(fa: Guide, en: Guide) -> list[str]:
    """Compare the heading outline of the two guides.

    The outline is the sequence of ``(level, section number)`` pairs; titles
    are free to differ because they are translated.

    Parameters
    ----------
    fa, en : Guide
        The parsed Persian and English guides.

    Returns
    -------
    list of str
        Empty when the outlines agree; otherwise one message that names the
        first heading where they part, in both files.

    Examples
    --------
    >>> a = parse_guide("# A\\n## بخش ۱ — x\\n## بخش ۲ — y\\n")
    >>> b = parse_guide("# A\\n## Part 1 — x\\n")
    >>> check_headings(a, b)[0].startswith("headings differ at position 3")
    True
    >>> check_headings(a, a)
    []
    """
    fa_outline = [(h.level, h.number) for h in fa.headings]
    en_outline = [(h.level, h.number) for h in en.headings]
    if fa_outline == en_outline:
        return []
    for index in range(max(len(fa_outline), len(en_outline))):
        left = fa.headings[index].label() if index < len(fa.headings) else "no heading"
        right = en.headings[index].label() if index < len(en.headings) else "no heading"
        if index >= len(fa_outline) or index >= len(en_outline) or (
            fa_outline[index] != en_outline[index]
        ):
            last_ok = fa.headings[index - 1].label() if index else "the start of the file"
            return [
                f"headings differ at position {index + 1}: "
                f"fa has {_describe(fa_outline, index)} {left}, "
                f"en has {_describe(en_outline, index)} {right}; "
                f"the last heading they agree on is {last_ok}"
            ]
    return []  # pragma: no cover - the outlines differ, so the loop returns


def _describe(outline: list[tuple[int, str]], index: int) -> str:
    """Return ``"level 3 §2.5"`` for outline entry *index*, or ``"nothing"``."""
    if index >= len(outline):
        return "nothing"
    level, number = outline[index]
    return f"level {level}" + (f" §{number}" if number else " (unnumbered)")


def check_code_blocks(fa: Guide, en: Guide) -> list[str]:
    """Compare the number of fenced code blocks, section by section.

    Parameters
    ----------
    fa, en : Guide
        The parsed Persian and English guides.

    Returns
    -------
    list of str
        Empty when the totals and every section agree; otherwise a message
        naming the first section whose block count differs (or, when the
        outlines differ too, the totals).

    Examples
    --------
    >>> a = parse_guide("# A\\n## B\\n```\\nx\\n```\\n## C\\n```\\ny\\n```\\n")
    >>> b = parse_guide("# A\\n## B\\n```\\nx\\n```\\n## C\\n")
    >>> check_code_blocks(a, b)[0].startswith("code blocks differ in")
    True
    """
    if len(fa.blocks) == len(en.blocks) and fa.blocks == en.blocks:
        return []
    shared = min(len(fa.headings), len(en.headings)) + 1
    fa_counts, en_counts = Counter(fa.blocks), Counter(en.blocks)
    for index in range(shared):
        if fa_counts[index] != en_counts[index]:
            return [
                f"code blocks differ in {fa.section_label(index)}: "
                f"fa has {fa_counts[index]}, en has {en_counts[index]} "
                f"(totals: fa {len(fa.blocks)}, en {len(en.blocks)})"
            ]
    return [f"code block totals differ: fa {len(fa.blocks)}, en {len(en.blocks)}"]


def _counter_diff(fa: Counter[str], en: Counter[str]) -> str:
    """Return ``"only in fa: ...; only in en: ..."`` for two counters."""
    only_fa = fa - en
    only_en = en - fa
    parts = []
    if only_fa:
        parts.append("only in fa: " + _format_counter(only_fa))
    if only_en:
        parts.append("only in en: " + _format_counter(only_en))
    return "; ".join(parts)


def _format_counter(counter: Counter[str]) -> str:
    """Format ``Counter({'--a': 2})`` as ``"--a x2"`` (sorted)."""
    return ", ".join(f"{k} x{n}" if n > 1 else k for k, n in sorted(counter.items()))


def _check_multiset(fa: Guide, en: Guide, pattern: re.Pattern[str], what: str) -> list[str]:
    """Compare the multiset of *pattern* matches over the whole of both files.

    When the whole-file multisets differ, the message names the first section
    whose own multiset differs, which is where the edit was missed.
    """
    fa_all: Counter[str] = Counter()
    en_all: Counter[str] = Counter()
    for text in fa.section_text:
        fa_all.update(_matches(pattern, text))
    for text in en.section_text:
        en_all.update(_matches(pattern, text))
    if fa_all == en_all:
        return []
    where = ""
    for index in range(min(len(fa.section_text), len(en.section_text))):
        left = Counter(_matches(pattern, fa.section_text[index]))
        right = Counter(_matches(pattern, en.section_text[index]))
        if left != right:
            where = (
                f" The first section that differs is {fa.section_label(index)} in fa "
                f"({_counter_diff(left, right)})."
            )
            break
    return [f"{what} differ: {_counter_diff(fa_all, en_all)}.{where}"]


def _matches(pattern: re.Pattern[str], text: str) -> list[str]:
    """Return the matches of *pattern* in *text*, group 1 if it has one."""
    return [m.group(1) if pattern.groups else m.group(0) for m in pattern.finditer(text)]


def check_flags(fa: Guide, en: Guide) -> list[str]:
    """Compare the command-line flags (``--check``, ``--no-server-header``).

    Parameters
    ----------
    fa, en : Guide
        The parsed Persian and English guides.

    Returns
    -------
    list of str
        Empty when both guides use every flag the same number of times.

    Examples
    --------
    >>> a = parse_guide("# A\\nrun `x --check --check`\\n")
    >>> b = parse_guide("# A\\nrun `x --check`\\n")
    >>> check_flags(a, b)[0].startswith("command-line flags differ: only in fa: --check")
    True
    """
    return _check_multiset(fa, en, _FLAG, "command-line flags")


def check_env_names(fa: Guide, en: Guide) -> list[str]:
    """Compare the environment variables written as ``NAME=``.

    Parameters
    ----------
    fa, en : Guide
        The parsed Persian and English guides.

    Returns
    -------
    list of str
        Empty when both guides set every variable the same number of times.

    Examples
    --------
    >>> a = parse_guide("# A\\n`LLM_PROVIDER=mock`\\n")
    >>> check_env_names(a, parse_guide("# A\\n")) != []
    True
    """
    return _check_multiset(fa, en, _ENV_NAME, "environment variables")


def link_targets(guide: Guide) -> set[str]:
    """Return the documents a guide links to or names, file part only.

    Both markdown links (``[x](../deployment-runbook.md#s17)``) and bare
    names in code spans (``docs/db-hardening.md``) count; anchors are cut off
    and the link to the other-language guide is left out.

    Parameters
    ----------
    guide : Guide
        A parsed guide.

    Returns
    -------
    set of str
        The file parts, e.g. ``{"docs/db-hardening.md", "CHANGELOG.md"}``.

    Examples
    --------
    >>> g = parse_guide("# A\\n[x](../fa/getting-started.md) [y](../a.md#z) `docs/b.md`\\n")
    >>> sorted(link_targets(g))
    ['../a.md', 'docs/b.md']
    """
    found: set[str] = set()
    for text in guide.section_text:
        for target in _matches(_MD_LINK, text):
            path = target.split("#", 1)[0]
            if path and not _COUNTERPART.search(path):
                found.add(path)
        found.update(
            name
            for name in _matches(_DOC_NAME, text)
            if not _COUNTERPART.search(name)
        )
    return found


def check_links(fa: Guide, en: Guide) -> list[str]:
    """Compare the set of linked and named documents.

    Parameters
    ----------
    fa, en : Guide
        The parsed Persian and English guides.

    Returns
    -------
    list of str
        Empty when both guides point to the same documents.

    Examples
    --------
    >>> a = parse_guide("# A\\nsee `docs/x.md` and [y](../y.md)\\n")
    >>> b = parse_guide("# A\\nsee `docs/x.md`\\n")
    >>> check_links(a, b)
    ['linked documents differ: only in fa: ../y.md']
    """
    fa_links, en_links = link_targets(fa), link_targets(en)
    if fa_links == en_links:
        return []
    return [
        "linked documents differ: "
        + _counter_diff(Counter(fa_links), Counter(en_links))
    ]


_CHECKS = (
    check_headings,
    check_code_blocks,
    check_flags,
    check_env_names,
    check_links,
)


def _load(path: Path) -> Guide:
    """Read and parse one guide file as UTF-8."""
    return parse_guide(path.read_text(encoding="utf-8"))


# --- the real guides ------------------------------------------------------


@pytest.fixture(scope="module")
def guides() -> tuple[Guide, Guide]:
    """The Persian and English guides, parsed once."""
    return _load(_FA_GUIDE), _load(_EN_GUIDE)


def test_both_guides_exist() -> None:
    """The English guide is not optional: it ships next to the Persian one."""
    assert _FA_GUIDE.is_file(), f"missing {_FA_GUIDE.relative_to(_REPO_ROOT)}"
    assert _EN_GUIDE.is_file(), f"missing {_EN_GUIDE.relative_to(_REPO_ROOT)}"


def test_headings_match(guides: tuple[Guide, Guide]) -> None:
    """Same headings at each level, in order, with the same section numbers."""
    assert check_headings(*guides) == []


def test_code_blocks_match(guides: tuple[Guide, Guide]) -> None:
    """Same number of fenced code blocks, section by section."""
    assert check_code_blocks(*guides) == []


def test_flags_match(guides: tuple[Guide, Guide]) -> None:
    """Every command-line flag appears as often in one language as the other."""
    assert check_flags(*guides) == []


def test_env_names_match(guides: tuple[Guide, Guide]) -> None:
    """Every ``NAME=`` environment setting appears as often in both."""
    assert check_env_names(*guides) == []


def test_linked_documents_match(guides: tuple[Guide, Guide]) -> None:
    """Both guides point to the same documents."""
    assert check_links(*guides) == []


def test_each_guide_links_to_the_other() -> None:
    """The language switch is there, and points the right way."""
    fa = _FA_GUIDE.read_text(encoding="utf-8")
    en = _EN_GUIDE.read_text(encoding="utf-8")
    assert "](../en/getting-started.md)" in "\n".join(fa.split("\n")[:5])
    assert "](../fa/getting-started.md)" in "\n".join(en.split("\n")[:5])


# --- the checks themselves: a broken copy must be caught -------------------


def test_normalise_digits_folds_persian_and_arabic_indic() -> None:
    """``۲.۵.۱`` and ``٢.٥.١`` are both section ``2.5.1``."""
    assert normalise_digits("۲.۵.۱") == "2.5.1" == normalise_digits("٢.٥.١")


def test_section_numbers_survive_translation() -> None:
    """``بخش ۰`` / ``Part 0`` and ``۰.۳.۱`` / ``0.3.1`` give the same numbers."""
    fa = parse_guide("## بخش ۰ — پیش‌نیازها\n### ۰.۳.۱ ویزارد\n#### بدون شماره\n")
    en = parse_guide("## Part 0 — Prerequisites\n### 0.3.1 The wizard\n#### Unnumbered\n")
    assert [(h.level, h.number) for h in fa.headings] == [
        (2, "0"),
        (3, "0.3.1"),
        (4, ""),
    ]
    assert check_headings(fa, en) == []


def test_removed_heading_is_reported_with_its_section(tmp_path: Path) -> None:
    """A copy of the English guide with one heading removed fails, by name."""
    broken = tmp_path / "getting-started.md"
    lines = _EN_GUIDE.read_text(encoding="utf-8").split("\n")
    target = next(i for i, line in enumerate(lines) if line.startswith("### 2.5.1 "))
    del lines[target]
    broken.write_text("\n".join(lines), encoding="utf-8")

    problems = check_headings(_load(_FA_GUIDE), _load(broken))

    assert len(problems) == 1
    assert "§2.5.1" in problems[0]
    assert "headings differ at position" in problems[0]


def test_changed_section_number_is_reported() -> None:
    """Renumbering a heading is a difference, even if the title is the same."""
    fa = parse_guide("# A\n## بخش ۱ — x\n### ۱.۱ y\n")
    en = parse_guide("# A\n## Part 1 — x\n### 1.2 y\n")
    assert "§1.1" in check_headings(fa, en)[0]


def test_removed_code_block_names_the_section(tmp_path: Path) -> None:
    """Dropping one fenced block from a copy points at the section it was in."""
    lines = _EN_GUIDE.read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("### 1.1 "))
    fence = [i for i in range(start, len(lines)) if lines[i].startswith("```")][:2]
    del lines[fence[0] : fence[1] + 1]
    broken = tmp_path / "getting-started.md"
    broken.write_text("\n".join(lines), encoding="utf-8")

    problems = check_code_blocks(_load(_FA_GUIDE), _load(broken))

    assert len(problems) == 1
    assert "§1.1" in problems[0]


def test_dropped_flag_is_reported_with_its_section() -> None:
    """A flag present in one language only fails, and the section is named."""
    fa = parse_guide("# A\n## B\nuse `--dry-run`\n## C\nuse `--resume`\n")
    en = parse_guide("# A\n## B\nuse `--dry-run`\n## C\nuse resume\n")
    problem = check_flags(fa, en)[0]
    assert "--resume" in problem
    assert "'C'" in problem


def test_flag_counts_are_a_multiset() -> None:
    """Using a flag twice in one language and once in the other is a difference."""
    fa = parse_guide("# A\n`--check` and `--check`\n")
    en = parse_guide("# A\n`--check`\n")
    assert check_flags(fa, en) != []


def test_dropped_env_name_is_reported() -> None:
    """An environment variable set in one guide only fails."""
    fa = parse_guide("# A\n```ini\nAPI_KEYS_FILE=x\nLLM_PROVIDER=mock\n```\n")
    en = parse_guide("# A\n```ini\nAPI_KEYS_FILE=x\n```\n")
    problem = check_env_names(fa, en)[0]
    assert "LLM_PROVIDER" in problem


def test_extra_link_is_reported() -> None:
    """A document named in one guide only fails; the language switch does not."""
    fa = parse_guide("# A\n[English](../en/getting-started.md)\n`docs/x.md`\n")
    en = parse_guide("# A\n[فارسی](../fa/getting-started.md)\n`docs/x.md` `docs/y.md`\n")
    assert check_links(fa, en) == ["linked documents differ: only in en: docs/y.md"]


def test_hash_lines_inside_a_fence_are_not_headings() -> None:
    """A ``# comment`` in a code block must not shift the heading outline."""
    guide = parse_guide("# A\n```powershell\n# 1. a comment\n```\n## B\n")
    assert [h.title for h in guide.headings] == ["A", "B"]
    assert guide.blocks == [1]


def test_fence_inside_a_blockquote_is_a_fence() -> None:
    """The guide has fenced blocks inside ``>`` quotes; they count once each."""
    guide = parse_guide("# A\n> ```ini\n> X=1\n> ```\n")
    assert guide.blocks == [1]
