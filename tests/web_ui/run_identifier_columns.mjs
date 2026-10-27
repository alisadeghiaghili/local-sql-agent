// SPDX-License-Identifier: BUSL-1.1
// Copyright (c) 2024-2026 Ali Sadeghi Aghili
//
// tests/web_ui/run_identifier_columns.mjs
//
// Node-side half of tests/web_ui/test_web_ui_identifier_columns.py.
//
// "All trading rings with their codes" returned (Name, ID) rows and the UI
// drew the ids as a bar chart: table.js::determineShape took the one numeric
// column of a two-column result as the measure. A numeric column whose NAME
// is an identifier is a label, not a measure. This checks:
//
// * num.js::isIdentifierName -- which names are identifiers (ID, Id, id,
//   *_ID, *ID after a lower-case letter, camel *Id, Code, *_Code, *Code,
//   Persian کد / شناسه as a word) and which merely end in the same letters
//   (Paid, Valid, Barcode, ...);
// * table.js::determineShape -- such a result is a table; a one-row or 1 x 1
//   result keeps its shape; every other decision is as run_shape_pins.mjs
//   pins it;
// * chart-engine/recommend.js, which may not import num.js and keeps its own
//   copy of the rule, agrees with num.js name by name and recommends nothing
//   for a result whose only numeric column is an identifier.
//
// Usage: node run_identifier_columns.mjs <table.mjs> <recommend.mjs>
// (num.mjs must sit beside table.mjs). Exits 0 and prints
// ALL_SCENARIOS_PASSED iff every scenario passed.

import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [tablePath, enginePath] = process.argv.slice(2);
if (!tablePath || !enginePath) {
  console.error("usage: node run_identifier_columns.mjs <table.mjs> <recommend.mjs>");
  process.exit(2);
}

globalThis.document = {
  createElement: () => ({ setAttribute() {}, appendChild() {}, classList: { add() {} } }),
  createElementNS: () => ({ setAttribute() {}, appendChild() {}, classList: { add() {} } }),
  createTextNode: () => ({}),
};

const numPath = path.join(path.dirname(tablePath), "num.mjs");
const { isIdentifierName } = await import(pathToFileURL(numPath).href);
const { determineShape, SHAPE } = await import(pathToFileURL(tablePath).href);
const { recommend } = await import(pathToFileURL(enginePath).href);

/* ── 1. Which names are identifiers ────────────────────────────────── */

const IDENTIFIERS = [
  // the whole name
  "ID", "Id", "id", "iD", "Code", "code", "CODE",
  // after a separator
  "ring_id", "Ring_ID", "RING_ID", "Ring_Id", "ring id", "Ring ID", "ring-id", "ring.id",
  "broker_code", "Broker_Code", "Broker Code", "national code", "National_Code",
  // upper-case ID after a lower-case letter or a digit
  "RingID", "ringID", "BrokerID", "CustomerID", "OrderDate_ID", "Order2ID", "p1ID",
  // a capitalised Id / Code after a letter or a digit
  "ringId", "RingId", "brokerId", "Order2Id", "BrokerCode", "brokerCode", "NationalCode", "ISOCode", "QRCode",
  // Persian, as a word
  "کد", "شناسه", "کد ملی", "کد_کارگزار", "کد‌ملی", "شناسهٔ سفارش", "شناسه سفارش", "کد حلقه", "كد ملي",
  // surrounding space
  "  ID ", " RingId",
];
for (const name of IDENTIFIERS) {
  assert.equal(isIdentifierName(name), true, `${JSON.stringify(name)} is an identifier`);
}

const MEASURES = [
  // end in the letters, no word boundary
  "Paid", "paid", "Valid", "valid", "Solid", "Liquid", "Fluid", "Void", "Android", "Width", "Grid", "Raid",
  "Barcode", "barcode", "Postcode", "Zipcode", "Passcode", "Opcode", "Decode", "Encode",
  // all capitals: not recognised, by design (write BROKER_ID)
  "PAID", "VALID", "BROKERID", "BROKERCODE",
  // id / code inside the name, or after it
  "Identity", "Idle", "Ident", "Identical", "Kidney", "Codex", "Coded", "Decoder", "Hidden", "Side", "Idea",
  "IdleTime", "CodeName", "IDs", "IdName", "ID_Name", "Codes",
  // ordinary measures
  "Total", "Count", "Volume", "Price", "Value", "Amount", "Qty", "Rank", "Year",
  // Persian look-alikes and ordinary measures
  "کدام", "شناسنامه", "اندیشه", "تعداد", "مبلغ", "حجم", "ارزش", "کدخدا",
  // nothing
  "", "   ",
];
for (const name of MEASURES) {
  assert.equal(isIdentifierName(name), false, `${JSON.stringify(name)} is not an identifier`);
}
for (const bad of [null, undefined, 7, {}, []]) {
  assert.equal(isIdentifierName(bad), false, `${String(bad)} is not a name, so not an identifier`);
}
console.log(`[ok] isIdentifierName: ${IDENTIFIERS.length} identifiers, ${MEASURES.length} names that are not`);

/* ── 2. Shapes ─────────────────────────────────────────────────────── */

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
const shape = (columns, rowCount) => determineShape(resultOf(columns, rowCount));

for (const name of IDENTIFIERS) {
  const trimmed = name.trim();
  assert.equal(shape([STR("Name"), NUM(trimmed)], 5), SHAPE.TABLE, `(Name, ${trimmed}) is a table, not a chart of ids`);
  assert.equal(shape([NUM(trimmed), STR("Name")], 5), SHAPE.TABLE, `(${trimmed}, Name) is a table`);
}
for (const rows of [2, 3, 12, 30]) {
  assert.equal(shape([STR("Name"), NUM("ID")], rows), SHAPE.TABLE, `(Name, ID) x ${rows} rows is a table`);
}
assert.equal(shape([STR("Name"), NUM("ID")], 31), SHAPE.TABLE, "(Name, ID) x 31 rows stays a table");

// What does not change when a column is an identifier.
assert.equal(shape([STR("Name"), NUM("ID")], 1), SHAPE.RECORD, "one row x 2 columns is still a record");
assert.equal(shape([NUM("ID")], 1), SHAPE.SCALAR, "1 x 1 numeric stays a scalar, whatever the column is called");
assert.equal(shape([NUM("RingID")], 1), SHAPE.SCALAR, "1 x 1 RingID stays a scalar");
assert.equal(shape([NUM("ID")], 5), SHAPE.TABLE, "one numeric column over several rows is a table");
assert.equal(shape([NUM("ID"), NUM("Total")], 5), SHAPE.TABLE, "two numeric columns stay a table");
assert.equal(shape([STR("Name"), NUM("Total"), NUM("ID")], 5), SHAPE.TABLE, "three columns stay a table");
assert.equal(shape([STR("Name"), STR("ID")], 5), SHAPE.TABLE, "a text id is a text column: a table, as before");
assert.equal(shape([STR("Code"), NUM("Total")], 5), SHAPE.CHART, "a TEXT code labels a chart; only a numeric one is no measure");
assert.equal(shape([STR("RingID"), NUM("Total")], 5), SHAPE.CHART, "a text RingID labels a chart");
assert.equal(shape([{ name: "Day", type: "datetime" }, NUM("Total")], 5), SHAPE.CHART, "a datetime label keeps charting");
assert.equal(shape([STR("Name"), NUM("Paid")], 5), SHAPE.CHART, "Paid is a measure");
assert.equal(shape([STR("Name"), NUM("Barcode")], 5), SHAPE.CHART, "Barcode is a measure by the rule (see num.js)");
console.log("[ok] determineShape: a numeric id is no measure; one-row, 1 x 1 and text-id shapes are as before");

/* ── 3. The chart engine keeps the same rule ───────────────────────── */

const CATALOG = {
  forms: [
    { id: "bar", jobs: ["rank", "comparison"] },
    { id: "line", jobs: ["trend"], requiresSequence: true },
    { id: "split-bar", jobs: ["comparison"], requiresSequence: true },
    { id: "pie", jobs: ["composition"], maxCategories: 6 },
  ],
};
function framingsFor(columns) {
  const rows = Array.from({ length: 5 }, (_, i) => {
    const row = {};
    columns.forEach((c, j) => { row[c.name] = c.type === "number" ? (5 - i) * (j + 1) : `v${i}`; });
    return row;
  });
  return recommend({
    columns, rows, rowCount: rows.length, truncated: false,
    sql: null, resolvedQuestion: null, tablesTouched: [],
  }, CATALOG).framings;
}

for (const name of [...IDENTIFIERS.map((n) => n.trim()), ...MEASURES.filter((n) => n.trim())]) {
  const offered = framingsFor([STR("Name"), NUM(name)]).length > 0;
  assert.equal(
    offered, !isIdentifierName(name),
    `recommend.js and num.js disagree about ${JSON.stringify(name)}: ` +
    `the engine ${offered ? "plots" : "refuses"} it, num.js says identifier=${isIdentifierName(name)}`,
  );
}

const withId = framingsFor([NUM("ID"), STR("Name"), NUM("Total")]);
assert.equal(withId.length, 1, "an id beside a real measure: one framing");
assert.equal(withId[0].spec.encoding.measure.field, "Total", "the measure is Total, not the id");
assert.equal(withId[0].spec.encoding.label.field, "ID", "the first other column labels it");
console.log("[ok] chart-engine/recommend.js agrees with num.js on every name and measures the first non-id numeric column");

console.log("ALL_SCENARIOS_PASSED");
