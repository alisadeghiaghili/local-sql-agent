// tests/web_ui/run_calendar_gate_render.mjs
//
// Node-side half of tests/web_ui/test_web_ui_calendar_gate.py's renderer
// scenario. Sibling to run_chart_form_choice.mjs, staged the same way
// (via test_web_ui_result_shapes._prepare_copy, which copies
// table.js/chart.js/export.js to .mjs with relative specifiers rewritten
// -- see that harness for why the rename is necessary).
//
// WHY THIS FILE EXISTS (task D4-calendar-gate)
// ---------------------------------------------
// run_chart_form_choice.mjs already pins the ORIGINAL incident: a
// categorical column offered a line because `chooseFramings` never
// questioned its own "the measure is along a sequence" claim. This file
// pins a DIFFERENT, narrower way the same claim came out true when it
// should not have: `isSequenceLabel` used to test the column NAME with
// `key.includes(word)`, a SUBSTRING match, so a column merely named
// "MonthlyCustomer" -- containing the substring "month" -- borrowed the
// sequence treatment without being one. That reaches `chooseFramings`
// through `labelKey` directly, so it deserves its own harness rather than
// a scenario bolted onto the existing one.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const chartMjsPath = process.argv[2];
if (!chartMjsPath) {
  console.error("usage: node run_calendar_gate_render.mjs <chart.mjs path>");
  process.exit(2);
}

const { chooseFramings } = await import(pathToFileURL(chartMjsPath).href);

const kinds = (framings) => framings.filter((f) => !f.rejected).map((f) => f.kind);

/* ── 1. "MonthlyCustomer" (categorical, contains "month" as a substring) ─ */
{
  const ranking = [
    { MonthlyCustomer: "گروه صنعتی سیمان یزد", TotalVolume: 95350 },
    { MonthlyCustomer: "گروه صنعتی ساختمانی خزر", TotalVolume: 81200 },
    { MonthlyCustomer: "کارخانجات سیمان شاهرود", TotalVolume: 40900 },
    { MonthlyCustomer: "سیمان تهران", TotalVolume: 33100 },
    { MonthlyCustomer: "سیمان آبیک", TotalVolume: 21400 },
  ];
  const offered = kinds(chooseFramings(ranking, "MonthlyCustomer", "TotalVolume"));

  assert.ok(
    !offered.includes("line"),
    `a line framing was offered for label key "MonthlyCustomer": ${JSON.stringify(offered)}. ` +
    'The old substring check ("month" inside "MonthlyCustomer") wrongly ' +
    "treated a plain customer ranking as a calendar sequence",
  );
  assert.ok(
    !offered.includes("split-bar"),
    `the first-half/second-half framing was offered for "MonthlyCustomer": ${JSON.stringify(offered)}. ` +
    "Halves are a property of an ordered series; this ranking has none",
  );
  assert.ok(
    offered.includes("bar"),
    'a ranking with label key "MonthlyCustomer" must still be offered as a bar chart',
  );
  console.log('[ok] "MonthlyCustomer" yields no line or split-bar framing');
}

/* ── 2. "Month" alone is still a real sequence ────────────────────── */
{
  const monthly = [
    { Month: "1403/01", Volume: 10 },
    { Month: "1403/02", Volume: 22 },
    { Month: "1403/03", Volume: 31 },
    { Month: "1403/04", Volume: 44 },
    { Month: "1403/05", Volume: 58 },
  ];
  const offered = kinds(chooseFramings(monthly, "Month", "Volume"));
  assert.ok(
    offered.includes("line"),
    `label key "Month" lost its line framing: ${JSON.stringify(offered)}. ` +
    "Fixing the substring false-positive on \"MonthlyCustomer\" must not " +
    "remove the true positive on \"Month\" itself",
  );
  console.log('[ok] "Month" still yields a line framing');
}

console.log("ALL_SCENARIOS_PASSED");
