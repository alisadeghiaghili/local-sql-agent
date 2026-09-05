// tests/web_ui/run_sessions_memory.mjs
//
// Node-side half of tests/web_ui/test_web_ui_sessions_memory.py.
//
// Drives the REAL web/js/api.js, web/js/apikey.js, web/js/state.js,
// web/js/render/sessions.js, web/js/render/memory.js and
// web/js/render/table.js source (via caller-supplied paths to copies with
// only the internal import specifiers rewritten to their sibling `.mjs`
// copies -- see the Python test for exactly which imports get rewritten
// and why the rename is necessary) under a mocked fetch/localStorage and
// a minimal DOM shim, and asserts, at the boundary that actually broke or
// would break:
//
// 1. Every new v2 call this feature adds (GET /v2/sessions,
//    PATCH /v2/sessions/{sid}, GET /v2/memory, PUT /v2/memory/{key},
//    DELETE /v2/memory/{key}, DELETE /v2/memory) attaches
//    `Authorization: Bearer <key>` -- the exact class of bug PR #44 fixed
//    for the original v2 routes, now extended to this feature's routes.
// 2. `rows_omitted: true` renders neither an empty table nor a zero row
//    count, and surfaces the row count that was actually returned.
// 3. A stored session id absent from the index falls back to the newest
//    session, and an empty index resolves to null rather than throwing.
// 4. `localStorage` throwing on read does not break start-up.
// 5. A memory value with a newline is refused before the request (the
//    onSetValue handler / the network call) is ever made.
// 6. An `applicable: false` entry renders as inactive, and is neither
//    hidden nor rendered as active.
// 7. The rail renders in `last_active_at` order regardless of the order
//    the (simulated or real) server returned.
//
// web/ ships no package.json / node_modules by design, so this brings its
// own minimal DOM shim (same spirit as run_result_shapes.mjs) rather than
// depending on jsdom or any other package.
//
// Usage: node run_sessions_memory.mjs <api.mjs> <table.mjs> <state.mjs> <sessions.mjs> <memory.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [apiMjsPath, tableMjsPath, stateMjsPath, sessionsMjsPath, memoryMjsPath] = process.argv.slice(2);
if (!apiMjsPath || !tableMjsPath || !stateMjsPath || !sessionsMjsPath || !memoryMjsPath) {
  console.error("usage: node run_sessions_memory.mjs <api.mjs> <table.mjs> <state.mjs> <sessions.mjs> <memory.mjs>");
  process.exit(2);
}
// assumptions.mjs is a sibling of table.mjs in the same tmp dir (table.mjs's
// own `./assumptions.mjs` import already depends on it being there) --
// derived rather than taking a 6th argv, same pattern as
// apiMjsPath -> apikey.mjs below.
const assumptionsMjsPathForPin = tableMjsPath.replace(/table\.mjs$/, "assumptions.mjs");
const memoryMjsPathForKey = memoryMjsPath;

/* ── Minimal DOM shim -- same primitives as run_result_shapes.mjs (no
 * jsdom dependency; web/ ships none). Extended with nothing new: table.js/
 * sessions.js/memory.js only ever call createElement/createTextNode,
 * textContent, className/classList, dataset, setAttribute/getAttribute,
 * appendChild, a simplified querySelector(All), and addEventListener/
 * click -- `value`/`maxLength` on an <input>/<select> are plain assigned
 * properties, never real form-control behaviour, so no special-casing is
 * needed for those either. ────────────────────────────────────────────*/

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
  hasAttribute(name) { return this._attrs.has(name); }
  appendChild(node) { node.parentNode = this; this.childNodes.push(node); return node; }
  addEventListener(type, handler) { (this._listeners[type] ||= []).push(handler); }
  click() { for (const h of this._listeners.click || []) h({ preventDefault() {}, target: this }); }
  focus() {}
  select() {}
  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }
  set textContent(value) { this.childNodes = [new FakeTextNode(value)]; }
  set innerHTML(_value) { this.childNodes = []; }
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
  const walk = (node) => {
    for (const child of node.children) { out.push(child); walk(child); }
  };
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

/* ── Scenario 1: Authorization boundary for every new v2 call site ──── *
 * Own in-memory localStorage + mocked fetch, same pattern as
 * run_auth_boundary.mjs (this is a SEPARATE harness process, so there is
 * no shared state to worry about with that suite). */

globalThis.localStorage = (() => {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  };
})();

const calls = [];
let nextResponse = null;
globalThis.fetch = async (url, init) => {
  calls.push({
    url: String(url),
    method: (init && init.method) || "GET",
    headers: { ...((init && init.headers) || {}) },
    body: init && init.body,
  });
  if (typeof nextResponse === "function") return nextResponse();
  return nextResponse;
};

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
  };
}
function lastCall() { return calls[calls.length - 1]; }

{
  const { Api, ApiError, UnauthorizedError, RateLimitError } = await import(pathToFileURL(apiMjsPath).href);
  const apikeyMjsPath = apiMjsPath.replace(/api\.mjs$/, "apikey.mjs");
  const { setApiKey, clearApiKey } = await import(pathToFileURL(apikeyMjsPath).href);

  const api = new Api("http://backend.example");
  clearApiKey();
  setApiKey("analyst-key-1");
  const expectedAuth = "Bearer analyst-key-1";

  nextResponse = jsonResponse(200, { sessions: [{ session_id: "s1" }], total: 1 });
  await api.listSessions();
  assert.equal(lastCall().url, "http://backend.example/v2/sessions");
  assert.equal(lastCall().method, "GET");
  assert.equal(lastCall().headers["Authorization"], expectedAuth, "GET /v2/sessions must send Authorization");

  nextResponse = jsonResponse(200, { session_id: "s1", title: "renamed" });
  await api.renameSession("s1", "renamed");
  assert.equal(lastCall().url, "http://backend.example/v2/sessions/s1");
  assert.equal(lastCall().method, "PATCH");
  assert.equal(lastCall().headers["Authorization"], expectedAuth, "PATCH /v2/sessions/{sid} must send Authorization");
  assert.equal(JSON.parse(lastCall().body).title, "renamed");

  nextResponse = jsonResponse(200, { entries: [], rememberable: [] });
  await api.getMemory();
  assert.equal(lastCall().url, "http://backend.example/v2/memory");
  assert.equal(lastCall().method, "GET");
  assert.equal(lastCall().headers["Authorization"], expectedAuth, "GET /v2/memory must send Authorization");

  nextResponse = jsonResponse(200, { key: "ring", value: "تالار سیمان" });
  await api.putMemory("ring", "تالار سیمان");
  assert.equal(lastCall().url, "http://backend.example/v2/memory/ring");
  assert.equal(lastCall().method, "PUT");
  assert.equal(lastCall().headers["Authorization"], expectedAuth, "PUT /v2/memory/{key} must send Authorization");
  assert.equal(JSON.parse(lastCall().body).value, "تالار سیمان");

  nextResponse = jsonResponse(200, {});
  await api.deleteMemoryEntry("ring");
  assert.equal(lastCall().url, "http://backend.example/v2/memory/ring");
  assert.equal(lastCall().method, "DELETE");
  assert.equal(lastCall().headers["Authorization"], expectedAuth, "DELETE /v2/memory/{key} must send Authorization");

  nextResponse = jsonResponse(200, {});
  await api.clearMemory();
  assert.equal(lastCall().url, "http://backend.example/v2/memory");
  assert.equal(lastCall().method, "DELETE");
  assert.equal(lastCall().headers["Authorization"], expectedAuth, "DELETE /v2/memory must send Authorization");

  console.log("[ok] every new v2 call (listSessions/renameSession/getMemory/putMemory/deleteMemoryEntry/clearMemory) sends Authorization: Bearer <key>");

  // Bonus: a 422 on PUT /v2/memory/{key} is a plain ApiError carrying the
  // server's real message (§ spec: "render the server's 422 message when
  // it comes ... never the authority" -- api.js must actually hand that
  // message through, not swallow it).
  nextResponse = jsonResponse(422, { error: { code: "INVALID_MEMORY_VALUE", message: "Value contains a newline." } });
  let threw = null;
  try { await api.putMemory("period", "line1\nline2"); } catch (err) { threw = err; }
  assert.ok(threw instanceof ApiError && !(threw instanceof UnauthorizedError) && !(threw instanceof RateLimitError));
  assert.equal(threw.message, "Value contains a newline.");
  console.log("[ok] PUT /v2/memory/{key} 422 surfaces the server's real error.message");
}

/* ── Scenario 2: rows_omitted -- neither an empty table nor "0 rows",
 * and the real row_count is surfaced. THE bug this exists to catch: an
 * earlier draft could easily fall through to the ordinary `empty` shape
 * (rows.length === 0), which renders "0 rows" and blames an assumption --
 * both false statements about a result that actually matched 6 rows. ──── */
{
  const { renderResult } = await import(pathToFileURL(tableMjsPath).href);

  const omitted = {
    columns: [{ name: "Weekday", type: "string" }, { name: "TotalWeightTons", type: "number" }],
    rows: [],
    row_count: 6,
    rows_omitted: true,
    truncated: false,
  };
  let rerunCalls = 0;
  const block = renderResult(omitted, { onRerun: () => { rerunCalls++; } });

  assert.equal(block.dataset.shape, "omitted", "a rows_omitted result must use its own shape, not fall through to `empty`");
  assert.ok(!/۰ ردیف/.test(block.textContent), "must never render the zero-row wording for a rows_omitted result");
  assert.ok(/۶/.test(block.textContent), "must surface the REAL row_count (۶), not zero");
  assert.equal(block.querySelector("table"), null, "must not render an (empty) table for a rows_omitted result");
  assert.ok(/Weekday/.test(block.textContent) && /TotalWeightTons/.test(block.textContent), "must show the result's column names");

  const rerunBtn = block.querySelector(".rows-omitted-rerun-btn");
  assert.ok(rerunBtn, "must offer a re-run affordance");
  rerunBtn.click();
  assert.equal(rerunCalls, 1, "the re-run control must call back into the ordinary ask path (opts.onRerun)");

  console.log("[ok] rows_omitted renders neither an empty table nor \"0 rows\", and surfaces the real row_count");
}

/* ── Scenario 3: a stored session id absent from the index falls back to
 * the newest session, without throwing; an empty index resolves to null
 * (first-run state), not an error. ─────────────────────────────────── */
{
  const { resolveActiveSessionId } = await import(pathToFileURL(stateMjsPath).href);

  const sessions = [
    { session_id: "a", last_active_at: "2024-01-01T00:00:00Z" },
    { session_id: "b", last_active_at: "2024-01-03T00:00:00Z" },
    { session_id: "c", last_active_at: "2024-01-02T00:00:00Z" },
  ];
  assert.equal(
    resolveActiveSessionId("does-not-exist-anymore", sessions), "b",
    "a stale/expired/deleted stored id must fall back to the most-recently-active session",
  );
  assert.equal(resolveActiveSessionId("a", sessions), "a", "a stored id still present in the index is kept");
  assert.equal(resolveActiveSessionId(null, sessions), "b", "no stored id falls back to the newest session too");
  assert.equal(resolveActiveSessionId(null, []), null, "an empty index resolves to null (first-run), not a thrown error");
  assert.equal(resolveActiveSessionId("anything", []), null, "an empty index resolves to null regardless of the stored id");

  console.log("[ok] resolveActiveSessionId falls back to the newest session on a stale/missing id, and never throws on an empty index");
}

/* ── Scenario 4: localStorage throwing on read does not break start-up. */
{
  const throwingStorage = {
    getItem() { throw new Error("SecurityError: storage disabled"); },
    setItem() { throw new Error("SecurityError: storage disabled"); },
    removeItem() { throw new Error("SecurityError: storage disabled"); },
  };
  globalThis.localStorage = throwingStorage;

  const { state, loadPersisted } = await import(pathToFileURL(stateMjsPath).href);
  assert.doesNotThrow(() => loadPersisted(), "loadPersisted must not throw when localStorage.getItem throws");
  assert.equal(state.sessionId, null, "with storage unavailable, the app must start with no remembered session, not crash");
  assert.equal(state.theme, "system", "other persisted fields must keep their defaults too, not just the session id");

  console.log("[ok] localStorage throwing on every read does not break start-up (loadPersisted degrades to defaults)");
}

/* ── Scenario 5: a memory value with a newline is refused BEFORE the
 * request is made -- both the pure validator and the actual save-button
 * wiring (renderMemoryPanel), so this catches a UI that validates but
 * forgets to gate the call, not just a validator that exists in isolation. */
{
  const { validateMemoryValue, renderMemoryPanel } = await import(pathToFileURL(memoryMjsPath).href);

  const rememberable = { key: "period", field: "دورهٔ زمانی پیش‌فرض", options: [], max_length: 40 };
  const withNewline = validateMemoryValue("۱۴۰۴/۰۱\n۱۴۰۴/۰۲", rememberable);
  assert.equal(withNewline.ok, false, "a value containing a newline must be rejected");
  assert.ok(withNewline.error && withNewline.error.length > 0, "a rejected value must carry a reason");

  const clean = validateMemoryValue("۱۴۰۴", rememberable);
  assert.equal(clean.ok, true, "a clean value within the rules must be accepted");

  let onSetValueCalls = 0;
  const panel = renderMemoryPanel(
    { entries: [], rememberable: [rememberable] },
    { onSetValue: () => { onSetValueCalls++; } },
  );
  const input = panel.querySelector(".memory-rememberable-input");
  assert.ok(input, "the rememberable field must render an input control");
  input.value = "۱۴۰۴/۰۱\n۱۴۰۴/۰۲";
  const saveBtn = panel.querySelector(".memory-save-btn");
  saveBtn.click();
  assert.equal(onSetValueCalls, 0, "a newline value must be refused before onSetValue (the request) is ever invoked");
  const errorEl = panel.querySelector(".memory-rememberable-error");
  assert.ok(errorEl && errorEl.textContent.length > 0, "an inline validation error must be shown next to the field");

  input.value = "۱۴۰۴";
  saveBtn.click();
  assert.equal(onSetValueCalls, 1, "a valid value must still reach onSetValue -- the gate must not block everything");

  console.log("[ok] a memory value with a newline is refused before the request is made (validator AND the wired save button)");
}

/* ── Scenario 6: an applicable:false entry renders inactive -- neither
 * hidden nor rendered as active. ───────────────────────────────────── */
{
  const { renderMemoryPanel } = await import(pathToFileURL(memoryMjsPath).href);

  const panel = renderMemoryPanel({
    entries: [
      { key: "ring", field: "تالار", value: "تالار سیمان", updated_at: "2024-01-01T00:00:00Z", applicable: true },
      { key: "denied_scope", field: "دامنهٔ قدیمی", value: "فقط تالار فلزات", updated_at: "2024-01-01T00:00:00Z", applicable: false },
    ],
    rememberable: [],
  }, {});

  const rows = panel.querySelectorAll(".memory-entry");
  assert.equal(rows.length, 2, "an applicable:false entry must still be rendered -- never hidden");

  const inactiveRow = rows.find((r) => r.dataset.key === "denied_scope");
  assert.ok(inactiveRow, "the inactive entry must be present in the DOM");
  assert.ok(inactiveRow.classList.contains("inactive"), "an inactive entry must carry a distinct class from an active one");
  assert.equal(inactiveRow.dataset.applicable, "false");
  assert.ok(/غیرفعال/.test(inactiveRow.textContent), "must show why it is inactive, not just that it is");

  const activeRow = rows.find((r) => r.dataset.key === "ring");
  assert.ok(activeRow, "the applicable entry must be present too");
  assert.ok(!activeRow.classList.contains("inactive"), "an applicable entry must NOT render with the inactive marker");
  assert.equal(activeRow.dataset.applicable, "true");

  console.log("[ok] an applicable:false memory entry renders visibly inactive -- neither hidden nor indistinguishable from an active one");
}

/* ── Scenario 7: the rail renders newest-`last_active_at`-first regardless
 * of the order the index array itself was given in. ───────────────────── */
{
  const { sortSessionsByRecency, renderSessionList } = await import(pathToFileURL(sessionsMjsPath).href);

  const unordered = [
    { session_id: "old", title: "Old", last_active_at: "2024-01-01T00:00:00Z", turn_count: 1 },
    { session_id: "newest", title: "Newest", last_active_at: "2024-01-05T00:00:00Z", turn_count: 3 },
    { session_id: "mid", title: "Mid", last_active_at: "2024-01-03T00:00:00Z", turn_count: 2 },
  ];

  const sorted = sortSessionsByRecency(unordered);
  assert.deepEqual(sorted.map((s) => s.session_id), ["newest", "mid", "old"]);
  assert.deepEqual(unordered.map((s) => s.session_id), ["old", "newest", "mid"], "sortSessionsByRecency must not mutate its input");

  const list = renderSessionList(unordered, {});
  const rowEls = list.querySelectorAll(".session-row");
  assert.deepEqual(
    rowEls.map((r) => r.dataset.sessionId), ["newest", "mid", "old"],
    "the rendered rail must show newest-active-first regardless of the order the index array itself was in",
  );

  const empty = renderSessionList([], {});
  assert.ok(empty.querySelector(".session-list-empty"), "an empty index must render a first-run message, not a blank rail");
  assert.equal(empty.querySelectorAll(".session-row").length, 0);

  console.log("[ok] the rail renders in last_active_at order regardless of the order the index array was given in");
}

/* ── Scenario 8: clicking a session row's main button fires onSelect with
 * that row's session_id -- this is the exact trigger main.js wires to
 * switchToSession (swap the transcript), regardless of which row in the
 * rendered order was clicked. ───────────────────────────────────────── */
{
  const { renderSessionList } = await import(pathToFileURL(sessionsMjsPath).href);

  const sessions = [
    { session_id: "alpha", title: "Alpha", last_active_at: "2024-02-01T00:00:00Z", turn_count: 1 },
    { session_id: "beta", title: "Beta", last_active_at: "2024-02-03T00:00:00Z", turn_count: 4 },
  ];
  let selected = null;
  const list = renderSessionList(sessions, { selectedId: "beta", onSelect: (id) => { selected = id; } });

  const rows = list.querySelectorAll(".session-row");
  const alphaRow = rows.find((r) => r.dataset.sessionId === "alpha");
  const betaRow = rows.find((r) => r.dataset.sessionId === "beta");
  assert.ok(betaRow.classList.contains("active"), "the currently-selected session's row must be marked active");
  assert.ok(!alphaRow.classList.contains("active"), "a non-selected row must not be marked active");

  alphaRow.querySelector(".session-row-main").click();
  assert.equal(selected, "alpha", "clicking a row's main button must fire onSelect with THAT row's session_id");

  console.log("[ok] clicking a session row fires onSelect with its session_id -- the trigger main.js swaps the transcript on");
}

/* ── Scenario 9: pinning an assumption issues the PUT. Two boundaries,
 * matching how main.js actually wires them (assumptions.js has no idea
 * api.js exists, and vice versa):
 *   (a) an editable, non-memory-sourced assumption chip renders a "pin"
 *       control that calls onPin(field, value) -- and a policy
 *       (non-editable) or already-memory-sourced chip does NOT get one;
 *   (b) the value that callback hands back is exactly what api.js's
 *       putMemory sends as PUT /v2/memory/{key} with Authorization --
 *       i.e. wiring onPin straight to api.putMemory (as main.js's
 *       pinLiveAssumption/pinSimulatedAssumption do) actually reaches the
 *       network as the frozen contract's shape. ─────────────────────── */
{
  const { renderAssumptions } = await import(pathToFileURL(assumptionsMjsPathForPin).href);
  const { Api } = await import(pathToFileURL(apiMjsPath).href);
  const apikeyMjsPath = apiMjsPath.replace(/api\.mjs$/, "apikey.mjs");
  const { setApiKey } = await import(pathToFileURL(apikeyMjsPath).href);

  const assumptions = [
    { field: "ring", value: "تالار فلزات", source: "session", editable: true },
    { field: "top_n", value: "100", source: "policy", editable: false },
    { field: "measure", value: "ارزش ریالی", source: "memory", editable: true },
  ];
  let pinned = null;
  const wrap = renderAssumptions(assumptions, () => {}, (field, value) => { pinned = { field, value }; });
  const chips = wrap.querySelectorAll(".chip");
  const ringChip = chips.find((c) => c.dataset.source === "session");
  const policyChip = chips.find((c) => c.dataset.source === "policy");
  const memoryChip = chips.find((c) => c.dataset.source === "memory");

  assert.ok(ringChip.querySelector(".chip-pin-btn"), "an editable, non-memory chip must render a pin control");
  assert.equal(policyChip.querySelector(".chip-pin-btn"), null, "a non-editable (policy) chip must never offer a pin control");
  assert.equal(memoryChip.querySelector(".chip-pin-btn"), null, "an already memory-sourced chip has nothing new to pin");

  ringChip.querySelector(".chip-pin-btn").click();
  assert.deepEqual(pinned, { field: "ring", value: "تالار فلزات" }, "the pin control must call onPin with this chip's own field/value");

  // Scenario 4 above deliberately replaced globalThis.localStorage with a
  // stub that throws on every call (to prove start-up survives it) and
  // never put a working one back -- reinstall one here so setApiKey below
  // actually persists, rather than silently no-op'ing into a false
  // negative for THIS scenario's own assertion.
  globalThis.localStorage = (() => {
    const store = new Map();
    return {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
      clear: () => store.clear(),
    };
  })();
  setApiKey("analyst-key-pin");
  const api = new Api("http://backend.example");
  nextResponse = jsonResponse(200, { key: pinned.field, value: pinned.value });
  await api.putMemory(pinned.field, pinned.value);
  assert.equal(lastCall().url, `http://backend.example/v2/memory/${pinned.field}`);
  assert.equal(lastCall().method, "PUT");
  assert.equal(lastCall().headers["Authorization"], "Bearer analyst-key-pin", "pinning must send Authorization like every other v2 call");
  assert.equal(JSON.parse(lastCall().body).value, pinned.value, "the PUT body must carry the exact value the chip showed");

  console.log("[ok] pinning an editable assumption fires onPin, and wiring it to api.putMemory issues an authenticated PUT /v2/memory/{key} with that value");
}

/* ── Scenario 10: a chip's display FIELD is not a memory KEY.
 *
 * Scenario 9 wires onPin straight into api.putMemory, which reads as
 * correct only because its fixture picked a field ("ring") whose label
 * and key happen to be the same string. In the real contract they are
 * not: `rememberable` carries {key, field}, where `field` is the Persian
 * label a chip shows and `key` is the identifier from the closed set
 * project_config/memory_policy.yaml declares. Pinning by label would PUT
 * /v2/memory/تالار against a backend that only accepts /v2/memory/scope,
 * so the entire pin feature would 4xx while every other test stayed
 * green.
 *
 * memoryKeyForField is the translation, and returning null is a real
 * answer: a field nobody can remember must not be pinnable at all. ── */
{
  const { memoryKeyForField } = await import(pathToFileURL(memoryMjsPathForKey).href);

  const rememberable = [
    { key: "scope", field: "تالار", options: [], max_length: 120 },
    { key: "measure", field: "سنجه", options: [], max_length: 120 },
  ];

  assert.equal(
    memoryKeyForField(rememberable, "تالار"), "scope",
    "a chip's Persian label must resolve to the config key the API expects",
  );
  assert.equal(
    memoryKeyForField(rememberable, "سنجه"), "measure",
    "every declared field must resolve, not just the first",
  );
  assert.equal(
    memoryKeyForField(rememberable, "scope"), "scope",
    "a caller that already holds the key must get it back unchanged",
  );
  assert.equal(
    memoryKeyForField(rememberable, "row_limit"), null,
    "a field outside the closed rememberable set is not pinnable, and must not be guessed at",
  );
  assert.equal(
    memoryKeyForField([], "تالار"), null,
    "an empty rememberable set means nothing is pinnable yet",
  );
  assert.equal(
    memoryKeyForField(undefined, "تالار"), null,
    "a missing rememberable set must not throw -- it is simply not loaded yet",
  );

  console.log("[ok] a chip's display field resolves to its memory key, and an unrememberable field resolves to null rather than being pinned by label");
}

/* ── Scenario 11: a simulated session id must never survive into live mode,
 * and a cross-origin transport failure must say so.
 *
 * Both come from one real incident. The UI was in live mode against a
 * healthy backend; the CLI was answering questions against that same
 * server. The UI showed API, LLM and DB all down and then posted
 *
 *   POST /v2/sessions/s_demo_1404/turns
 *
 * -- a session id invented by data.js, sent to a server that has never
 * heard of it. Two separate defects stacked:
 *
 *   (a) the server's CORS allowlist was empty, so every call from the
 *       page was refused at the preflight and the browser reported the
 *       same opaque "Failed to fetch" it uses for a refused connection;
 *   (b) when listing sessions failed, main.js returned early WITHOUT
 *       clearing the active session, so whatever was active in simulated
 *       mode stayed active in live mode.
 *
 * resolveActiveSessionId was never the problem -- it discards an id that
 * is not in the index it is given. It was simply not being reached. This
 * pins the invariant it enforces, so a future refactor that skips it
 * again has something to fail against. ───────────────────────────────── */
{
  const { resolveActiveSessionId } = await import(pathToFileURL(stateMjsPath).href);
  const { describeTransportFailure } = await import(pathToFileURL(apiMjsPath).href);

  const SIMULATED_ID = "s_demo_1404";

  assert.equal(
    resolveActiveSessionId(SIMULATED_ID, []),
    null,
    "with no live sessions there is nothing to resume -- a scripted id must not be carried over",
  );

  const liveIndex = [
    { session_id: "s_0a1b2c3d4e", last_active_at: "2026-09-05T10:00:00Z" },
    { session_id: "s_1122334455", last_active_at: "2026-09-05T12:00:00Z" },
  ];
  const resolved = resolveActiveSessionId(SIMULATED_ID, liveIndex);
  assert.notEqual(resolved, SIMULATED_ID, "a scripted id is not a real session and must never be selected");
  assert.equal(resolved, "s_1122334455", "falling back to the most recently active real session");

  // The transport message must name the actual suspect. "Failed to fetch"
  // alone is what made this cost an afternoon.
  globalThis.location = { href: "http://localhost:8080/", origin: "http://localhost:8080" };
  const crossOrigin = describeTransportFailure(
    "http://localhost:8000", "/v2/sessions", new Error("Failed to fetch"),
  );
  assert.ok(crossOrigin.includes("CORS_ALLOWED_ORIGINS"), "must name the setting that fixes it");
  assert.ok(crossOrigin.includes("http://localhost:8080"), "must name the origin to allow");

  const sameOrigin = describeTransportFailure(
    "http://localhost:8080", "/v2/sessions", new Error("Failed to fetch"),
  );
  assert.ok(
    !sameOrigin.includes("CORS_ALLOWED_ORIGINS"),
    "a same-origin failure is not a CORS problem, and guessing at one would send the reader the wrong way",
  );

  console.log("[ok] a simulated session id never survives into a live index, and a cross-origin transport failure names CORS_ALLOWED_ORIGINS");
}

console.log("ALL_SCENARIOS_PASSED");
