// tests/web_ui/run_result_shapes.mjs
//
// Node-side half of tests/web_ui/test_web_ui_result_shapes.py.
//
// Drives the REAL web/js/render/table.js and web/js/render/chart.js source
// (via caller-supplied paths to copies with only the internal
// `./chart.js` / `./table.js` / `../export.js` / `./assumptions.js` import
// specifiers rewritten to their `.mjs` copies -- see the Python test for
// why the rename is necessary) and asserts, against contract-shaped
// TurnResult objects (columns/rows/row_count/truncated, matching
// session/models.py::TurnResult), that:
//
// * determineShape() picks the presentation the brief specifies for each
//   shape -- scalar (1x1 numeric), record (1 row, >1 col), chart (<=30
//   rows, one label + one numeric measure), table (many numeric columns,
//   many rows, or non-numeric-only), and empty (0 rows) -- driven only by
//   `columns[].type` and row counts, never by sniffing cell values;
// * a chart-shaped result renders BOTH a table (view switch) and the
//   figures/story elements, and a non-chart shape does not fabricate a
//   chart;
// * pickLikelyWrongAssumption() prefers a session-sourced assumption over
//   a default/policy one, and a question-sourced one last -- the exact
//   ordering the brief's motivating example depends on ("I am still
//   filtering by the commodity you named two turns ago");
// * a guard-REJECTED empty result never gets the "likely wrong
//   assumption" framing (nothing executed, so that framing would be
//   false) -- this is the exact bug this suite would have caught: an
//   earlier draft of table.js applied that framing to every 0-row result
//   unconditionally, including a guard rejection with no assumptions at
//   all;
// * chooseFramings() only offers the rejected "pie" option once category
//   count makes it genuinely illegible, and always marks it rejected
//   (never silently omitted);
// * chooseFocus() names a real rule (never "none") for every offered
//   framing kind on a non-empty series.
//
// web/ ships no package.json / node_modules (no build step, by design --
// see web/README.md), so this harness brings its own MINIMAL DOM shim
// below rather than depending on jsdom or any other package. It implements
// only the handful of DOM primitives table.js/chart.js actually call
// (createElement/createElementNS/createTextNode, textContent,
// className/classList, dataset, setAttribute/getAttribute, appendChild,
// a simplified querySelector(All) covering tag/class/descendant
// selectors, and addEventListener/click) -- the same spirit as
// run_auth_boundary.mjs's mocked fetch/localStorage: a stand-in for the
// browser runtime, not a mock of the code under test.
//
// Usage: node run_result_shapes.mjs <path-to-copied-table.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const tableMjsPath = process.argv[2];
if (!tableMjsPath) {
  console.error("usage: node run_result_shapes.mjs <path-to-copied-table.mjs>");
  process.exit(2);
}
const chartMjsPath = tableMjsPath.replace(/table\.mjs$/, "chart.mjs");

/* ── Minimal DOM shim (see docstring above) ─────────────────────────── */

class FakeTextNode {
  constructor(text) { this.nodeType = 3; this.textContent = String(text); }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.childNodes = [];
    this._attrs = new Map();
    this._classSet = new Set();
    this._listeners = {};
    this.parentNode = null;
  }
  get children() { return this.childNodes.filter((n) => n.nodeType !== 3); }
  get className() { return [...this._classSet].join(" "); }
  set className(value) { this._classSet = new Set(String(value).split(/\s+/).filter(Boolean)); }
  get classList() {
    const set = this._classSet;
    return {
      add: (c) => set.add(c),
      remove: (c) => set.delete(c),
      contains: (c) => set.has(c),
      toggle: (c) => (set.has(c) ? (set.delete(c), false) : (set.add(c), true)),
    };
  }
  get dataset() {
    const el = this;
    return new Proxy({}, {
      get(_, prop) { return el._attrs.get(`data-${kebab(String(prop))}`) ?? undefined; },
      set(_, prop, value) { el._attrs.set(`data-${kebab(String(prop))}`, String(value)); return true; },
    });
  }
  setAttribute(name, value) {
    if (name === "class") { this.className = value; return; }
    this._attrs.set(name, String(value));
  }
  getAttribute(name) {
    if (name === "class") return this.className;
    return this._attrs.has(name) ? this._attrs.get(name) : null;
  }
  appendChild(node) { node.parentNode = this; this.childNodes.push(node); return node; }
  addEventListener(type, handler) { (this._listeners[type] ||= []).push(handler); }
  click() { for (const h of this._listeners.click || []) h({ preventDefault() {}, target: this }); }
  get textContent() {
    return this.childNodes.map((n) => (n.nodeType === 3 ? n.textContent : n.textContent)).join("");
  }
  set textContent(value) { this.childNodes = [new FakeTextNode(value)]; }
  set innerHTML(_value) { this.childNodes = []; } // only ever set to "" in chart.js, to clear
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const tokens = selector.trim().split(/\s+/);
    let matches = descendantsOf(this).filter((el) => matchesSimple(el, tokens[0]));
    for (let i = 1; i < tokens.length; i++) {
      const next = [];
      for (const m of matches) for (const d of descendantsOf(m)) if (matchesSimple(d, tokens[i])) next.push(d);
      matches = next;
    }
    return matches;
  }
}

function kebab(s) { return s.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`); }

function descendantsOf(el) {
  const out = [];
  const walk = (node) => {
    for (const child of node.children) { out.push(child); walk(child); }
  };
  walk(el);
  return out;
}

function matchesSimple(el, simple) {
  const tagMatch = simple.match(/^[a-zA-Z][a-zA-Z0-9-]*/);
  let rest = simple;
  let tag = null;
  if (tagMatch) { tag = tagMatch[0].toLowerCase(); rest = simple.slice(tag.length); }
  const classes = [...rest.matchAll(/\.([\w-]+)/g)].map((m) => m[1]);
  if (tag && el.tagName.toLowerCase() !== tag) return false;
  for (const c of classes) if (!el.classList.contains(c)) return false;
  return true;
}

globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_ns, tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeTextNode(text),
};

/* ── Load the real modules under test ─────────────────────────────── */

const { determineShape, SHAPE, pickLikelyWrongAssumption, renderResult } =
  await import(pathToFileURL(tableMjsPath).href);
const { chooseFramings, chooseFocus } = await import(pathToFileURL(chartMjsPath).href);

/* ── Scenario 1: shape selection is driven by columns[].type + row counts,
 * per session/models.py::TurnResult / session/engine.py::_infer_type's
 * frozen vocabulary ("number" | "string" | "boolean" | "datetime"). ──── */

function result(columns, rows, { truncated = false } = {}) {
  return { columns, rows, row_count: rows.length, truncated };
}

assert.equal(
  determineShape(result([{ name: "Total", type: "number" }], [{ Total: 42 }])),
  SHAPE.SCALAR,
  "1 row x 1 numeric column must be `scalar`",
);

assert.equal(
  determineShape(result(
    [{ name: "Date", type: "string" }, { name: "Price", type: "number" }, { name: "Broker", type: "string" }],
    [{ Date: "1403/08/21", Price: 2015750, Broker: "Mofid" }],
  )),
  SHAPE.RECORD,
  "1 row x several columns must be `record`, not a one-row table",
);

// Same 2-column, 1-numeric shape as `chart` below, but only 1 row: the
// brief is explicit that a single-row result is a record card, never a
// (useless) one-point chart.
assert.equal(
  determineShape(result([{ name: "Month", type: "string" }, { name: "Value", type: "number" }], [{ Month: "Farvardin", Value: 10 }])),
  SHAPE.RECORD,
  "1 row, even in a label+measure shape, is `record` — a chart needs >=2 points",
);

const seriesRows = Array.from({ length: 12 }, (_, i) => ({ Month: `m${i}`, Value: i * 10 }));
assert.equal(
  determineShape(result([{ name: "Month", type: "string" }, { name: "Value", type: "number" }], seriesRows)),
  SHAPE.CHART,
  "<=30 rows, one label + one numeric measure, must be `chart`",
);

const manyRows = Array.from({ length: 31 }, (_, i) => ({ Month: `m${i}`, Value: i }));
assert.equal(
  determineShape(result([{ name: "Month", type: "string" }, { name: "Value", type: "number" }], manyRows)),
  SHAPE.TABLE,
  "more than ~30 rows must fall back to `table` even with a label+measure shape",
);

assert.equal(
  determineShape(result(
    [{ name: "A", type: "number" }, { name: "B", type: "number" }],
    [{ A: 1, B: 2 }, { A: 3, B: 4 }, { A: 5, B: 6 }],
  )),
  SHAPE.TABLE,
  "two numeric columns (no label column) must be `table`, not `chart`",
);

assert.equal(
  determineShape(result(
    [{ name: "Date", type: "string" }, { name: "Symbol", type: "string" }, { name: "Broker", type: "string" },
     { name: "Volume", type: "number" }, { name: "Price", type: "number" }, { name: "Value", type: "number" }],
    [{ Date: "x", Symbol: "y", Broker: "z", Volume: 1, Price: 2, Value: 3 },
     { Date: "x2", Symbol: "y2", Broker: "z2", Volume: 4, Price: 5, Value: 6 }],
  )),
  SHAPE.TABLE,
  "many numeric columns (a wide detail row) must be `table`",
);

assert.equal(
  determineShape(result([{ name: "Name", type: "string" }], [{ Name: "only a name" }])),
  SHAPE.TABLE,
  "non-numeric-only, even 1x1, must be `table` -- scalar is reserved for numeric",
);

assert.equal(determineShape(result([], [])), SHAPE.EMPTY, "0 rows, no columns, must be `empty`");
assert.equal(
  determineShape(result([{ name: "A", type: "number" }], [])),
  SHAPE.EMPTY,
  "0 rows with real column metadata must still be `empty`, not `table`",
);

console.log("[ok] determineShape matches the brief's shape table for scalar/record/chart/table/empty");

/* ── Scenario 2: a `chart` shape actually renders a table (behind the
 * view switch) AND the chart apparatus; a `table` shape never fabricates
 * chart elements. A test that only checked determineShape's return value
 * would not catch a renderer that ignores it. ──────────────────────── */

const chartBlock = renderResult(result([{ name: "Month", type: "string" }, { name: "Value", type: "number" }], seriesRows));
assert.equal(chartBlock.dataset.shape, "chart");
assert.ok(chartBlock.querySelector(".story-strip"), "chart shape must render the framings story strip");
assert.ok(chartBlock.querySelector(".chart-block svg"), "chart shape must render an SVG chart");
assert.ok(chartBlock.querySelector(".figures-strip"), "chart shape must render the figures strip");
assert.ok(chartBlock.querySelector("table.result-table"), "chart shape must still embed the plain table (view switch)");

const tableBlock = renderResult(result(
  [{ name: "A", type: "number" }, { name: "B", type: "number" }],
  [{ A: 1, B: 2 }, { A: 3, B: 4 }, { A: 5, B: 6 }],
));
assert.equal(tableBlock.dataset.shape, "table");
assert.equal(tableBlock.querySelector(".chart-block svg"), null, "table shape must never fabricate a chart");
assert.equal(tableBlock.querySelector(".story-strip"), null, "table shape must never fabricate a story strip");
assert.ok(tableBlock.querySelector("table.result-table"), "table shape must render the plain table");

const scalarBlock = renderResult(result([{ name: "Total", type: "number" }], [{ Total: 42 }]));
assert.equal(scalarBlock.querySelector("table"), null, "scalar shape: no chart, no table");
assert.equal(scalarBlock.querySelector(".figures-strip"), null, "scalar shape: figures strip is empty per the brief, not three empty tiles");
assert.ok(scalarBlock.querySelector(".scalar-figure"), "scalar shape must render one large figure");

console.log("[ok] renderResult dispatches real DOM per shape (chart embeds a table; table never fakes a chart; scalar has neither)");

/* ── Scenario 3: pickLikelyWrongAssumption prioritises session over
 * default/policy, and only falls back to a question-sourced assumption
 * when nothing else is available. This is the exact ordering the brief's
 * motivating example needs: "I am still filtering by the commodity you
 * named two turns ago" must outrank a same-turn `question` assumption. */

const culprit = pickLikelyWrongAssumption([
  { field: "period", value: "1404", source: "question", editable: true },
  { field: "ring", value: "Metals", source: "session", editable: true },
  { field: "top_n", value: "1000", source: "policy", editable: false },
]);
assert.equal(culprit.field, "ring", "session-sourced assumption must be picked over policy/question");
assert.equal(pickLikelyWrongAssumption([]), null);
assert.equal(pickLikelyWrongAssumption(null), null);

const onlyQuestion = pickLikelyWrongAssumption([{ field: "commodity", value: "steel", source: "question", editable: true }]);
assert.equal(onlyQuestion.field, "commodity", "falls back to a question-sourced assumption when it's the only one available");

console.log("[ok] pickLikelyWrongAssumption prefers session > default/policy > question");

/* ── Scenario 4: THE bug this suite exists to catch. A guard-REJECTED
 * empty result must NOT get the "likely wrong assumption" framing --
 * nothing executed, so "the query ran fine" would be false. renderResult
 * must branch on opts.guardRejected, not render the assumption-focused
 * empty state unconditionally for every 0-row result. ─────────────────*/

const rejectedBlock = renderResult(result([], []), { assumptions: [], guardRejected: true });
assert.equal(rejectedBlock.querySelector(".zero-rows"), null,
  "a guard-rejected 0-row result must NOT render the 'likely wrong assumption' box");
assert.ok(/رد شد/.test(rejectedBlock.textContent), "guard-rejected empty result must say the query was rejected, not blame an assumption");

const allowedEmptyBlock = renderResult(result([], []), {
  assumptions: [{ field: "commodity", value: "steel", source: "session", editable: true }],
  guardRejected: false,
});
assert.ok(allowedEmptyBlock.querySelector(".zero-rows"), "a guard-ALLOWED 0-row result must render the assumption-likely-wrong box");
assert.ok(/commodity/.test(allowedEmptyBlock.textContent), "must name the likely-wrong assumption");

console.log("[ok] guard-rejected 0 rows never gets the assumption-likely-wrong framing; guard-allowed 0 rows does");

/* ── Scenario 5: chooseFramings offers the rejected pie option only once
 * illegible, and it is always explicitly rejected, never silently
 * omitted -- the brief: "an analyst looking for that option deserves an
 * answer". chooseFocus names a real rule for every framing kind. ────── */

const rows6 = Array.from({ length: 6 }, (_, i) => ({ L: `l${i}`, V: i }));
assert.ok(!chooseFramings(rows6, "L", "V").some((f) => f.kind === "pie"), "pie is not offered at all under the illegibility threshold");

const rows7 = Array.from({ length: 7 }, (_, i) => ({ L: `l${i}`, V: i }));
const framings7 = chooseFramings(rows7, "L", "V");
const pie = framings7.find((f) => f.kind === "pie");
assert.ok(pie, "pie must be OFFERED (not omitted) once it stops being legible");
assert.equal(pie.rejected, true, "pie must be explicitly REJECTED, with a reason, not just another option");
assert.ok(pie.reason && pie.reason.length > 0, "the rejected option must carry a reason");
assert.ok(!framings7.some((f) => f.kind !== "pie" && f.rejected), "no non-pie framing should ever be marked rejected");

for (const kind of ["line", "bar", "split-bar"]) {
  const focus = chooseFocus([3, 9, 1, 6], kind);
  assert.notEqual(focus.rule, "none", `chooseFocus must name a real rule for framing kind "${kind}"`);
  assert.ok(focus.index >= 0 && focus.index < 4);
}
assert.equal(chooseFocus([], "line").rule, "none", "an empty series has no rule to name");

console.log("[ok] chooseFramings rejects (never omits) pie once illegible; chooseFocus always names its basis");

/* ── Scenario: no chart label may be drawn outside its own viewBox.
 *
 * SVG neither clips nor reflows text. A label positioned near an edge does
 * not wrap or truncate -- it renders past the boundary and is simply gone.
 * Both directions of that were live:
 *
 *   - the ranked bar chart reserved a fixed 60px gutter for its value
 *     label, which holds "1,240" and not a rial figure in the billions, so
 *     the number ran off the right edge;
 *   - the line chart anchored its end-of-axis labels `middle` at the
 *     extreme x positions, putting half of each label outside the box on
 *     both sides, and drew the focus label above a point that could be the
 *     maximum -- i.e. above the top edge.
 *
 * Asserting geometry rather than appearance is the point: this checks that
 * every text node's estimated box lies within the viewBox, for inputs
 * chosen to stress each edge. A screenshot could not be asserted on, and
 * asserting exact coordinates would break on any legitimate layout change.
 * ─────────────────────────────────────────────────────────────────────── */
{
  const { renderResult } = await import(pathToFileURL(tableMjsPath).href);

  // Deliberately hostile: huge values (long labels), a long category name,
  // and a maximum at the last point so the focus label sits at the top edge.
  const hostileRows = [
    { month: "فروردین", value: 48320000000 },
    { month: "اردیبهشت", value: 51004000000 },
    { month: "خرداد", value: 12000000000 },
    { month: "نام بسیار بسیار طولانی برای یک دسته", value: 97555123456 },
  ];
  const hostile = {
    columns: [{ name: "month", type: "string" }, { name: "value", type: "number" }],
    rows: hostileRows,
    row_count: hostileRows.length,
  };

  // A categorical dimension so the RANKED BAR path is exercised too -- the
  // time-like fixture above only reaches the line chart, and the fixed
  // gutter bug lived in the bar renderer.
  const hostileCategorical = {
    columns: [{ name: "customer", type: "string" }, { name: "value", type: "number" }],
    rows: [
      { customer: "شرکت با نام بسیار طولانی برای آزمودن ستون برچسب", value: 97555123456 },
      { customer: "دوم", value: 51004000000 },
      { customer: "سوم", value: 12000000000 },
    ],
    row_count: 3,
  };

  // Only the ACTIVE framing is in the DOM at a time, so clicking through
  // every offered framing is the only way to reach the bar and split-bar
  // renderers -- and the fixed-gutter overflow lived in the bar one. This
  // also means the check covers whichever framing an analyst actually
  // picks, not just the default.
  const svgs = [];
  for (const fixture of [hostile, hostileCategorical]) {
    const host = renderResult(fixture, {});
    const options = [...host.querySelectorAll(".story-opt")].filter((b) => !b.disabled);
    assert.ok(options.length >= 1, "a chart must offer at least one framing");
    for (const opt of options) {
      opt.click();
      for (const svg of host.querySelectorAll("svg")) svgs.push(svg);
    }
  }
  assert.ok(svgs.length >= 2, `expected charts from every framing, saw ${svgs.length}`);

  const CHAR_W = 12 * 0.6;   // generous: wider than the renderer's own estimate
  let checked = 0;

  for (const svg of svgs) {
    const vb = (svg.getAttribute("viewBox") || "").split(/\s+/).map(Number);
    assert.equal(vb.length, 4, "every chart must declare a viewBox");
    const [, , W, H] = vb;

    for (const text of svg.querySelectorAll("text")) {
      // Direct text nodes only. A <text> may carry a nested <title> for
      // screen readers (the bar chart adds one when it truncates a long
      // category name), and textContent would concatenate that into the
      // measurement, reporting a width the glyphs never occupy.
      const content = [...text.childNodes]
        .filter((node) => node.nodeType === 3)
        .map((node) => node.textContent)
        .join("")
        .trim();
      if (!content) continue;
      const x = Number(text.getAttribute("x"));
      const y = Number(text.getAttribute("y"));
      const anchor = text.getAttribute("text-anchor") || "start";
      const w = content.length * CHAR_W;

      let left = x;
      if (anchor === "middle") left = x - w / 2;
      else if (anchor === "end") left = x - w;
      const right = left + w;

      assert.ok(
        left >= -1,
        `label ${JSON.stringify(content)} starts at ${left.toFixed(1)}, left of the viewBox — it would be clipped`,
      );
      assert.ok(
        right <= W + 1,
        `label ${JSON.stringify(content)} ends at ${right.toFixed(1)} but the viewBox is ${W} wide — it would render outside`,
      );
      // Baseline minus ascent must clear the top; y itself must clear the bottom.
      assert.ok(y - 10 >= -1, `label ${JSON.stringify(content)} sits above the top edge (y=${y})`);
      assert.ok(y <= H + 1, `label ${JSON.stringify(content)} sits below the bottom edge (y=${y}, H=${H})`);
      checked++;
    }
  }

  assert.ok(checked >= 4, `expected several labels to check, saw ${checked}`);
  console.log(`[ok] every chart label stays inside its viewBox (${checked} labels, hostile inputs)`);
}

console.log("ALL_SCENARIOS_PASSED");
