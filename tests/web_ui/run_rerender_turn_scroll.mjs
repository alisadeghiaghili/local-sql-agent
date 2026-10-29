// tests/web_ui/run_rerender_turn_scroll.mjs
//
// Node-side half of tests/web_ui/test_web_ui_rerender_turn_scroll.py.
//
// Drives the REAL, unmodified `rerenderTurn` from web/js/main.js (extracted
// verbatim by the Python wrapper) against spies for document.getElementById,
// createTurnCard, $, clearTranscriptEmptyState and
// scrollSettledResultAboveComposer, to prove (#219):
//
// * a turn with no card yet (the new turn a resolved clarification or an
//   edited assumption comes back as) is appended to the transcript AND the
//   page is scrolled to its result;
// * a turn that already has a card is replaced in place, with no scroll, as
//   before.
//
// Usage: node run_rerender_turn_scroll.mjs <path-to-generated-harness-module.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [modulePath] = process.argv.slice(2);
if (!modulePath) {
  console.error("usage: node run_rerender_turn_scroll.mjs <path-to-generated-harness-module.mjs>");
  process.exit(2);
}

const { calls, resetCalls, dom, rerenderTurn } = await import(pathToFileURL(modulePath).href);

/* ── Scenario 1: a new turn is appended and scrolled to. ─────────────── */
{
  resetCalls();
  dom.existing = new Map();
  rerenderTurn({ turn_id: "t_new" });
  assert.equal(calls.appended.length, 1, "the card is appended to the transcript");
  assert.equal(calls.appended[0].turnId, "t_new");
  assert.equal(calls.cleared, 1, "the empty-state placeholder is cleared, as before");
  assert.equal(calls.scrolled.length, 1, "the page is brought to the new answer");
  assert.equal(calls.scrolled[0], calls.appended[0].result, "to its result card");
  console.log("[ok] a turn with no card is appended and scrolled to");
}

/* ── Scenario 2: no result card (a failed answer): scroll to the card. ── */
{
  resetCalls();
  dom.existing = new Map();
  dom.withResult = false;
  rerenderTurn({ turn_id: "t_err" });
  assert.equal(calls.scrolled.length, 1);
  assert.equal(calls.scrolled[0], calls.appended[0], "to the card itself when there is no result");
  dom.withResult = true;
  console.log("[ok] a new turn without a result card is scrolled to as a whole");
}

/* ── Scenario 3: a turn that already has a card is replaced in place. ── */
{
  resetCalls();
  const replaced = [];
  dom.existing = new Map([["turn-t_old", { replaceWith: (el) => replaced.push(el) }]]);
  rerenderTurn({ turn_id: "t_old" });
  assert.equal(replaced.length, 1, "the old card is replaced");
  assert.equal(calls.appended.length, 0, "nothing is appended");
  assert.equal(calls.scrolled.length, 0, "and the page does not move");
  console.log("[ok] a turn with a card is replaced in place, without a scroll");
}

console.log("ALL_SCENARIOS_PASSED");
