// tests/web_ui/run_sql_highlight.mjs
//
// Node-side half of tests/web_ui/test_web_ui_sql_highlight.py.
//
// web/js/render/turn.js now syntax-highlights the generated-SQL block with
// the vendored Prism (web/assets/vendor/prism.min.js/prism-sql.min.js,
// loaded as plain <script> globals in index.html -- see turn.js's
// highlightSql). This drives the REAL turn.js (and its full real render/
// dependency chain: pipeline.js, assumptions.js, table.js, chart.js,
// export.js, llm-status.js -- via caller-supplied paths to copies with
// only the internal import specifiers rewritten to their sibling `.mjs`
// copies, same pattern as run_result_shapes.mjs) under a mocked
// `window.Prism` / `navigator.clipboard` and a minimal DOM shim, and
// asserts, at the actual boundary the brief calls out:
//
// * highlighting actually runs: the rendered <code> ends up with real
//   `.token.*` child elements, not just plain text;
// * highlighting NEVER changes what the SQL block visually represents as
//   text -- codeEl.textContent (i.e. reading the highlighted markup back)
//   still equals the exact original SQL string, entities and all;
// * the copy button copies that exact original string TO THE CLIPBOARD --
//   not the highlighted markup, not a re-serialization of the DOM -- for
//   both `turn.sql` and a turn carrying a distinct `sql_display`;
// * highlighting is presentation-only in the failure direction too: with
//   no `window.Prism` at all, and separately with a `Prism.highlight` that
//   throws, the SQL still renders as plain, correct, uncorrupted text and
//   still copies correctly -- a decoration failure must never hide or
//   corrupt the SQL itself.
//
// web/ ships no package.json / node_modules by design, so this brings its
// own minimal DOM shim (same spirit as run_result_shapes.mjs's), extended
// with a real (if tiny) inline-HTML parser for `innerHTML` -- needed here,
// unlike the other harnesses in this directory, because this suite must
// actually inspect the *markup* Prism produced, not just clear it.
//
// Usage: node run_sql_highlight.mjs <path-to-copied-turn.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const turnMjsPath = process.argv[2];
if (!turnMjsPath) {
  console.error("usage: node run_sql_highlight.mjs <path-to-copied-turn.mjs>");
  process.exit(2);
}
const dir = (name) => turnMjsPath.replace(/turn\.mjs$/, name);
const pipelineMjsPath = dir("pipeline.mjs");
const assumptionsMjsPath = dir("assumptions.mjs");
const tableMjsPath = dir("table.mjs");
const llmStatusMjsPath = dir("llm-status.mjs");

/* ── Minimal DOM shim (same spirit as run_result_shapes.mjs's, extended
 * with a real inline-HTML parser for innerHTML -- see module docstring
 * for why this suite needs one where the others don't). ───────────────── */

function decodeEntities(s) {
  return s
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&"); // must run last
}

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
    this.style = {};
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
  append(...nodes) {
    for (const n of nodes) this.appendChild(typeof n === "string" ? new FakeTextNode(n) : n);
  }
  addEventListener(type, handler) { (this._listeners[type] ||= []).push(handler); }
  click() { for (const h of this._listeners.click || []) h({ preventDefault() {}, target: this }); }
  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }
  set textContent(value) { this.childNodes = [new FakeTextNode(value)]; }
  // A real (if tiny) parser: this suite needs to inspect the *markup*
  // Prism produced (real .token spans), not just clear it the way the
  // other harnesses' innerHTML stub does (they only ever set it to "").
  // Controlled input only (our own fake Prism.highlight's output below),
  // so a single-pass tag/text tokenizer is sufficient -- no attributes
  // besides `class` are ever produced, and every tag is well-formed and
  // properly closed.
  set innerHTML(value) {
    // NOTE: this pushes through each frame's real `appendChild` (root
    // included, via this tiny stand-in), never a bare `.children` array --
    // FakeElement's `.children` above is a read-only GETTER that computes
    // a fresh filtered array from `childNodes` on every access, so
    // `.children.push(...)` on a real node would silently push onto a
    // throwaway array and vanish. That exact mistake is why an earlier
    // version of this parser reconstructed a highlighted <code> whose
    // token spans were all rendered EMPTY (their text quietly discarded)
    // while textContent still "looked" plausible-ish -- appendChild avoids
    // it entirely by reusing the same, already-correct method real
    // elements use everywhere else in this shim.
    const rootFrame = { _kids: [], appendChild(n) { this._kids.push(n); return n; } };
    const stack = [rootFrame];
    const tagRe = /<(\/?)([a-zA-Z][\w-]*)([^>]*)>|([^<]+)/g;
    let m;
    while ((m = tagRe.exec(value))) {
      if (m[4] !== undefined) {
        stack[stack.length - 1].appendChild(new FakeTextNode(decodeEntities(m[4])));
      } else if (m[1] === "/") {
        stack.pop();
      } else {
        const node = new FakeElement(m[2]);
        const classMatch = /class="([^"]*)"/.exec(m[3] || "");
        if (classMatch) node.className = classMatch[1];
        stack[stack.length - 1].appendChild(node);
        stack.push(node);
      }
    }
    this.childNodes = rootFrame._kids;
    for (const c of this.childNodes) c.parentNode = this;
  }
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
  const walk = (node) => { for (const child of node.children) { out.push(child); walk(child); } };
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

// turn.js checks `window.Prism` (a plain <script> global in the real
// page, not an import -- see highlightSql's comment). In a browser,
// `window === globalThis`; mirroring that here is what lets
// `window.Prism = ...` below and turn.js's own `window.Prism` read agree.
globalThis.window = globalThis;

// Clipboard mock: records the exact string the copy button handed it.
// Modern Node (>=21) already defines a read-only `navigator` global of its
// own (a getter with no setter), so a plain `globalThis.navigator = ...`
// throws "Cannot set property navigator of #<Object> which has only a
// getter" -- redefine the property outright instead of assigning to it.
let clipboardCalls = [];
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async (text) => { clipboardCalls.push(text); } } },
  configurable: true,
  writable: true,
});

async function flushMicrotasks() {
  // copyToClipboard is async and its caller (the click listener) does not
  // await it, so the click() call above returns before the awaited
  // navigator.clipboard.writeText promise actually settles. A couple of
  // event-loop turns is enough to let it resolve.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
}

/* ── Load the real modules under test ───────────────────────────────── */

const { createTurnCard } = await import(pathToFileURL(turnMjsPath).href);
// Loaded only to confirm they import cleanly under this shim (turn.js
// pulls them in transitively); not driven directly by this suite.
await import(pathToFileURL(pipelineMjsPath).href);
await import(pathToFileURL(assumptionsMjsPath).href);
await import(pathToFileURL(tableMjsPath).href);
await import(pathToFileURL(llmStatusMjsPath).href);

/* ── Fixture: a realistic Turn carrying real SQL, matching the shape
 * every other Turn in this codebase does (see web/js/data.js's t_01). ── */

function llm() {
  return {
    backend: "ollama", model: "gpt-oss-20b", endpoint_status: 200, attempts: 1,
    finish_reason: "stop", structured_output: true, prompt_tokens: 4680,
    completion_tokens: 132, prefill_ms: 2210, decode_ms: 430, total_ms: 2640,
    tokens_per_second: 30.7, prefix_cache_hit: false, temperature: 0, seed: 7,
    corrections: 0,
  };
}

const noopCtx = {
  onJumpToTurn() {}, onEditAssumption() {}, onClarify() {}, onPin() {}, onRerun() {},
};

function baseTurn(overrides) {
  return {
    turn_id: "t_test", session_id: "s_test", index: 1,
    question: "نمونه", resolved_question: null,
    basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
    ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
    guard: { verdict: "allowed", rule: null, injected_top: 100, tables_touched: ["CustomerContract"] },
    result: { columns: [{ name: "Total", type: "number" }], rows: [{ Total: 2015750 }], row_count: 1, truncated: false },
    interpretation: null, tier: "T1", warnings: [], llm: llm(), timings: {}, error: null,
    ...overrides,
  };
}

// Deliberately includes a `<` (an operator Prism must escape when it
// re-serializes the token stream) so decode round-tripping is actually
// exercised, not just plain alphanumeric text that would pass even with
// a broken entity decoder.
const SQL = "SELECT TOP 100 c.Name\nFROM [Auction_Dim].[Customer] c\nWHERE ct.TotalPrice < 1000 -- demo comment\nORDER BY c.Name";

/* ── Scenario 1: Prism present and working -- highlighting actually
 * applies real .token elements, AND textContent (reading the highlighted
 * markup back) still equals the exact original SQL, AND the copy button
 * still copies that exact original string, not the markup. ───────────── */

// A deliberately realistic fake: real Prism.highlight HTML-escapes the
// source text and wraps recognized substrings in `<span class="token
// KIND">...</span>`, leaving everything else as plain text in between --
// exactly what this fake does, just with a tiny hand-picked rule set
// instead of a full SQL grammar.
function fakeHighlight(text) {
  const escaped = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return escaped
    .replace(/\b(SELECT|TOP|FROM|WHERE|ORDER BY)\b/g, '<span class="token keyword">$1</span>')
    .replace(/(-- .*)$/gm, '<span class="token comment">$1</span>');
}

globalThis.window.Prism = { languages: { sql: {} }, highlight: (text) => fakeHighlight(text) };

clipboardCalls = [];
let turn = baseTurn({ sql: SQL, sql_display: undefined });
let card = createTurnCard(turn, noopCtx);
let codeEl = card.el.querySelector("code.language-sql");
assert.ok(codeEl, "the SQL <code> element must carry the language-sql class");
assert.ok(codeEl.querySelectorAll(".token").length > 0, "Prism highlighting must actually run: expected real .token elements, found none");
assert.equal(codeEl.textContent, SQL, "reading the highlighted element's textContent back must still equal the exact original SQL (Prism only wraps characters, never changes them)");

const copyBtn = card.el.querySelector("button.btn-copy");
assert.ok(copyBtn, "expected a copy button in the SQL section");
copyBtn.click();
await flushMicrotasks();
assert.equal(clipboardCalls.length, 1, "clicking copy must call navigator.clipboard.writeText exactly once");
assert.equal(clipboardCalls[0], SQL, "the clipboard must receive the exact original SQL text, not Prism's highlighted markup");
console.log("[ok] Prism highlighting applies real .token markup, textContent still equals the exact original SQL, and copy copies that exact original text");

/* ── Scenario 2: a turn with a DISTINCT sql_display (e.g. a reformatted
 * display variant) -- highlighting must run against sql_display (what is
 * actually shown), and copy must still yield sql_display exactly, not
 * `sql`, matching the pre-existing `turn.sql_display || turn.sql`
 * precedence this change must not disturb. ─────────────────────────── */

const RAW_SQL = "SELECT*FROM t WHERE x<1";
const DISPLAY_SQL = "SELECT *\nFROM t\nWHERE x < 1";
clipboardCalls = [];
turn = baseTurn({ sql: RAW_SQL, sql_display: DISPLAY_SQL, turn_id: "t_test2" });
card = createTurnCard(turn, noopCtx);
codeEl = card.el.querySelector("code.language-sql");
assert.equal(codeEl.textContent, DISPLAY_SQL, "must render/highlight sql_display, not the raw sql, when both are present");
card.el.querySelector("button.btn-copy").click();
await flushMicrotasks();
assert.equal(clipboardCalls[0], DISPLAY_SQL, "copy must yield sql_display exactly (the pre-existing precedence), never the raw sql nor any markup");
console.log("[ok] sql_display takes precedence for both highlighting and copy, exactly as before this change");

/* ── Scenario 3: no window.Prism at all (script blocked / offline asset
 * missing) -- must fall back to plain, uncorrupted text, and copy must
 * still work correctly. Presentation-only in the failure direction. ─── */

delete globalThis.window.Prism;
clipboardCalls = [];
turn = baseTurn({ sql: SQL, turn_id: "t_test3" });
card = createTurnCard(turn, noopCtx);
codeEl = card.el.querySelector("code.language-sql");
assert.equal(codeEl.querySelectorAll(".token").length, 0, "with no Prism available, no .token markup should appear");
assert.equal(codeEl.textContent, SQL, "with no Prism available, the SQL must still render as the exact original text");
card.el.querySelector("button.btn-copy").click();
await flushMicrotasks();
assert.equal(clipboardCalls[0], SQL, "copy must still work correctly with no Prism available");
console.log("[ok] missing window.Prism: falls back to plain, correct text; copy still works");

/* ── Scenario 4: Prism.highlight THROWS -- must not propagate, must not
 * blank the SQL, must fall back to plain correct text, copy still
 * works. This is the exact "a decoration failure must never hide or
 * corrupt the SQL" guarantee highlightSql's docstring promises. ──────── */

globalThis.window.Prism = {
  languages: { sql: {} },
  highlight() { throw new Error("boom — simulated Prism failure"); },
};
clipboardCalls = [];
turn = baseTurn({ sql: SQL, turn_id: "t_test4" });
assert.doesNotThrow(() => { card = createTurnCard(turn, noopCtx); }, "a throwing Prism.highlight must not propagate out of createTurnCard");
codeEl = card.el.querySelector("code.language-sql");
assert.equal(codeEl.textContent, SQL, "a throwing Prism.highlight must still leave the exact original SQL text rendered, not blank/corrupted");
card.el.querySelector("button.btn-copy").click();
await flushMicrotasks();
assert.equal(clipboardCalls[0], SQL, "copy must still work correctly when Prism.highlight throws");
console.log("[ok] a throwing Prism.highlight is caught: plain correct text still renders, copy still works");

console.log("ALL_SCENARIOS_PASSED");
