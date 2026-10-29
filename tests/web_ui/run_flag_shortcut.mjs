// tests/web_ui/run_flag_shortcut.mjs
//
// Node-side half of tests/web_ui/test_web_ui_flag_shortcut.py.
//
// Drives the REAL turn.js (and its real render/ dependency chain, staged
// like run_confirmation_placement.mjs) with the real confirmation.js, wired
// the way web/js/main.js wires them, to prove the «پاسخ نادرست است» button
// (#223):
//
// * is on the answer, in the result footer beside «پاسخ درست است», outside
//   the closed drawer, exactly once;
// * sends nothing itself: pressing it opens the drawer and the flag form
//   and puts focus on the category list;
// * reports a sent flag with a check mark, and is cleared again when a
//   later confirmation replaces the flag;
// * is absent where no flag can be sent, and present (alone in the footer)
//   when the page wires a flag but no confirmation.
//
// Usage: node run_flag_shortcut.mjs <copied-turn.mjs> <copied-confirmation.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [turnMjsPath, confirmationMjsPath] = process.argv.slice(2);
if (!turnMjsPath || !confirmationMjsPath) {
  console.error("usage: node run_flag_shortcut.mjs <turn.mjs> <confirmation.mjs>");
  process.exit(2);
}
const dir = (name) => turnMjsPath.replace(/turn\.mjs$/, name);

/* ── Minimal DOM shim -- copied from run_confirmation_placement.mjs (keep
 * in step with it), plus focus(). ───────────────────────────── */

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
    this.value = "";
    this.title = "";
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
  focus() { globalThis.document.activeElement = this; }
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
    if (selector.includes(",")) {
      const seen = new Set();
      for (const part of selector.split(",")) for (const el of this.querySelectorAll(part)) seen.add(el);
      return [...seen];
    }
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
const { canConfirm, renderConfirmationControl } = await import(pathToFileURL(confirmationMjsPath).href);
await import(pathToFileURL(dir("sql-display.mjs")).href);

globalThis.window.Prism = { languages: { sql: {} }, highlight: (text) => text };

function llm() {
  return {
    backend: "ollama", model: "local", endpoint_status: 200, attempts: 1,
    finish_reason: "stop", structured_output: true, prompt_tokens: 10,
    completion_tokens: 5, prefill_ms: 1, decode_ms: 1, total_ms: 2,
    tokens_per_second: 1, prefix_cache_hit: true, temperature: 0, seed: 1,
    corrections: 0,
  };
}

const BASE = {
  session_id: "s_test", index: 1, question: "تعداد مشتریان؟", resolved_question: null,
  basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
  ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
  interpretation: null, tier: "T2", warnings: [], llm: llm(), timings: {}, error: null,
};

function successTurn(turnId = "t_ok") {
  return {
    ...BASE, turn_id: turnId, sql: "SELECT 1 AS Total", sql_display: null,
    guard: { verdict: "allowed", rule: null, injected_top: null, tables_touched: [] },
    result: { columns: [{ name: "Total", type: "number" }], rows: [{ Total: 7 }], row_count: 1, truncated: false },
  };
}

function outOfScopeTurn() {
  return {
    ...BASE, turn_id: "t_oos", sql: null, sql_display: null, guard: null, result: null,
    error: { code: "OUT_OF_SCOPE", message: "این پرسش خارج از دامنه است.", request_id: "r_1" },
  };
}

function guardRejectedTurn() {
  return {
    ...BASE, turn_id: "t_rej", sql: "SELECT 1", sql_display: null,
    guard: {
      verdict: "rejected", rule: "Forbidden keyword", reason: "forbidden_keyword",
      subject: "DROP", rejected_sql: "DROP TABLE x", injected_top: null, tables_touched: [],
    },
    result: { columns: [], rows: [], row_count: 0, truncated: false },
  };
}

function failedExecutionTurn() {
  return {
    ...successTurn("t_exec"), result: null,
    error: { code: "QUERY_EXECUTION_ERROR", message: "خطا", request_id: "r_2" },
  };
}

/** The page's wiring, as web/js/main.js's turnCtx does it. */
function makeCtx(log, { withConfirmation = true, withFlag = true } = {}) {
  const confirmed = new Set();
  const ctx = {};
  if (withFlag) ctx.onFlag = async (turnId, category, note) => { log.push(["flag", turnId, category]); };
  if (withConfirmation) {
    ctx.renderConfirmation = (turn, { onReplacedFlag }) => {
      if (!canConfirm(turn)) return null;
      return renderConfirmationControl(turn, {
        confirmed: confirmed.has(turn.turn_id),
        onConfirm: async () => { log.push(["confirm", turn.turn_id]); confirmed.add(turn.turn_id); return { removed_flags: ctx.removedFlags ?? 0 }; },
        onWithdraw: async () => { log.push(["withdraw", turn.turn_id]); confirmed.delete(turn.turn_id); },
        onConfirmed: (result) => {
          const replaced = Boolean(result && result.removed_flags > 0);
          if (replaced) onReplacedFlag();
          return replaced;
        },
      });
    };
  }
  return ctx;
}

const all = (root, selector) => root.querySelectorAll(selector);
const drawerOf = (card) => all(card.el, "details")[0];
async function press(el) { for (const h of el._listeners.click || []) await h({ preventDefault() {} }); }

// ---------------------------------------------------------------------
// Scenario 1: on the answer, beside the confirmation, outside the drawer.
// ---------------------------------------------------------------------
{
  const card = createTurnCard(successTurn(), makeCtx([]));
  const shortcuts = all(card.el, "button.flag-shortcut");
  assert.equal(shortcuts.length, 1, "exactly one «پاسخ نادرست است» button per card");
  const shortcut = shortcuts[0];
  assert.equal(shortcut.textContent, "پاسخ نادرست است");
  assert.equal(shortcut.type, "button");
  const footer = shortcut.closest(".result-footer");
  assert.ok(footer, "in the answer's footer row");
  assert.equal(footer.querySelector("button.confirm-toggle") !== null, true, "beside «پاسخ درست است»");
  const kids = footer.children.map((c) => c.className);
  assert.ok(kids.indexOf("confirmation-control") < kids.indexOf("flag-shortcut"), `the confirmation comes first: ${kids}`);
  assert.equal(drawerOf(card).querySelector("button.flag-shortcut"), null, "NOT inside the drawer");
  assert.equal(all(card.el, "button.feedback-toggle").length, 1, "the drawer's own flag control is still there, once");
  console.log("[ok] one button, in the result footer after the confirmation, outside the drawer");
}

// ---------------------------------------------------------------------
// Scenario 2: pressing it opens the form and sends nothing.
// ---------------------------------------------------------------------
{
  const log = [];
  const card = createTurnCard(successTurn(), makeCtx(log));
  const drawer = drawerOf(card);
  const form = drawer.querySelector(".feedback-form");
  const flagToggle = drawer.querySelector("button.feedback-toggle");
  assert.equal(drawer.open ?? false, false, "precondition: the drawer is closed");
  assert.equal(form.hidden, true, "precondition: the form is collapsed");
  globalThis.document.activeElement = null;

  await press(all(card.el, "button.flag-shortcut")[0]);
  assert.equal(drawer.open, true, "the drawer is opened");
  assert.equal(form.hidden, false, "the flag form is expanded");
  assert.equal(flagToggle.getAttribute("aria-expanded"), "true");
  assert.equal(globalThis.document.activeElement, drawer.querySelector("select.feedback-category"), "focus is on the category list");
  assert.deepEqual(log, [], "nothing is sent by the button itself");

  // Pressing it again leaves the form open (it opens, it does not toggle).
  await press(all(card.el, "button.flag-shortcut")[0]);
  assert.equal(form.hidden, false);
  console.log("[ok] pressing it opens the drawer and the form, focuses the category, sends nothing");
}

// ---------------------------------------------------------------------
// Scenario 3: a sent flag shows on the button; a replacing confirmation clears it.
// ---------------------------------------------------------------------
{
  const log = [];
  const ctx = makeCtx(log);
  const card = createTurnCard(successTurn(), ctx);
  const shortcut = all(card.el, "button.flag-shortcut")[0];
  const toggle = all(card.el, "button.confirm-toggle")[0];
  const drawer = drawerOf(card);

  await press(shortcut);
  await press(drawer.querySelector("button.feedback-submit"));
  assert.deepEqual(log.at(-1).slice(0, 2), ["flag", "t_ok"], "the report is sent from the form");
  assert.equal(shortcut.textContent, "✓ گزارش شد", "the button says the report was sent (text, not colour alone)");
  assert.ok(shortcut.classList.contains("is-flagged"));
  assert.equal(toggle.getAttribute("aria-pressed"), "false", "the confirmation is still reset by a flag");

  // After a report, the form is disabled; pressing the button focuses its toggle.
  globalThis.document.activeElement = null;
  await press(shortcut);
  assert.equal(globalThis.document.activeElement, drawer.querySelector("button.feedback-toggle"));

  ctx.removedFlags = 1;
  await press(toggle);
  assert.equal(toggle.getAttribute("aria-pressed"), "true");
  assert.equal(shortcut.textContent, "پاسخ نادرست است", "a confirmation that replaced the report clears the button");
  assert.equal(shortcut.classList.contains("is-flagged"), false);
  assert.equal(drawer.querySelector("button.feedback-submit").disabled, false, "and reopens the flag form, as before");
  console.log("[ok] a sent flag is shown on the button and cleared by a replacing confirmation");
}

// ---------------------------------------------------------------------
// Scenario 4: absent where no flag can be sent; alone when only a flag is wired.
// ---------------------------------------------------------------------
{
  for (const [name, turn] of [
    ["out-of-scope", outOfScopeTurn()],
    ["guard-rejected", guardRejectedTurn()],
    ["execution error", failedExecutionTurn()],
  ]) {
    const card = createTurnCard(turn, makeCtx([]));
    assert.equal(all(card.el, "button.flag-shortcut").length, 0, `no button on a ${name} turn`);
    assert.equal(all(card.el, ".result-footer").length, 0, `no footer on a ${name} turn`);
  }
  const noFlag = createTurnCard(successTurn(), makeCtx([], { withFlag: false }));
  assert.equal(all(noFlag.el, "button.flag-shortcut").length, 0, "no button when the page wires no flag");
  assert.equal(all(noFlag.el, "button.confirm-toggle").length, 1, "the confirmation is unaffected");

  const flagOnly = createTurnCard(successTurn(), makeCtx([], { withConfirmation: false }));
  assert.equal(all(flagOnly.el, "button.confirm-toggle").length, 0);
  const footers = all(flagOnly.el, ".result-footer");
  assert.equal(footers.length, 1, "a footer for the button alone");
  assert.equal(footers[0].querySelectorAll("button.flag-shortcut").length, 1);

  const bare = createTurnCard(successTurn(), makeCtx([], { withConfirmation: false, withFlag: false }));
  assert.equal(all(bare.el, ".result-footer").length, 0, "no footer when the page wires neither");
  console.log("[ok] absent on failed turns and when unwired; alone in the footer when only a flag is wired");
}

console.log("ALL_SCENARIOS_PASSED");
