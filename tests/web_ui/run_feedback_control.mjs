// tests/web_ui/run_feedback_control.mjs
//
// Node-side half of tests/web_ui/test_web_ui_feedback_control.py.
//
// Drives the REAL web/js/render/feedback.js source (via a caller-supplied
// path to a byte-identical copy renamed to `.mjs` -- this module has no
// internal `./*.js` imports of its own, so no import-specifier rewriting
// is needed, unlike run_sql_highlight.mjs's turn.js/table.js/chart.js
// chain) under a MINIMAL DOM shim covering only what
// render/feedback.js actually calls: createElement, appendChild,
// className/dataset/textContent as plain properties (never a full text
// node tree -- nothing in feedback.js ever reads rendered text back out
// of the DOM, only writes it), setAttribute/getAttribute,
// addEventListener/a synchronous click() dispatch, and a `<select>` that
// defaults its `.value` to its first `<option>` the way a real browser
// does (feedback.js relies on that default -- it never sets an initial
// selection itself).
//
// Usage: node run_feedback_control.mjs <path-to-copied-feedback.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario below
// passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const feedbackMjsPath = process.argv[2];
if (!feedbackMjsPath) {
  console.error("usage: node run_feedback_control.mjs <path-to-copied-feedback.mjs>");
  process.exit(2);
}

/* ── Minimal DOM shim (see docstring above) ─────────────────────────── */

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this._attrs = new Map();
    this._listeners = {};
    this.dataset = {};
    this.textContent = "";
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.className = "";
    this.type = "";
    const self = this;
    this.classList = {
      add(...names) {
        const set = new Set(self.className.split(/\s+/).filter(Boolean));
        for (const n of names) set.add(n);
        self.className = [...set].join(" ");
      },
    };
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    // Real <select>/<option> default: the first appended option becomes
    // the selected value until something explicitly changes it.
    if (this.tagName === "SELECT" && child.tagName === "OPTION" && this.value === "") {
      this.value = child.value;
    }
    return child;
  }

  setAttribute(name, value) { this._attrs.set(name, String(value)); }
  getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; }

  addEventListener(type, fn) {
    (this._listeners[type] ||= []).push(fn);
  }

  /** Synchronously dispatches every registered "click" listener, awaiting
   * each in turn -- feedback.js's submit handler is async, and the test
   * needs to wait for it to settle before asserting. */
  async click() {
    for (const fn of this._listeners.click || []) await fn();
  }

  querySelectorAll(selector) {
    const wantedTags = selector.split(",").map((s) => s.trim().toUpperCase());
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (wantedTags.includes(child.tagName)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
};

const { renderFeedbackControl } = await import(pathToFileURL(feedbackMjsPath).href);

function find(root, tagName) {
  const out = [];
  const walk = (node) => {
    for (const child of node.children) {
      if (child.tagName === tagName) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

/** True if *el* currently carries CSS class *cls* -- checked as a
 * whitespace-separated membership test (matching real `classList`
 * semantics), not string equality, since `classList.add()` appends
 * rather than replaces (e.g. "feedback-status" becomes "feedback-status
 * ok" on success). */
function hasClass(el, cls) {
  return el.className.split(/\s+/).filter(Boolean).includes(cls);
}

// ---------------------------------------------------------------------
// Scenario 1: initial render -- form hidden, dataset carries the turn id.
// ---------------------------------------------------------------------
{
  const root = renderFeedbackControl({ turn_id: "t_1" }, async () => {});
  assert.equal(root.dataset.turnId, "t_1");
  const toggle = find(root, "BUTTON")[0];
  const [form] = find(root, "DIV").filter((d) => hasClass(d, "feedback-form"));
  assert.ok(form, "expected a .feedback-form container");
  assert.equal(form.hidden, true, "the form must start collapsed -- one low-key control, not a competing form");
  await toggle.click();
  assert.equal(form.hidden, false, "clicking the toggle must reveal the form");
  console.log("[ok] starts collapsed; toggle reveals the form");
}

// ---------------------------------------------------------------------
// Scenario 2: submitting calls onFlag with the selected category and the
// TRIMMED note, and shows a success status on resolution.
// ---------------------------------------------------------------------
{
  const calls = [];
  const root = renderFeedbackControl({ turn_id: "t_2" }, async (category, note) => {
    calls.push({ category, note });
  });
  const select = find(root, "SELECT")[0];
  const note = find(root, "TEXTAREA")[0];
  const submit = find(root, "BUTTON").find((b) => hasClass(b, "feedback-submit"));

  assert.equal(select.value, "wrong_number", "the first category must be selected by default");
  select.value = "wrong_filter_or_period";
  note.value = "  فیلتر بازهٔ زمانی درست نبود  ";

  await submit.click();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].category, "wrong_filter_or_period");
  assert.equal(calls[0].note, "فیلتر بازهٔ زمانی درست نبود", "the note must be trimmed before being sent");

  const [status] = find(root, "DIV").filter((d) => hasClass(d, "feedback-status"));
  assert.ok(status.textContent.length > 0, "a success message must be shown");
  assert.equal(submit.disabled, true, "controls must be disabled once a flag is recorded -- one submission per turn");
  console.log("[ok] submit sends the selected category and trimmed note; shows success and disables the form");
}

// ---------------------------------------------------------------------
// Scenario 3: a rejected onFlag (e.g. "already flagged", a network error)
// shows the failure message and leaves the control usable again.
// ---------------------------------------------------------------------
{
  const root = renderFeedbackControl({ turn_id: "t_3" }, async () => {
    throw new Error("already flagged");
  });
  const submit = find(root, "BUTTON").find((b) => hasClass(b, "feedback-submit"));

  await submit.click();

  const [status] = find(root, "DIV").filter((d) => hasClass(d, "feedback-status"));
  assert.equal(status.textContent, "already flagged");
  assert.equal(submit.disabled, false, "a failed submission must be retryable, not permanently disabled");
  console.log("[ok] a rejected onFlag shows its message and leaves the control usable again");
}

console.log("ALL_SCENARIOS_PASSED");
