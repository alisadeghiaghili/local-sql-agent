# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The chart engine's boundary, pinned before the engine exists.

Specification: ``docs/design/CHART-ENGINE-BOUNDARY.md``. Read it first — this
module asserts that document's §4, §5 and §8, and every failure message below
points back at a numbered section rather than restating the argument.

Why a boundary needs its own tests
----------------------------------
The engine is being built **inside this repository** with the explicit
intention that it could later be extracted as a separate product (§2). That
intention is worth nothing on its own: every module that was ever "designed to
be extractable later" and not tested for it grew a dependency on its host
within a few commits, and the discovery came at extraction time, when the cost
is a rewrite rather than an edit.

So the extractability claims are assertions, not aspirations. §8.1 fails on the
commit that imports a renderer, not six months later.

Why these tests are red right now
---------------------------------
Every one of them is written against a module that has not been built. That is
deliberate and is the point of the exercise: the interface is agreed in the
test before it is agreed in the code, because the motivating defect
(``test_web_ui_chart_form_choice.py`` §0) was precisely a case of an interface
asserting something — «سنجه در طول یک توالی است» — that no test had ever
required it to check.

The agreed path is ``web/js/chart-engine/recommend.js``. **Not** under
``web/js/render/``: the boundary is visible in the directory tree, so a review
of any future diff can see an import crossing it without reading the imports.

Policy: ``docs/design/CHART-ENGINE-BOUNDARY.md`` §4, §5, §8.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENGINE_DIR = _REPO_ROOT / "web" / "js" / "chart-engine"
_ENGINE_ENTRY = _ENGINE_DIR / "recommend.js"
_HARNESS = Path(__file__).resolve().parent / "run_chart_engine_boundary.mjs"

_NODE = shutil.which("node")

_ENGINE_MISSING = f"{_ENGINE_ENTRY} does not exist yet — see docs/design/CHART-ENGINE-BOUNDARY.md §7 step 1"


def _engine_sources() -> list[Path]:
    return sorted(_ENGINE_DIR.rglob("*.js")) if _ENGINE_DIR.is_dir() else []


def _stage(tmp_path: Path) -> Path:
    """Copy the engine into *tmp_path* as ESM, returning the staged entry.

    Same mechanism and same reason as ``test_web_ui_result_shapes._prepare_copy``
    (Node needs an ``.mjs`` extension or a controlling ``package.json``, and
    ``web/`` deliberately ships neither) — but deliberately **not** a reuse of
    that helper. That one stages ``table.js``/``chart.js``/``export.js`` and
    their whole import web; staging the engine through it would quietly give
    the engine access to the very modules §4.2 forbids it from importing, and
    the harness would then pass while the boundary was broken.

    Only relative specifiers are rewritten. An import of anything outside this
    directory survives untouched and fails to resolve under Node, which is the
    correct outcome: §8.1 is supposed to be loud.
    """
    for src in _engine_sources():
        rel = src.relative_to(_ENGINE_DIR)
        dest = tmp_path / rel.with_suffix(".mjs")
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        text = re.sub(r'(from\s+["\'])(\.{1,2}/[^"\']+?)\.js(["\'])', r"\1\2.mjs\3", text)
        dest.write_text(text, encoding="utf-8")
    return tmp_path / "recommend.mjs"


# ---------------------------------------------------------------------------
# §8.1 — the boundary itself
# ---------------------------------------------------------------------------

class TestTheEngineDoesNotReachIntoItsHost:
    """§4.2. These are source assertions on purpose: an import that is present
    but never executed on the harness's particular inputs would pass a
    behavioural test and still be a dependency at extraction time.
    """

    def test_the_engine_exists_at_the_agreed_path(self):
        assert _ENGINE_ENTRY.is_file(), _ENGINE_MISSING

    def test_no_module_imports_from_the_render_layer(self):
        sources = _engine_sources()
        assert sources, _ENGINE_MISSING
        for path in sources:
            for spec in re.findall(r'from\s+["\']([^"\']+)["\']', path.read_text(encoding="utf-8")):
                assert "render/" not in spec, (
                    f"{path.relative_to(_REPO_ROOT)} imports {spec!r}. §4.2: the "
                    "engine may not import anything under web/js/render/. The "
                    "renderer consumes the engine's declarative spec; the "
                    "dependency runs one way only"
                )

    def test_no_module_touches_a_dom_or_ambient_global(self):
        """§4.2, including the clock.

        ``Date.now()`` is listed with the DOM globals rather than treated as a
        lesser sin: a selector whose output depends on the time of day cannot
        be pinned by any test, and §8's tests are the only thing standing
        between this engine and the defect that motivated it.
        """
        forbidden = ("document", "window", "localStorage", "sessionStorage", "fetch", "Date.now")
        sources = _engine_sources()
        assert sources, _ENGINE_MISSING
        for path in sources:
            text = path.read_text(encoding="utf-8")
            # Strip comments first: this file's own prose names these globals,
            # and so will the engine's docstrings explaining why it avoids them.
            code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
            for name in forbidden:
                assert not re.search(rf"\b{re.escape(name)}\b", code), (
                    f"{path.relative_to(_REPO_ROOT)} references {name!r}. §4.2: "
                    "the engine is pure — result shape in, ordered framings "
                    "out. A DOM reference is what makes extraction (§2) a "
                    "rewrite instead of a move"
                )

    def test_no_module_declares_a_package_dependency(self):
        """§9. ``web/`` ships no build step by design (``web/README.md``). An
        engine that needs one has broken the boundary it claims to keep.
        """
        assert not (_ENGINE_DIR / "package.json").exists(), (
            "the engine directory declares a package.json. §9: no new runtime "
            "dependency, and no build step"
        )


# ---------------------------------------------------------------------------
# §8.2-§8.5 — behaviour, driven through the real module under bare Node
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_the_engine_obeys_its_catalog_and_names_the_job():
    """Runs the real engine with **no DOM shim of any kind**.

    Every other ``.mjs`` harness in this directory builds a stub DOM because
    the modules it drives call ``createElement``. This one supplies nothing,
    which turns §4.2 from a source-scan into an execution fact: if the engine
    touches a DOM global on any path the harness exercises, it throws a
    ReferenceError and this test goes red with the stack attached.

    The scenarios themselves are in ``run_chart_engine_boundary.mjs``.
    """
    assert _ENGINE_ENTRY.is_file(), _ENGINE_MISSING
    assert _HARNESS.exists(), f"expected {_HARNESS} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        entry = _stage(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(entry)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    assert result.returncode == 0, (
        "chart-engine boundary check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        "harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# §7 step 1 — the refactor may not change what the analyst sees
# ---------------------------------------------------------------------------

def test_the_existing_form_choice_tests_are_still_present():
    """§7 step 1 rebuilds today's selection behind the new boundary with **no
    new chart forms**, so the two suites that pin today's behaviour are what
    prove the refactor preserved the fix rather than re-landing the defect.

    Asserted as presence, not content: this module has no business duplicating
    their assertions, but it does have business noticing if a refactor made
    them disappear. That is the one failure mode a green suite cannot show.
    """
    here = Path(__file__).resolve().parent
    for name in ("test_web_ui_chart_form_choice.py", "test_web_ui_result_shapes.py"):
        assert (here / name).is_file(), (
            f"{name} is gone. It pins the behaviour the chart engine refactor "
            "must preserve (CHART-ENGINE-BOUNDARY.md §7 step 1, §8.5). If the "
            "engine replaced it, the replacement is an addition, not a swap"
        )
