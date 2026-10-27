// SPDX-License-Identifier: BUSL-1.1
// Copyright (c) 2024-2026 Ali Sadeghi Aghili
//
// tests/web_ui/run_shape_pins.mjs
//
// Node-side half of tests/web_ui/test_web_ui_shape_pins.py.
//
// What the UI decides today for a result whose columns are NOT identifiers,
// pinned before the identifier rule (tests/web_ui/run_identifier_columns.mjs)
// changes how a measure is picked. Every case here passes before and after:
//
// * table.js::determineShape -- scalar, record, chart, table, empty -- for
//   names that merely look like an identifier (Paid, Valid, Barcode, Width,
//   Identity, ...) and for ordinary measures, in Latin and in Persian;
// * a 1 x 1 numeric result stays a scalar whatever the column is called, and a
//   one-row result stays a record;
// * chart-engine/recommend.js picks the same label and measure for a result
//   whose numeric columns are ordinary measures.
//
// Usage: node run_shape_pins.mjs <path-to-staged-table.mjs> <path-to-staged-recommend.mjs>
// Exits 0 and prints ALL_SCENARIOS_PASSED iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [tablePath, enginePath] = process.argv.slice(2);
if (!tablePath || !enginePath) {
  console.error("usage: node run_shape_pins.mjs <table.mjs> <recommend.mjs>");
  process.exit(2);
}

// table.js builds DOM only when it renders; determineShape is pure. The
// modules it imports must still be loadable, so give them a stub document.
globalThis.document = {
  createElement: () => ({ setAttribute() {}, appendChild() {}, classList: { add() {} } }),
  createElementNS: () => ({ setAttribute() {}, appendChild() {}, classList: { add() {} } }),
  createTextNode: () => ({}),
};

const { determineShape, SHAPE } = await import(pathToFileURL(tablePath).href);
const { recommend } = await import(pathToFileURL(enginePath).href);

const STR = (name) => ({ name, type: "string" });
const NUM = (name) => ({ name, type: "number" });

function resultOf(columns, rowCount) {
  const rows = Array.from({ length: rowCount }, (_, i) => {
    const row = {};
    columns.forEach((c, j) => { row[c.name] = c.type === "number" ? (i + 1) * (j + 1) : `v${i}`; });
    return row;
  });
  return { columns, rows, row_count: rows.length, truncated: false };
}

function shape(columns, rowCount) {
  return determineShape(resultOf(columns, rowCount));
}

/* ── 1. A label and an ordinary measure is a chart ─────────────────── */

const ORDINARY_MEASURES = [
  "Total", "Count", "Volume", "Price", "Value", "Amount", "TotalValue", "OrderCount", "Qty", "Rate",
  "Weight", "Percent", "Score", "Rank", "Year", "Month",
  // names that END in "id" / "code" / "Id" without being identifiers
  "Paid", "Valid", "Solid", "Liquid", "Fluid", "Android", "Void", "Width", "Grid", "Raid",
  "Barcode", "Postcode", "Zipcode", "Decode", "Encode", "Passcode", "Opcode",
  // names that CONTAIN id / code / Id in the middle
  "Identity", "Idle", "Ident", "Identical", "Kidney", "Codex", "Coded", "Decoder", "Hidden", "Side",
  "Idea", "Idx", "IdleTime", "CodeName",
  // Persian
  "تعداد", "مبلغ", "ارزش", "حجم", "درصد", "قیمت", "کدام", "شناسنامه", "اندیشه", "مقدار",
];
for (const measure of ORDINARY_MEASURES) {
  assert.equal(
    shape([STR("Name"), NUM(measure)], 5), SHAPE.CHART,
    `(Name, ${measure}) x 5 rows is a chart: ${measure} is an ordinary measure`,
  );
}
console.log(`[ok] ${ORDINARY_MEASURES.length} ordinary measures still draw a chart`);

/* ── 2. The row-count and column-count rules around it ─────────────── */

assert.equal(shape([STR("Name"), NUM("Total")], 2), SHAPE.CHART, "2 rows is the smallest chart");
assert.equal(shape([STR("Name"), NUM("Total")], 30), SHAPE.CHART, "30 rows is the largest chart");
assert.equal(shape([STR("Name"), NUM("Total")], 31), SHAPE.TABLE, "31 rows is a table");
assert.equal(shape([STR("Name"), NUM("Total")], 1), SHAPE.RECORD, "1 row x 2 columns is a record");
assert.equal(shape([STR("Name"), NUM("Total"), NUM("Count")], 5), SHAPE.TABLE, "two measures is a table");
assert.equal(shape([NUM("Total"), NUM("Count")], 5), SHAPE.TABLE, "two numeric columns is a table");
assert.equal(shape([STR("A"), STR("B")], 5), SHAPE.TABLE, "no numeric column is a table");
assert.equal(shape([STR("A"), STR("B"), NUM("Total")], 5), SHAPE.TABLE, "three columns is a table");
assert.equal(shape([NUM("Total")], 5), SHAPE.TABLE, "one numeric column over several rows is a table");
assert.equal(shape([STR("Name")], 1), SHAPE.TABLE, "1 x 1 non-numeric is a table");
assert.equal(shape([NUM("Total")], 1), SHAPE.SCALAR, "1 x 1 numeric is a scalar");
assert.equal(shape([STR("Name"), NUM("Total"), STR("Kind")], 1), SHAPE.RECORD, "1 row x 3 columns is a record");
assert.equal(
  shape([{ name: "Day", type: "datetime" }, NUM("Total")], 7), SHAPE.CHART,
  "a datetime label with a measure is a chart",
);
assert.equal(
  shape([{ name: "Active", type: "boolean" }, NUM("Total")], 2), SHAPE.CHART,
  "a boolean label with a measure is a chart",
);
assert.equal(determineShape({ columns: [], rows: [], row_count: 0 }), SHAPE.EMPTY, "0 rows is empty");
assert.equal(determineShape({ columns: [STR("A")], rows: [{ A: "x" }], row_count: 1 }), SHAPE.TABLE);
assert.equal(
  determineShape({ columns: [], rows: [{ A: 1 }, { A: 2 }], row_count: 2 }), SHAPE.TABLE,
  "rows without column metadata is a table",
);
console.log("[ok] row-count and column-count rules are unchanged");

/* ── 3. The chart engine splits label and measure the same way ─────── */

const CATALOG = {
  forms: [
    { id: "bar", jobs: ["rank", "comparison"] },
    { id: "line", jobs: ["trend"], requiresSequence: true },
    { id: "split-bar", jobs: ["comparison"], requiresSequence: true },
    { id: "pie", jobs: ["composition"], maxCategories: 6 },
  ],
};

function engineInput(columns, rows) {
  return {
    columns, rows, rowCount: rows.length, truncated: false,
    sql: null, resolvedQuestion: null, tablesTouched: [],
  };
}

function engineForms(columns, rows) {
  return recommend(engineInput(columns, rows), CATALOG).framings.map((f) => `${f.form}:${f.rejected}`);
}

const named = Array.from({ length: 5 }, (_, i) => ({ Name: `n${i}`, Total: (5 - i) * 10, Paid: i + 1 }));
const ranked = engineForms([STR("Name"), NUM("Total")], named);
assert.deepEqual(ranked, ["bar:false"], "a ranked label + measure still gets one bar");

const labelOfPaid = recommend(engineInput([STR("Name"), NUM("Paid")], named), CATALOG).framings;
assert.equal(labelOfPaid.length, 1, "(Name, Paid): Paid is an ordinary measure for the engine too");
assert.equal(labelOfPaid[0].spec.encoding.measure.field, "Paid", "the engine measures Paid");
assert.equal(labelOfPaid[0].spec.encoding.label.field, "Name", "against Name");

const firstMeasure = recommend(engineInput([STR("Name"), NUM("Total"), NUM("Paid")], named), CATALOG).framings;
assert.equal(firstMeasure[0].spec.encoding.measure.field, "Total", "with two numeric columns the first is the measure");

assert.deepEqual(engineForms([STR("A"), STR("B")], named), [], "no numeric column: nothing to recommend");
assert.deepEqual(engineForms([NUM("Total")], named), [], "no label column: nothing to recommend");
assert.deepEqual(engineForms([STR("Name"), NUM("Total")], []), [], "no rows: nothing to recommend");
console.log("[ok] the chart engine splits label and measure as before");

console.log("ALL_SCENARIOS_PASSED");
