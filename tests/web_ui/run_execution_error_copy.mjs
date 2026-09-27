// tests/web_ui/run_execution_error_copy.mjs
//
// Node-side half of tests/web_ui/test_web_ui_execution_error_copy.py.
//
// Drives the REAL web/js/render/turn.js (and its full real render/
// dependency chain -- same staging as run_failure_sentences.mjs) to prove
// the QUERY_EXECUTION_ERROR banner never shows raw English under its
// Persian lead:
//
// (a) the backend's generic fallback message (DB_GENERIC_REJECTION in
//     turn.js, `_GENERIC_STATEMENT_MESSAGE` in database/errors.py) renders
//     no detail line at all -- the lead sentence already says the database
//     rejected the query, so repeating "The database rejected the query."
//     as English detail would say nothing new;
// (b) a database-specific message (e.g. "Invalid column name 'X'. (207)")
//     renders as a labelled technical detail: the exact Persian label,
//     then the message inside a dir="ltr" element, so the embedded English
//     does not scramble inside the right-to-left page;
// (c) an HTML-injection attempt as the message renders as inert text, not
//     markup -- it must never reach innerHTML/outerHTML/insertAdjacentHTML
//     anywhere in the rendered tree, and must never cause an <img> (or any
//     other) element to be constructed from it;
// (d) DATABASE_UNAVAILABLE and QUERY_TIMEOUT -- codes with their own
//     English messages that never touch this detail-line logic at all --
//     still show none of their backend English, confirming this task did
//     not accidentally widen the leak to codes it was not supposed to
//     touch.
//
// The expected Persian label (EXPECTED_LABEL, below) is written as a JS
// string literal built from \u escapes -- not copied as literal Persian
// text -- so the expectation is independent of turn.js's own literal
// Persian text (and of this file's own encoding of it): this test would
// fail if turn.js's label were ever silently changed, rather than
// trivially agreeing with whatever turn.js happens to say. Other fixture
// strings in this file (e.g. the sample question text) are ordinary
// literal Persian, same as the rest of this test suite.
//
// Usage: node run_execution_error_copy.mjs <path-to-copied-turn.mjs>
//
// Exits 0 and prints "ALL_EXECUTION_ERROR_COPY_TESTS_PASSED" iff every
// scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const turnMjsPath = process.argv[2];
if (!turnMjsPath) {
  console.error("usage: node run_execution_error_copy.mjs <path-to-copied-turn.mjs>");
  process.exit(2);
}

/* ── Minimal DOM shim (same shape as run_failure_sentences.mjs's), plus
 * two extra pieces of bookkeeping used only by test (c) below:
 *
 * - `createdTags` records every tag name ever passed to
 *   `document.createElement`, across the WHOLE rendered card (not just the
 *   detail line) -- test (c) asserts "img" never appears in it, i.e. the
 *   injected string never caused a new element to be constructed from it
 *   anywhere in the tree.
 * - `htmlSinkWrites` records every value ever assigned to `innerHTML` /
 *   `outerHTML`, or passed to `insertAdjacentHTML`, again across the WHOLE
 *   tree. This deliberately does NOT throw on those writes: llm-status.js
 *   and sql-display.js's syntax highlighter use `innerHTML` internally for
 *   their OWN static/trusted markup elsewhere in the same card (the
 *   collapsed "جزئیات" drawer renders regardless of the error), so a
 *   blanket throw would fail this test for reasons that have nothing to do
 *   with the vulnerability being checked. Instead, test (c) asserts the
 *   ATTACKER STRING never appears in any recorded write -- proving the
 *   injected message never reached an HTML-parsing sink anywhere, which is
 *   the property that actually matters. ────────────────────────────────── */

const createdTags = [];
const htmlSinkWrites = [];

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
    this.dir = null;
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
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) { this.childNodes = [new FakeTextNode(v)]; }
  // Recorded, never thrown on -- see the block comment above this class.
  set innerHTML(v) { htmlSinkWrites.push({ sink: "innerHTML", tag: this.tagName, value: String(v) }); }
  get innerHTML() { return ""; }
  set outerHTML(v) { htmlSinkWrites.push({ sink: "outerHTML", tag: this.tagName, value: String(v) }); }
  get outerHTML() { return ""; }
  insertAdjacentHTML(position, html) {
    htmlSinkWrites.push({ sink: "insertAdjacentHTML", tag: this.tagName, value: String(html) });
  }
}

function matches(node, sel) {
  if (sel.startsWith(".")) return node.classList.contains(sel.slice(1));
  if (sel.includes(".")) {
    const [tag, cls] = sel.split(".");
    return (!tag || node.tagName === tag.toUpperCase()) && node.classList.contains(cls);
  }
  return node.tagName === sel.toUpperCase();
}

globalThis.window = globalThis;
globalThis.document = {
  createElement: (t) => { createdTags.push(String(t).toUpperCase()); return new FakeElement(t); },
  createElementNS: (_ns, t) => { createdTags.push(String(t).toUpperCase()); return new FakeElement(t); },
  createTextNode: (t) => new FakeTextNode(t),
  body: new FakeElement("body"),
};
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async () => {} } },
  configurable: true,
  writable: true,
});

/* ── Load the real module under test ────────────────────────────────── */

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

function baseErrorTurn(overrides) {
  return {
    turn_id: "t_err", session_id: "s_test", index: 1,
    question: "چند مورد نمونه چیست؟", resolved_question: null,
    basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
    ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
    sql: null, sql_display: null,
    guard: null,
    result: null,
    interpretation: null, tier: null, warnings: [], llm: llm(), timings: {},
    error: null,
    ...overrides,
  };
}

function fullCtx() {
  return {
    onJumpToTurn() {}, onEditAssumption() {}, onClarify() {}, onPin() {},
    onRerun() {},
    onRephrase() {},
    onAskWithoutColumn() {},
  };
}

const ctx = fullCtx();

// Messages copied verbatim from database/errors.py -- see that module's
// DATABASE_UNAVAILABLE_MESSAGE, QUERY_TIMEOUT_MESSAGE and
// _GENERIC_STATEMENT_MESSAGE. tests/test_failure_copy_parity.py separately
// enforces that turn.js's own DB_GENERIC_REJECTION constant matches the
// RUNTIME value of _GENERIC_STATEMENT_MESSAGE; this file's copy below is
// only a fixture input, never the source of truth for that comparison.
const GENERIC_REJECTION = "The database rejected the query.";
const DB_UNAVAILABLE_MSG = "The database is currently unavailable. Please try again shortly.";
const QUERY_TIMEOUT_MSG = "The query took too long to run and was cancelled. Please try again or narrow your request.";

// The Persian label turn.js must render before a database-specific
// message, built from \u escapes (not literal Persian text) so this
// expectation does not depend on turn.js's own literal Persian text, or
// on this file's own encoding of it. Decodes to "Database message:"
// (payam-e paygah-e dadeh:).
const EXPECTED_LABEL =
  "\u067e\u06cc\u0627\u0645\u0020\u067e\u0627\u06cc\u06af\u0627\u0647\u0020\u062f\u0627\u062f\u0647\u003a";
assert.equal(EXPECTED_LABEL.length, 17, "sanity check: the expected label must be 17 code points");

/* ── Test (a): the generic fallback message suppresses the detail line
 * entirely -- the lead sentence already says the database rejected the
 * query, so there is nothing left for the fallback to add. ────────────── */

{
  const turn = baseErrorTurn({
    error: { code: "QUERY_EXECUTION_ERROR", message: GENERIC_REJECTION, request_id: "req_1" },
  });
  const card = createTurnCard(turn, ctx);

  const leadEl = card.el.querySelector(".failure-lead");
  assert.ok(leadEl, "test (a): expected a .failure-lead element");
  assert.equal(
    leadEl.textContent,
    "پرس‌وجو اجرا شد، اما پایگاه داده آن را با خطا رد کرد.",
    "test (a): lead sentence must be the Persian sentence",
  );
  assert.ok(
    !card.el.textContent.includes(GENERIC_REJECTION),
    "test (a): the generic rejection message must NOT appear anywhere on the card",
  );
  assert.equal(
    card.el.querySelector(".failure-detail"),
    null,
    "test (a): no detail line should appear when message is the generic rejection",
  );
  console.log("[ok] test (a): QUERY_EXECUTION_ERROR with generic message - no English leak, no detail line");
}

/* ── Test (b): a database-specific message renders as a labelled,
 * dir="ltr" technical detail. ──────────────────────────────────────────── */

{
  const specificMessage = "Invalid column name 'X'. (207)";
  const turn = baseErrorTurn({
    error: { code: "QUERY_EXECUTION_ERROR", message: specificMessage, request_id: "req_2" },
  });
  const card = createTurnCard(turn, ctx);

  const leadEl = card.el.querySelector(".failure-lead");
  assert.equal(
    leadEl.textContent,
    "پرس‌وجو اجرا شد، اما پایگاه داده آن را با خطا رد کرد.",
    "test (b): lead sentence must be preserved",
  );

  const detailEl = card.el.querySelector(".failure-detail");
  assert.ok(detailEl, "test (b): expected a .failure-detail element");

  const labelEl = detailEl.querySelector(".failure-detail-label");
  assert.ok(labelEl, "test (b): expected a .failure-detail-label element");
  const labelText = labelEl.textContent.trim();
  assert.equal(labelText, EXPECTED_LABEL, "test (b): label must be exactly the expected Persian label");
  assert.ok(!/[A-Za-z]/.test(labelText), "test (b): label must contain no ASCII letters");

  const textEl = detailEl.querySelector("bdi.failure-detail-text");
  assert.ok(textEl, "test (b): expected a <bdi> element with class failure-detail-text");
  assert.equal(textEl.textContent, specificMessage, "test (b): message text must be inside the detail text element");
  assert.equal(textEl.dir, "ltr", "test (b): the detail text element must have dir=\"ltr\"");

  console.log("[ok] test (b): database-specific message renders with the Persian label and dir=\"ltr\"");
}

/* ── Test (c): an HTML-injection attempt renders as inert text, never as
 * markup. See the DOM-shim block comment above for what "meaningful" means
 * here and why it does not simply throw on every innerHTML write. ─────── */

{
  const injectMessage = "<img src=x onerror=alert(1)>";
  const turn = baseErrorTurn({
    error: { code: "QUERY_EXECUTION_ERROR", message: injectMessage, request_id: "req_3" },
  });
  const createdTagsBefore = createdTags.length;
  const htmlSinkWritesBefore = htmlSinkWrites.length;

  const card = createTurnCard(turn, ctx);

  const detailEl = card.el.querySelector(".failure-detail");
  assert.ok(detailEl, "test (c): expected a .failure-detail element");

  const textEl = detailEl.querySelector("bdi.failure-detail-text");
  assert.ok(textEl, "test (c): expected a <bdi> element with class failure-detail-text");
  assert.equal(textEl.textContent, injectMessage, "test (c): the injection attempt must render as literal text");
  assert.equal(textEl.dir, "ltr", "test (c): the detail text element must have dir=\"ltr\"");

  // No element anywhere in the whole render (not just the detail line) was
  // ever constructed with tag "img" -- the string never got interpreted as
  // markup, only ever handled as text.
  const newlyCreatedTags = createdTags.slice(createdTagsBefore);
  assert.ok(
    !newlyCreatedTags.includes("IMG"),
    `test (c): no <img> element may be constructed while rendering this card, got tags: ${newlyCreatedTags.join(", ")}`,
  );
  const imgElements = card.el.querySelectorAll("img");
  assert.equal(imgElements.length, 0, "test (c): the card must contain zero <img> elements");

  // The attacker string never reached innerHTML/outerHTML/insertAdjacentHTML
  // anywhere in the tree (see the DOM-shim comment above: those sinks are
  // legitimately used elsewhere in the SAME card for unrelated, trusted,
  // static markup, so this checks the payload specifically rather than
  // forbidding the sinks outright).
  const newSinkWrites = htmlSinkWrites.slice(htmlSinkWritesBefore);
  const leaked = newSinkWrites.filter((w) => w.value.includes(injectMessage));
  assert.equal(
    leaked.length,
    0,
    `test (c): the injected message must never reach innerHTML/outerHTML/insertAdjacentHTML, but found: ${JSON.stringify(leaked)}`,
  );

  console.log("[ok] test (c): HTML-injection attempt rendered as inert text -- no <img>, no HTML-sink write");
}

/* ── Test (d): codes with their own English messages, untouched by this
 * task's detail-line logic, still show none of their backend English. ── */

{
  const turn = baseErrorTurn({
    error: { code: "DATABASE_UNAVAILABLE", message: DB_UNAVAILABLE_MSG, request_id: "req_4" },
  });
  const card = createTurnCard(turn, ctx);
  assert.ok(
    !card.el.textContent.includes(DB_UNAVAILABLE_MSG),
    "test (d): DATABASE_UNAVAILABLE's English message must not appear anywhere on the card",
  );
  console.log("[ok] test (d.1): DATABASE_UNAVAILABLE message not leaked");
}

{
  const turn = baseErrorTurn({
    error: { code: "QUERY_TIMEOUT", message: QUERY_TIMEOUT_MSG, request_id: "req_5" },
  });
  const card = createTurnCard(turn, ctx);
  assert.ok(
    !card.el.textContent.includes(QUERY_TIMEOUT_MSG),
    "test (d): QUERY_TIMEOUT's English message must not appear anywhere on the card",
  );
  console.log("[ok] test (d.2): QUERY_TIMEOUT message not leaked");
}

console.log("ALL_EXECUTION_ERROR_COPY_TESTS_PASSED");
