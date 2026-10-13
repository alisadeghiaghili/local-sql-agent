// tests/web_ui/run_confirmation_control.mjs
//
// Node-side half of tests/web_ui/test_web_ui_confirmation_control.py.
//
// Drives the REAL web/js/render/confirmation.js (and, for the "later action
// wins" scenario, the real web/js/render/feedback.js) via caller-supplied
// byte-identical copies renamed to `.mjs`, under a MINIMAL DOM shim covering
// only what those modules call: createElement, appendChild, className /
// dataset / textContent as plain properties, setAttribute / getAttribute,
// addEventListener and a synchronous click() dispatch that awaits each
// (async) listener. Same approach as run_feedback_control.mjs.
//
// Usage: node run_confirmation_control.mjs <confirmation.mjs> <feedback.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [confirmationPath, feedbackPath] = process.argv.slice(2);
if (!confirmationPath || !feedbackPath) {
  console.error("usage: node run_confirmation_control.mjs <confirmation.mjs> <feedback.mjs>");
  process.exit(2);
}

/* ── Minimal DOM shim ───────────────────────────────────────────────── */

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
    if (this.tagName === "SELECT" && child.tagName === "OPTION" && this.value === "") {
      this.value = child.value;
    }
    return child;
  }

  setAttribute(name, value) { this._attrs.set(name, String(value)); }
  getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; }

  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }

  async click() {
    if (this.disabled) return;
    for (const fn of this._listeners.click || []) await fn();
  }

  querySelectorAll(selector) {
    const wanted = selector.split(",").map((s) => s.trim().toUpperCase());
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (wanted.includes(child.tagName)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

globalThis.document = { createElement: (tag) => new FakeElement(tag) };

const { canConfirm, renderConfirmationControl } = await import(pathToFileURL(confirmationPath).href);
const { renderFeedbackControl } = await import(pathToFileURL(feedbackPath).href);

function find(root, predicate) {
  const out = [];
  const walk = (node) => {
    for (const child of node.children) {
      if (predicate(child)) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}
const hasClass = (el, cls) => el.className.split(/\s+/).filter(Boolean).includes(cls);
const buttonOf = (root) => find(root, (n) => n.tagName === "BUTTON")[0];
const statusOf = (root) => find(root, (n) => hasClass(n, "confirmation-status"))[0];

const LABEL = "پاسخ درست است";

// ---------------------------------------------------------------------
// Scenario 1: only a successful answer can be confirmed.
// ---------------------------------------------------------------------
{
  const ok = { turn_id: "t", sql: "SELECT 1", result: { rows: [] }, error: null, guard: { verdict: "allowed" } };
  assert.equal(canConfirm(ok), true);
  assert.equal(canConfirm({ ...ok, result: null }), false, "no result -> no button");
  assert.equal(canConfirm({ ...ok, error: { code: "OUT_OF_SCOPE" } }), false, "an error -> no button");
  assert.equal(canConfirm({ ...ok, sql: null }), false, "no SQL ran -> no button");
  assert.equal(canConfirm({ ...ok, guard: { verdict: "rejected" } }), false, "guard-rejected -> no button");
  assert.equal(canConfirm({ ...ok, guard: null }), true, "a turn with no guard block is judged on the rest");
  assert.equal(canConfirm(null), false);
  console.log("[ok] the button is offered on a successful answer only");
}

// ---------------------------------------------------------------------
// Scenario 2: initial render -- a real button, Persian label, aria-pressed.
// ---------------------------------------------------------------------
{
  const root = renderConfirmationControl({ turn_id: "t_1" }, { onConfirm: async () => ({}), onWithdraw: async () => ({}) });
  assert.equal(root.dataset.turnId, "t_1");
  const button = buttonOf(root);
  assert.equal(button.tagName, "BUTTON", "keyboard reachable: a real <button>, not a div");
  assert.equal(button.type, "button", "must not submit an enclosing form");
  assert.equal(button.getAttribute("aria-pressed"), "false");
  assert.equal(button.textContent, LABEL);
  const status = statusOf(root);
  assert.equal(status.getAttribute("role"), "status", "failures must be announced");
  console.log("[ok] renders a real button with a Persian label and aria-pressed=false");
}

// ---------------------------------------------------------------------
// Scenario 3: pressing confirms; pressing again withdraws.
// ---------------------------------------------------------------------
{
  const calls = [];
  const root = renderConfirmationControl({ turn_id: "t_2" }, {
    onConfirm: async () => { calls.push("confirm"); return { removed_flags: 0 }; },
    onWithdraw: async () => { calls.push("withdraw"); return {}; },
  });
  const button = buttonOf(root);

  await button.click();
  assert.deepEqual(calls, ["confirm"]);
  assert.equal(button.getAttribute("aria-pressed"), "true");
  assert.ok(button.textContent.includes(LABEL), "the label does not change when pressed");
  assert.ok(button.textContent.includes("✓"), "pressed is shown by a check mark, not colour alone");
  assert.ok(hasClass(button, "is-confirmed"));
  assert.ok(statusOf(root).textContent.length > 0, "a success message is shown");
  assert.equal(button.disabled, false, "the toggle stays usable");

  await button.click();
  assert.deepEqual(calls, ["confirm", "withdraw"]);
  assert.equal(button.getAttribute("aria-pressed"), "false");
  assert.equal(button.textContent, LABEL);
  assert.ok(!hasClass(button, "is-confirmed"));
  console.log("[ok] press confirms, press again withdraws, state follows aria-pressed");
}

// ---------------------------------------------------------------------
// Scenario 4: a turn already confirmed renders pressed.
// ---------------------------------------------------------------------
{
  const root = renderConfirmationControl({ turn_id: "t_3" }, {
    confirmed: true, onConfirm: async () => ({}), onWithdraw: async () => ({}),
  });
  assert.equal(buttonOf(root).getAttribute("aria-pressed"), "true");
  root.setConfirmed(false);
  assert.equal(buttonOf(root).getAttribute("aria-pressed"), "false", "the page can reset it");
  console.log("[ok] an already-confirmed turn renders pressed; setConfirmed resets it");
}

// ---------------------------------------------------------------------
// Scenario 5: a rejected request (a 409, a network error) keeps the old
// state, says why, and leaves the control usable.
// ---------------------------------------------------------------------
{
  const root = renderConfirmationControl({ turn_id: "t_4" }, {
    onConfirm: async () => { throw new Error("only an answer that ran can be confirmed"); },
    onWithdraw: async () => ({}),
  });
  const button = buttonOf(root);
  await button.click();
  assert.equal(button.getAttribute("aria-pressed"), "false", "a failed confirmation must not look confirmed");
  assert.equal(statusOf(root).textContent, "only an answer that ran can be confirmed");
  assert.ok(hasClass(statusOf(root), "error"));
  assert.equal(button.disabled, false, "a failed request is retryable");

  const pressed = renderConfirmationControl({ turn_id: "t_5" }, {
    confirmed: true, onConfirm: async () => ({}), onWithdraw: async () => { throw new Error("network"); },
  });
  await buttonOf(pressed).click();
  assert.equal(buttonOf(pressed).getAttribute("aria-pressed"), "true", "a failed withdrawal keeps it confirmed");
  console.log("[ok] a rejected request keeps the previous state, shows the message and stays usable");
}

// ---------------------------------------------------------------------
// Scenario 6: confirming a turn the analyst had flagged replaces the flag;
// a failing page hook never undoes a stored confirmation.
// ---------------------------------------------------------------------
{
  const root = renderConfirmationControl({ turn_id: "t_6" }, {
    onConfirm: async () => ({ removed_flags: 1 }),
    onWithdraw: async () => ({}),
    onConfirmed: (result) => result.removed_flags > 0,
  });
  await buttonOf(root).click();
  assert.equal(buttonOf(root).getAttribute("aria-pressed"), "true");
  assert.ok(statusOf(root).textContent.includes("جایگزین"), "says the earlier report was replaced");

  const flaky = renderConfirmationControl({ turn_id: "t_7" }, {
    onConfirm: async () => ({}), onWithdraw: async () => ({}),
    onConfirmed: () => { throw new Error("page hook bug"); },
  });
  await buttonOf(flaky).click();
  assert.equal(buttonOf(flaky).getAttribute("aria-pressed"), "true");
  console.log("[ok] confirming reports a replaced flag; a failing hook does not undo the confirmation");
}

// ---------------------------------------------------------------------
// Scenario 7: the flag control's reopen() (used when a confirmation
// replaces its flag) makes a submitted flag usable again.
// ---------------------------------------------------------------------
{
  const feedback = renderFeedbackControl({ turn_id: "t_8" }, async () => {});
  const submit = find(feedback, (n) => n.tagName === "BUTTON" && hasClass(n, "feedback-submit"))[0];
  const toggle = find(feedback, (n) => n.tagName === "BUTTON" && hasClass(n, "feedback-toggle"))[0];
  await submit.click();
  assert.equal(submit.disabled, true, "precondition: the flag was submitted");
  assert.equal(typeof feedback.reopen, "function");
  feedback.reopen();
  assert.equal(submit.disabled, false, "after reopen the flag control is usable again");
  assert.equal(toggle.textContent, "این عدد درست نیست؟");
  console.log("[ok] reopen() makes a submitted flag usable again");
}

console.log("ALL_SCENARIOS_PASSED");
