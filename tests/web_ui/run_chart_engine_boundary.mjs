// tests/web_ui/run_chart_engine_boundary.mjs
//
// Node-side half of tests/web_ui/test_chart_engine_boundary.py.
// Specification: docs/design/CHART-ENGINE-BOUNDARY.md.
//
// NO DOM SHIM. That absence is the test.
//
// Every other harness in this directory (run_result_shapes.mjs,
// run_turn_anatomy.mjs, ...) opens with a hand-written stub DOM, because
// the modules they drive call createElement. This one supplies nothing at
// all: no document, no window, no localStorage. If the engine touches a
// DOM global on any path exercised below, Node throws a ReferenceError and
// the Python half prints the stack. §4.2 is therefore an execution fact
// here, not only a source scan.
//
// THE LOCKED INTERFACE (spec §5). Agreed here before it is agreed in the
// code, deliberately -- the defect this whole engine is a response to was
// an interface that asserted something no test required it to check:
//
//   recommend(input, catalog) -> { framings: Framing[] }
//
//   input = {
//     columns:          [{ name, type }],   // session/models.py::ResultColumn
//     rows:             [ {...} ],
//     rowCount:         number,
//     truncated:        boolean,
//     sql:              string | null,      // Turn.sql   -- spec §4.1
//     resolvedQuestion: string | null,      // Turn.resolved_question
//     tablesTouched:    string[],           // GuardVerdict.tables_touched
//   }
//
//   catalog = { forms: [{ id, jobs: string[], requiresSequence?: bool,
//                         maxCategories?: number }] }
//
//   Framing = {
//     form:   string,            // MUST be an id from catalog.forms
//     job:    string,            // MUST be from the spec §3 closed set
//     reason: string,            // MUST only claim what was actually tested
//     rejected: boolean,
//     rejectionReason?: string,  // required when rejected
//     spec?: object,             // declarative; required when not rejected
//   }
//
// Usage: node run_chart_engine_boundary.mjs <path-to-staged-recommend.mjs>
// Exits 0 and prints ALL_SCENARIOS_PASSED iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const enginePath = process.argv[2];
if (!enginePath) {
  console.error("usage: node run_chart_engine_boundary.mjs <recommend.mjs path>");
  process.exit(2);
}

const mod = await import(pathToFileURL(enginePath).href);
const recommend = mod.recommend;
assert.equal(
  typeof recommend,
  "function",
  "web/js/chart-engine/recommend.js must export a function named `recommend` " +
  "(spec §5). Exports found: " + Object.keys(mod).join(", "),
);

// The §3 closed set. Adding to this is a decision recorded in the spec, not
// an implementation detail -- which is the whole reason it is a closed set.
const JOBS = new Set([
  "rank", "trend", "comparison", "composition",
  "distribution", "correlation", "deviation",
]);

const FULL_CATALOG = {
  forms: [
    { id: "bar", jobs: ["rank", "comparison"] },
    { id: "line", jobs: ["trend"], requiresSequence: true },
    { id: "split-bar", jobs: ["comparison"], requiresSequence: true },
    { id: "pie", jobs: ["composition"], maxCategories: 6 },
  ],
};

// The exact shape of the motivating defect: a ranking, already sorted
// descending by the measure, which is what made it look like a trend.
const RANKING = {
  columns: [{ name: "CustomerName", type: "string" }, { name: "TotalVolume", type: "number" }],
  rows: [
    { CustomerName: "گروه صنعتی سیمان یزد", TotalVolume: 95350 },
    { CustomerName: "گروه صنعتی ساختمانی خزر", TotalVolume: 81200 },
    { CustomerName: "کارخانجات سیمان شاهرود", TotalVolume: 40900 },
    { CustomerName: "سیمان تهران", TotalVolume: 33100 },
    { CustomerName: "سیمان آبیک", TotalVolume: 21400 },
  ],
  rowCount: 5,
  truncated: false,
  sql: "SELECT TOP 10 c.CustomerName, SUM(cc.Volume) AS TotalVolume FROM CustomerContract cc JOIN Customer c ON c.Id = cc.CustomerId GROUP BY c.CustomerName ORDER BY SUM(cc.Volume) DESC",
  resolvedQuestion: "بیشترین حجم معامله به تفکیک مشتری",
  tablesTouched: ["CustomerContract", "Customer"],
};

const MONTHLY = {
  columns: [{ name: "Month", type: "string" }, { name: "Volume", type: "number" }],
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

const offered = (r) => r.framings.filter((f) => !f.rejected);
const formsOf = (r) => offered(r).map((f) => f.form);

/* ── 1. Every framing is well-formed (spec §5) ─────────────────────── */
{
  for (const [name, input] of [["ranking", RANKING], ["monthly", MONTHLY]]) {
    const out = recommend(input, FULL_CATALOG);
    assert.ok(out && Array.isArray(out.framings), `recommend() must return { framings: [] } for ${name}`);
    assert.ok(out.framings.length > 0, `recommend() returned no framings at all for ${name}`);

    for (const f of out.framings) {
      assert.ok(typeof f.form === "string" && f.form, `a framing has no form id (${name})`);
      assert.ok(JOBS.has(f.job), `framing ${f.form} names job ${JSON.stringify(f.job)}, which is not in the §3 closed set (${name})`);
      assert.ok(typeof f.reason === "string" && f.reason.trim(), `framing ${f.form} carries no reason (${name}). §5: a recommendation is a reason, or it is noise`);
      assert.equal(typeof f.rejected, "boolean", `framing ${f.form} does not say whether it is rejected (${name})`);
      if (f.rejected) {
        assert.ok(
          typeof f.rejectionReason === "string" && f.rejectionReason.trim(),
          `framing ${f.form} is rejected with no reason (${name}). §5: rejections are part of the output, never silently dropped`,
        );
      } else {
        assert.ok(f.spec && typeof f.spec === "object", `offered framing ${f.form} carries no declarative spec (${name}). §5`);
      }
    }
  }
  console.log("[ok] every framing carries form, a §3 job, a reason, and either a spec or a rejection reason");
}

/* ── 2. Catalog obedience (spec §8.2) ──────────────────────────────── */
{
  const out = recommend(RANKING, FULL_CATALOG);
  const ids = new Set(FULL_CATALOG.forms.map((f) => f.id));
  for (const f of out.framings) {
    assert.ok(ids.has(f.form), `recommend() returned form ${JSON.stringify(f.form)}, which the host never declared. §5: the form id must come from the catalog`);
  }

  // A host that only ships a bar chart must not be handed a line.
  const barOnly = { forms: [{ id: "bar", jobs: ["rank", "comparison"] }] };
  const narrowed = recommend(MONTHLY, barOnly);
  for (const f of narrowed.framings) {
    assert.equal(f.form, "bar", `a bar-only host was offered ${f.form}. §8.2: the engine selects from the catalog, it does not extend it`);
  }
  assert.ok(narrowed.framings.length > 0, "a bar-only host got nothing at all for a time series. §8.2: degrade honestly, do not go silent");
  console.log("[ok] the engine never invents a form the host did not declare, and still answers a narrowed catalog");
}

/* ── 3. An empty catalog is answered, not crashed ──────────────────── */
{
  const out = recommend(RANKING, { forms: [] });
  assert.ok(out && Array.isArray(out.framings), "recommend() must still return { framings: [] } for an empty catalog");
  assert.equal(offered(out).length, 0, "an empty catalog somehow produced an offered framing");
  console.log("[ok] an empty catalog yields no offered framing rather than an exception");
}

/* ── 4. The §0 regression, at the job layer (spec §8.5) ────────────── */
{
  const out = recommend(RANKING, FULL_CATALOG);

  for (const f of offered(out)) {
    assert.notEqual(
      f.job, "trend",
      `a ranking of customer names was assigned the 'trend' job via form ${f.form}. ` +
      "Company names are not a sequence; a trend reading of them is the defect " +
      "this engine exists to make impossible (spec §0, §3)",
    );
  }
  assert.ok(!formsOf(out).includes("line"), `a line was offered for categorical labels: ${JSON.stringify(formsOf(out))}. A line asserts order and continuity the data does not have`);
  assert.ok(!formsOf(out).includes("split-bar"), "the first-half/second-half framing was offered for categorical labels. Halves are a property of an ordered series");
  assert.ok(formsOf(out).includes("bar"), "a ranking was not offered a bar chart -- rank is what this data is about");
  assert.ok(offered(out).some((f) => f.job === "rank"), "no framing named the 'rank' job for an explicitly ranked query");

  // Over-correction guard: removing the line entirely is as wrong as always offering it.
  const seq = recommend(MONTHLY, FULL_CATALOG);
  assert.ok(formsOf(seq).includes("line"), `a real monthly series lost its line framing: ${JSON.stringify(formsOf(seq))}`);
  assert.ok(offered(seq).some((f) => f.job === "trend"), "a real monthly series was never assigned the 'trend' job");
  console.log("[ok] a ranking is never a trend, and a real sequence still gets its line");
}

/* ── 5. No offered framing describes categories with trend language ── */
{
  const out = recommend(RANKING, FULL_CATALOG);
  for (const f of offered(out)) {
    for (const w of ["صعودی", "کاهش", "روند", "trend", "rising", "declining"]) {
      assert.ok(
        !String(f.reason).includes(w) && !String(f.spec?.headline || "").includes(w),
        `framing ${f.form} describes categorical data with trend language (${JSON.stringify(w)}). ` +
        "The number was right and the sentence was wrong, which is worse than no sentence",
      );
    }
  }
  console.log("[ok] no offered framing describes a ranking as a trend");
}

/* ── 6. A reason may only claim what was tested (spec §5, §8.4) ────── */
{
  // The original defect shipped a reason asserting «سنجه در طول یک توالی است»
  // while nothing checked the label axis. Whatever wording replaces it, a
  // sequence claim must appear only where a sequence genuinely exists.
  const out = recommend(RANKING, FULL_CATALOG);
  for (const f of offered(out)) {
    for (const w of ["توالی", "sequence", "over time", "در طول زمان"]) {
      assert.ok(
        !String(f.reason).includes(w),
        `framing ${f.form} claims a sequence (${JSON.stringify(w)}) for categorical labels. ` +
        "§5: a reason may only state what the engine actually tested -- a reason " +
        "that outruns its check is how the original defect survived review",
      );
    }
  }
  console.log("[ok] no reason claims a sequence that the data does not have");
}

/* ── 7. Determinism (spec §8.3) ────────────────────────────────────── */
{
  const a = recommend(RANKING, FULL_CATALOG);
  const b = recommend(RANKING, FULL_CATALOG);
  assert.deepEqual(a, b, "recommend() is not deterministic: two identical calls produced different output. §8.3");

  // Serialisable, because §8.3's cross-process claim is only meaningful if
  // the output survives JSON -- and because a value that does not (a DOM
  // node, a function, a Symbol) would be a §4.2 violation wearing a disguise.
  let json;
  try {
    json = JSON.stringify(a);
  } catch (e) {
    assert.fail(`recommend()'s output is not JSON-serialisable (${e.message}). §5: the output is declarative data`);
  }
  assert.deepEqual(JSON.parse(json), a, "recommend()'s output does not survive a JSON round-trip. §5: declarative data, not objects with behaviour");
  console.log("[ok] output is deterministic and JSON-serialisable");
}

/* ── 8. Input is not mutated ───────────────────────────────────────── */
{
  // A selector that rewrites its caller's rows (sorting them in place, say)
  // makes every later reading of that turn depend on whether a chart was
  // recommended first. Pure means pure.
  const before = JSON.stringify(RANKING);
  recommend(RANKING, FULL_CATALOG);
  assert.equal(JSON.stringify(RANKING), before, "recommend() mutated its input. §4: the engine is pure -- sort a copy");
  console.log("[ok] the caller's input is left untouched");
}

/* ── 9. A truncated result is never described as a whole ───────────── */
{
  // `truncated` means the rows on screen are a prefix of a larger answer.
  // A composition framing ("share of the whole") over a prefix states
  // something false about the warehouse -- the same class of error as §0.
  const truncated = { ...RANKING, truncated: true, rowCount: 4200 };
  const out = recommend(truncated, FULL_CATALOG);
  for (const f of offered(out)) {
    assert.notEqual(
      f.job, "composition",
      "a truncated result was offered a composition framing. Shares of a " +
      "whole computed over a prefix of the rows are not shares of the whole",
    );
  }
  console.log("[ok] a truncated result is not framed as a composition");
}

console.log("ALL_SCENARIOS_PASSED");
