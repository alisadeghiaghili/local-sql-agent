// tests/web_ui/run_streaming_sql_display.mjs
//
// Node-side half of tests/web_ui/test_web_ui_streaming_sql_display.py.
//
// The streamed `sql` event carries `sql_display` (the server's layout of the
// SQL, api/v2_routes.py). This drives the REAL, unmodified `askLive` source
// (extracted verbatim from web/js/main.js by the Python wrapper) against spies
// standing in for the card, the API client and the DOM, and the REAL
// web/js/sql-display.js for what the card would copy and show, to prove:
//
// * after the `sql` event the turn being rendered carries the event's
//   `sql_display`, so the first paint uses it (copySourceOfTruth prefers it)
//   and is the same text the `done` turn brings;
// * `sql` itself is never replaced by the display form;
// * an event without `sql_display` (an older server) still works and falls
//   back to today's behaviour: the SQL is what is shown.
//
// Usage: node run_streaming_sql_display.mjs <harness-module.mjs> <sql-display.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [modulePath, sqlDisplayPath] = process.argv.slice(2);
if (!modulePath || !sqlDisplayPath) {
  console.error("usage: node run_streaming_sql_display.mjs <harness-module.mjs> <sql-display.mjs>");
  process.exit(2);
}

const { askLive, setEvents, renders } = await import(pathToFileURL(modulePath).href);
const { copySourceOfTruth, displaySqlForTurn } = await import(pathToFileURL(sqlDisplayPath).href);

const RAW_SQL = "SELECT TOP 10 a, b FROM t WHERE a = 1";
const DISPLAY_SQL = "SELECT TOP (10)\n     a\n    ,b\n\nFROM t\nWHERE a = 1";
const GUARD = { verdict: "allowed", rule: null, injected_top: null, tables_touched: ["t"] };

function doneTurn(extra) {
  return { turn_id: "t_done", session_id: "s_test", index: 1, question: "q", sql: RAW_SQL, guard: GUARD, ...extra };
}

/* ── Scenario 1: the event carries sql_display ─────────────────────────── */

setEvents([
  ["stage", { stage: "generate", state: "running" }],
  ["sql", { sql: RAW_SQL, sql_display: DISPLAY_SQL, guard: GUARD }],
  ["done", { turn: doneTurn({ sql_display: DISPLAY_SQL }) }],
]);
renders.length = 0;
await askLive("q");

const afterSql = renders.find((r) => r.sql === RAW_SQL && r.turn_id !== "t_done");
assert.ok(afterSql, "a render must follow the sql event");
assert.equal(afterSql.sql_display, DISPLAY_SQL, "the first paint's turn must carry the event's sql_display");
assert.equal(afterSql.sql, RAW_SQL, "sql must stay what ran, never the display form");
assert.deepEqual(afterSql.guard, GUARD, "the guard verdict must still arrive with the event");
assert.equal(copySourceOfTruth(afterSql), DISPLAY_SQL, "copy / display must prefer sql_display");
assert.equal(displaySqlForTurn(afterSql), DISPLAY_SQL, "a multi-line display form is shown as it is");

const last = renders[renders.length - 1];
assert.equal(last.turn_id, "t_done", "done replaces the turn");
assert.equal(copySourceOfTruth(last), copySourceOfTruth(afterSql), "first paint and final paint show the same text");

console.log("[ok] the sql event's sql_display is on the turn at first paint and matches the done turn");

/* ── Scenario 2: an older server sends no sql_display ──────────────────── */

setEvents([
  ["sql", { sql: RAW_SQL, guard: GUARD }],
  ["done", { turn: doneTurn({}) }],
]);
renders.length = 0;
await askLive("q");

const old = renders.find((r) => r.sql === RAW_SQL && r.turn_id !== "t_done");
assert.ok(old, "a render must follow the sql event");
assert.ok(!old.sql_display, "no sql_display must be invented when the event has none");
assert.equal(copySourceOfTruth(old), RAW_SQL, "without sql_display the SQL itself is what is shown and copied");

console.log("[ok] an sql event without sql_display still renders, from sql, as before");

console.log("ALL_SCENARIOS_PASSED");
