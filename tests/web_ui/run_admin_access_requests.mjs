// tests/web_ui/run_admin_access_requests.mjs
//
// Node-side half of tests/web_ui/test_web_ui_admin_access_requests.py.
//
// Drives the REAL web/admin/access-requests.js source under a MINIMAL DOM
// shim (mirrors tests/web_ui/run_feedback_control.mjs's shim -- uppercase
// tagName, tag-name-only querySelectorAll, plain dataset/classList/
// textContent properties) covering only what this module actually calls.
//
// Proves:
// * an empty list shows the "no requests" message, no per-row markup;
// * a non-empty list renders one row per request with requester id,
//   column name, status pill, the joined question, and created time;
// * an "open" row gets Approve/Deny controls; approved/denied rows do not;
// * a "denied" row with a resolution_note shows the reason line;
// * clicking Approve calls onApprove(request_id, btn); clicking Deny
//   calls onDeny(request_id, <reason input value>, btn);
// * an XSS payload in `column_name` AND in the joined `audit.question`
//   renders as TEXT everywhere -- no <img>/<script> element is ever
//   created anywhere in the rendered tree (the admin panel's own stored-
//   XSS class, fixed in 4.12.1 -- this module must never reintroduce it).
//
// Usage: node run_admin_access_requests.mjs <path-to-copied-access-requests.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const moduleMjsPath = process.argv[2];
if (!moduleMjsPath) {
  console.error("usage: node run_admin_access_requests.mjs <path-to-copied-access-requests.mjs>");
  process.exit(2);
}

/* ── Minimal DOM shim (see docstring above). ────────────────────────── */

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this._attrs = new Map();
    this._listeners = {};
    this.dataset = {};
    this._text = "";
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.className = "";
    this.type = "";
    this.style = {};
    const self = this;
    this.classList = {
      add(...names) {
        const set = new Set(self.className.split(/\s+/).filter(Boolean));
        for (const n of names) set.add(n);
        self.className = [...set].join(" ");
      },
      contains(name) {
        return self.className.split(/\s+/).filter(Boolean).includes(name);
      },
    };
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  set innerHTML(value) {
    // Only ever set to "" (clearing) by this module -- anything else is a
    // regression toward string-built markup, which this module's whole
    // point is to avoid. Enforced here rather than merely documented.
    if (value !== "") {
      throw new Error(
        `access-requests.js must never assign non-empty innerHTML (got: ${JSON.stringify(value)}) -- ` +
        "every element must be built with createElement/textContent",
      );
    }
    this.children = [];
  }

  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((c) => c.textContent).join("");
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  setAttribute(name, value) { this._attrs.set(name, String(value)); }
  getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; }

  addEventListener(type, fn) {
    (this._listeners[type] ||= []).push(fn);
  }
  click() {
    for (const fn of this._listeners.click || []) fn();
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

class FakeTextNode {
  constructor(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text; }
}

globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeTextNode(text),
};

const { renderAccessRequestsList } = await import(pathToFileURL(moduleMjsPath).href);

function findAll(root, tag) {
  return root.querySelectorAll(tag);
}

/* ── Scenario 1: empty list -- the "no requests" message, no rows. ─────── */
{
  const host = new FakeElement("div");
  renderAccessRequestsList(host, [], { onApprove() {}, onDeny() {} });
  assert.ok(host.textContent.includes("درخواستی یافت نشد"), "expected the empty-state message");
  assert.equal(findAll(host, "BUTTON").length, 0, "an empty list must render no buttons");
  console.log("[ok] empty list shows the no-requests message");
}

/* ── Scenario 2: a full row renders requester, column, status, question,
 * created time, and (only for an OPEN row) Approve/Deny controls. ─────── */
{
  const host = new FakeElement("div");
  const rows = [
    {
      request_id: 7, requester_principal_id: "analyst-1", column_name: "NationalID",
      status: "open", created_at: "2026-09-22T00:00:00Z", resolution_note: null,
      audit: { question: "چند مشتری فعال داریم؟" },
    },
  ];
  renderAccessRequestsList(host, rows, { onApprove() {}, onDeny() {} });

  assert.ok(host.textContent.includes("analyst-1"), "expected the requester id");
  assert.ok(host.textContent.includes("NationalID"), "expected the column name");
  assert.ok(host.textContent.includes("چند مشتری فعال داریم؟"), "expected the joined question");
  assert.ok(host.textContent.includes("2026-09-22T00:00:00Z"), "expected the created time");
  assert.ok(host.textContent.includes("باز"), "expected the open-status label");

  const buttons = findAll(host, "BUTTON");
  assert.equal(buttons.length, 2, "an open row must render exactly Approve and Deny");
  const approveBtn = buttons.find((b) => b.textContent === "تأیید");
  const denyBtn = buttons.find((b) => b.textContent === "رد کردن");
  assert.ok(approveBtn, "expected an approve button");
  assert.ok(denyBtn, "expected a deny button");

  console.log("[ok] an open row renders requester/column/question/created time and both action buttons");
}

/* ── Scenario 3: approved/denied rows render no action controls; a denied
 * row's reason is shown. ────────────────────────────────────────────── */
{
  const host = new FakeElement("div");
  const rows = [
    {
      request_id: 8, requester_principal_id: "analyst-2", column_name: "Phone",
      status: "approved", created_at: "2026-09-22T00:00:00Z", resolution_note: null,
      audit: { question: "q1" },
    },
    {
      request_id: 9, requester_principal_id: "analyst-3", column_name: "Salary",
      status: "denied", created_at: "2026-09-22T00:00:00Z",
      resolution_note: "Column is under legal hold.",
      audit: { question: "q2" },
    },
  ];
  renderAccessRequestsList(host, rows, { onApprove() {}, onDeny() {} });

  assert.equal(findAll(host, "BUTTON").length, 0, "resolved rows must render no action buttons");
  assert.ok(host.textContent.includes("Column is under legal hold."), "expected the denial reason to be shown");
  assert.ok(host.textContent.includes("تأیید شده"), "expected the approved-status label");
  assert.ok(host.textContent.includes("رد شده"), "expected the denied-status label");

  console.log("[ok] resolved rows render no action controls; a denial reason is shown");
}

/* ── Scenario 4: clicking Approve/Deny calls the right handler with the
 * right arguments. ─────────────────────────────────────────────────── */
{
  const host = new FakeElement("div");
  const rows = [
    {
      request_id: 42, requester_principal_id: "analyst-1", column_name: "NationalID",
      status: "open", created_at: "t", resolution_note: null, audit: null,
    },
  ];
  const approveCalls = [];
  const denyCalls = [];
  renderAccessRequestsList(host, rows, {
    onApprove: (id, btn) => approveCalls.push({ id, btn }),
    onDeny: (id, reason, btn) => denyCalls.push({ id, reason, btn }),
  });

  const buttons = findAll(host, "BUTTON");
  const inputs = findAll(host, "INPUT");
  const approveBtn = buttons.find((b) => b.textContent === "تأیید");
  const denyBtn = buttons.find((b) => b.textContent === "رد کردن");
  const reasonInput = inputs[0];

  approveBtn.click();
  assert.equal(approveCalls.length, 1);
  assert.equal(approveCalls[0].id, 42);

  reasonInput.value = "Not now.";
  denyBtn.click();
  assert.equal(denyCalls.length, 1);
  assert.equal(denyCalls[0].id, 42);
  assert.equal(denyCalls[0].reason, "Not now.");

  console.log("[ok] Approve/Deny call the right handler with (request_id[, reason], button)");
}

/* ── Scenario 5: an unaudited row (audit: null) falls back honestly, no
 * question line asserting something that was never retrieved. ────────── */
{
  const host = new FakeElement("div");
  const rows = [
    {
      request_id: 10, requester_principal_id: "analyst-1", column_name: "Phone",
      status: "open", created_at: "t", resolution_note: null, audit: null,
    },
  ];
  renderAccessRequestsList(host, rows, { onApprove() {}, onDeny() {} });
  assert.ok(
    host.textContent.includes("دیگر قابل بازیابی از گزارش ممیزی نیست"),
    "expected the honest 'no longer retrievable' fallback for a null audit join",
  );
  console.log("[ok] a null audit join falls back to the honest 'not retrievable' message");
}

/* ── Scenario 6: XSS payloads in column_name AND the joined question --
 * render as TEXT everywhere, no <img>/<script> element is ever created. ── */
{
  const host = new FakeElement("div");
  const XSS_COLUMN = "<img src=x onerror=alert(1)>";
  const XSS_QUESTION = "<script>alert(1)</script>";
  const XSS_REASON = "<img src=y onerror=alert(2)>";
  const rows = [
    {
      request_id: 11, requester_principal_id: "analyst-1", column_name: XSS_COLUMN,
      status: "denied", created_at: "t", resolution_note: XSS_REASON,
      audit: { question: XSS_QUESTION },
    },
  ];
  renderAccessRequestsList(host, rows, { onApprove() {}, onDeny() {} });

  assert.ok(host.textContent.includes(XSS_COLUMN), "the column payload must appear as literal text");
  assert.ok(host.textContent.includes(XSS_QUESTION), "the question payload must appear as literal text");
  assert.ok(host.textContent.includes(XSS_REASON), "the reason payload must appear as literal text");
  assert.equal(findAll(host, "IMG").length, 0, "no <img> element must ever be created from any of these fields");
  assert.equal(findAll(host, "SCRIPT").length, 0, "no <script> element must ever be created from any of these fields");

  console.log("[ok] XSS payloads in column_name, the joined question, and a denial reason all render as text -- no live element is created");
}

console.log("ALL_SCENARIOS_PASSED");
