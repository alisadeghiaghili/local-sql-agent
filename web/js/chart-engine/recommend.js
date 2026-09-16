// SPDX-License-Identifier: BUSL-1.1
// Copyright (c) 2024-2026 Ali Sadeghi Aghili
//
// web/js/chart-engine/recommend.js — the chart-recommendation engine.
//
// Specification: docs/design/CHART-ENGINE-BOUNDARY.md. Read it first; this
// file implements that document's §3-§6 and §7 step 1, and every design
// choice below that is not obvious from the code points back at a numbered
// section rather than re-arguing it here.
//
// THE INCIDENT THIS EXISTS FOR (spec §0)
// ---------------------------------------
// Ten customers, ranked by traded volume, once rendered as a LINE chart
// under a headline claiming the amount "was declining". Nothing was
// declining -- the rows arrive sorted by value, so a ranking drew the
// shape of a downward trend, and the headline reported that invented
// shape as fact. The cause was one unconditional branch whose own stated
// reason -- "the measure is along a sequence" -- was never tested.
//
// `web/js/render/chart.js`'s `chooseFramings` already carries the fix (a
// real `isSequenceLabel` gate) as of the commit that closed the incident.
// This module is NOT that fix landing twice; it is the SAME decision moved
// behind a boundary strict enough that a future wrong choice is caught by
// a test in this directory rather than rediscovered in production (spec
// §2). `web/js/render/chart.js` is left untouched and still owns what the
// analyst actually sees -- wiring this engine in is a separate change with
// its own test (spec §7 step 1, §9).
//
// WHY A SEPARATE MODULE AND NOT A SHARED IMPORT
// -----------------------------------------------
// The obvious shortcut is to have this file `import` the logic straight
// out of chart.js and re-export it. That would satisfy every test below on
// the day it is written and fail the intent behind writing them: chart.js
// builds DOM (`document.createElement`), and an import graph that crosses
// into it is exactly the "grew roots" failure spec §2 exists to catch
// before extraction, not after. So the sequence-label gate, the framing
// logic, and the focus-point logic are re-derived here from the same
// reasoning, deliberately duplicated once rather than shared once and
// coupled forever.
//
// WHAT THIS FILE MAY NOT DO (spec §4.2), AND WHY THAT IS A TEST, NOT A
// STYLE RULE
// -----------------------------------------------------------------------
// No `document`, `window`, `localStorage`, `sessionStorage`, `fetch`, or
// `Date.now`. The clock is grouped with the DOM globals on purpose: a
// selector whose output depends on the time of day cannot be pinned by a
// test, and the tests in `tests/web_ui/test_chart_engine_boundary.py` are
// the only thing standing between this module and spec §0 happening again
// at this layer. `tests/web_ui/run_chart_engine_boundary.mjs` runs this
// module in bare Node with NO DOM shim at all, so a violation is a
// ReferenceError with a stack trace, not a passed test that happened not
// to exercise the bad path.
//
// This module also imports nothing outside this directory. Not `../num.js`
// (Persian-digit formatting), not anything under `web/js/render/`. Two
// independent reasons, not one:
//   1. spec §4.2 forbids a dependency on the render layer outright.
//   2. `tests/web_ui/test_chart_engine_boundary.py::_stage` copies only
//      `web/js/chart-engine/**/*.js` into a scratch directory and rewrites
//      *relative* import specifiers to `.mjs`. An import of `../num.js`
//      would become `../num.mjs`, pointing outside the staged directory,
//      and Node would fail to resolve it -- loudly, which is the point,
//      but there is no reason to court that failure. So `spec.headline`
//      below carries whatever raw numbers it needs UNFORMATTED (a JS
//      number, or a plain digit string), and formatting them for a
//      Persian-reading analyst is left to whichever renderer eventually
//      consumes this module's output. Presentation is the far side of the
//      boundary this file exists to keep (spec §5: "spec is data, not
//      DOM").

"use strict";

/* ── §3: the job vocabulary ───────────────────────────────────────────
 * Closed set. Adding to it is a decision recorded in the spec, not an
 * implementation detail -- which is the entire reason it is closed.
 * Step 1 (this file) only ever ASSIGNS "rank", "trend" and "comparison" to
 * an OFFERED framing; "composition" appears exactly once below, on the
 * pie form, and only ever on a REJECTED framing (see `evaluatePie`) --
 * never recommended, so it is not a job this step "produces" in the sense
 * the spec means, only one it correctly NAMES for a form it refuses to
 * offer. "distribution", "correlation" and "deviation" have no form in
 * today's vocabulary and are not referenced anywhere below; they exist so
 * a later step can add a form under an existing job name instead of
 * inventing one under time pressure. */
const JOBS = Object.freeze({
  RANK: "rank",
  TREND: "trend",
  COMPARISON: "comparison",
  COMPOSITION: "composition",
});

/* ── §4.1: the sequence gate, ported unchanged ────────────────────────
 * This list and the two-signal check below are `web/js/render/chart.js`'s
 * `isSequenceLabel` and `SEQUENCE_LABEL_WORDS`, moved rather than
 * reinvented. Restated here (see this file's header) because importing
 * the original would cross the render boundary this module exists to
 * keep on the far side of.
 *
 * `"string"` alone is deliberately never a sequence signal -- that is
 * precisely the assumption that drew a trend line through customer names
 * in the incident this engine is a response to (spec §0). A generic label
 * ("name", a customer/broker/ring column) falls through both checks and
 * gets no `trend` job, no matter how the rows happen to be sorted.
 *
 * A SECOND, NARROWER NAME-COLLISION INCIDENT -- also §0-shaped, caught in
 * the same review -- lived inside this same function: it used to test
 * `key.includes(word)`, a SUBSTRING match. "MonthlyCustomer" contains the
 * substring "month", so a plain ranking of customers was offered `trend`
 * via a name collision, no sorted-by-value setup required. The fix, ported
 * here identically from `chart.js` (see that file's comment on
 * `isSequenceLabel` for the full account), matches WHOLE TOKENS only:
 * split the column name at camelCase boundaries and at runs of non-letter
 * separators, lowercase each piece, and require an EXACT match against
 * SEQUENCE_LABEL_WORDS -- so "Monthly" (token "monthly") no longer matches
 * "month", and "تاریخچه" (Persian letters kept together as one token, not
 * torn apart by an ASCII-only split) no longer matches "تاریخ". A known,
 * stated limitation survives this fix: a compound name whose OWN camelCase
 * component genuinely equals a calendar word -- "YearEndBroker" tokenizes
 * to ["year", "end", "broker"], and "year" really is on the list -- still
 * reads as a sequence. That is a limit of matching on the name at all, not
 * a regression this change introduces; `labelType === "datetime"` is the
 * real signal for those cases, and the name check remains a heuristic on
 * top of it. */
const SEQUENCE_LABEL_WORDS = [
  "date", "day", "month", "quarter", "year",
  "تاریخ", "روز", "ماه", "فصل", "سال",
];

function isSequenceLabel(labelKey, labelType) {
  if (labelType === "datetime") return true;
  const tokens = String(labelKey || "")
    .split(/(?=[A-Z])|[^A-Za-z؀-ۿ]+/)
    .map((t) => t.toLowerCase())
    .filter(Boolean);
  return tokens.some((t) => SEQUENCE_LABEL_WORDS.includes(t));
}

/* ── §4.1: `sql` as a signal, added here for the first time ───────────
 * `ORDER BY <measure> DESC` is a ranking, stated by the query itself --
 * the motivating incident's own query was a `SELECT TOP 10 ... ORDER BY
 * SUM(...) DESC`, and nothing read that before now. Constraint from spec
 * §4.1: a SUBSTRING SCAN, never a SQL parser, and a wrong parse must
 * degrade to "no signal", never to a wrong signal.
 *
 * That constraint is why this function's result is only ever used
 * ADDITIVELY below (it enriches a `rank` reason with a claim that is then
 * actually true, because the regex that backs it just ran) and NEVER to
 * assign or veto a job by itself. `isSequenceLabel` alone still decides
 * whether `trend` is even on the table; a query with a stray `DESC` in an
 * unrelated clause can, at worst, make the `bar` framing's reason say
 * something true-but-unnecessary. It can never manufacture a `trend` it
 * should not, which is the failure mode spec §0 is about. */
function sqlStatesDescendingOrder(sql) {
  if (typeof sql !== "string" || sql.length === 0) return false;
  return /\bORDER\s+BY\b[\s\S]*?\bDESC\b/i.test(sql);
}

/* ── shared arithmetic, pure ──────────────────────────────────────────
 * `rows` is never sorted or written to anywhere in this file -- only
 * read, via `Number(row[key])` and `.map()`/`.slice()`, both of which
 * return new arrays. That is what makes purity (spec §5.1, "recommend
 * does not mutate input") a fact about the code rather than a claim about
 * it: there is no in-place array method call for a mutation to hide in. */
function numericValues(rows, measureKey) {
  return rows.map((row) => {
    const v = Number(row[measureKey]);
    return Number.isFinite(v) ? v : 0;
  });
}

function indexOfMax(values) {
  let idx = 0;
  for (let i = 1; i < values.length; i++) if (values[i] > values[idx]) idx = i;
  return idx;
}

/** Ported verbatim from `chart.js::lineHeadline` -- reachable only through
 * `evaluateLine`, i.e. only once `isSequenceLabel` has already returned
 * true for this data, so its trend/direction wording is a claim the
 * sequence gate actually backs (spec §5). No number appears in any of the
 * three cases, so there is no formatting decision to defer to a renderer
 * here -- see this file's header on why that matters for `../num.js`. */
function lineHeadline(rows, labelKey, values, maxIdx) {
  const lastIdx = values.length - 1;
  if (maxIdx === lastIdx) return `روند تا ${rows[lastIdx][labelKey]} صعودی بود`;
  if (maxIdx === 0) return `مقدار از همان ${rows[0][labelKey]} رو به کاهش بود`;
  return `روند تا ${rows[maxIdx][labelKey]} صعودی بود، سپس آرام گرفت`;
}

/** Picks the row that carries a framing's message and names the rule used
 * -- ported from `chart.js`'s `chooseFocus`. `spec.emphasis` carries only
 * this (`index` + `rule`); *how* that translates to lightness and stroke
 * weight is DESIGN-INVARIANTS.md §10's job, decided once there and not
 * reinvented as a second colour policy here (spec §6). No hex, no CSS
 * variable name, anywhere in this file. */
function focusFor(values, formId) {
  if (!values.length) return { index: -1, rule: "none" };
  if (formId === "bar" || formId === "line") return { index: indexOfMax(values), rule: "max" };
  if (formId === "split-bar") return { index: values.length - 1, rule: "latest" };
  let idx = 0;
  let biggest = -1;
  for (let i = 1; i < values.length; i++) {
    const d = Math.abs(values[i] - values[i - 1]);
    if (d > biggest) { biggest = d; idx = i; }
  }
  return { index: idx, rule: "largest_change" };
}

/* ── §4/§5: turning `input` into the context each form evaluator reads ─
 * Today's whole vocabulary (bar, line, split-bar, pie) is a reading of
 * ONE label column plus ONE measure column -- the same shape
 * `table.js::determineShape`'s `CHART` case already requires before this
 * module is ever reached in the wired-up product (`columns.length === 2`,
 * exactly one of them numeric). This engine does not get that guarantee
 * for free -- its input contract is the raw `TurnResult` shape, not a
 * pre-filtered one -- so it re-derives the same label/measure split
 * itself (mirroring `renderChartAndTable`'s `columns.find`) and answers
 * "nothing to recommend" honestly when the data does not fit that shape,
 * rather than guessing at a wrong pair of columns. */
function buildContext(input) {
  const columns = Array.isArray(input.columns) ? input.columns : [];
  const rows = Array.isArray(input.rows) ? input.rows : [];
  const measureCol = columns.find((c) => c && c.type === "number");
  const labelCol = columns.find((c) => c && c !== measureCol);
  if (!measureCol || !labelCol || rows.length === 0) return null;

  const measureKey = measureCol.name;
  const labelKey = labelCol.name;
  const values = numericValues(rows, measureKey);

  return {
    rows,
    labelKey,
    labelType: labelCol.type,
    measureKey,
    values,
    n: values.length,
    maxIdx: indexOfMax(values),
    isSequence: isSequenceLabel(labelKey, labelCol.type),
    sqlRanks: sqlStatesDescendingOrder(input.sql),
    truncated: input.truncated === true,
  };
}

/* ── §5.1: one evaluator per form id in today's vocabulary ─────────────
 * Each returns a well-formed `Framing`, or `null` to mean "this form has
 * nothing to say about this data" -- which is a SILENT omission, not a
 * rejection. That distinction matters and is not accidental: spec §5
 * keeps only ONE explicitly-modelled rejection in today's vocabulary
 * (pie, over the legibility line) because that is the one
 * `chooseFramings` already surfaced with a reason ("an analyst looking
 * for an option they cannot find deserves an answer"). `line` and
 * `split-bar` failing their sequence gate is not that case -- today's UI
 * never showed a disabled "line" chip on a customer-name ranking either,
 * it simply never offered one, and this port changes nothing an analyst
 * would see (spec §7 step 1, §9) by keeping that silence. */

function evaluateBar(ctx, formDecl) {
  if (!Array.isArray(formDecl.jobs) || !formDecl.jobs.includes(JOBS.RANK)) return null;

  // Wording is deliberately different for the sequence-true and
  // sequence-false cases, and NEITHER may use the words
  // tests/web_ui/run_chart_engine_boundary.mjs bans from an OFFERED
  // framing on categorical data ("صعودی", "کاهش", "روند", "توالی",
  // "sequence", ...). The obvious phrasing for the false case --
  // "labels are not a sequence" -- itself CONTAINS the word "توالی" and
  // would fail that check while being a true statement; this is exactly
  // spec §5's rule that a reason may only claim what was tested, applied
  // to wording rather than to fact: saying what a sequence check found
  // is fine when it found one (see `evaluateLine`), and saying so in the
  // negative is what the test refuses, so the negative case is phrased
  // without the word at all.
  let reason;
  if (ctx.isSequence) {
    reason = "برچسب‌ها یک بازهٔ زمانی‌اند، اما پرسش رتبه است نه مسیر حرکت؛ میلهٔ افقی مستقیم رتبه را نشان می‌دهد.";
  } else if (ctx.sqlRanks) {
    reason = "پرس‌وجو با ORDER BY نزولی روی همین سنجه نوشته شده -- یعنی خودِ پرسش رتبه‌بندی خواسته؛ میلهٔ افقی مستقیم آن رتبه را نشان می‌دهد.";
  } else {
    reason = "برچسب‌ها نام‌های دسته‌ای‌اند، نه ردیف‌های پیاپی یک بازه؛ میلهٔ افقی مستقیم رتبه را نشان می‌دهد.";
  }

  return {
    form: "bar",
    job: JOBS.RANK,
    reason,
    rejected: false,
    spec: {
      mark: "bar",
      orientation: "horizontal",
      sort: "descending",
      encoding: {
        label: { field: ctx.labelKey, type: ctx.labelType || "string" },
        measure: { field: ctx.measureKey, type: "number" },
      },
      emphasis: focusFor(ctx.values, "bar"),
      // A plain string, not an object -- `run_chart_engine_boundary.mjs`
      // scenario 5 scans `spec.headline` as text for the same banned
      // trend/sequence words it scans `reason` for. Handing it structured
      // data instead of prose would make that scan vacuous (`String({})`
      // is `"[object Object]"`, which contains none of the banned words
      // by construction) -- passing the check without it having checked
      // anything is exactly the failure mode spec §5's "a reason may only
      // claim what was tested" rule exists to catch, so this stays text a
      // human could read as-is. It needs no number, so it needs no
      // formatting decision either -- the ONE ported headline that does
      // (split-bar's) is built the other way; see `evaluateSplitBar`.
      headline: `${ctx.rows[ctx.maxIdx][ctx.labelKey]} بیشترین مقدار را داشت`,
    },
  };
}

function evaluateLine(ctx, formDecl) {
  // A catalog entry's own `requiresSequence` is deliberately NOT consulted
  // here -- it would let a catalog that marks line as not requiring one
  // license this ported "line" reading without a real sequence, and the
  // whole point of the gate below is that nothing does that (spec §0).
  // `isSequenceLabel` stays the sole authority for this vocabulary.
  if (!ctx.isSequence) return null; // silent omission -- see this section's header comment
  if (!Array.isArray(formDecl.jobs) || !formDecl.jobs.includes(JOBS.TREND)) return null;

  const focus = focusFor(ctx.values, "line");

  return {
    form: "line",
    job: JOBS.TREND,
    // Honest by construction: `isSequence` is the gate this branch is
    // behind, so a claim that a sequence check ran and passed is true
    // every time this string is produced -- spec §5's rule stated
    // positively instead of merely avoided.
    reason: "ستون برچسب یک توالی است -- نوع آن «datetime» است یا نامش با واژگان تقویمی (روز/ماه/فصل/سال) مطابقت دارد؛ خط پیوستگی و جهت تغییر را نشان می‌دهد.",
    rejected: false,
    spec: {
      mark: "line",
      encoding: {
        label: { field: ctx.labelKey, type: ctx.labelType || "string" },
        measure: { field: ctx.measureKey, type: "number" },
      },
      emphasis: focus,
      // Ported from `chart.js::lineHeadline` verbatim (same three cases,
      // same wording) -- only reachable once `isSequence` is true, so the
      // trend/direction words it uses are never in front of the
      // categorical-data check above. No number to format, same as bar.
      headline: lineHeadline(ctx.rows, ctx.labelKey, ctx.values, focus.index),
    },
  };
}

function evaluateSplitBar(ctx, formDecl) {
  if (!ctx.isSequence) return null; // silent omission -- see this section's header comment
  if (ctx.n < 4) return null; // ported threshold: a two-point-per-half split needs >= 4 rows
  if (!Array.isArray(formDecl.jobs) || !formDecl.jobs.includes(JOBS.COMPARISON)) return null;

  const half = Math.floor(ctx.n / 2);
  const firstHalfTotal = ctx.values.slice(0, half).reduce((a, b) => a + b, 0);
  const secondHalfTotal = ctx.values.slice(half).reduce((a, b) => a + b, 0);
  const deltaPct = firstHalfTotal === 0
    ? null
    : Math.round(((secondHalfTotal - firstHalfTotal) / Math.abs(firstHalfTotal)) * 100);

  return {
    form: "split-bar",
    job: JOBS.COMPARISON,
    reason: "ساده‌ترین شکل وقتی پیام یک مقایسهٔ دوتایی است؛ چند نقطهٔ پیاپی به دو عدد خلاصه می‌شود.",
    rejected: false,
    spec: {
      mark: "grouped-bar",
      encoding: {
        measure: { field: ctx.measureKey, type: "number" },
      },
      buckets: [
        { label: "first_half", total: firstHalfTotal },
        { label: "second_half", total: secondHalfTotal },
      ],
      // `deltaPct` (a raw, unformatted number, or null when the first
      // half summed to zero and a percentage change is undefined) lives
      // on the bucket data above, not spelled out here -- the ORIGINAL
      // `chooseFramings` headline embedded the formatted magnitude
      // ("۴۲٪ بیشتر") via `../num.js`'s Persian-digit formatter, which
      // this module cannot import (see this file's header). Rather than
      // embed the raw Latin-digit number as a downgrade, the headline
      // stays qualitative (direction only, which needs no formatting) and
      // the magnitude is left for a renderer to state, formatted, from
      // `buckets` -- a smaller, honest headline beats a precise-looking
      // one with the wrong digits.
      headline:
        deltaPct === null
          ? "مقایسهٔ نیمهٔ اول و دوم"
          : `نیمهٔ دوم نسبت به نیمهٔ اول ${deltaPct >= 0 ? "بیشتر" : "کمتر"} بود`,
    },
  };
}

/** Pie is ported as spec §5 requires -- "chooseFramings already shows the
 * rejected pie framing with its reason, and that is the behaviour to
 * keep" -- and ONLY that behaviour. `chart.js::chooseFramings` never once
 * OFFERS a pie; it is either absent (category count within legibility) or
 * present and explicitly rejected (category count past it). This
 * evaluator preserves both halves of that: `null` (silent omission)
 * within the limit, a rejected framing past it, never a `rejected: false`
 * pie. `job: "composition"` on that rejected framing names what pie is
 * FOR, per the catalog's own declaration -- it is not this step
 * recommending a composition reading, which is why it carries no `spec`
 * and can never appear in `offered()`; see this file's `JOBS` comment. */
function evaluatePie(ctx, formDecl) {
  if (!Array.isArray(formDecl.jobs) || !formDecl.jobs.includes(JOBS.COMPOSITION)) return null;
  const maxCategories = typeof formDecl.maxCategories === "number" ? formDecl.maxCategories : 6;
  if (ctx.n <= maxCategories) return null; // silent omission -- see this section's header comment

  return {
    form: "pie",
    job: JOBS.COMPOSITION,
    reason: "رد شد: تعداد دسته‌ها از سقف خوانایی این شکل بیشتر است.",
    rejected: true,
    rejectionReason: `مناسب نیست: مقایسهٔ ${ctx.n} زاویه برای چشم دشوار است و ترتیب داده‌ها از بین می‌رود.`,
  };
}

const STEP1_FORM_EVALUATORS = Object.freeze({
  bar: evaluateBar,
  line: evaluateLine,
  "split-bar": evaluateSplitBar,
  pie: evaluatePie,
});

/**
 * Recommends chart framings for a query result, from a host-declared
 * catalog of forms it is allowed to choose among (spec §1, borrowing
 * A2UI's catalog and rejecting its "model chooses" authority).
 *
 * @param {{
 *   columns: {name: string, type: string}[],
 *   rows: object[],
 *   rowCount: number,
 *   truncated: boolean,
 *   sql: string|null,
 *   resolvedQuestion: string|null,
 *   tablesTouched: string[],
 * }} input
 * @param {{forms: {id: string, jobs: string[], requiresSequence?: boolean, maxCategories?: number}[]}} catalog
 * @returns {{framings: object[]}}
 */
export function recommend(input, catalog) {
  const forms = catalog && Array.isArray(catalog.forms) ? catalog.forms : [];
  const framings = [];

  const ctx = buildContext(input || {});
  if (!ctx) return { framings };

  // §5.1 "truncation honesty": a `truncated` result is never given the
  // `composition` job. Today's vocabulary never OFFERS composition at all
  // (see `evaluatePie`'s docstring), so this guard is currently
  // unreachable in practice -- kept explicit anyway, as the rule this
  // file must not silently rely on a coincidence to satisfy: the day a
  // later step adds a form that DOES offer `composition`, this check is
  // already here instead of needing to be remembered.
  const truncated = ctx.truncated === true;

  for (const formDecl of forms) {
    if (!formDecl || typeof formDecl.id !== "string") continue;
    const evaluate = STEP1_FORM_EVALUATORS[formDecl.id];
    if (!evaluate) continue; // spec §9: no new chart forms -- an unknown catalog id is simply not served
    const framing = evaluate(ctx, formDecl);
    if (!framing) continue;
    if (truncated && !framing.rejected && framing.job === JOBS.COMPOSITION) continue;
    framings.push(framing);
  }

  return { framings };
}
