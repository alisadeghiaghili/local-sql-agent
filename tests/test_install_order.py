# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The first-install checklists put things in the order that works.

A first install has to go in a particular order, and the guides used to get it
wrong in three ways: the tools that draft files from the databases were run
before the databases were described, the preflight was run before the first API
key existed (and Part 0 of the guide reached the preflight section before the
section that issues a key), and there was no step that could make the
descriptions pass the preflight at all. The three checklists -- the English
guide, the Persian guide and the runbook -- are one list in three places, so
this module reads each of them and checks the same landmarks come in the same
order, with the same step numbers:

1. the data sources are described (``datasources.yaml``);
2. the template is copied into ``project_config/``;
3. ``schema.yaml`` is drafted (the inspector);
4. its structure is checked (``sync_schema.py --check``);
5. the descriptions are drafted (``scripts/draft_descriptions.py``);
6. the wizard drafts entities, aliases, rules and examples (``setup_project.py``);
7. the first admin key is issued (``issue_api_key``);
8. the preflight is run (``verify_deployment``).

Prose is not compared, only where the commands sit. Each check is a pure
function over the text, so the tests at the bottom show that a list in the old
order is caught.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EN_GUIDE = _REPO_ROOT / "docs" / "en" / "getting-started.md"
_FA_GUIDE = _REPO_ROOT / "docs" / "fa" / "getting-started.md"
_RUNBOOK = _REPO_ROOT / "docs" / "deployment-runbook.md"

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

#: Where a checklist starts and what ends it.
_EN_START = re.compile(r"^## Install from scratch, in order\s*$", re.MULTILINE)
_FA_START = re.compile(r"^## نصب از صفر، به ترتیب\s*$", re.MULTILINE)
_RUNBOOK_START = re.compile(r"^### First install, in order\s*$", re.MULTILINE)
_END = re.compile(r"^(?:---|#{1,3} )", re.MULTILINE)

#: ``(name, pattern)`` in the order the steps must come. A landmark is the first
#: step whose text matches; the commands are language-neutral.
LANDMARKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("describe the data sources", re.compile(r"datasource_names\(\)")),
    ("copy the template", re.compile(r"(?:Copy-Item -Recurse|cp -r) project_config\.example")),
    ("draft schema.yaml", re.compile(r"python -m database\.schema_inspector_cli")),
    ("check the structure", re.compile(r"python scripts/sync_schema\.py --check")),
    ("draft the descriptions", re.compile(r"python scripts/draft_descriptions\.py")),
    ("run the wizard", re.compile(r"python setup_project\.py")),
    ("issue the first key", re.compile(r"python -m scripts\.issue_api_key")),
    ("run the preflight", re.compile(r"verify_deployment")),
)

_STEP = re.compile(r"^\s{0,3}(\d+)\.\s+\S", re.MULTILINE)


def checklist_steps(text: str, start: re.Pattern[str]) -> dict[int, str]:
    """The numbered steps of the checklist that begins at *start*: ``{number: text}``.

    Persian digits are read as digits. The list ends at the next heading or rule.

    Examples
    --------
    >>> text = "## L\\n\\n1. **A.** one\\n   more\\n2. **B.** two\\n\\n---\\n3. not\\n"
    >>> steps = checklist_steps(text, re.compile(r"^## L$", re.MULTILINE))
    >>> sorted(steps), "more" in steps[1]
    ([1, 2], True)
    """
    match = start.search(text)
    assert match, f"checklist heading {start.pattern!r} not found"
    body = text[match.end():]
    stop = _END.search(body)
    body = body[: stop.start()] if stop else body
    body = body.translate(_DIGITS)
    heads = list(_STEP.finditer(body))
    steps: dict[int, str] = {}
    for n, head in enumerate(heads):
        end = heads[n + 1].start() if n + 1 < len(heads) else len(body)
        steps[int(head.group(1))] = body[head.start():end]
    return steps


def landmark_steps(steps: dict[int, str]) -> dict[str, int]:
    """For each landmark, the number of the first step that mentions its command."""
    found: dict[str, int] = {}
    for name, pattern in LANDMARKS:
        number = next((n for n in sorted(steps) if pattern.search(steps[n])), None)
        assert number is not None, f"no step of the checklist has: {name}"
        found[name] = number
    return found


def order_problems(found: dict[str, int]) -> list[str]:
    """What is out of order among the landmarks (empty when the order works)."""
    problems = []
    names = [name for name, _ in LANDMARKS]
    for earlier, later in zip(names, names[1:]):
        if not found[earlier] < found[later]:
            problems.append(f"{earlier!r} (step {found[earlier]}) must come before {later!r} (step {found[later]})")
    return problems


def heading_numbers(text: str) -> list[str]:
    """Section numbers of the headings of a guide, Persian digits folded, in file order."""
    numbers = []
    for line in text.translate(_DIGITS).splitlines():
        match = re.match(r"^#{2,4}\s+(?:Part\s+|بخش\s+)?(\d+(?:\.\d+)*)(?![\d.]*\w)", line)
        if match:
            numbers.append(match.group(1))
    return numbers


def _steps(path: Path, start: re.Pattern[str]) -> dict[int, str]:
    return checklist_steps(path.read_text(encoding="utf-8"), start)


@pytest.fixture(scope="module")
def lists() -> dict[str, dict[int, str]]:
    return {
        "en": _steps(_EN_GUIDE, _EN_START),
        "fa": _steps(_FA_GUIDE, _FA_START),
        "runbook": _steps(_RUNBOOK, _RUNBOOK_START),
    }


@pytest.mark.parametrize("which", ["en", "fa", "runbook"])
def test_the_checklist_lists_the_databases_before_the_tools_that_read_them(lists, which) -> None:
    """datasources, template, inspector, check, descriptions, wizard, key, preflight -- in that order."""
    assert order_problems(landmark_steps(lists[which])) == []


@pytest.mark.parametrize("which", ["en", "fa", "runbook"])
def test_the_key_is_issued_before_the_first_preflight_and_the_descriptions_before_the_wizard(lists, which) -> None:
    found = landmark_steps(lists[which])
    assert found["issue the first key"] < found["run the preflight"]
    assert found["draft the descriptions"] < found["run the wizard"] < found["issue the first key"]
    assert found["describe the data sources"] < found["draft schema.yaml"]


def test_the_three_checklists_put_every_landmark_at_the_same_step(lists) -> None:
    landmarks = {which: landmark_steps(steps) for which, steps in lists.items()}
    assert landmarks["en"] == landmarks["fa"] == landmarks["runbook"], landmarks
    assert sorted(lists["en"]) == sorted(lists["fa"]) == sorted(lists["runbook"])


@pytest.mark.parametrize("which", ["en", "fa", "runbook"])
def test_nothing_before_the_data_sources_step_reads_a_database(lists, which) -> None:
    """The first four steps need no database: prerequisites, install, ``.env``, the data sources."""
    found = landmark_steps(lists[which])
    for number in range(1, found["describe the data sources"]):
        for _, pattern in LANDMARKS[2:]:
            assert not pattern.search(lists[which][number]), (which, number)


@pytest.mark.parametrize("path", [_EN_GUIDE, _FA_GUIDE], ids=["en", "fa"])
def test_part_zero_reaches_the_first_key_before_the_preflight(path) -> None:
    """A reader going through Part 0 top-down meets key issuance (0.3.5) before the preflight (0.4)."""
    numbers = heading_numbers(path.read_text(encoding="utf-8"))
    assert "0.3.5" in numbers and "0.4" in numbers
    assert numbers.index("0.3.5") < numbers.index("0.4")
    assert numbers.index("0.3.4") < numbers.index("0.3.5")
    assert numbers.index("0.4") < numbers.index("2.2")  # the full explanation stays where it was


@pytest.mark.parametrize("path", [_EN_GUIDE, _FA_GUIDE], ids=["en", "fa"])
def test_the_first_key_section_points_to_the_full_explanation(path) -> None:
    text = path.read_text(encoding="utf-8")
    head = re.search(r"^### (?:0\.3\.5|۰\.۳\.۵) .*$", text, re.MULTILINE)
    assert head
    section = text[head.end(): text.index("### ", head.end())].translate(_DIGITS)
    assert "issue_api_key" in section and "--full-admin" in section
    assert "API_KEYS_FILE=project_config/api_keys.json" in section
    assert "2.2" in section


# --- the checks themselves: a list in the old order must be caught ----------


OLD_ORDER = """\
## L

1. **.env.** copy
2. **Template.** `Copy-Item -Recurse project_config.example project_config`; run `python -m database.schema_inspector_cli`
   and `python setup_project.py`
3. **Describe the databases.** `python -c "print(datasource_names())"`
4. **Check.** `python scripts/sync_schema.py --check`
5. **Preflight.** `python -m scripts.verify_deployment`
6. **Key.** `python -m scripts.issue_api_key`
7. **Descriptions.** `python scripts/draft_descriptions.py`

---
"""


def test_a_list_in_the_old_order_is_caught() -> None:
    steps = checklist_steps(OLD_ORDER, re.compile(r"^## L$", re.MULTILINE))

    problems = order_problems(landmark_steps(steps))

    assert "'describe the data sources' (step 3) must come before 'copy the template' (step 2)" in problems
    assert "'copy the template' (step 2) must come before 'draft schema.yaml' (step 2)" in problems
    assert "'run the wizard' (step 2) must come before 'issue the first key' (step 6)" not in problems  # that pair is fine
    assert "'draft the descriptions' (step 7) must come before 'run the wizard' (step 2)" in problems
    assert "'issue the first key' (step 6) must come before 'run the preflight' (step 5)" in problems


def test_persian_digits_number_the_steps() -> None:
    steps = checklist_steps("## L\n\n۱. یک\n۲. دو\n۱۰. ده\n", re.compile(r"^## L$", re.MULTILINE))
    assert sorted(steps) == [1, 2, 10]


def test_a_missing_landmark_is_reported_by_name() -> None:
    with pytest.raises(AssertionError, match="draft the descriptions"):
        landmark_steps({1: "datasource_names()", 2: "cp -r project_config.example", 3: "python -m database.schema_inspector_cli",
                        4: "python scripts/sync_schema.py --check"})
