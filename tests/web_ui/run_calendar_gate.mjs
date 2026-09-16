// tests/web_ui/run_calendar_gate.mjs
//
// Node-side half of tests/web_ui/test_web_ui_calendar_gate.py.
//
// Drives the REAL web/js/chart-engine/recommend.js (via a caller-supplied
// path to a staged copy whose relative import specifiers were rewritten to
// .mjs -- see test_chart_engine_boundary.py::_stage, reused by the Python
// half, for why the rename is necessary and why the engine is staged alone
// rather than through test_web_ui_result_shapes._prepare_copy).
//
// WHY THIS FILE EXISTS (task D4-calendar-gate)
// ---------------------------------------------
// `isSequenceLabel` -- the one gate standing between a categorical label
// and a "trend" reading it never earned -- used to test the column name
// with `key.includes(word)`, a SUBSTRING match. That let a column whose
// name merely CONTAINS a calendar word borrow the sequence treatment
// without earning it: "MonthlyCustomer" contains "month", so a plain
// ranking of customers -- no sorted-by-value setup required, unlike the
// original §0 incident this engine already guards against -- was assigned
// the `trend` job and offered a `line` framing over data that was never a
// series. That is the same class of mistake test_chart_engine_boundary.py
// already pins for the sorted-ranking case, reached here through a name
// collision instead.
//
// The fix (see the comment on `isSequenceLabel` in recommend.js and
// chart.js) matches WHOLE TOKENS -- split at camelCase boundaries and
// non-letter separators, lowercase, exact-match against
// SEQUENCE_LABEL_WORDS -- so "Monthly" (token "monthly") no longer matches
// "month" and Persian "تاریخچه" no longer matches "تاریخ". This harness
// asserts that fix through the engine's public `recommend(input, catalog)`
// interface, the same way run_chart_engine_boundary.mjs pins the original
// incident.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const enginePath = process.argv[2];
if (!enginePath) {
  console.error("usage: node run_calendar_gate.mjs <recommend.mjs path>");
  process.exit(2);
}

const mod = await import(pathToFileURL(enginePath).href);
const recommend = mod.recommend;
assert.equal(
  typeof recommend,
  "function",
  "web/js/chart-engine/recommend.js must export a function named `recommend`. " +
  "Exports found: " + Object.keys(mod).join(", "),
);

const FULL_CATALOG = {
  forms: [
    { id: "bar", jobs: ["rank", "comparison"] },
    { id: "line", jobs: ["trend"], requiresSequence: true },
    { id: "split-bar", jobs: ["comparison"], requiresSequence: true },
    { id: "pie", jobs: ["composition"], maxCategories: 6 },
  ],
};

const offered = (r) => r.framings.filter((f) => !f.rejected);
const formsOf = (r) => offered(r).map((f) => f.form);
const jobsOf = (r) => offered(r).map((f) => f.job);

/* ── 1. A categorical column whose NAME contains a calendar word ─────
 * "MonthlyCustomer" is a ranking of customers -- string-typed, no time
 * axis at all -- whose label column name merely happens to CONTAIN
 * "month". Before the fix, `key.includes("month")` matched and the row
 * was wrongly treated as a sequence. */
{
  const input = {
    columns: [
      { name: "MonthlyCustomer", type: "string" },
      { name: "TotalVolume", type: "number" },
    ],
    rows: [
      { MonthlyCustomer: "گروه صنعتی سیمان یزد", TotalVolume: 95350 },
      { MonthlyCustomer: "گروه صنعتی ساختمانی خزر", TotalVolume: 81200 },
      { MonthlyCustomer: "کارخانجات سیمان شاهرود", TotalVolume: 40900 },
      { MonthlyCustomer: "سیمان تهران", TotalVolume: 33100 },
      { MonthlyCustomer: "سیمان آبیک", TotalVolume: 21400 },
    ],
    rowCount: 5,
    truncated: false,
    sql: "SELECT c.MonthlyCustomer, SUM(cc.Volume) AS TotalVolume FROM CustomerContract cc JOIN Customer c ON c.Id = cc.CustomerId GROUP BY c.MonthlyCustomer",
    resolvedQuestion: "حجم معامله به تفکیک مشتری ماهانه",
    tablesTouched: ["CustomerContract", "Customer"],
  };
  const out = recommend(input, FULL_CATALOG);

  assert.ok(
    !formsOf(out).includes("line"),
    `a line framing was offered for label column "MonthlyCustomer": ${JSON.stringify(formsOf(out))}. ` +
    'The old substring check matched "month" inside "MonthlyCustomer" and ' +
    "wrongly treated a plain customer ranking as a time series",
  );
  assert.ok(
    !jobsOf(out).includes("trend"),
    `label column "MonthlyCustomer" was assigned the 'trend' job: ${JSON.stringify(jobsOf(out))}. ` +
    "A customer-ranking column whose name merely contains the substring " +
    '"month" is not a calendar dimension',
  );
  assert.ok(
    formsOf(out).includes("bar"),
    "a ranking with label column \"MonthlyCustomer\" was not offered a bar chart -- rank is what this data is about",
  );
  console.log('[ok] "MonthlyCustomer" (categorical, contains "month" as a substring) is not offered line/trend');
}

/* ── 2. "Month" itself is still a real sequence ───────────────────── */
{
  const input = {
    columns: [
      { name: "Month", type: "string" },
      { name: "Volume", type: "number" },
    ],
    rows: [
      { Month: "1403/01", Volume: 10 }, { Month: "1403/02", Volume: 22 },
      { Month: "1403/03", Volume: 31 }, { Month: "1403/04", Volume: 44 },
      { Month: "1403/05", Volume: 58 },
    ],
    rowCount: 5,
    truncated: false,
    sql: "SELECT d.Month, SUM(cc.Volume) AS Volume FROM CustomerContract cc JOIN Date d ON d.Id = cc.DateId GROUP BY d.Month ORDER BY d.Month",
    resolvedQuestion: "روند ماهانهٔ حجم معاملات",
    tablesTouched: ["CustomerContract", "Date"],
  };
  const out = recommend(input, FULL_CATALOG);
  assert.ok(
    formsOf(out).includes("line"),
    `a named time dimension ("Month") lost its line framing: ${JSON.stringify(formsOf(out))}. ` +
    "Fixing the substring false-positive must not remove the true positive",
  );
  assert.ok(
    jobsOf(out).includes("trend"),
    `label column "Month" was never assigned the 'trend' job: ${JSON.stringify(jobsOf(out))}`,
  );
  console.log('[ok] "Month" is still recognised as a real sequence');
}

/* ── 3. A declared datetime column is a sequence whatever it is called ─
 * Same over-correction guard as run_chart_engine_boundary.mjs: the
 * `labelType === "datetime"` short-circuit must survive untouched --
 * it is the stronger signal and must run before the name heuristic. */
{
  const input = {
    columns: [
      { name: "when", type: "datetime" },
      { name: "v", type: "number" },
    ],
    rows: [
      { when: "2026-01-01", v: 5 },
      { when: "2026-02-01", v: 9 },
      { when: "2026-03-01", v: 14 },
    ],
    rowCount: 3,
    truncated: false,
    sql: null,
    resolvedQuestion: null,
    tablesTouched: [],
  };
  const out = recommend(input, FULL_CATALOG);
  assert.ok(
    formsOf(out).includes("line"),
    "a column declared type=datetime must count as a sequence even when its " +
    "name matches no known calendar word -- the declared type is the stronger signal",
  );
  console.log("[ok] a declared datetime label still counts as a sequence regardless of its name");
}

/* ── 4. The Persian categorical name-collision case ───────────────────
 * "تاریخچه_مشتری" ("customer history") CONTAINS "تاریخ" ("date") as a
 * substring but is not itself a calendar column -- a customer-history
 * ranking, not a series. */
{
  const input = {
    columns: [
      { name: "تاریخچه_مشتری", type: "string" },
      { name: "TotalVolume", type: "number" },
    ],
    rows: [
      { "تاریخچه_مشتری": "گروه صنعتی سیمان یزد", TotalVolume: 95350 },
      { "تاریخچه_مشتری": "گروه صنعتی ساختمانی خزر", TotalVolume: 81200 },
      { "تاریخچه_مشتری": "کارخانجات سیمان شاهرود", TotalVolume: 40900 },
    ],
    rowCount: 3,
    truncated: false,
    sql: "SELECT c.[تاریخچه_مشتری], SUM(cc.Volume) AS TotalVolume FROM CustomerContract cc JOIN Customer c ON c.Id = cc.CustomerId GROUP BY c.[تاریخچه_مشتری]",
    resolvedQuestion: "حجم معامله به تفکیک تاریخچهٔ مشتری",
    tablesTouched: ["CustomerContract", "Customer"],
  };
  const out = recommend(input, FULL_CATALOG);

  assert.ok(
    !jobsOf(out).includes("trend"),
    `Persian label column "تاریخچه_مشتری" was assigned the 'trend' job: ${JSON.stringify(jobsOf(out))}. ` +
    'It contains "تاریخ" ("date") only as a substring of "تاریخچه" ("history"), ' +
    "not as its own word",
  );
  assert.ok(
    !formsOf(out).includes("line"),
    `a line framing was offered for "تاریخچه_مشتری": ${JSON.stringify(formsOf(out))}`,
  );
  console.log('[ok] Persian "تاریخچه_مشتری" (contains "تاریخ" only as a substring) is not a trend');
}

console.log("ALL_SCENARIOS_PASSED");
