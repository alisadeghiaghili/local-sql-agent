// tests/web_ui/run_failure_sentences.mjs
//
// Node-side half of tests/web_ui/test_web_ui_failure_sentences.py.
//
// Drives the REAL web/js/render/turn.js (and its full real render/
// dependency chain -- same staging as run_turn_anatomy.mjs) to prove
// DESIGN-INVARIANTS.md §8's rule -- "what happened, in the analyst's
// terms, not the system's" -- actually holds for every backend failure
// code this UI can receive:
//
// * for every `TurnErrorInfo.code` this suite knows about, the rendered
//   failure banner's LEAD is the owner-approved Persian sentence, never
//   `turn.error.message` (the backend's English text, which each fixture
//   sets to a distinctive, greppable marker so this test can prove it is
//   ABSENT from the card's rendered text, not just that the Persian lead
//   is present);
// * MODEL_UNAVAILABLE keeps its existing Persian lead but no longer shows
//   the English "why" line;
// * QUERY_EXECUTION_ERROR, LLM_OUTPUT_TRUNCATED and FORBIDDEN_SQL are
//   UNCHANGED -- they still show `turn.error.message` as their "why" line
//   (the first is being reworked in a parallel change; this suite only
//   proves this task did not disturb the other two);
// * a code this UI has never seen before renders the INTERNAL_ERROR
//   sentence, not the raw code and not the (fabricated) English message;
// * each closed-set `GuardVerdict.reason` renders its own sentence, with
//   `denied_column` unchanged and an unrecognised reason falling back to
//   the generic guard sentence;
// * an action button ("تلاش دوباره" / "ویرایش پرسش") renders only when its
//   callback was actually supplied to `createTurnCard`, exactly as before
//   this table existed.
//
// Usage: node run_failure_sentences.mjs <path-to-copied-turn.mjs>
//
// Exits 0 and prints "ALL_FAILURE_SENTENCES_PASSED" iff every scenario
// passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const turnMjsPath = process.argv[2];
if (!turnMjsPath) {
  console.error("usage: node run_failure_sentences.mjs <path-to-copied-turn.mjs>");
  process.exit(2);
}

/* ── Minimal DOM shim (same shape as run_turn_anatomy.mjs's). ──────────── */

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
  click() { for (const h of this._listeners.click || []) h({ preventDefault() {}, target: this }); }
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

// A distinctive, greppable stand-in for "whatever English text the
// backend happened to send" -- chosen to look nothing like any Persian
// sentence in the mapping table, so `.includes()` cannot accidentally
// match. Synthetic, table-agnostic wording (DESIGN-INVARIANTS.md §8's own
// prose, not any deployment's real schema) -- this suite must stay
// database-agnostic.
const ENGLISH_MARKER = "ENGLISH_BACKEND_MARKER_do_not_leak_to_analyst";

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

function baseGuardTurn(overrides) {
  return {
    turn_id: "t_guard", session_id: "s_test", index: 1,
    question: "چند مورد نمونه چیست؟", resolved_question: null,
    basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
    ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
    sql: null, sql_display: null,
    guard: { verdict: "rejected", rule: "synthetic rule text", reason: "other", subject: null, rejected_sql: null, injected_top: null, tables_touched: [] },
    result: { columns: [], rows: [], row_count: 0, truncated: false },
    interpretation: null, tier: "T2", warnings: [], llm: llm(), timings: {}, error: null,
    ...overrides,
  };
}

function fullCtx() {
  const calls = { rerun: 0, rephrase: 0, askWithoutColumn: 0 };
  return {
    ctx: {
      onJumpToTurn() {}, onEditAssumption() {}, onClarify() {}, onPin() {},
      onRerun() { calls.rerun++; },
      onRephrase() { calls.rephrase++; },
      onAskWithoutColumn() { calls.askWithoutColumn++; },
    },
    calls,
  };
}

function noActionCtx() {
  return { onJumpToTurn() {}, onEditAssumption() {}, onClarify() {} };
}

function actionLabels(card) {
  return card.el.querySelectorAll(".failure-action-btn").map((b) => b.textContent);
}

/* ── Scenario A: every error code in the owner-approved table gets its
 * Persian sentence, never the backend's English message. ─────────────── */

// [code, expected lead, expected action labels (exact set, any order)]
const CODE_TABLE = [
  [
    "MODEL_TIMEOUT",
    "مدل در زمان مجاز پاسخ نداد. پرسش شما نگه داشته شد.",
    ["تلاش دوباره"],
  ],
  [
    "OUT_OF_SCOPE",
    "این پرسش به داده‌هایی که این سامانه در اختیار دارد مربوط نیست.",
    ["ویرایش پرسش"],
  ],
  [
    "NO_PREVIOUS_TURN",
    "این پرسش ادامهٔ پرسش قبلی به نظر می‌رسد، اما در این گفتگو پرسش قبلی‌ای برای ادامه نیست. آن را به‌صورت یک پرسش کامل بنویسید.",
    ["ویرایش پرسش"],
  ],
  [
    "EMPTY_SQL_RESPONSE",
    "مدل برای این پرسش هیچ پرس‌وجویی تولید نکرد.",
    ["ویرایش پرسش", "تلاش دوباره"],
  ],
  [
    "INVALID_SQL_RESPONSE",
    "پاسخ مدل یک پرس‌وجوی معتبر نبود.",
    ["تلاش دوباره"],
  ],
  [
    "QUERY_TIMEOUT",
    "اجرای پرس‌وجو بیش از زمان مجاز طول کشید و متوقف شد. پرسش را محدودتر کنید — بازهٔ زمانی کوتاه‌تر یا فیلتر بیشتر.",
    ["ویرایش پرسش"],
  ],
  [
    "DATABASE_UNAVAILABLE",
    "پایگاه داده در دسترس نبود — این یک مشکل سیستمی است، نه ایرادی در پرسش شما.",
    ["تلاش دوباره"],
  ],
  [
    "SERVER_OVERLOAD",
    "سامانه الان پرمشغله است. چند ثانیه بعد دوباره تلاش کنید.",
    ["تلاش دوباره"],
  ],
  [
    "MAINTENANCE_MODE",
    "سامانه موقتاً برای نگهداری در دسترس نیست.",
    [],
  ],
  [
    "TRANSPORT_ERROR",
    "ارتباط با سرور برقرار نشد. اتصال شبکه را بررسی کنید و دوباره تلاش کنید.",
    ["تلاش دوباره"],
  ],
  [
    "INJECTION_ATTEMPT",
    "این پرسش شامل دستورهایی خطاب به خودِ سامانه بود و پردازش نشد. لطفاً فقط پرسش تحلیلی خود را بنویسید.",
    ["ویرایش پرسش"],
  ],
  [
    "UNAUTHENTICATED",
    "کلید API معتبر نیست یا دیگر فعال نیست.",
    [],
  ],
  [
    "INTERNAL_ERROR",
    "خطای داخلی سامانه. اگر تکرار شد، شناسهٔ درخواست را به مدیر سامانه بدهید.",
    ["تلاش دوباره"],
  ],
];

for (const [code, expectedLead, expectedActions] of CODE_TABLE) {
  const { ctx } = fullCtx();
  const turn = baseErrorTurn({ error: { code, message: ENGLISH_MARKER, request_id: "req_1" } });
  const card = createTurnCard(turn, ctx);

  const leadEl = card.el.querySelector(".failure-lead");
  assert.ok(leadEl, `${code}: expected a .failure-lead element`);
  assert.equal(leadEl.textContent, expectedLead, `${code}: lead sentence mismatch`);
  assert.ok(
    !card.el.textContent.includes(ENGLISH_MARKER),
    `${code}: the backend's English message must not appear anywhere on the card`,
  );

  const gotActions = actionLabels(card).sort();
  assert.deepEqual(gotActions, [...expectedActions].sort(), `${code}: action set mismatch`);

  console.log(`[ok] ${code}: Persian lead rendered, English backend text absent, actions = [${gotActions.join(", ")}]`);
}

console.log("[ok] all table-driven error codes render their Persian sentence with no English leak");

/* ── Scenario B: MODEL_UNAVAILABLE keeps its lead, loses the English why. */

{
  const { ctx } = fullCtx();
  const turn = baseErrorTurn({ error: { code: "MODEL_UNAVAILABLE", message: ENGLISH_MARKER, request_id: "req_2" } });
  const card = createTurnCard(turn, ctx);
  const leadEl = card.el.querySelector(".failure-lead");
  assert.ok(leadEl, "MODEL_UNAVAILABLE: expected a .failure-lead element");
  assert.equal(
    leadEl.textContent,
    "پرسش شما نگه داشته شد — سامانهٔ مدل در دسترس نبود. این یک مشکل سیستمی است، نه ایرادی در پرسش شما.",
    "MODEL_UNAVAILABLE: lead sentence must stay the existing Persian sentence",
  );
  assert.equal(card.el.querySelector(".failure-why"), null, "MODEL_UNAVAILABLE: the English why line must be gone");
  assert.ok(!card.el.textContent.includes(ENGLISH_MARKER), "MODEL_UNAVAILABLE: English backend text must not appear anywhere");
  assert.deepEqual(actionLabels(card), ["تلاش دوباره"], "MODEL_UNAVAILABLE: retry action must still render");
  console.log("[ok] MODEL_UNAVAILABLE: unchanged Persian lead, English why line removed");
}

/* ── Scenario C: QUERY_EXECUTION_ERROR, LLM_OUTPUT_TRUNCATED and
 * FORBIDDEN_SQL are UNCHANGED -- they still show turn.error.message as
 * their "why" line (the first is being reworked in a parallel change to
 * this same file; this only proves this task did not touch the other
 * two). ─────────────────────────────────────────────────────────────── */

for (const code of ["QUERY_EXECUTION_ERROR", "LLM_OUTPUT_TRUNCATED", "FORBIDDEN_SQL"]) {
  const { ctx } = fullCtx();
  const turn = baseErrorTurn({ error: { code, message: ENGLISH_MARKER, request_id: "req_3" } });
  const card = createTurnCard(turn, ctx);
  const whyEl = card.el.querySelector(".failure-why");
  assert.ok(whyEl, `${code}: expected the pre-existing .failure-why line to still render`);
  assert.equal(whyEl.textContent, ENGLISH_MARKER, `${code}: why line must still be turn.error.message, unchanged by this task`);
  console.log(`[ok] ${code}: left exactly as it was (still shows turn.error.message as "why")`);
}

/* ── Scenario D: a code this UI has never seen before falls back to the
 * INTERNAL_ERROR sentence, not the raw code and not the English message. */

{
  const { ctx } = fullCtx();
  const turn = baseErrorTurn({ error: { code: "SOME_FUTURE_CODE_NOBODY_MAPPED_YET", message: ENGLISH_MARKER, request_id: "req_4" } });
  const card = createTurnCard(turn, ctx);
  const leadEl = card.el.querySelector(".failure-lead");
  assert.equal(
    leadEl.textContent,
    "خطای داخلی سامانه. اگر تکرار شد، شناسهٔ درخواست را به مدیر سامانه بدهید.",
    "an unlisted code must render the INTERNAL_ERROR sentence",
  );
  assert.ok(!card.el.textContent.includes(ENGLISH_MARKER), "an unlisted code must not leak the English message either");
  assert.deepEqual(actionLabels(card), ["تلاش دوباره"], "an unlisted code must still offer retry, per the INTERNAL_ERROR entry");
  console.log("[ok] an unrecognised error code renders the INTERNAL_ERROR sentence");
}

/* ── Scenario E: actions only render when their callback was supplied. ── */

{
  const turn = baseErrorTurn({ error: { code: "MODEL_TIMEOUT", message: ENGLISH_MARKER, request_id: "req_5" } });
  const card = createTurnCard(turn, noActionCtx());
  assert.deepEqual(actionLabels(card), [], "MODEL_TIMEOUT: no retry button without ctx.onRerun");
  console.log("[ok] MODEL_TIMEOUT with no onRerun supplied: no action button renders");
}

/* ── Scenario F: each closed-set GuardVerdict.reason renders its own
 * sentence; denied_column is unchanged; an unrecognised reason falls back
 * to the generic guard sentence. ───────────────────────────────────────── */

const GUARD_REASON_TABLE = [
  [
    "forbidden_statement",
    "این پرسش اجرا نشد — پرس‌وجوی تولیدشده کاری می‌خواست که این سامانه اجازه نمی‌دهد: تغییر داده، یا خواندن اطلاعات خودِ سرور به‌جای داده‌های انبار.",
  ],
  [
    "system_catalogue",
    "این پرسش اجرا نشد — پرس‌وجوی تولیدشده می‌خواست جدول‌های سیستمی پایگاه داده را بخواند، نه داده‌های انبار را.",
  ],
  [
    "unknown_table",
    "این پرسش اجرا نشد — پرس‌وجوی تولیدشده به جدولی اشاره کرد که در داده‌های این سامانه وجود ندارد.",
  ],
  [
    "no_table_reference",
    "این پرسش اجرا نشد — پرس‌وجوی تولیدشده از هیچ جدول داده‌ای نمی‌خواند؛ هر پاسخ باید از داده‌های انبار بیاید.",
  ],
  [
    "other",
    "این پرسش اصلاً اجرا نشد — لایهٔ نگهبانی امنیتی پیش از اجرا آن را رد کرد.",
  ],
  [
    // a reason value this UI does not recognise (e.g. an older persisted
    // turn, or a future guard reason not yet wired) must still fall back
    // to the generic sentence, not render blank or throw.
    "some_future_reason_nobody_mapped_yet",
    "این پرسش اصلاً اجرا نشد — لایهٔ نگهبانی امنیتی پیش از اجرا آن را رد کرد.",
  ],
];

for (const [reason, expectedLead] of GUARD_REASON_TABLE) {
  const { ctx } = fullCtx();
  const turn = baseGuardTurn({ guard: { verdict: "rejected", rule: "synthetic rule text", reason, subject: null, rejected_sql: null, injected_top: null, tables_touched: [] } });
  const card = createTurnCard(turn, ctx);
  const leadEl = card.el.querySelector(".failure-lead");
  assert.ok(leadEl, `guard reason ${reason}: expected a .failure-lead element`);
  assert.equal(leadEl.textContent, expectedLead, `guard reason ${reason}: lead sentence mismatch`);
  console.log(`[ok] guard reason "${reason}": renders its sentence`);
}

// denied_column: unchanged sentence, and the targeted "ask without that
// column" action still renders alongside the generic rephrase action.
{
  const { ctx, calls } = fullCtx();
  const turn = baseGuardTurn({
    guard: { verdict: "rejected", rule: "synthetic rule text", reason: "denied_column", subject: "some_synthetic_column", rejected_sql: null, injected_top: null, tables_touched: [] },
  });
  const card = createTurnCard(turn, ctx);
  const leadEl = card.el.querySelector(".failure-lead");
  assert.equal(
    leadEl.textContent,
    "این پرسش اجرا نشد — یکی از ستون‌های لازم برای پاسخ به آن برای حساب شما محدود شده است. این به معنای «نتیجه‌ای یافت نشد» نیست.",
    "denied_column: lead sentence must be unchanged",
  );
  const labels = actionLabels(card);
  assert.ok(labels.includes("پرسش بدون «some_synthetic_column»"), "denied_column: targeted column action must still render");
  assert.ok(labels.includes("ویرایش پرسش"), "denied_column: generic rephrase action must still render alongside it");
  const btn = card.el.querySelectorAll(".failure-action-btn")[0];
  btn.click();
  assert.equal(calls.askWithoutColumn, 1, "the targeted action must still be wired to onAskWithoutColumn");
  console.log("[ok] guard reason \"denied_column\": unchanged sentence, targeted action still wired");
}

console.log("ALL_FAILURE_SENTENCES_PASSED");
