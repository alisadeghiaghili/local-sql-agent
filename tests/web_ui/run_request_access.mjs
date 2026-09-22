// tests/web_ui/run_request_access.mjs
//
// Node-side half of tests/web_ui/test_web_ui_request_access.py.
//
// Drives the REAL turn.js (and its full real render/ dependency chain --
// same staging as run_rejected_sql.mjs/run_sql_highlight.mjs) to prove
// "درخواست دسترسی" (ADR-004 part 1 -- DESIGN-INVARIANTS.md §8's
// "Ask without that column · Request access") is wired correctly:
//
// * a denied-column rejection with ctx.onRequestAccess wired renders a
//   «درخواست دسترسی» button alongside «پرسش بدون «...»»;
// * clicking it calls onRequestAccess(turnId, column) with the exact
//   column name (guard.subject);
// * while the call is pending the button is disabled and its label
//   changes;
// * on success it shows the "ثبت شد و در انتظار بررسی است" pending
//   sentence, worded differently when the server reports
//   already_pending: true ("پیش‌تر ثبت شده");
// * on a rejected call the button re-enables with its original label and
//   an error message shows instead;
// * a column name containing `<img src=x onerror=alert(1)>` renders as
//   TEXT everywhere it appears (the button label is unaffected -- it
//   never echoes the column -- and the pending/error sentence is built
//   through the same safe `el()`/`textContent` path every other field in
//   this file uses) -- no `<img>` element is ever created;
// * the button is absent when ctx.onRequestAccess is not supplied, or
//   when the rejection names no single column (subject: null), or when
//   the rejection reason is not "denied_column" -- mirroring
//   onAskWithoutColumn's own "offer only what is wired, and only when
//   there is one column to name" rule.
//
// Usage: node run_request_access.mjs <path-to-copied-turn.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const turnMjsPath = process.argv[2];
if (!turnMjsPath) {
  console.error("usage: node run_request_access.mjs <path-to-copied-turn.mjs>");
  process.exit(2);
}
const dir = (name) => turnMjsPath.replace(/turn\.mjs$/, name);
const pipelineMjsPath = dir("pipeline.mjs");
const assumptionsMjsPath = dir("assumptions.mjs");
const tableMjsPath = dir("table.mjs");
const llmStatusMjsPath = dir("llm-status.mjs");
const sqlDisplayMjsPath = dir("sql-display.mjs");

/* ── Minimal DOM shim -- identical shape to run_rejected_sql.mjs's (this
 * suite needs the same querySelector/dataset/classList/click surface, and
 * inspects the DOM the same way to prove no live element is created from
 * an untrusted payload). ────────────────────────────────────────────── */

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
    this.disabled = false;
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
  insertBefore(node, ref) {
    node.parentNode = this;
    const idx = ref ? this.childNodes.indexOf(ref) : -1;
    if (idx === -1) this.childNodes.push(node);
    else this.childNodes.splice(idx, 0, node);
    return node;
  }
  append(...nodes) {
    for (const n of nodes) this.appendChild(typeof n === "string" ? new FakeTextNode(n) : n);
  }
  addEventListener(type, handler) { (this._listeners[type] ||= []).push(handler); }
  click() { for (const h of this._listeners.click || []) h({ preventDefault() {}, currentTarget: this, target: this }); }
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
  closest(selector) {
    let node = this;
    while (node) {
      if (matchesSimple(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
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

function deniedColumnTurn({ subject = "SecretColumnXYZ", turnId = "t_1" } = {}) {
  return {
    turn_id: turnId, session_id: "s_test", index: 1,
    question: "چیزی که ستون محدود دارد", resolved_question: null,
    basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
    ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
    sql: null, sql_display: null,
    guard: {
      verdict: "rejected", rule: `Forbidden keyword detected: denied column '${subject}'`,
      reason: "denied_column", subject, rejected_sql: null, injected_top: null, tables_touched: [],
    },
    result: { columns: [], rows: [], row_count: 0, truncated: false },
    interpretation: null, tier: "T2", warnings: [], llm: llm(), timings: {}, error: null,
  };
}

function findAll(root, tagName) {
  // FakeElement.tagName is stored exactly as `document.createElement`
  // received it (turn.js's own `el()` helper always passes lowercase tag
  // names, e.g. "button") -- unlike a real DOM element, it is never
  // normalised to uppercase. Compared case-insensitively so callers can
  // still pass the conventional uppercase tag name.
  const wanted = tagName.toUpperCase();
  const out = [];
  const walk = (node) => {
    for (const child of node.children) {
      if (String(child.tagName).toUpperCase() === wanted) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

function findButtonByText(root, text) {
  return findAll(root, "BUTTON").find((b) => b.textContent === text);
}

/** `btn.click()` here is synchronous (the FakeElement shim calls every
 * registered listener directly, without awaiting) -- but
 * handleRequestAccessClick's own success/failure handling runs inside a
 * `.then()`/`.catch()` chained onto an ASYNC function's returned promise,
 * which is two independent microtask hops away (one for the async
 * function's own resolution, one more for the `.then`/`.catch` callback
 * attached to it) -- more than a single bare `await` reliably flushes.
 * Awaiting this a few times after a click gives every pending microtask
 * chain a chance to fully settle before assertions run. */
async function flushMicrotasks(times = 8) {
  for (let i = 0; i < times; i++) await Promise.resolve();
}

const noopCtx = {
  onJumpToTurn() {}, onEditAssumption() {}, onClarify() {}, onPin() {},
  onRerun() {}, onRephrase() {}, onAskWithoutColumn() {},
};

/* ── Scenario 1: button renders, alongside "Ask without that column",
 * with the correct label, and calls onRequestAccess with (turnId, column). */
{
  const calls = [];
  const turn = deniedColumnTurn({ subject: "SecretColumnXYZ", turnId: "t_1" });
  const card = createTurnCard(turn, {
    ...noopCtx,
    onRequestAccess: async (turnId, column) => {
      calls.push({ turnId, column });
      return { already_pending: false };
    },
  });

  const askWithoutBtn = findButtonByText(card.el, "پرسش بدون «SecretColumnXYZ»");
  assert.ok(askWithoutBtn, "expected the existing 'ask without column' button to still render");

  const btn = findButtonByText(card.el, "درخواست دسترسی");
  assert.ok(btn, "expected a «درخواست دسترسی» button for a denied-column rejection");

  btn.click();
  await flushMicrotasks();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].turnId, "t_1");
  assert.equal(calls[0].column, "SecretColumnXYZ");

  console.log("[ok] «درخواست دسترسی» renders beside «پرسش بدون «...»» and calls onRequestAccess(turnId, column)");
}

/* ── Scenario 2: pending state -- disabled, label changes while in flight,
 * then shows the "ثبت شد و در انتظار بررسی است" sentence on success. ──── */
{
  let resolveCall;
  const pending = new Promise((resolve) => { resolveCall = resolve; });
  const turn = deniedColumnTurn({ subject: "SecretColumnXYZ", turnId: "t_2" });
  const card = createTurnCard(turn, {
    ...noopCtx,
    onRequestAccess: async () => pending,
  });
  const btn = findButtonByText(card.el, "درخواست دسترسی");

  btn.click();
  assert.equal(btn.disabled, true, "the button must disable itself while the request is in flight");
  assert.notEqual(btn.textContent, "درخواست دسترسی", "the label must change while pending");

  resolveCall({ already_pending: false });
  await flushMicrotasks();

  assert.ok(
    card.el.textContent.includes("ثبت شد و در انتظار بررسی است"),
    `expected the pending sentence somewhere on the card, got: ${card.el.textContent}`,
  );
  assert.ok(card.el.textContent.includes("SecretColumnXYZ"), "the sentence must name the column");
  const status = card.el.querySelector(".request-access-status");
  assert.ok(status, "expected a .request-access-status element");
  assert.ok(status.classList.contains("ok"), "a successful submission must be styled as ok");

  console.log("[ok] pending state disables the button and shows the submitted sentence on success");
}

/* ── Scenario 2b: already_pending: true gets the "already open" wording. */
{
  const turn = deniedColumnTurn({ subject: "SecretColumnXYZ", turnId: "t_2b" });
  const card = createTurnCard(turn, {
    ...noopCtx,
    onRequestAccess: async () => ({ already_pending: true }),
  });
  const btn = findButtonByText(card.el, "درخواست دسترسی");
  btn.click();
  await flushMicrotasks();

  assert.ok(
    card.el.textContent.includes("پیش‌تر ثبت شده"),
    `expected the 'already pending' sentence, got: ${card.el.textContent}`,
  );

  console.log("[ok] already_pending: true shows the 'already open' wording, not the fresh-submission one");
}

/* ── Scenario 3: a rejected onRequestAccess re-enables the button with its
 * original label and shows the error message. ─────────────────────────── */
{
  const turn = deniedColumnTurn({ subject: "SecretColumnXYZ", turnId: "t_3" });
  const card = createTurnCard(turn, {
    ...noopCtx,
    onRequestAccess: async () => { throw new Error("network error"); },
  });
  const btn = findButtonByText(card.el, "درخواست دسترسی");
  const originalLabel = btn.textContent;

  btn.click();
  await flushMicrotasks();

  assert.equal(btn.disabled, false, "a failed submission must be retryable, not permanently disabled");
  assert.equal(btn.textContent, originalLabel, "the button label must revert on failure");
  const status = card.el.querySelector(".request-access-status");
  assert.ok(status, "expected a .request-access-status element");
  assert.equal(status.textContent, "network error");
  assert.ok(status.classList.contains("error"));

  console.log("[ok] a rejected onRequestAccess re-enables the button and shows the error");
}

/* ── Scenario 4: XSS payload as the column name -- renders as TEXT
 * everywhere, no <img> element is ever created. ───────────────────────── */
{
  const XSS_COLUMN = "<img src=x onerror=alert(1)>";
  const turn = deniedColumnTurn({ subject: XSS_COLUMN, turnId: "t_xss" });
  const card = createTurnCard(turn, {
    ...noopCtx,
    onRequestAccess: async () => ({ already_pending: false }),
  });

  // The button's own label is fixed text ("درخواست دسترسی") -- it never
  // echoes the column, so there is nothing there for the payload to reach.
  const btn = findButtonByText(card.el, "درخواست دسترسی");
  assert.ok(btn, "expected the button to still render for an (unsanitized) subject");

  btn.click();
  await flushMicrotasks();

  const status = card.el.querySelector(".request-access-status");
  assert.ok(status.textContent.includes(XSS_COLUMN), "the exact payload must appear as literal text");
  assert.equal(card.el.querySelectorAll("img").length, 0, "the payload must never produce a live <img> element");

  console.log("[ok] a column name containing <img src=x onerror=alert(1)> renders as text, no <img> element created");
}

/* ── Scenario 5: the button is absent when not wired, or when there is no
 * single column to name, or when the rejection is not denied_column. ──── */
{
  const turn = deniedColumnTurn({ subject: "SecretColumnXYZ", turnId: "t_5a" });
  const card = createTurnCard(turn, noopCtx); // no onRequestAccess at all
  assert.equal(findButtonByText(card.el, "درخواست دسترسی"), undefined, "must not render without ctx.onRequestAccess");
}
{
  const turn = deniedColumnTurn({ subject: null, turnId: "t_5b" });
  const card = createTurnCard(turn, { ...noopCtx, onRequestAccess: async () => ({ already_pending: false }) });
  assert.equal(findButtonByText(card.el, "درخواست دسترسی"), undefined, "must not render when guard.subject is null (a `*` denial)");
}
{
  const turn = deniedColumnTurn({ subject: "SecretColumnXYZ", turnId: "t_5c" });
  turn.guard.reason = "forbidden_statement";
  const card = createTurnCard(turn, { ...noopCtx, onRequestAccess: async () => ({ already_pending: false }) });
  assert.equal(findButtonByText(card.el, "درخواست دسترسی"), undefined, "must not render for a non-denied_column rejection");
}
console.log("[ok] the button is offered only when wired, denied_column, and a single subject is known");

console.log("ALL_SCENARIOS_PASSED");
