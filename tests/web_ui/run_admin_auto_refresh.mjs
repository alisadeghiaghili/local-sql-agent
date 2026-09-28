// tests/web_ui/run_admin_auto_refresh.mjs
//
// Node-side half of tests/web_ui/test_web_ui_admin_auto_refresh.py.
//
// 2026 warehouse-load audit, Required item 1: the two warehouse-touching
// admin cards (deployment checks, schema drift) must load once when the
// page opens, and must NOT be part of the 30-second auto-refresh. This
// drives the REAL web/admin/main.js (and its real import graph -- admin.js,
// access-requests.js, apikey.js, state.js, all via caller-supplied copies
// with only their internal relative import specifiers rewritten to the
// sibling .mjs files) under a mocked DOM/fetch/timers, and asserts, at the
// actual boundary that changed, which HTTP paths are fetched during BOOT
// versus during ONE invocation of the auto-refresh interval's own callback.
//
// This intentionally does more DOM mocking than the other harnesses in
// this directory (see access-requests.js's own module docstring for why
// main.js was previously left untested directly: its top-level bootstrap
// assumes a full page's worth of elements). The mock here is a single,
// maximally permissive fake element (a Proxy over a no-op function) reused
// for every id/selector -- it is not a faithful DOM, only enough of one
// that main.js's real render functions can run to completion without
// throwing, so the fetch calls they trigger are real.
//
// Usage: node run_admin_auto_refresh.mjs <path-to-copied-main.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario below
// passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const mainMjsPath = process.argv[2];
if (!mainMjsPath) {
  console.error("usage: node run_admin_auto_refresh.mjs <path-to-copied-main.mjs>");
  process.exit(2);
}

// ---------------------------------------------------------------------
// A maximally permissive fake DOM element: a Proxy over a no-op function
// so it can be read as an object (el.value, el.classList, ...), written
// to (el.innerHTML = "...") and called as a function (querySelectorAll(...)
// forEach callback chains), always returning another instance of itself
// for anything not specifically handled below. See the module docstring.
// ---------------------------------------------------------------------
function makeFakeElement() {
  const fn = function () { return makeFakeElement(); };
  const scalarDefaults = {
    value: "", checked: false, hidden: false, disabled: false,
    textContent: "", innerHTML: "", className: "",
  };
  return new Proxy(fn, {
    get(_target, prop) {
      if (prop in scalarDefaults) return scalarDefaults[prop];
      if (prop === "classList") return { add() {}, remove() {}, contains() { return false; }, toggle() {} };
      if (prop === "dataset") return {};
      if (prop === "style") return {};
      // Everything else (addEventListener, appendChild, querySelector,
      // querySelectorAll, focus, ...) resolves to a callable fake, so
      // both `el.foo` and `el.foo(...)` are always safe.
      return makeFakeElement();
    },
    set() { return true; },
    apply() { return makeFakeElement(); },
  });
}

// ---------------------------------------------------------------------
// Minimal document/localStorage. querySelectorAll returns a REAL empty
// array (not a fake) -- main.js's own button-wiring loops
// (`document.querySelectorAll("[data-refresh]").forEach(...)`) then
// simply wire nothing, which is fine: this harness never simulates a
// click, only boot and one auto-refresh tick.
// ---------------------------------------------------------------------
globalThis.localStorage = (() => {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  };
})();

// main.js's boot now resolves its backend-address default (config.js's
// DEFAULT_BASE_URL/DEFAULT_API_PORT) via resolveDefaultBaseUrl, and reads
// `?base=` off `location.search` -- neither existed as a module-scope
// reference before. No `?base=` param for this harness (boot order/timer
// behaviour is what is under test here, not address resolution -- see
// test_web_ui_topbar_config.py / the address-normalisation tests for
// that), so an empty search string is enough.
globalThis.location = { protocol: "http:", hostname: "localhost", search: "", href: "http://localhost/admin/" };

globalThis.document = {
  hidden: false,
  documentElement: { removeAttribute() {}, setAttribute() {} },
  getElementById() { return makeFakeElement(); },
  createElement() { return makeFakeElement(); },
  querySelectorAll() { return []; },
  // refreshOne looks up each card's own refresh button (to disable it
  // during the fetch); a fake element accepts .disabled being set safely.
  querySelector() { return makeFakeElement(); },
  addEventListener() {},
};

// ---------------------------------------------------------------------
// Mocked fetch: records every request path and returns a minimal,
// correctly-shaped 200 response for whichever /admin/* endpoint was
// asked for, so every real renderer in main.js reaches an early-return
// ("no rows") branch instead of throwing on a missing field.
// ---------------------------------------------------------------------
const fetchedPaths = [];

function jsonResponse(body) {
  return { ok: true, status: 200, json: async () => body };
}

const RESPONSES = [
  [/\/admin\/summary/, { mode: "aggregate_safe", record_count: 0 }],
  [/\/admin\/health\/checks/, { checks: [], deep: false, cache: { cached: false, age_seconds: 0, ttl_seconds: 300 } }],
  [/\/admin\/cache$/, { hits: 0, misses: 0, evictions: 0, size: 0, enabled: true }],
  [/\/admin\/config/, { project_config_dir: "project_config", files: [] }],
  [/\/admin\/feedback\/stats/, {}],
  [/\/admin\/feedback(\?|$)/, { feedback: [] }],
  [/\/admin\/access-requests/, { access_requests: [] }],
  [/\/admin\/maintenance/, { active: false }],
  [/\/admin\/keys/, { keys: [] }],
  [/\/admin\/roles\//, { principal_ids: [] }],
  [/\/admin\/schema-drift/, {
    checked_at: "t", schemas_scanned: [], warehouse_only: [], schema_only: [],
    type_changed: [], unverifiable_tables: [], baseline_available: true,
    cache: { cached: false, age_seconds: 0, ttl_seconds: 300 },
  }],
  [/\/admin\/vocabulary/, { columns: [] }],
  [/\/admin\/usage/, {}],
  [/\/admin\/security\/auth-failures/, {}],
];

globalThis.fetch = async (url) => {
  const s = String(url);
  fetchedPaths.push(s);
  for (const [pattern, body] of RESPONSES) {
    if (pattern.test(s)) return jsonResponse(body);
  }
  return jsonResponse({});
};

// ---------------------------------------------------------------------
// Mocked setInterval: records (callback, delayMs) instead of actually
// scheduling anything on a real timer -- this harness drives the
// auto-refresh tick by invoking the recorded callback itself, once, so
// the test is not a 30-second sleep and is not flaky under CI load.
// ---------------------------------------------------------------------
const registeredIntervals = [];
globalThis.setInterval = (callback, delayMs) => {
  registeredIntervals.push({ callback, delayMs });
  return registeredIntervals.length; // a fake, unique-enough "timer id"
};
globalThis.clearInterval = () => {};

// ---------------------------------------------------------------------
// Import the real main.js (module-graph copy). Its top-level code runs
// on this import: loadPersisted/applyTheme/wireTopbar/tickClock, and
// (fire-and-forget, not awaited by main.js itself) refreshAll().
// ---------------------------------------------------------------------
await import(pathToFileURL(mainMjsPath).href);

// Flush the microtask/macrotask queue so the fire-and-forget refreshAll()
// triggered at module load has finished every one of its Promise.all'd
// card fetches before we inspect what was called.
await new Promise((resolve) => setTimeout(resolve, 50));

// ---------------------------------------------------------------------
// Scenario 1: boot fetches EVERY card once, including both expensive
// ones -- "they load once when the page opens" (Required item 1).
// ---------------------------------------------------------------------
const bootPaths = fetchedPaths.slice();
assert.ok(
  bootPaths.some((p) => p.includes("/admin/health/checks")),
  `boot must fetch /admin/health/checks once. Fetched: ${JSON.stringify(bootPaths)}`,
);
assert.ok(
  bootPaths.some((p) => p.includes("/admin/schema-drift")),
  `boot must fetch /admin/schema-drift once. Fetched: ${JSON.stringify(bootPaths)}`,
);
assert.ok(
  bootPaths.some((p) => p.includes("/admin/cache")),
  `boot must also fetch an ordinary, cheap card (/admin/cache) once. Fetched: ${JSON.stringify(bootPaths)}`,
);
console.log("[ok] page load fetches every card once, including the two expensive ones");

// ---------------------------------------------------------------------
// Scenario 2: THE required behaviour. Exactly one registered interval
// runs on a 30-second cadence (main.js's AUTO_REFRESH_MS) -- invoking
// its callback must fetch the cheap cards again, but must NOT fetch
// /admin/health/checks or /admin/schema-drift.
// ---------------------------------------------------------------------
const autoRefreshEntry = registeredIntervals.find((e) => e.delayMs === 30_000);
assert.ok(
  autoRefreshEntry,
  `expected one setInterval registered at 30000ms (AUTO_REFRESH_MS). Got delays: ${JSON.stringify(registeredIntervals.map((e) => e.delayMs))}`,
);

fetchedPaths.length = 0; // reset: only the interval tick's own calls matter now
autoRefreshEntry.callback();
await new Promise((resolve) => setTimeout(resolve, 50));

const tickPaths = fetchedPaths.slice();
assert.ok(
  tickPaths.length > 0,
  "the auto-refresh tick fetched nothing at all -- EXPENSIVE_CARDS exclusion should still leave the cheap cards refreshing",
);
assert.ok(
  !tickPaths.some((p) => p.includes("/admin/health/checks")),
  `the 30s auto-refresh must NOT call /admin/health/checks. Fetched on tick: ${JSON.stringify(tickPaths)}`,
);
assert.ok(
  !tickPaths.some((p) => p.includes("/admin/schema-drift")),
  `the 30s auto-refresh must NOT call /admin/schema-drift. Fetched on tick: ${JSON.stringify(tickPaths)}`,
);
assert.ok(
  tickPaths.some((p) => p.includes("/admin/cache")),
  `the 30s auto-refresh must still refresh an ordinary, cheap card (/admin/cache). Fetched on tick: ${JSON.stringify(tickPaths)}`,
);
console.log("[ok] the 30s auto-refresh tick skips /admin/health/checks and /admin/schema-drift, but still refreshes cheap cards");

console.log("ALL_SCENARIOS_PASSED");
