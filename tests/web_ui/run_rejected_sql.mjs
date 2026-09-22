// tests/web_ui/run_rejected_sql.mjs
//
// Node-side half of tests/web_ui/test_web_ui_rejected_sql.py.
//
// Drives the REAL turn.js (and its full real render/ dependency chain --
// same staging as run_sql_highlight.mjs) to prove the "See the SQL" action
// (GuardVerdict.rejected_sql, session/models.py) is wired correctly:
//
// * a guard-rejected turn carrying `guard.rejected_sql` renders a "دیدن SQL"
//   control;
// * the refused statement is NOT anywhere in the DOM until that control is
//   activated -- collapsed by default means genuinely absent, not merely
//   hidden;
// * once activated, the statement appears, labelled unmistakably as never
//   having run ("SQL ردشده — اجرا نشد" -- or at least "اجرا نشد" -- appears
//   near it);
// * a statement containing `<img src=x onerror=alert(1)>` renders as TEXT --
//   `codeEl.textContent` equals it exactly, and no `<img>` element is ever
//   created (the same safe display path -- highlightSql -- that the normal
//   SQL box uses, never raw innerHTML of untrusted text);
// * a turn with `guard.rejected_sql: null` (an older persisted turn) falls
//   back to the pre-existing "not retained" message, with no "دیدن SQL"
//   control at all.
//
// Usage: node run_rejected_sql.mjs <path-to-copied-turn.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const turnMjsPath = process.argv[2];
if (!turnMjsPath) {
  console.error("usage: node run_rejected_sql.mjs <path-to-copied-turn.mjs>");
  process.exit(2);
}
const dir = (name) => turnMjsPath.replace(/turn\.mjs$/, name);
const pipelineMjsPath = dir("pipeline.mjs");
const assumptionsMjsPath = dir("assumptions.mjs");
const tableMjsPath = dir("table.mjs");
const llmStatusMjsPath = dir("llm-status.mjs");
const sqlDisplayMjsPath = dir("sql-display.mjs");

/* ── Minimal DOM shim (same as run_sql_highlight.mjs's, including its real
 * inline-HTML parser for innerHTML -- needed here too: this suite must
 * inspect what highlightSql actually put in the DOM, in particular that it
 * never created an <img> element from the injected payload). ──────────── */

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
    this.hidden = false;
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
  set innerHTML(value) {
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
globalThis.window = globalThis;

Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async () => {} } },
  configurable: true,
  writable: true,
});

/* ── Load the real modules under test ───────────────────────────────── */

const { createTurnCard } = await import(pathToFileURL(turnMjsPath).href);
await import(pathToFileURL(sqlDisplayMjsPath).href);
await import(pathToFileURL(pipelineMjsPath).href);
await import(pathToFileURL(assumptionsMjsPath).href);
await import(pathToFileURL(tableMjsPath).href);
await import(pathToFileURL(llmStatusMjsPath).href);

// A real (if hand-picked, tiny) fake -- same shape as run_sql_highlight.mjs's:
// escapes `&`/`<`/`>` and wraps a couple of recognizable substrings in
// `.token` spans, leaving everything else as plain text. Good enough to
// prove highlightSql's "always textContent first, HTML only ever overlays"
// contract without needing the real vendored Prism.
function fakeHighlight(text) {
  const escaped = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return escaped.replace(/\b(SELECT|FROM)\b/g, '<span class="token keyword">$1</span>');
}
globalThis.window.Prism = { languages: { sql: {} }, highlight: (text) => fakeHighlight(text) };

function llm() {
  return {
    backend: "ollama", model: "local", endpoint_status: 200, attempts: 1,
    finish_reason: "stop", structured_output: true, prompt_tokens: 10,
    completion_tokens: 5, prefill_ms: 1, decode_ms: 1, total_ms: 2,
    tokens_per_second: 1, prefix_cache_hit: true, temperature: 0, seed: 1,
    corrections: 0,
  };
}

const noopCtx = {
  onJumpToTurn() {}, onEditAssumption() {}, onClarify() {}, onPin() {},
  onRerun() {}, onRephrase() {}, onAskWithoutColumn() {},
};

function baseRejectedTurn(overrides) {
  return {
    turn_id: "t_rej", session_id: "s_test", index: 1,
    question: "چیزی که نباید اجرا شود", resolved_question: null,
    basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
    ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
    sql: null, sql_display: null,
    guard: { verdict: "rejected", rule: "Forbidden keyword detected: DROP", reason: "forbidden_statement", subject: "DROP", rejected_sql: null, injected_top: null, tables_touched: [] },
    result: { columns: [], rows: [], row_count: 0, truncated: false },
    interpretation: null, tier: "T2", warnings: [], llm: llm(), timings: {}, error: null,
    ...overrides,
  };
}

/* ── Scenario 1: rejected_sql present -- "دیدن SQL" renders, the refused
 * statement is absent from the DOM until activated, then appears labelled
 * as never having run. ─────────────────────────────────────────────────── */

const REJECTED_SQL = "DROP TABLE Customer";
let turn = baseRejectedTurn({
  guard: { verdict: "rejected", rule: "Forbidden keyword detected: DROP", reason: "forbidden_statement", subject: "DROP", rejected_sql: REJECTED_SQL, injected_top: null, tables_touched: [] },
});
let card = createTurnCard(turn, noopCtx);

const toggle = card.el.querySelector(".rejected-sql-toggle");
assert.ok(toggle, "expected a دیدن SQL control (.rejected-sql-toggle) for a guard rejection carrying rejected_sql");
assert.equal(toggle.textContent, "دیدن SQL", "the control's label must be the Persian «دیدن SQL»");

let codeEl = card.el.querySelector("code.language-sql");
assert.equal(codeEl, null, "the refused statement must not be in the DOM at all before the control is activated");
assert.ok(!card.el.textContent.includes(REJECTED_SQL), "the refused SQL text must not appear anywhere on the card before activation");

toggle.click();

codeEl = card.el.querySelector("code.language-sql");
assert.ok(codeEl, "activating the control must render the refused statement");
assert.equal(codeEl.textContent, REJECTED_SQL, "the rendered text must equal the exact refused statement");

const revealBody = card.el.querySelector(".rejected-sql-body");
assert.ok(revealBody, "expected a .rejected-sql-body container");
assert.equal(revealBody.hidden, false, "the body must be revealed (not hidden) after activation");

const heading = card.el.querySelector(".rejected-sql-heading");
assert.ok(heading, "expected a heading labelling the statement as not run");
assert.ok(heading.textContent.includes("اجرا نشد"), `heading must say the statement did not run, got: ${heading.textContent}`);

// Never wired into the copy-SQL / result surfaces.
assert.equal(card.el.querySelector("button.btn-copy"), null, "a guard-rejected turn's rejected-SQL reveal must not carry the normal copy button");
assert.equal(card.result, null, "a guard rejection must still render no result card");

console.log("[ok] guard rejection with rejected_sql: دیدن SQL control renders, refused SQL absent until activated, then shown labelled as not run");

/* ── Scenario 2: XSS payload -- renders as TEXT, never creates a live
 * element from the injected markup. ─────────────────────────────────── */

const XSS_SQL = "SELECT * FROM t WHERE x = '<img src=x onerror=alert(1)>'";
turn = baseRejectedTurn({
  turn_id: "t_rej_xss",
  guard: { verdict: "rejected", rule: "Forbidden keyword detected: DROP", reason: "forbidden_statement", subject: null, rejected_sql: XSS_SQL, injected_top: null, tables_touched: [] },
});
card = createTurnCard(turn, noopCtx);
card.el.querySelector(".rejected-sql-toggle").click();

codeEl = card.el.querySelector("code.language-sql");
assert.ok(codeEl, "expected the rendered code element after activation");
// The payload's `<img ...>` substring must round-trip as exact, inert TEXT
// (Prism only wraps characters in <span> markup, never rewrites them) --
// this is the assertion, not a contradiction of the element check below.
assert.equal(codeEl.textContent, XSS_SQL, "reading the rendered element's textContent back must equal the exact original payload, <img> substring included as plain text");
assert.equal(card.el.querySelectorAll("img").length, 0, "the XSS payload must never produce a live <img> element anywhere on the card");
assert.equal(codeEl.querySelectorAll("img").length, 0, "…nor specifically inside the rendered SQL element");

console.log("[ok] a rejected statement containing <img src=x onerror=alert(1)> renders as text: no <img> element is ever created");

/* ── Scenario 2b: the same XSS payload with NO window.Prism available --
 * must still render as safe plain text (highlightSql's textContent-first
 * guarantee holds even without any highlighting library). ─────────────── */

delete globalThis.window.Prism;
turn = baseRejectedTurn({
  turn_id: "t_rej_xss_no_prism",
  guard: { verdict: "rejected", rule: "boom", reason: "other", subject: null, rejected_sql: XSS_SQL, injected_top: null, tables_touched: [] },
});
card = createTurnCard(turn, noopCtx);
card.el.querySelector(".rejected-sql-toggle").click();
codeEl = card.el.querySelector("code.language-sql");
assert.equal(codeEl.textContent, XSS_SQL, "with no Prism available, the payload must still render as exact plain text");
assert.equal(card.el.querySelectorAll("img").length, 0, "with no Prism available, no <img> element must be created either");
globalThis.window.Prism = { languages: { sql: {} }, highlight: (text) => fakeHighlight(text) };

console.log("[ok] XSS payload stays safe text even with no window.Prism available");

/* ── Scenario 3: rejected_sql is null (an older persisted turn) -- falls
 * back to the pre-existing "not retained" message, no دیدن SQL control. ── */

turn = baseRejectedTurn({
  turn_id: "t_rej_null",
  guard: { verdict: "rejected", rule: "boom", reason: "other", subject: null, rejected_sql: null, injected_top: null, tables_touched: [] },
});
card = createTurnCard(turn, noopCtx);

assert.equal(card.el.querySelector(".rejected-sql-toggle"), null, "no دیدن SQL control must render when rejected_sql is null");
assert.ok(
  card.el.textContent.includes("نگه‌داری نمی‌شود") || card.el.textContent.includes("تولید نشد"),
  "the pre-existing fallback message must still render when rejected_sql is null",
);

console.log("[ok] rejected_sql: null falls back to the pre-existing not-retained message, with no دیدن SQL control");

/* ── Scenario 4: an ALLOWED turn (normal SQL) is completely unaffected --
 * no .rejected-sql-reveal anywhere, normal SQL box renders as before. ──── */

turn = {
  turn_id: "t_allowed", session_id: "s_test", index: 1,
  question: "سوال معمولی", resolved_question: null,
  basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
  ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
  sql: "SELECT TOP 10 c.Name FROM Customer c",
  guard: { verdict: "allowed", rule: null, injected_top: 10, tables_touched: ["Customer"] },
  result: { columns: [{ name: "Name", type: "string" }], rows: [{ Name: "A" }], row_count: 1, truncated: false },
  interpretation: null, tier: "T1", warnings: [], llm: llm(), timings: {}, error: null,
};
card = createTurnCard(turn, noopCtx);
assert.equal(card.el.querySelector(".rejected-sql-reveal"), null, "an allowed turn must never render the rejected-SQL reveal");
assert.ok(card.el.querySelector("code.language-sql"), "an allowed turn's normal SQL box must still render as before");

console.log("[ok] an allowed turn is unaffected: no rejected-SQL reveal, normal SQL box renders as before");

console.log("ALL_SCENARIOS_PASSED");
