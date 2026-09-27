// tests/web_ui/run_onclarify_live_mode.mjs
//
// Node-side half of tests/web_ui/test_web_ui_onclarify_live_mode.py.
//
// Before this change, web/js/main.js's turnCtx().onClarify had no
// live-mode branch: resolving a "which hall did you mean?" clarification
// chip always mutated the chip locally and showed a Persian "local
// simulation only" notice, even against a live backend -- the analyst's
// answer never reached the server and the turn's actual result never
// changed. Its sibling onEditAssumption already had the live branch
// (`if (state.mode === "live") { patchLiveAssumption(...); return; }`);
// the fix mirrors that into onClarify.
//
// This drives the REAL, unmodified turnCtx/patchLiveAssumption source
// (extracted verbatim from web/js/main.js by the Python wrapper -- see
// that file's `_extract_function`) against spies standing in for
// state/findTurn/rerenderTurn/showNotice/api/handleLiveError.
//
// Usage: node run_onclarify_live_mode.mjs <path-to-generated-harness-module.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario below
// passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [modulePath] = process.argv.slice(2);
if (!modulePath) {
  console.error("usage: node run_onclarify_live_mode.mjs <path-to-generated-harness-module.mjs>");
  process.exit(2);
}

const {
  state, calls, resetCalls, setPatchAssumptionsImpl, turnCtx,
} = await import(pathToFileURL(modulePath).href);

function seedTurn(turnId, field, value) {
  const turn = {
    turn_id: turnId,
    ambiguity: { assumptions: [{ field, value, source: "resolved" }] },
  };
  state.turns = [turn];
  return turn;
}

// Flushes every pending microtask (promise-chain continuation) without
// waiting on a real timer -- Node drains the microtask queue before running
// any macrotask, including a `setImmediate` callback, so by the time this
// resolves, onClarify's fire-and-forget `patchLiveAssumption(...)` call has
// run to completion (success or failure) if it was going to.
function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function noticeMentionsLocalSimulation() {
  return calls.showNotice.some((n) => n.message.includes("شبیه‌سازی محلی"));
}

/* ── Scenario 1: SIMULATED mode is untouched by this fix. ───────────── */
{
  state.mode = "simulated";
  state.sessionId = "sess-sim";
  resetCalls();
  const turn = seedTurn("t1", "ring", "تالار قدیم");

  const ctx = turnCtx();
  ctx.onClarify("t1", "ring", "تالار سیمان");

  assert.equal(calls.patchAssumptions.length, 0, "simulated mode must never call api.patchAssumptions");
  assert.equal(turn.ambiguity.assumptions[0].value, "تالار سیمان", "simulated mode must still mutate the chip locally");
  assert.equal(turn.ambiguity.assumptions[0].source, "question");
  assert.equal(calls.rerenderTurn.length, 1, "simulated mode must still re-render the (locally mutated) turn");
  assert.ok(noticeMentionsLocalSimulation(), "simulated mode must still show the local-simulation notice");
  console.log("[ok] simulated mode: onClarify behaves exactly as before (local mutation, local-simulation notice)");
}

/* ── Scenario 2: LIVE mode, successful PATCH -- THE fix. ─────────────── */
{
  state.mode = "live";
  state.sessionId = "sess-live-1";
  resetCalls();
  seedTurn("t2", "ring", "تالار قدیم");

  const serverTurn = {
    turn_id: "t2",
    ambiguity: { assumptions: [{ field: "ring", value: "تالار سیمان", source: "question" }] },
    sql: "SELECT 1 -- resolved against تالار سیمان",
  };
  setPatchAssumptionsImpl(async (sessionId, turnId, patches) => {
    assert.equal(sessionId, "sess-live-1");
    assert.equal(turnId, "t2");
    assert.deepEqual(patches, [{ field: "ring", value: "تالار سیمان" }]);
    return serverTurn;
  });

  const ctx = turnCtx();
  ctx.onClarify("t2", "ring", "تالار سیمان");
  await flush();

  assert.equal(calls.patchAssumptions.length, 1, "live mode must call api.patchAssumptions exactly once");
  assert.deepEqual(
    calls.patchAssumptions[0],
    { sessionId: "sess-live-1", turnId: "t2", patches: [{ field: "ring", value: "تالار سیمان" }] },
    "live mode must send the SAME {field, value} shape onEditAssumption's PATCH sends",
  );
  assert.equal(state.turns[0], serverTurn, "the server's response must replace the local turn, not a locally-mutated copy");
  assert.equal(calls.rerenderTurn.length, 1, "the turn card must be re-rendered from the server's response");
  assert.equal(calls.rerenderTurn[0], serverTurn);
  assert.ok(!noticeMentionsLocalSimulation(), "live mode must NEVER show the local-simulation notice");
  assert.equal(calls.handleLiveError.length, 0, "a successful PATCH must not route through handleLiveError");
  console.log("[ok] live mode: onClarify sends the PATCH through the same path as onEditAssumption, and the server's turn wins");
}

/* ── Scenario 3: LIVE mode, failed PATCH -- must route through the SAME
 * error handling onEditAssumption's live path already uses, not silently
 * do nothing. ─────────────────────────────────────────────────────────── */
{
  state.mode = "live";
  state.sessionId = "sess-live-2";
  resetCalls();
  seedTurn("t3", "ring", "تالار قدیم");

  const boom = new Error("simulated 500 from PATCH /v2/sessions/.../assumptions");
  setPatchAssumptionsImpl(async () => {
    throw boom;
  });

  const ctx = turnCtx();
  ctx.onClarify("t3", "ring", "تالار سیمان");
  await flush();

  assert.equal(calls.patchAssumptions.length, 1);
  assert.equal(calls.handleLiveError.length, 1, "a failed live PATCH must route through handleLiveError");
  assert.equal(calls.handleLiveError[0], boom);
  assert.equal(calls.rerenderTurn.length, 0, "no re-render on a failed PATCH");
  assert.ok(!noticeMentionsLocalSimulation(), "a failed live PATCH must not fall back to the local-simulation notice either");
  console.log("[ok] live mode: a failed PATCH routes through handleLiveError, same as onEditAssumption");
}

/* ── Scenario 4: onEditAssumption's own live branch still works (guards
 * the premise that both handlers now agree, not just that onClarify
 * changed). ────────────────────────────────────────────────────────────── */
{
  state.mode = "live";
  state.sessionId = "sess-live-3";
  resetCalls();
  seedTurn("t4", "period", "1402");

  const serverTurn = { turn_id: "t4", ambiguity: { assumptions: [{ field: "period", value: "1403", source: "question" }] } };
  setPatchAssumptionsImpl(async (sessionId, turnId, patches) => {
    assert.deepEqual(patches, [{ field: "period", value: "1403" }]);
    return serverTurn;
  });

  const ctx = turnCtx();
  ctx.onEditAssumption("t4", "period", "1403");
  await flush();

  assert.equal(calls.patchAssumptions.length, 1);
  assert.equal(state.turns[0], serverTurn);
  console.log("[ok] live mode: onEditAssumption is unchanged and still sends the PATCH");
}

console.log("ALL_SCENARIOS_PASSED");
