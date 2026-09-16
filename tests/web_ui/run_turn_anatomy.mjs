// tests/web_ui/run_turn_anatomy.mjs
// Asserts DESIGN.md §5.2 / api-contract §5/§7 on createTurnCard DOM order.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const turnMjsPath = process.argv[2];
if (!turnMjsPath) {
  console.error("usage: node run_turn_anatomy.mjs <turn.mjs>");
  process.exit(2);
}

class FakeTextNode {
  constructor(text) { this.nodeType = 3; this.textContent = String(text); }
}
class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this._attrs = new Map();
    this._classSet = new Set();
    this._listeners = {};
    this.hidden = false;
    this.dataset = {};
    this.style = {};
  }
  get className() { return [...this._classSet].join(" "); }
  set className(v) { this._classSet = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get classList() {
    const s = this._classSet;
    return {
      add: (c) => s.add(c),
      remove: (c) => s.delete(c),
      contains: (c) => s.has(c),
      toggle: (c) => (s.has(c) ? (s.delete(c), false) : (s.add(c), true)),
    };
  }
  setAttribute(k, v) { this._attrs.set(k, String(v)); if (k.startsWith("data-")) this.dataset[k.slice(5)] = String(v); }
  getAttribute(k) { return this._attrs.has(k) ? this._attrs.get(k) : null; }
  appendChild(n) { n.parentNode = this; this.childNodes.push(n); return n; }
  append(...nodes) {
    for (const n of nodes) this.appendChild(typeof n === "string" ? new FakeTextNode(n) : n);
  }
  addEventListener(t, h) { (this._listeners[t] ||= []).push(h); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      if (!n || n.nodeType === 3) return;
      if (matches(n, sel)) out.push(n);
      (n.childNodes || []).forEach(walk);
    };
    walk(this);
    return out;
  }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) { this.childNodes = [new FakeTextNode(v)]; }
}
function matches(node, sel) {
  if (sel.startsWith(".")) return node.classList.contains(sel.slice(1));
  if (sel.includes(".")) {
    const [tag, cls] = sel.split(".");
    return (!tag || node.tagName === tag.toUpperCase()) && node.classList.contains(cls);
  }
  return node.tagName === sel.toUpperCase();
}

function childrenInOrder(root) {
  const seq = [];
  const walk = (n) => {
    if (!n || n.nodeType === 3) return;
    seq.push(n);
    (n.childNodes || []).forEach(walk);
  };
  (root.childNodes || []).forEach(walk);
  return seq;
}

globalThis.window = globalThis;
globalThis.document = {
  createElement: (t) => new FakeElement(t),
  createElementNS: (_ns, t) => new FakeElement(t),
  createTextNode: (t) => new FakeTextNode(t),
  body: new FakeElement("body"),
};
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async () => {} } },
  configurable: true,
  writable: true,
});

const { createTurnCard } = await import(pathToFileURL(turnMjsPath).href);

function llm() {
  return {
    backend: "ollama", model: "local", endpoint_status: 200, attempts: 1,
    finish_reason: "stop", structured_output: true, prompt_tokens: 10,
    completion_tokens: 5, prefill_ms: 1, decode_ms: 1, total_ms: 2,
    tokens_per_second: 1, prefix_cache_hit: true, temperature: 0, seed: 1,
    corrections: 0,
  };
}

const ctx = {
  onJumpToTurn() {}, onEditAssumption() {}, onClarify() {}, onPin() {},
  onRerun() {}, async onFlag() {},
};

const turn = {
  turn_id: "t1", session_id: "s1", index: 1,
  question: "Top customers",
  resolved_question: "Top customers by settled trade value in 1403",
  basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
  ambiguity: {
    is_ambiguous: true,
    assumptions: [{ field: "year", value: "1403", source: "question", editable: true }],
    clarifications: [{ field: "ring", options: ["cement"] }],
  },
  sql: "SELECT TOP 10 c.Name FROM t",
  guard: { verdict: "allowed", rule: null, injected_top: 10, tables_touched: ["t"] },
  result: { columns: [{ name: "Name", type: "string" }], rows: [{ Name: "A" }], row_count: 1, truncated: false },
  interpretation: "One row.",
  tier: "T1", warnings: [], llm: llm(),
  timings: { total_ms: 1200, plan_ms: 1, prompt_ms: 1, llm_ms: 1, guard_ms: 1, execute_ms: 1, interpret_ms: 0 },
  error: null,
};

const card = createTurnCard(turn, ctx);
const seq = childrenInOrder(card.el);
const classOf = (n) => n.className || "";

function indexOfClass(sub) {
  return seq.findIndex((n) => classOf(n).includes(sub));
}

const iResolved = indexOfClass("resolved-card");
const iAssump = indexOfClass("assumptions");
const iClar = indexOfClass("clarif");
const iStrip = indexOfClass("stage-strip");
const iOutcome = indexOfClass("turn-outcome");
const iSql = indexOfClass("sql-card-inner");
const iResult = indexOfClass("card") >= 0
  ? seq.findIndex((n, i) => classOf(n) === "card" && seq.slice(0, i).some((p) => classOf(p).includes("sql-card")))
  : -1;
// Result card is the first .card after SQL that is not pipeline/details.
let iResultCard = -1;
for (let i = iSql + 1; i < seq.length; i++) {
  if (classOf(seq[i]) === "card" && classOf(seq[i - 1]) !== "card-title-row") {
    // skip if parent chain is drawer — FakeElement has no parent walk; use class
    iResultCard = i;
    break;
  }
}

assert.ok(iResolved >= 0, "resolved-card must render");
assert.ok(iAssump > iResolved, "assumptions after resolved");
assert.ok(iOutcome > iAssump, "outcome after assumptions");
assert.ok(iSql > iOutcome, "SQL after outcome");
assert.ok(iStrip >= 0, "stage strip must render");
assert.ok(iAssump < iResultCard, `assumptions (${iAssump}) must be before result card (${iResultCard})`);
assert.ok(classOf(card.pipeline.el).includes("steps") || card.pipeline.el.tagName === "OL", "full pipeline list kept for drawer");
assert.equal(typeof card.pipeline.setStage, "function");

// Outcome mentions guard
assert.ok(card.el.textContent.includes("گارد"), "outcome/guard visible in turn text");
// Strip exists
assert.ok(card.el.querySelector(".stage-strip"), "stage-strip in DOM");
// Details drawer
assert.ok(card.el.querySelector(".turn-details"), "details drawer present");

console.log("ALL_ANATOMY_PASSED");
