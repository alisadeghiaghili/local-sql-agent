// tests/web_ui/run_base_url_resolution.mjs
//
// Node-side half of tests/web_ui/test_web_ui_base_url_resolution.py.
//
// Drives the REAL web/js/state.js (resolveDefaultBaseUrl, normalizeBaseUrl,
// loadPersisted), web/js/config.js (DEFAULT_BASE_URL/DEFAULT_API_PORT),
// web/js/api.js (Api -- the analyst UI's request layer) and
// web/admin/admin.js (AdminApi -- the admin panel's) together, via
// caller-supplied copies with only their internal relative import
// specifiers rewritten to sibling .mjs files (Node's ESM loader needs a
// recognized extension; the sources ship as plain .js for the browser).
//
// This is the address-resolution/normalisation regression suite for BOTH
// pages at once: web/ (main UI) and web/admin/ (admin panel) resolve their
// backend base URL through the exact same two state.js functions, so one
// harness driving state.js + both pages' request layers proves both pages
// stay in lockstep rather than drifting into two almost-identical, silently
// diverging implementations.
//
// Usage: node run_base_url_resolution.mjs <path-to-copied-state.mjs>
//          <path-to-copied-config.mjs> <path-to-copied-api.mjs>
//          <path-to-copied-admin.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario below
// passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [stateMjsPath, configMjsPath, apiMjsPath, adminMjsPath] = process.argv.slice(2);
if (!stateMjsPath || !configMjsPath || !apiMjsPath || !adminMjsPath) {
  console.error(
    "usage: node run_base_url_resolution.mjs <state.mjs> <config.mjs> <api.mjs> <admin.mjs>",
  );
  process.exit(2);
}

// ---------------------------------------------------------------------
// Minimal in-memory localStorage -- same shape as every other harness in
// this directory.
// ---------------------------------------------------------------------
const storageBacking = new Map();
globalThis.localStorage = {
  getItem: (k) => (storageBacking.has(k) ? storageBacking.get(k) : null),
  setItem: (k, v) => storageBacking.set(k, String(v)),
  removeItem: (k) => storageBacking.delete(k),
  clear: () => storageBacking.clear(),
};

// state.js's applyTheme() (unused here) touches document.documentElement;
// not exercised by any scenario below, but importing the module must not
// throw regardless.
globalThis.document = { documentElement: { removeAttribute() {}, setAttribute() {} } };

// ---------------------------------------------------------------------
// Mocked fetch: records every request URL actually built by Api/AdminApi,
// and returns a minimal, correctly-shaped 200 response for either
// /health or /admin/maintenance so both request layers reach their
// ordinary success path instead of throwing.
// ---------------------------------------------------------------------
const fetchedUrls = [];
globalThis.fetch = async (url) => {
  const s = String(url);
  fetchedUrls.push(s);
  if (s.includes("/admin/maintenance")) {
    return { ok: true, status: 200, json: async () => ({ active: false }) };
  }
  return { ok: true, status: 200, json: async () => ({ status: "ok", openai: true, database: true }) };
};

const { state, loadPersisted, resolveDefaultBaseUrl, normalizeBaseUrl } =
  await import(pathToFileURL(stateMjsPath).href);
const { DEFAULT_BASE_URL, DEFAULT_API_PORT } = await import(pathToFileURL(configMjsPath).href);
const { Api } = await import(pathToFileURL(apiMjsPath).href);
const { AdminApi } = await import(pathToFileURL(adminMjsPath).href);

/* ── Scenario 1: resolveDefaultBaseUrl -- config.js default wins whenever
 * non-empty, unchanged; only derived from the page's own origin when the
 * configured default is empty. ────────────────────────────────────────── */
assert.equal(
  resolveDefaultBaseUrl("http://configured:9999", 8000, { protocol: "http:", hostname: "ignored" }),
  "http://configured:9999",
  "a non-empty DEFAULT_BASE_URL must win unchanged, regardless of page origin",
);
assert.equal(
  resolveDefaultBaseUrl("", 8076, { protocol: "http:", hostname: "172.16.101.42" }),
  "http://172.16.101.42:8076",
  "an empty DEFAULT_BASE_URL must derive <page protocol>//<page hostname>:<DEFAULT_API_PORT>",
);
assert.equal(
  resolveDefaultBaseUrl("", 8443, { protocol: "https:", hostname: "host.example" }),
  "https://host.example:8443",
  "derivation must use the page's own protocol, not always http",
);
assert.equal(
  typeof DEFAULT_BASE_URL, "string",
  "config.js must still export DEFAULT_BASE_URL as a string",
);
assert.equal(typeof DEFAULT_API_PORT, "number", "config.js must export DEFAULT_API_PORT as a number");
console.log("[ok] resolveDefaultBaseUrl: config.js default wins when set, derives from page origin + DEFAULT_API_PORT when not");

/* ── Scenario 2: normalizeBaseUrl -- every case a real deployment or an
 * operator's typo could produce. ───────────────────────────────────────── */
const ACCEPTED = [
  ["172.16.101.42:8076", "http://172.16.101.42:8076"], // the real incident: no scheme
  [" http://172.16.101.42:8076 ", "http://172.16.101.42:8076"], // surrounding whitespace
  ["http://172.16.101.42:8076/", "http://172.16.101.42:8076"], // trailing slash
  ["http://172.16.101.42:8076/admin", "http://172.16.101.42:8076"], // pasted admin path
  ["http://172.16.101.42:8076/admin/", "http://172.16.101.42:8076"], // pasted admin path + slash
  ["HTTP://172.16.101.42:8076", "http://172.16.101.42:8076"], // upper-case scheme
  ["https://host.example", "https://host.example"], // https, no port -- kept as-is
  ["localhost:8076", "http://localhost:8076"], // bare host:port
  ["[::1]:8076", "http://[::1]:8076"], // IPv6 literal, no scheme
  ["http://[::1]:8076/admin", "http://[::1]:8076"], // IPv6 literal + pasted admin path
  ["http://user:pass@172.16.101.42:8076/admin/", "http://172.16.101.42:8076"], // credentials + pasted path
];
for (const [input, expected] of ACCEPTED) {
  const result = normalizeBaseUrl(input);
  assert.equal(result.ok, true, `expected ${JSON.stringify(input)} to be accepted, got ${JSON.stringify(result)}`);
  assert.equal(result.url, expected, `expected ${JSON.stringify(input)} to normalise to ${expected}, got ${result.url}`);
}
console.log("[ok] normalizeBaseUrl accepts and correctly normalises every valid-address case (including IPv6 literals and a pasted admin path)");

/* ── Scenario 2b: credentials embedded in a typed/saved address (e.g.
 * pasted from a connection string, or a browser autofill) must never
 * survive into the normalised, SAVED/USED address -- `origin` never
 * carries userinfo, but this asserts that explicitly rather than only
 * incidentally via the ACCEPTED table above. ───────────────────────────── */
{
  const withCreds = normalizeBaseUrl("http://user:pass@host.example:8076");
  assert.equal(withCreds.ok, true);
  assert.equal(withCreds.url, "http://host.example:8076");
  assert.ok(!withCreds.url.includes("user"), `credentials must not survive normalisation, got ${withCreds.url}`);
  assert.ok(!withCreds.url.includes("pass"), `credentials must not survive normalisation, got ${withCreds.url}`);
  assert.ok(!withCreds.url.includes("@"), `no userinfo separator must remain, got ${withCreds.url}`);
}
console.log("[ok] normalizeBaseUrl strips embedded credentials, keeping only the origin");

const REJECTED = [
  "/http://172.16.101.42:8076", // accidental leading slash in front of an embedded scheme --
  // must be rejected outright, never silently mis-parsed toward a wrong
  // host (see state.js's own normalizeBaseUrl docstring for the exact
  // `new URL("http:///http://host:port")` footgun this guards against).
  "ftp://x",
  "javascript:alert(1)",
  "",
  "::::",
];
for (const input of REJECTED) {
  const result = normalizeBaseUrl(input);
  assert.equal(result.ok, false, `expected ${JSON.stringify(input)} to be rejected, got ${JSON.stringify(result)}`);
  assert.equal(typeof result.reason, "string", `rejection of ${JSON.stringify(input)} must carry a reason`);
}
console.log("[ok] normalizeBaseUrl rejects every invalid-address case, including the leading-slash/embedded-scheme footgun, with a stated reason");

/* ── Scenario 3: loadPersisted() repairs a fixable bad saved value in
 * place, and discards (never loads) one that cannot be fixed. ─────────── */
storageBacking.clear();
storageBacking.set("lsa-web-base", "172.16.101.42:8076"); // the real incident, but already saved
state.baseUrl = "http://should-be-overwritten:1";
loadPersisted();
assert.equal(state.baseUrl, "http://172.16.101.42:8076", "loadPersisted() must repair a fixable saved value");
assert.equal(
  storageBacking.get("lsa-web-base"), "http://172.16.101.42:8076",
  "the repaired value must be written back to localStorage, so the bad value does not keep resurfacing",
);

storageBacking.clear();
storageBacking.set("lsa-web-base", "javascript:alert(1)"); // unfixable
state.baseUrl = "http://deploy-default:9000";
loadPersisted();
assert.equal(
  state.baseUrl, "http://deploy-default:9000",
  "loadPersisted() must ignore an unfixable saved value and leave the caller's default standing",
);
assert.equal(
  storageBacking.has("lsa-web-base"), false,
  "an unfixable saved value must be dropped from localStorage, not left to keep failing silently",
);
console.log("[ok] loadPersisted() repairs a fixable saved base URL and discards an unfixable one");

/* ── Scenario 4: end-to-end -- for BOTH pages, the actual request URL
 * built from an accepted, messy input is exactly <origin> + <path>, never
 * a doubled/relative path (the concrete "GET /admin/172.16.101.42:8076/
 * admin/maintenance" incident this whole change exists to prevent). ───── */
for (const [input, expectedOrigin] of ACCEPTED) {
  const result = normalizeBaseUrl(input);
  fetchedUrls.length = 0;

  const api = new Api(result.url);
  await api.health();
  assert.equal(
    fetchedUrls[0], `${expectedOrigin}/health`,
    `web/ (Api.health()) from input ${JSON.stringify(input)}: expected ${expectedOrigin}/health, got ${fetchedUrls[0]}`,
  );

  fetchedUrls.length = 0;
  const adminApi = new AdminApi(result.url);
  await adminApi.maintenanceState();
  assert.equal(
    fetchedUrls[0], `${expectedOrigin}/admin/maintenance`,
    `web/admin/ (AdminApi.maintenanceState()) from input ${JSON.stringify(input)}: expected ${expectedOrigin}/admin/maintenance, got ${fetchedUrls[0]}`,
  );
}
console.log("[ok] end-to-end: both pages build the request URL as exactly <normalised origin> + <path>, for every accepted input");

console.log("ALL_SCENARIOS_PASSED");
